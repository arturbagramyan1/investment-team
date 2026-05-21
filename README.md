# FMP -> Teams News Bot

Posts stock news from Financial Modeling Prep into a Microsoft Teams channel
as Adaptive Cards, via a Teams Workflows incoming webhook.

## Setup

1. Install dependencies:

   ```
   uv sync
   ```

2. Create your secrets file:

   ```
   copy .env.example .env
   ```

   Then edit `.env` and set:
   - `FMP_API_KEY` - your Financial Modeling Prep API key
   - `TEAMS_WEBHOOK_URL` - your Teams Workflows webhook URL

3. Edit `watchlist.txt` - one ticker per line (starts with `NBIS`).

## Usage

Verify the Teams webhook works (posts one sample card, no FMP call):

```
uv run news_to_teams.py --test
```

Fetch news and post new articles:

```
uv run news_to_teams.py
```

## How it works

- For each ticker in `watchlist.txt`, fetches its latest news from FMP.
- Posts **one Adaptive Card per ticker**: the ticker name plus its latest
  1-5 news items - each with a linked headline, a short summary, and source.
- A card is posted for every ticker on every run, whether or not the news
  changed (digest mode - no deduplication).

## Notes

- FMP's stock-news endpoint requires a paid FMP plan; a free key may return
  HTTP 401/403.
- The Teams webhook URL carries its own auth signature - keep it in `.env`,
  never commit it, and rotate it if it is ever exposed.
