"""
FMP -> earnings.ics generator.

For every ticker in watchlist.txt, fetches upcoming earnings dates from
Financial Modeling Prep and writes them as all-day events into earnings.ics
in the repo root. Outlook (or any calendar app) can subscribe to that file
by URL and re-fetch it on its own schedule.

Each event uses a stable UID of the form "{SYMBOL}-{YYYY-MM-DD}@investment-team"
so regenerating the file updates existing events instead of duplicating them.

Usage:
    python earnings_to_ics.py            # fetch from FMP, write earnings.ics
    python earnings_to_ics.py --test     # write earnings.ics with fake data

Secrets are read from environment variables (.env is loaded if present):
    FMP_API_KEY  Financial Modeling Prep API key
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_FILE = BASE_DIR / "watchlist.txt"
OUTPUT_FILE = BASE_DIR / "earnings.ics"

FMP_EARNINGS_URL = "https://financialmodelingprep.com/stable/earnings"

DAYS_AHEAD = 365       # only include earnings within this many days from today
REQUEST_TIMEOUT = 20
UID_NAMESPACE = "investment-team"


def log(msg: str) -> None:
    print(msg, flush=True)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        log(f"ERROR: environment variable {name} is not set.")
        sys.exit(1)
    return value


def load_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        log(f"ERROR: watchlist file not found: {WATCHLIST_FILE}")
        sys.exit(1)
    tickers: list[str] = []
    for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            tickers.append(line.upper())
    if not tickers:
        log("ERROR: watchlist.txt contains no tickers.")
        sys.exit(1)
    return tickers


def fetch_earnings(symbol: str, api_key: str) -> list[dict]:
    """Fetch earnings rows for one ticker. [] on error."""
    params = {"symbol": symbol, "apikey": api_key}
    try:
        resp = requests.get(
            FMP_EARNINGS_URL, params=params, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        log(f"  WARNING: FMP request failed for {symbol}: {exc}")
        return []

    if resp.status_code != 200:
        log(f"  WARNING: FMP returned HTTP {resp.status_code} for {symbol}: "
            f"{resp.text[:200]}")
        return []

    try:
        data = resp.json()
    except ValueError:
        log(f"  WARNING: FMP returned non-JSON for {symbol}.")
        return []

    if not isinstance(data, list):
        log(f"  WARNING: unexpected FMP response for {symbol}: {data}")
        return []

    return data


def filter_upcoming(rows: list[dict], today: date, horizon: date) -> list[dict]:
    """Keep only rows whose date is between today and horizon (inclusive)."""
    keep: list[dict] = []
    for row in rows:
        raw = (row.get("date") or "").strip()
        if not raw:
            continue
        try:
            event_date = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= event_date <= horizon:
            keep.append({**row, "_date": event_date})
    keep.sort(key=lambda r: r["_date"])
    return keep


# --- ICS writing ------------------------------------------------------------

def _ics_escape(text: str) -> str:
    """Escape per RFC 5545: backslash, comma, semicolon, newline."""
    return (
        text.replace("\\", "\\\\")
            .replace(",", "\\,")
            .replace(";", "\\;")
            .replace("\n", "\\n")
    )


def _fold_line(line: str) -> str:
    """RFC 5545 line folding: max 75 octets per line, continuation lines
    start with a single space."""
    if len(line) <= 75:
        return line
    chunks = [line[:75]]
    rest = line[75:]
    while rest:
        chunks.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(chunks)


def build_event(symbol: str, row: dict, dtstamp: str) -> list[str]:
    event_date: date = row["_date"]
    next_day = event_date + timedelta(days=1)
    uid = f"{symbol}-{event_date.isoformat()}@{UID_NAMESPACE}"

    summary = f"{symbol} Earnings"

    desc_parts = [f"Ticker: {symbol}", f"Date: {event_date.isoformat()}"]
    eps_est = row.get("epsEstimated")
    if eps_est is not None:
        desc_parts.append(f"EPS estimate: {eps_est}")
    rev_est = row.get("revenueEstimated")
    if rev_est is not None:
        desc_parts.append(f"Revenue estimate: {rev_est}")
    desc_parts.append("Source: Financial Modeling Prep")
    description = _ics_escape("\n".join(desc_parts))

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;VALUE=DATE:{event_date.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{next_day.strftime('%Y%m%d')}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{description}",
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]
    return [_fold_line(line) for line in lines]


def build_calendar(events_rows: list[tuple[str, dict]]) -> str:
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Investment Team//Earnings Bot//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Watchlist Earnings",
        f"X-WR-CALDESC:Auto-generated earnings dates for tickers in watchlist.txt",
    ]
    for symbol, row in events_rows:
        out.extend(build_event(symbol, row, dtstamp))
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


# --- Run modes --------------------------------------------------------------

def run_test() -> None:
    """Write earnings.ics with a couple of fake events. No FMP call."""
    today = date.today()
    fake = [
        ("AAPL", {"_date": today + timedelta(days=7),
                  "epsEstimated": 1.42, "revenueEstimated": 95000000000}),
        ("MSFT", {"_date": today + timedelta(days=14),
                  "epsEstimated": 2.95, "revenueEstimated": 64000000000}),
        ("NBIS", {"_date": today + timedelta(days=21),
                  "epsEstimated": None, "revenueEstimated": None}),
    ]
    ics = build_calendar(fake)
    OUTPUT_FILE.write_text(ics, encoding="utf-8", newline="")
    log(f"Wrote {len(fake)} test event(s) to {OUTPUT_FILE}")


def run() -> None:
    api_key = require_env("FMP_API_KEY")
    tickers = load_watchlist()

    today = date.today()
    horizon = today + timedelta(days=DAYS_AHEAD)

    all_events: list[tuple[str, dict]] = []
    for symbol in tickers:
        log(f"Checking {symbol}...")
        rows = fetch_earnings(symbol, api_key)
        upcoming = filter_upcoming(rows, today, horizon)
        log(f"  {len(upcoming)} upcoming earnings date(s) within {DAYS_AHEAD}d.")
        for row in upcoming:
            all_events.append((symbol, row))

    ics = build_calendar(all_events)
    OUTPUT_FILE.write_text(ics, encoding="utf-8", newline="")
    log(f"Wrote {len(all_events)} event(s) to {OUTPUT_FILE}")


def main() -> None:
    parser = argparse.ArgumentParser(description="FMP -> earnings.ics generator")
    parser.add_argument(
        "--test", action="store_true",
        help="write earnings.ics with fake data (no FMP call)",
    )
    args = parser.parse_args()

    if args.test:
        run_test()
    else:
        run()


if __name__ == "__main__":
    main()
