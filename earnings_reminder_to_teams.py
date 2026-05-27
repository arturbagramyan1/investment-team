"""
earnings.json -> Microsoft Teams earnings reminder.

Reads earnings.json (produced by earnings_to_ics.py) and posts a Teams
card (orange "warning" style) for any ticker reporting tomorrow. Silent
if nothing reports tomorrow.

Intended to run once a day at 10:00 Yerevan time (06:00 UTC), staggered
~15 min after the earnings_to_ics workflow so it sees fresh data.

Usage:
    python earnings_reminder_to_teams.py            # post real reminders
    python earnings_reminder_to_teams.py --test     # post one sample card

Secrets are read from environment variables (.env is loaded if present):
    TEAMS_WEBHOOK_URL  Teams Workflows incoming webhook URL
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


BASE_DIR = Path(__file__).resolve().parent
EVENTS_JSON = BASE_DIR / "earnings.json"

FMP_LOGO_URL = "https://images.financialmodelingprep.com/symbol/{symbol}.png"

REQUEST_TIMEOUT = 20


def log(msg: str) -> None:
    print(msg, flush=True)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        log(f"ERROR: environment variable {name} is not set.")
        sys.exit(1)
    return value


def load_events() -> list[dict]:
    if not EVENTS_JSON.exists():
        log(f"ERROR: {EVENTS_JSON} not found. "
            f"Run earnings_to_ics.py first to generate it.")
        sys.exit(1)
    try:
        payload = json.loads(EVENTS_JSON.read_text(encoding="utf-8"))
    except ValueError as exc:
        log(f"ERROR: {EVENTS_JSON} is not valid JSON: {exc}")
        sys.exit(1)
    events = payload.get("events", [])
    if not isinstance(events, list):
        log(f"ERROR: {EVENTS_JSON} has no 'events' list.")
        sys.exit(1)
    return events


def find_events_for_date(events: list[dict], target: date) -> list[dict]:
    target_str = target.isoformat()
    return [e for e in events if (e.get("date") or "") == target_str]


def format_revenue(raw: float | int | None) -> str | None:
    if raw is None:
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    return f"${n:,.0f}"


def build_reminder_card(event: dict, event_date: date) -> dict:
    symbol = event["symbol"]
    eps_est = event.get("epsEstimated")
    revenue_str = format_revenue(event.get("revenueEstimated"))

    facts: list[dict] = [{"title": "Report date", "value": event_date.isoformat()}]
    if eps_est is not None:
        facts.append({"title": "EPS estimate", "value": str(eps_est)})
    if revenue_str:
        facts.append({"title": "Revenue estimate", "value": revenue_str})

    header_row = {
        "type": "ColumnSet",
        "columns": [
            {
                "type": "Column",
                "width": "auto",
                "verticalContentAlignment": "Center",
                "items": [{
                    "type": "Image",
                    "url": FMP_LOGO_URL.format(symbol=symbol),
                    "size": "Small",
                    "altText": f"{symbol} logo",
                }],
            },
            {
                "type": "Column",
                "width": "stretch",
                "verticalContentAlignment": "Center",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": symbol,
                        "weight": "Bolder",
                        "size": "Large",
                        "color": "Accent",
                    },
                    {
                        "type": "TextBlock",
                        "text": "Earnings tomorrow",
                        "weight": "Bolder",
                        "size": "Medium",
                        "spacing": "None",
                    },
                ],
            },
        ],
    }

    container = {
        "type": "Container",
        "style": "warning",
        "bleed": True,
        "items": [
            header_row,
            {"type": "FactSet", "facts": facts, "spacing": "Medium"},
        ],
    }

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [container],
    }
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card,
        }],
    }


def post_to_teams(payload: dict, webhook_url: str) -> bool:
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                webhook_url, json=payload, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            log(f"  WARNING: Teams request failed (attempt {attempt}): {exc}")
            time.sleep(2 * attempt)
            continue

        if resp.status_code in (200, 202):
            return True

        if resp.status_code == 429 or resp.status_code >= 500:
            log(f"  WARNING: Teams returned HTTP {resp.status_code} "
                f"(attempt {attempt}); retrying...")
            time.sleep(2 * attempt)
            continue

        log(f"  ERROR: Teams returned HTTP {resp.status_code}: "
            f"{resp.text[:200]}")
        return False

    log("  ERROR: gave up posting to Teams after 3 attempts.")
    return False


def run_test() -> None:
    webhook_url = require_env("TEAMS_WEBHOOK_URL")
    tomorrow = date.today() + timedelta(days=1)
    sample = {
        "symbol": "AAPL",
        "epsEstimated": 1.86,
        "revenueEstimated": 108414000000,
    }
    log("Posting a test reminder card to Teams...")
    ok = post_to_teams(build_reminder_card(sample, tomorrow), webhook_url)
    log("Test card posted." if ok else "Test card FAILED.")
    sys.exit(0 if ok else 1)


def run() -> None:
    webhook_url = require_env("TEAMS_WEBHOOK_URL")
    events = load_events()

    tomorrow = date.today() + timedelta(days=1)
    log(f"Looking for earnings on {tomorrow.isoformat()}...")
    matches = find_events_for_date(events, tomorrow)

    if not matches:
        log(f"No tickers report on {tomorrow.isoformat()}. Nothing to post.")
        return

    posted = 0
    for event in matches:
        symbol = event.get("symbol", "?")
        log(f"  {symbol} reports tomorrow; posting reminder card.")
        if post_to_teams(build_reminder_card(event, tomorrow), webhook_url):
            posted += 1
        else:
            log(f"  failed to post reminder for {symbol}.")

    log(f"Done. Posted {posted}/{len(matches)} reminder card(s).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="earnings.json -> Teams reminder (day before)"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="post one sample reminder card and exit",
    )
    args = parser.parse_args()

    if args.test:
        run_test()
    else:
        run()


if __name__ == "__main__":
    main()
