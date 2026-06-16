#!/usr/bin/env python3
"""
FastAPI HTTP wrapper around last30days watchlist.

Endpoints:
  POST /watchlist/add    — register a niche topic (called by PostGlider on business onboarding)
  GET  /watchlist/topics — list all registered topics and their schedules
  POST /watchlist/run    — trigger an immediate run (called by Railway Cron weekly)
  GET  /health           — liveness check

PostGlider calls /watchlist/add with the NAICS-derived topic_slug when a new business
onboards. Railway Cron calls /watchlist/run weekly to execute all registered topics and
write TrendingHooks rows to Airtable via airtable_writer.py.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

SCRIPTS = Path(__file__).parent / "skills" / "last30days" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import store

app = FastAPI(title="Social Listening Agent", version="1.0.0")

WATCHLIST = Path(__file__).parent / "skills" / "last30days" / "scripts" / "watchlist.py"
WRITER = Path(__file__).parent / "airtable_writer.py"


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


# --- Endpoints ---

@app.get("/health")
def health():
    return {"status": "ok", "service": "30-day-agent"}


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
