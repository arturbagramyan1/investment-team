"""
FMP -> Microsoft Teams news bot.

For every ticker in watchlist.txt, fetches the latest stock news from
Financial Modeling Prep and posts ONE Adaptive Card per ticker (the ticker
name plus today's headlines, up to 5) to a Teams channel via a Workflows
incoming webhook.

A card is posted only when a ticker has articles that are new since the last
run; if nothing changed, that ticker is skipped. Posted articles are keyed by
URL + title and tracked for the current day in posted_state.json.

Usage:
    python news_to_teams.py            # post a news card for every ticker
    python news_to_teams.py --test     # post one sample card (no FMP call)

Secrets are read from environment variables. A local .env file is loaded
automatically if python-dotenv is installed:
    FMP_API_KEY        Financial Modeling Prep API key (needs a paid plan)
    TEAMS_WEBHOOK_URL  Teams Workflows incoming webhook URL
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Load a local .env file if python-dotenv is installed (optional).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


# --- Configuration ----------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_FILE = BASE_DIR / "watchlist.txt"
STATE_FILE = BASE_DIR / "posted_state.json"  # article keys posted today

FMP_NEWS_URL = "https://financialmodelingprep.com/stable/news/stock"
FMP_LOGO_URL = "https://images.financialmodelingprep.com/symbol/{symbol}.png"

NEWS_PER_CARD = 5      # max headlines shown on each ticker card
FETCH_LIMIT = 12       # articles to request per ticker (extra as a buffer)
SUMMARY_MAX_CHARS = 280  # truncate each article's summary text to this length
REQUEST_TIMEOUT = 20   # seconds

# FMP news timestamps carry no timezone marker; we assume they are UTC.
# (If a live FMP response shows otherwise, change this one line.)
FMP_SOURCE_TZ = ZoneInfo("UTC")
# Dates shown on cards are converted to this zone — the team is in Armenia.
DISPLAY_TZ = ZoneInfo("Asia/Yerevan")


# --- Helpers ----------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        log(f"ERROR: environment variable {name} is not set.")
        log("Set it in your environment or in a .env file (see .env.example).")
        sys.exit(1)
    return value


def load_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        log(f"ERROR: watchlist file not found: {WATCHLIST_FILE}")
        sys.exit(1)
    tickers: list[str] = []
    for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()  # strip inline comments
        if line:
            tickers.append(line.upper())
    if not tickers:
        log("ERROR: watchlist.txt contains no tickers.")
        sys.exit(1)
    return tickers


# --- Posted-state store -----------------------------------------------------

def load_state() -> dict:
    """Load the set of article keys already posted today (Armenia time),
    as `{"date": "YYYY-MM-DD", "seen": set()}`. A new day, or a missing or
    corrupt state file, yields an empty set — so each new day naturally
    treats every article as new."""
    today = datetime.now(DISPLAY_TZ).date().isoformat()
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log(f"WARNING: could not read state file ({exc}); starting fresh.")
            data = {}
        if isinstance(data, dict) and data.get("date") == today:
            seen = data.get("seen")
            if isinstance(seen, list):
                return {"date": today, "seen": set(seen)}
    return {"date": today, "seen": set()}


def save_state(state: dict) -> None:
    """Persist today's posted-key set so the next run can skip old news."""
    payload = {"date": state["date"], "seen": sorted(state["seen"])}
    try:
        STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        log(f"WARNING: could not write state file: {exc}")


def article_key(article: dict) -> str:
    """A stable identity for an article, used for dedup. Combines URL and
    title, so the same URL reused for a different story still counts as new
    (while an unchanged story is still recognised and skipped)."""
    return f"{article['url']}\n{article['title']}"


# --- FMP news ---------------------------------------------------------------

def normalize_article(item: dict, symbol: str) -> dict:
    return {
        "symbol": (item.get("symbol") or symbol).upper(),
        "title": (item.get("title") or "").strip(),
        "text": (item.get("text") or "").strip(),
        "url": (item.get("url") or "").strip(),
        "site": (item.get("site") or item.get("publisher") or "").strip(),
        "published": (item.get("publishedDate") or "").strip(),
    }


def fetch_news(symbol: str, api_key: str) -> list[dict]:
    """Fetch one ticker's news for today (Armenia time), newest first. The
    API is queried for a yesterday-to-today window — FMP timestamps are UTC,
    so an Armenia day straddles two UTC dates — and `_is_today` then trims the
    result to the exact Armenia day. [] on error."""
    today = datetime.now(DISPLAY_TZ).date()
    params = {
        "symbols": symbol,
        "from": (today - timedelta(days=1)).isoformat(),
        "to": today.isoformat(),
        "limit": FETCH_LIMIT,
        "apikey": api_key,
    }
    try:
        resp = requests.get(FMP_NEWS_URL, params=params, timeout=REQUEST_TIMEOUT)
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

    articles = [normalize_article(item, symbol) for item in data]
    articles = [a for a in articles if a["url"]]  # drop entries with no link
    articles = [a for a in articles if _is_today(a["published"])]  # today only
    articles.sort(key=lambda a: a["published"], reverse=True)  # newest first
    return articles


# --- Adaptive Card ----------------------------------------------------------

def _md_safe(text: str) -> str:
    """Neutralise characters that would break a markdown link."""
    return text.replace("[", "(").replace("]", ")")


HEADER_STYLE = "emphasis"   # subtle light banner behind the ticker name


_FMP_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


def _parse_fmp_date(raw: str) -> datetime | None:
    """Parse an FMP timestamp into a timezone-aware datetime (tagged with
    FMP_SOURCE_TZ), or None if it matches no known format."""
    raw = raw.strip()
    for fmt in _FMP_DATE_FORMATS:
        try:
            naive = datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=FMP_SOURCE_TZ)
    return None


def _pretty_date(raw: str) -> str:
    """Format an FMP timestamp in Armenia time, e.g. 'May 21 · 18:30'.
    Falls back to the raw string."""
    parsed = _parse_fmp_date(raw)
    if parsed is None:
        return raw.strip()
    return parsed.astimezone(DISPLAY_TZ).strftime("%b %d · %H:%M")


def _relative_date(raw: str) -> str:
    """Format an FMP timestamp relative to now, e.g. '3h ago', '2d ago'.
    Older than a week falls back to an Armenia-time 'Mon DD' date. The result
    is the same wherever the script runs, since the maths is timezone-aware."""
    parsed = _parse_fmp_date(raw)
    if parsed is None:
        return raw.strip()
    delta = datetime.now(tz=FMP_SOURCE_TZ) - parsed
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if delta.days < 7:
        return f"{delta.days}d ago"
    return parsed.astimezone(DISPLAY_TZ).strftime("%b %d")


def _is_today(raw: str) -> bool:
    """True if the FMP timestamp falls on the current day in Armenia time.
    Unparseable timestamps return False (cannot be confirmed as today's)."""
    parsed = _parse_fmp_date(raw)
    if parsed is None:
        return False
    today = datetime.now(DISPLAY_TZ).date()
    return parsed.astimezone(DISPLAY_TZ).date() == today


def _header_block(symbol: str, subtitle: str) -> dict:
    """Tinted banner: company logo beside the ticker name and a subtitle."""
    return {
        "type": "Container",
        "style": HEADER_STYLE,
        "bleed": True,
        "roundedCorners": True,
        "items": [{
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
                            "size": "ExtraLarge",
                            "color": "Accent",
                            "spacing": "None",
                        },
                        {
                            "type": "TextBlock",
                            "text": subtitle,
                            "isSubtle": True,
                            "size": "Small",
                            "spacing": "None",
                        },
                    ],
                },
            ],
        }],
    }


def _article_block(article: dict, index: int) -> dict:
    """One collapsible article. The headline row is always visible and, when
    tapped, toggles a hidden detail block (thumbnail, summary, source/date
    and a link to the full article). The ▸/▾ chevron flips with it."""
    title = _md_safe(article["title"] or "(no title)")
    detail_id = f"detail{index}"
    chevron_closed = f"chevClosed{index}"
    chevron_open = f"chevOpen{index}"

    # --- hidden detail block (revealed on tap) --------------------------
    summary = article.get("text", "")
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS].rstrip() + "..."

    detail_items: list[dict] = [{
        "type": "TextBlock",
        "text": summary or "No summary available.",
        "wrap": True,
    }]

    meta = "  ·  ".join(
        p for p in (article["site"], _pretty_date(article["published"])) if p
    )
    if meta:
        detail_items.append({
            "type": "TextBlock",
            "text": meta,
            "isSubtle": True,
            "size": "Small",
            "spacing": "Small",
            "wrap": True,
        })
    if article["url"]:
        detail_items.append({
            "type": "ActionSet",
            "spacing": "Small",
            "actions": [{
                "type": "Action.OpenUrl",
                "title": "Read full article",
                "url": article["url"],
            }],
        })

    detail = {
        "type": "Container",
        "id": detail_id,
        "isVisible": False,
        "spacing": "Small",
        "items": detail_items,
    }

    # --- always-visible headline row (chevron + title + recency) --------
    headline_columns: list[dict] = [
        {
            "type": "Column",
            "width": "auto",
            "verticalContentAlignment": "Center",
            "items": [
                {
                    "type": "TextBlock",
                    "id": chevron_closed,
                    "text": "▸",
                    "color": "Accent",
                    "weight": "Bolder",
                },
                {
                    "type": "TextBlock",
                    "id": chevron_open,
                    "text": "▾",
                    "color": "Accent",
                    "weight": "Bolder",
                    "isVisible": False,
                },
            ],
        },
        {
            "type": "Column",
            "width": "stretch",
            "verticalContentAlignment": "Center",
            "items": [{
                "type": "TextBlock",
                "text": title,
                "weight": "Bolder",
                "wrap": True,
            }],
        },
    ]

    recency = _relative_date(article["published"])
    if recency:
        headline_columns.append({
            "type": "Column",
            "width": "auto",
            "verticalContentAlignment": "Center",
            "items": [{
                "type": "TextBlock",
                "text": recency,
                "isSubtle": True,
                "size": "Small",
                "horizontalAlignment": "Right",
                "wrap": False,
            }],
        })

    headline_row = {"type": "ColumnSet", "columns": headline_columns}

    return {
        "type": "Container",
        "separator": index > 0,  # divider line between articles
        "spacing": "Medium",
        "selectAction": {
            "type": "Action.ToggleVisibility",
            "title": f"Toggle {title}",
            "targetElements": [detail_id, chevron_closed, chevron_open],
        },
        "items": [headline_row, detail],
    }


def build_ticker_card(symbol: str, articles: list[dict]) -> dict:
    """Build one Teams webhook payload: a card with up to NEWS_PER_CARD
    headlines for a single ticker."""
    items = articles[:NEWS_PER_CARD]
    subtitle = "Tap a headline to expand" if items else "Latest stock news"
    body: list[dict] = [_header_block(symbol, subtitle)]

    if not items:
        body.append({
            "type": "TextBlock",
            "text": "No recent news for this ticker.",
            "isSubtle": True,
            "wrap": True,
            "spacing": "Medium",
        })

    for index, article in enumerate(items):
        body.append(_article_block(article, index))

    card: dict = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
        "msteams": {"width": "Full"},
    }
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card,
        }],
    }


# --- Teams webhook ----------------------------------------------------------

def post_to_teams(payload: dict, webhook_url: str) -> bool:
    """POST a card to the Teams webhook. Retries transient errors."""
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


# --- Run modes --------------------------------------------------------------

def run_test() -> None:
    """Post one sample ticker card to verify the webhook (no FMP call)."""
    webhook_url = require_env("TEAMS_WEBHOOK_URL")
    today = date.today().isoformat()
    sample = [
        {"symbol": "NBIS", "title": "Sample headline one - webhook works",
         "text": "This is the sample summary text. Real cards show the "
                 "article's text here, truncated to keep the card compact.",
         "url": "https://financialmodelingprep.com",
         "site": "news-bot", "published": today},
        {"symbol": "NBIS", "title": "Sample headline two",
         "text": "Another short summary so you can see how multiple news "
                 "items stack inside a single ticker card.",
         "url": "https://financialmodelingprep.com",
         "site": "news-bot", "published": today},
        {"symbol": "NBIS", "title": "Sample headline three",
         "text": "A third sample summary line.",
         "url": "https://financialmodelingprep.com",
         "site": "news-bot", "published": today},
    ]
    log("Posting a test card to Teams...")
    ok = post_to_teams(build_ticker_card("NBIS", sample), webhook_url)
    log("Test card posted." if ok else "Test card FAILED.")
    sys.exit(0 if ok else 1)


def run() -> None:
    """Post a card per ticker, but only for news that is new since the last
    run. Tickers with no new articles are skipped, so old news is never
    re-posted."""
    api_key = require_env("FMP_API_KEY")
    webhook_url = require_env("TEAMS_WEBHOOK_URL")
    tickers = load_watchlist()
    state = load_state()
    seen = state["seen"]

    posted = skipped = 0
    for symbol in tickers:
        log(f"Checking {symbol}...")
        articles = fetch_news(symbol, api_key)
        fresh = [a for a in articles if article_key(a) not in seen]
        if not fresh:
            log(f"  no new articles since last run; skipping {symbol}.")
            skipped += 1
            continue

        log(f"  {len(fresh)} new article(s); posting card with up to "
            f"{NEWS_PER_CARD}.")
        if post_to_teams(build_ticker_card(symbol, fresh), webhook_url):
            posted += 1
            seen.update(article_key(a) for a in fresh)  # mark only on success
        else:
            log(f"  failed to post card for {symbol}.")

    save_state(state)
    log(f"Done. Posted {posted} card(s); "
        f"skipped {skipped} ticker(s) with no new news.")


def main() -> None:
    parser = argparse.ArgumentParser(description="FMP -> Teams news bot")
    parser.add_argument(
        "--test", action="store_true",
        help="post one sample card and exit (no FMP call)",
    )
    args = parser.parse_args()

    if args.test:
        run_test()
    else:
        run()


if __name__ == "__main__":
    main()
