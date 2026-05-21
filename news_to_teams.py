"""
FMP -> Microsoft Teams news bot.

For every ticker in watchlist.txt, fetches the latest stock news from
Financial Modeling Prep and posts ONE Adaptive Card per ticker (the ticker
name plus its latest 1-5 headlines) to a Teams channel via a Workflows
incoming webhook.

A card is posted for every ticker on every run, whether or not the news
changed (digest mode - no deduplication).

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
import os
import sys
import time
from datetime import date
from pathlib import Path

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

FMP_NEWS_URL = "https://financialmodelingprep.com/stable/news/stock"
FMP_LOGO_URL = "https://images.financialmodelingprep.com/symbol/{symbol}.png"

NEWS_PER_CARD = 5      # max headlines shown on each ticker card
FETCH_LIMIT = 12       # articles to request per ticker (extra as a buffer)
SUMMARY_MAX_CHARS = 280  # truncate each article's summary text to this length
REQUEST_TIMEOUT = 20   # seconds


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
    """Fetch the latest news for one ticker, newest first. [] on error."""
    params = {
        "symbols": symbol,
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
    articles.sort(key=lambda a: a["published"], reverse=True)  # newest first
    return articles


# --- Adaptive Card ----------------------------------------------------------

def _md_safe(text: str) -> str:
    """Neutralise characters that would break a markdown link."""
    return text.replace("[", "(").replace("]", ")")


def build_ticker_card(symbol: str, articles: list[dict]) -> dict:
    """Build one Teams webhook payload: a card with up to NEWS_PER_CARD
    headlines for a single ticker."""
    body: list[dict] = [{
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
                "items": [{
                    "type": "TextBlock",
                    "text": symbol,
                    "weight": "Bolder",
                    "size": "Large",
                    "color": "Accent",
                }],
            },
        ],
    }]

    items = articles[:NEWS_PER_CARD]
    if not items:
        body.append({
            "type": "TextBlock",
            "text": "No recent news.",
            "isSubtle": True,
            "wrap": True,
            "spacing": "Small",
        })

    for index, article in enumerate(items):
        title = _md_safe(article["title"] or "(no title)")
        headline = f"[{title}]({article['url']})" if article["url"] else title
        body.append({
            "type": "TextBlock",
            "text": headline,
            "weight": "Bolder",
            "wrap": True,
            "spacing": "Medium",
            "separator": index > 0,  # divider line between articles
        })
        summary = article.get("text", "")
        if len(summary) > SUMMARY_MAX_CHARS:
            summary = summary[:SUMMARY_MAX_CHARS].rstrip() + "..."
        if summary:
            body.append({
                "type": "TextBlock",
                "text": summary,
                "wrap": True,
                "spacing": "Small",
            })
        meta = " | ".join(
            p for p in (article["site"], article["published"]) if p
        )
        if meta:
            body.append({
                "type": "TextBlock",
                "text": meta,
                "isSubtle": True,
                "size": "Small",
                "spacing": "None",
                "wrap": True,
            })

    card: dict = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
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
    """Post one news card per ticker in the watchlist."""
    api_key = require_env("FMP_API_KEY")
    webhook_url = require_env("TEAMS_WEBHOOK_URL")
    tickers = load_watchlist()

    posted = 0
    for symbol in tickers:
        log(f"Checking {symbol}...")
        articles = fetch_news(symbol, api_key)
        log(f"  {len(articles)} article(s) found; "
            f"posting card with up to {NEWS_PER_CARD}.")
        if post_to_teams(build_ticker_card(symbol, articles), webhook_url):
            posted += 1
        else:
            log(f"  failed to post card for {symbol}.")

    log(f"Done. Posted {posted}/{len(tickers)} ticker card(s).")


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
