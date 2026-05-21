# FMP → Teams News Bot — Design

**Date:** 2026-05-21
**Status:** Implemented

## Overview

A scheduled bot that pulls stock news from the Financial Modeling Prep (FMP)
API for a configurable watchlist and posts, for each ticker, **one Adaptive
Card** containing that ticker's latest 1–5 headlines into a Microsoft Teams
channel via a Teams Workflows incoming webhook.

Built and tested locally; next phase wraps it in a GitHub Actions cron
workflow.

## Goals

- One clean card per ticker, showing its latest 1–5 headlines as links.
- A card for every ticker on every run (digest mode).
- Watchlist editable without touching code.
- Secrets never hardcoded — read from environment.

## Non-goals

- No deduplication / state store — a card posts every run regardless of
  whether the news changed.
- No FastAPI service, Redis, or Kubernetes (overkill for a poller).
- No sentiment analysis / sector tagging.

## Implementation

A single script, `news_to_teams.py`, with focused functions:

| Area | Functions |
|------|-----------|
| Config | `require_env`, `load_watchlist` |
| FMP | `fetch_news`, `normalize_article` |
| Card | `build_ticker_card`, `_md_safe` |
| Teams | `post_to_teams` (retries transient errors) |
| Entry | `run`, `run_test`, `main` |

Dependencies (`requests`, `python-dotenv`) are managed by `uv` via
`pyproject.toml`. Run with `uv run news_to_teams.py`.

## Data flow

1. Load `FMP_API_KEY` and `TEAMS_WEBHOOK_URL` from env (`.env` via
   python-dotenv); read `watchlist.txt`.
2. For each ticker: `fetch_news()` calls
   `https://financialmodelingprep.com/stable/news/stock` with `symbols` and
   `limit`, returning normalized articles `{symbol, title, url, site,
   published}`, newest first.
3. `build_ticker_card()` builds one Adaptive Card: ticker name header plus up
   to 5 headlines (each a markdown link) with a subtle `source | date` line.
   If a ticker has no news, the card shows "No recent news."
4. `post_to_teams()` POSTs the card; expects HTTP 200/202; retries 429/5xx
   with backoff.

## Adaptive Card design (one card per ticker)

- Header — company logo (from FMP) beside the ticker name (bold, large,
  accent colour).
- Up to 5 articles, each: bold headline as a clickable markdown link, a short
  summary (article text truncated to ~280 chars), then a subtle
  `source | published date` line, separated by divider lines.

The card is wrapped in the Teams `attachments` envelope with
`contentType: application/vnd.microsoft.card.adaptive`.

## Error handling

- Missing/empty env var → clear message, exit non-zero.
- FMP request error / non-200 → log, treat as no news for that ticker, the
  card shows "No recent news", continue with other tickers.
- Teams non-2xx → retry transient errors (429, 5xx) with backoff.
- `--test` flag posts one sample ticker card to verify the webhook
  independently of FMP.

## Configuration / secrets

- `FMP_API_KEY` and `TEAMS_WEBHOOK_URL` come from environment, loaded from
  `.env` locally (`.env` is gitignored; `.env.example` is committed).
- For GitHub Actions: stored as repository secrets.

## Deployment (next phase)

- GitHub Actions workflow on a `cron` schedule (~every 15 min).
- No state to persist between runs (no dedup store), which keeps the
  workflow simple.

## Security note

The Teams webhook URL carries its own auth signature, and the FMP API key is
a credential. Neither is ever committed (`.env` is gitignored). Both values
shared during development should be rotated before go-live.
