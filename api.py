#!/usr/bin/env python3
"""
FastAPI HTTP wrapper around last30days watchlist.

Endpoints:
  POST /watchlist/add    — register a niche topic (called by PostGlider on business onboarding)
  GET  /watchlist/topics — list all registered topics and their schedules
  POST /watchlist/run    — trigger an immediate run (called by Railway Cron weekly)
  POST /query            — one-off synchronous research call, returns the answer directly
  GET  /diagnose         — per-source key/CLI availability report, no search run
  GET  /health           — liveness check

PostGlider calls /watchlist/add with the NAICS-derived topic_slug when a new business
onboards. Railway Cron calls /watchlist/run weekly to execute all registered topics and
write TrendingHooks rows to Airtable via airtable_writer.py. /query is the direct-answer
path for ViralTrends' EXPLAIN step (and ad-hoc use) -- no watchlist registration, no
Airtable, just last30days.py run once against a topic and its stdout returned as JSON.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import json

SCRIPTS = Path(__file__).parent / "skills" / "last30days" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import store

app = FastAPI(title="Social Listening Agent", version="1.0.0")

NAICS_TABLE = Path(__file__).parent / "naics_topics.json"

WATCHLIST = Path(__file__).parent / "skills" / "last30days" / "scripts" / "watchlist.py"
WRITER = Path(__file__).parent / "airtable_writer.py"
LAST30DAYS = Path(__file__).parent / "skills" / "last30days" / "scripts" / "last30days.py"


# --- Request / Response models ---

class AddTopicRequest(BaseModel):
    topic: str
    naics: Optional[str] = None
    schedule: str = "weekly"


class AddTopicResponse(BaseModel):
    topic: str
    naics: Optional[str]
    status: str
    message: str


class RunResponse(BaseModel):
    status: str
    topics_run: int
    message: str


class NaicsRequest(BaseModel):
    naics: str
    business_id: Optional[str] = None


class NaicsResponse(BaseModel):
    naics: str
    topic_slug: str
    status: str
    message: str


class QueryRequest(BaseModel):
    topic: str
    quick: bool = True
    auto_resolve: bool = False


class QueryResponse(BaseModel):
    topic: str
    output: str
    stderr: Optional[str] = None


# --- Endpoints ---

@app.get("/health")
def health():
    return {"status": "ok", "service": "30-day-agent"}


@app.get("/diagnose")
def diagnose():
    """
    Per-source key/CLI availability report -- which keys were detected, which CLIs are
    installed, which backends are reachable. No search run, no API spend.
    """
    result = subprocess.run(
        [sys.executable, str(LAST30DAYS), "--diagnose"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return JSONResponse({
        "returncode": result.returncode,
        "output": result.stdout.strip(),
        "stderr": result.stderr.strip() if result.returncode != 0 else None,
    })


@app.post("/watchlist/add", response_model=AddTopicResponse)
def add_topic(req: AddTopicRequest):
    """
    Register a niche topic with the Watchlist.
    Called by PostGlider onboarding when a new business's NAICS code maps to a topic_slug.
    Idempotent — safe to call multiple times for the same topic.
    """
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic is required")

    # Check if already registered
    existing = store.get_topic(topic)
    if existing:
        return AddTopicResponse(
            topic=topic,
            naics=req.naics,
            status="exists",
            message=f"'{topic}' already on watchlist — no action taken",
        )

    result = subprocess.run(
        [sys.executable, str(WATCHLIST), "add", topic, "--weekly"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"watchlist.py add failed: {result.stderr.strip()}",
        )

    return AddTopicResponse(
        topic=topic,
        naics=req.naics,
        status="added",
        message=f"'{topic}' added to watchlist with weekly schedule",
    )


@app.post("/watchlist/add-by-naics", response_model=NaicsResponse)
def add_by_naics(req: NaicsRequest):
    """
    PostGlider calls this at business onboarding with the business's NAICS code.
    Looks up the topic_slug in naics_topics.json and delegates to /watchlist/add.
    Idempotent — safe if NAICS already enrolled.
    """
    if not NAICS_TABLE.exists():
        raise HTTPException(status_code=503, detail="naics_topics.json not found")

    lookup = json.loads(NAICS_TABLE.read_text())
    match = next((t for t in lookup.get("topics", []) if t["naics"] == req.naics.strip()), None)
    if not match:
        raise HTTPException(
            status_code=404,
            detail=f"NAICS {req.naics} not in lookup table — add it to naics_topics.json or register the topic manually",
        )

    topic_slug = match["topic_slug"]

    existing = store.get_topic(topic_slug)
    if existing:
        return NaicsResponse(
            naics=req.naics,
            topic_slug=topic_slug,
            status="exists",
            message=f"'{topic_slug}' already on watchlist",
        )

    result = subprocess.run(
        [sys.executable, str(WATCHLIST), "add", topic_slug, "--weekly"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"watchlist.py add failed: {result.stderr.strip()}",
        )

    return NaicsResponse(
        naics=req.naics,
        topic_slug=topic_slug,
        status="added",
        message=f"'{topic_slug}' enrolled from NAICS {req.naics} with weekly schedule",
    )


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest):
    """
    One-off synchronous research call -- no watchlist registration, no Airtable write.
    Runs last30days.py against the given topic and returns its compact-text output directly.
    Intended for ViralTrends' EXPLAIN step and ad-hoc use; --quick keeps latency reasonable
    for a synchronous HTTP call (set quick=false for a fuller, slower pass).
    """
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic is required")

    cmd = [sys.executable, str(LAST30DAYS), topic, "--emit", "compact"]
    if req.quick:
        cmd.append("--quick")
    if req.auto_resolve:
        cmd.append("--auto-resolve")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"last30days.py failed: {result.stderr.strip()[:1000]}",
        )

    github_lines = "\n".join(
        l for l in result.stderr.splitlines() if "github" in l.lower() or "GitHub" in l
    ) or None
    return QueryResponse(topic=topic, output=result.stdout.strip(), stderr=github_lines)


@app.get("/watchlist/topics")
def list_topics():
    """
    List all registered topics and their schedules.
    """
    topics = store.list_topics()
    return {"topics": topics, "count": len(topics)}


@app.post("/watchlist/run", response_model=RunResponse)
def run_all():
    """
    Trigger an immediate research run across all watchlist topics,
    then write findings to Airtable TrendingHooks via airtable_writer.py.
    Called by Railway Cron weekly (replaces the old chained start command).
    """
    topics = store.list_topics()
    if not topics:
        return RunResponse(
            status="skipped",
            topics_run=0,
            message="No topics on watchlist — nothing to run",
        )

    # Run watchlist
    r1 = subprocess.run(
        [sys.executable, str(WATCHLIST), "run-all"],
        capture_output=True,
        text=True,
    )
    if r1.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"watchlist run-all failed: {r1.stderr.strip()[:500]}",
        )

    # Write to Airtable
    r2 = subprocess.run(
        [sys.executable, str(WRITER)],
        capture_output=True,
        text=True,
    )
    if r2.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"airtable_writer failed: {r2.stderr.strip()[:500]}",
        )

    return RunResponse(
        status="ok",
        topics_run=len(topics),
        message=r2.stdout.strip().split("\n")[-1] if r2.stdout else "Run complete",
    )
