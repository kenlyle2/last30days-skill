#!/usr/bin/env python3
"""
Post-run shim: reads this week's top findings from the last30days SQLite store,
generates PostGlider-ready hooks via OpenAI, and writes rows to Airtable TrendingHooks.

Run after: watchlist.py run-all
Railway start command: watchlist.py run-all && python3 airtable_writer.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Allow store.py imports
SCRIPTS = Path(__file__).parent / "skills" / "last30days" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import store

# --- Config from environment ---
AIRTABLE_PAT = os.environ.get("AIRTABLE_PAT", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "app7HHjseQMlJHkFA")
AIRTABLE_TABLE_ID = os.environ.get("AIRTABLE_TABLE_TRENDING_HOOKS", "tblUGLOyLaHiG5kWs")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

TOP_N = 5  # hooks per topic per run
MODEL = "gpt-4o-mini"


def week_monday() -> str:
    """ISO date string for Monday of the current week."""
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


def week_label() -> str:
    """ISO week label, e.g. 2026-W25."""
    today = datetime.now(timezone.utc).date()
    return today.strftime("%G-W%V")


def generate_hook(niche: str, finding: dict) -> str:
    """Call OpenAI to produce a PostGlider-ready hook from a finding."""
    if not OPENAI_API_KEY:
        return f"[Hook pending — no OPENAI_API_KEY] {finding.get('source_title', '')}"

    prompt = (
        f"You are writing a social media post hook for a {niche} business owner.\n\n"
        f"Source platform: {finding['source']}\n"
        f"Title: {finding.get('source_title', '')}\n"
        f"Content summary: {finding.get('summary') or finding.get('content', '')[:600]}\n\n"
        "Write a 3-sentence social post:\n"
        "1. An opening line that casually references where this is trending "
        "(e.g. 'There's a thread blowing up on Reddit right now...')\n"
        "2. 1-2 sentences of context that make it relevant to their customers.\n"
        "3. A soft CTA question to spark engagement.\n\n"
        "Return only the post text, no labels or quotes."
    )

    payload = json.dumps({
        "model": MODEL,
        "input": prompt,
        "max_output_tokens": 200,
    })

    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST",
            "https://api.openai.com/v1/responses",
            "-H", f"Authorization: Bearer {OPENAI_API_KEY}",
            "-H", "Content-Type: application/json",
            "-d", payload,
        ],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(result.stdout)
        return data["output"][0]["content"][0]["text"].strip()
    except Exception as e:
        print(f"  Hook generation failed: {e}", file=sys.stderr)
        return finding.get("source_title", "")


def airtable_post(record_fields: dict) -> dict:
    """POST a single record to Airtable TrendingHooks."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"
    payload = json.dumps({"fields": record_fields})
    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST", url,
            "-H", f"Authorization: Bearer {AIRTABLE_PAT}",
            "-H", "Content-Type: application/json",
            "-d", payload,
        ],
        capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def run():
    if not AIRTABLE_PAT:
        print("ERROR: AIRTABLE_PAT not set — skipping Airtable write.", file=sys.stderr)
        sys.exit(1)

    topics = store.list_topics()
    if not topics:
        print("No topics in watchlist — nothing to write.")
        return

    week = week_monday()
    label = week_label()
    written = 0

    for topic in topics:
        niche = topic["name"]
        topic_id = topic["id"]
        findings = store.get_new_findings(topic_id, since=week)

        if not findings:
            print(f"  {niche}: no findings this week, skipping.")
            continue

        # Top N by engagement_score descending
        top = sorted(findings, key=lambda f: f.get("engagement_score") or 0, reverse=True)[:TOP_N]
        print(f"  {niche}: {len(top)} findings → generating hooks...")

        for f in top:
            platform = (f.get("source") or "Unknown").title()
            name = f"{niche}|{label}|{platform}"
            hook = generate_hook(niche, f)

            fields = {
                "Name": name,
                "Week Of": week,
                "Platform": platform,
                "Source URL": f.get("source_url") or "",
                "Source Title": f.get("source_title") or "",
                "Engagement Score": round(f.get("engagement_score") or 0, 1),
                "Raw Signal": (f.get("summary") or f.get("content") or "")[:10000],
                "Hook": hook,
                "Niche": niche,
                "Status": "Draft",
                "Created At": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }

            resp = airtable_post(fields)
            if "id" in resp:
                print(f"    ✓ {name}")
                written += 1
            else:
                print(f"    ✗ {name}: {resp.get('error', resp)}", file=sys.stderr)

    print(f"\nDone — {written} hooks written to TrendingHooks.")


if __name__ == "__main__":
    run()
