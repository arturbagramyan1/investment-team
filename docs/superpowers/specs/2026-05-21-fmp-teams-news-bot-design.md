# FMP → Teams News Bot — Design

**Date:** 2026-05-21
**Status:** Implemented

## Overview

A scheduled bot that pulls stock news from the Financial Modeling Prep (FMP)
API for a configurable watchlist and posts, for each ticker, **one Adaptive
Card** with that ticker's news for the current day into a Microsoft Teams
channel via a Teams Workflows incoming webhook.

A card is posted only when a ticker has articles that are new since the last
run; if nothing changed, that ticker is skipped.

Built and tested locally; next phase wraps it in a GitHub Actions cron
workflow.

## Goals

- One clean card per ticker, showing up to 5 collapsible headlines from the
  current day (Armenia time).
- Never re-post old news — skip a ticker when it has nothing new since the
  last run.
- Watchlist editable without touching code.
- Secrets never hardcoded — read from environment.

## Non-goals

- No FastAPI service, Redis, or Kubernetes (overkill for a poller).
- No sentiment analysis / sector tagging.
- No database — the dedup store is a single small JSON file.

## Implementation

A single script, `news_to_teams.py`, with focused functions:

| Area | Functions |
|------|-----------|
| Config | `require_env`, `load_watchlist` |
| State | `load_state`, `save_state`, `article_key` |
| Dates | `_parse_fmp_date`, `_pretty_date`, `_clock_time`, `_is_today` |
| FMP | `fetch_news`, `normalize_article` |
| Card | `build_ticker_card`, `_header_block`, `_article_block`, `_md_safe` |
| Teams | `post_to_teams` (retries transient errors) |
| Entry | `run`, `run_test`, `main` |

Dependencies (`requests`, `python-dotenv`, `tzdata`) are managed by `uv` via
`pyproject.toml`. Run with `uv run news_to_teams.py`.

## Timezone handling

The team is in Armenia, so all dates shown on cards are converted to
`Asia/Yerevan` (`DISPLAY_TZ`). FMP news timestamps carry no timezone marker;
a live-response check confirmed they are US Eastern time, so they are tagged
`America/New_York` (`FMP_SOURCE_TZ`). "Today" and all displayed times are
computed timezone-aware, so results are identical wherever the script runs.
Times shown on cards are absolute (`HH:MM`), never relative — a posted
Adaptive Card is static, so a relative "Xh ago" would silently go stale.

## Data flow

1. Load `FMP_API_KEY` and `TEAMS_WEBHOOK_URL` from env (`.env` via
   python-dotenv); read `watchlist.txt`; load `posted_state.json`.
2. For each ticker: `fetch_news()` calls
   `https://financialmodelingprep.com/stable/news/stock` with `symbols`,
   `from`/`to` (a yesterday-to-today window) and `limit`. Articles are
   normalized to `{symbol, title, text, url, site, published}` and trimmed to
   exactly the current Armenia day by `_is_today`, newest first.
3. Articles whose key (URL + title) is already in the posted-state store are
   dropped. If a ticker has no new articles, it is skipped (no card).
4. `build_ticker_card()` builds one Adaptive Card from the new articles (see
   Adaptive Card design below).
5. `post_to_teams()` POSTs the card; expects HTTP 200/202; retries 429/5xx
   with backoff. On success the posted articles' keys are added to the state
   set.
6. `save_state()` writes the day's posted-URL set back to `posted_state.json`.

## Posted-state store

`posted_state.json` holds `{"date": "YYYY-MM-DD", "seen": [keys]}` for the
current day. An article's key is `URL + title` (`article_key`), so the same
URL reused for a different headline still counts as new. On a new day, or if
the file is missing or corrupt, the store resets to empty — so every article
on a fresh day counts as new. Because it is scoped to one day, the file stays
tiny.

**Persistence in the cloud:** GitHub Actions runners are ephemeral, so this
file does not survive between runs by default. The deployment must restore
and save it — see Deployment.

## Adaptive Card design (one card per ticker)

- The card spans Teams' full width (`msteams.width: "Full"`).
- Header — a subtle (`emphasis`), rounded-corner banner: company logo (from
  FMP), the ticker name (bold, extra-large, accent colour) with a subtitle,
  and the card's post date/time (Armenia time) at the right.
- Up to 5 **collapsible** articles (those new since the last run). Each
  collapsed row is one line — ▸ chevron, headline, and the article's publish
  time (`HH:MM`, Armenia). Tapping it (`Action.ToggleVisibility`) reveals a detail
  block — summary (article text truncated to ~280 chars), a subtle
  `source · date` line and a "Read full article" button (`Action.OpenUrl`)
  — and flips the chevron to ▾. Articles are separated by divider lines.
- All dates/times are shown in Armenia time (see Timezone handling).
- Card schema version 1.5.

The card is wrapped in the Teams `attachments` envelope with
`contentType: application/vnd.microsoft.card.adaptive`.

## Error handling

- Missing/empty env var → clear message, exit non-zero.
- FMP request error / non-200 → log, treat as no news for that ticker,
  continue with other tickers.
- Teams non-2xx → retry transient errors (429, 5xx) with backoff. A URL is
  marked "seen" only after a successful post, so a failed card retries next
  run.
- Unreadable/corrupt state file → log a warning and start from an empty set.
- `--test` flag posts one sample ticker card to verify the webhook
  independently of FMP and of the state store.

## Configuration / secrets

- `FMP_API_KEY` and `TEAMS_WEBHOOK_URL` come from environment, loaded from
  `.env` locally (`.env` is gitignored; `.env.example` is committed).
- For GitHub Actions: stored as repository secrets.

## Deployment (next phase)

- GitHub Actions workflow on a `cron` schedule (UTC; Armenia is UTC+4).
- The workflow must persist `posted_state.json` between runs, otherwise the
  dedup store resets every run and old news is re-posted. Options:
  commit the file back to the repo each run (reliable, adds commit noise),
  or use `actions/cache` with a rolling key (no commit noise, a rare cache
  miss re-posts one batch). To be decided when the workflow is built.

## Security note

The Teams webhook URL carries its own auth signature, and the FMP API key is
a credential. Neither is ever committed (`.env` is gitignored). Both values
shared during development should be rotated before go-live.
