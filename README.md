# StockTitan → Discord news bot

Runs on GitHub Actions (`.github/workflows/news.yml`, every 15 min, each run loops for `RUN_MINUTES`).
Polls https://www.stocktitan.net/rss every `POLL_SECONDS`, dedupes via `state/seen.json`
(committed back by the workflow) and posts one embed per new item to `DISCORD_WEBHOOK_URL`.
On the very first run (no state file) everything currently in the feeds is marked seen
without posting, so the channel is not flooded.

## Features
- **Ticker extraction** – StockTitan titles end in `| TICKER Stock News`; the link path
  `/news/TICKER/...` is used as a fallback. (The feed carries no category/exchange data,
  so the exchange is not shown.) The card shows `$TICKER` as author plus links to the
  StockTitan overview, Yahoo Finance and TradingView, and the company logo as thumbnail.
- **Colour-coded cards** by headline keywords (first match wins):
  🔴 Offering (offering, priced, registered direct, warrants, private placement, ATM) – red ·
  📈 Earnings (earnings, results) – green · 💊 FDA / Clinical (FDA, regulatory approval,
  clinical, trial, phase N) – purple · 🤝 M&A (merger, acquisition, business combination) – blue ·
  🎯 Analyst (upgrade, downgrade, price target) – orange · 📰 News – gray.
  Embed timestamp comes from `pubDate`.
- **Filters** (comma lists, empty = allow all, case-insensitive):
  - `TICKERS` – only these tickers (e.g. `AAPL,TSLA`)
  - `KEYWORDS` – headline must contain at least one
  - `EXCLUDE` – skip if headline contains any
- **SEC EDGAR filings (optional)** – set `SEC_FORMS` (e.g. `8-K,4`) to also poll the EDGAR
  "current filings" Atom feed per form, at most once per 60 s per form, with the
  User-Agent `hedgedvol-news-bot hedgedvol@gmail.com` (override with `SEC_USER_AGENT`).
  Posted as dark-blue `🏛️ SEC 8-K — Company` cards. `TICKERS` filtering works for filings
  by mapping the filer CIK to tickers via SEC's `company_tickers.json` (companies with no
  listed ticker, e.g. many funds/insiders, are dropped when `TICKERS` is set).
  `KEYWORDS`/`EXCLUDE` match form + company name + item list.
  **Off by default** – all-company 8-Ks/Form 4s are very noisy; the feed only shows the
  latest 40 per form, so bursts beyond 40/min can be missed. A newly added form is seeded
  silently on its first poll.
- Discord rate limits: 429 `retry_after` is honoured, ~1 s between posts. Each source is
  wrapped in try/except so one failing source doesn't stop the loop.

## Env vars
| var | default | meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | (secret) | webhook; bot exits if empty |
| `POLL_SECONDS` | 30 | poll interval |
| `RUN_MINUTES` | 0 (workflow: 340) | loop duration, 0 = single pass |
| `STOCKTITAN_FEED_URL` | stocktitan rss | feed URL |
| `TICKERS`, `KEYWORDS`, `EXCLUDE` | empty | filters |
| `SEC_FORMS` | empty | e.g. `8-K,4` to enable EDGAR |
| `SEC_USER_AGENT` | hedgedvol-news-bot hedgedvol@gmail.com | SEC UA |
| `STATE_FILE` | state/seen.json | dedupe state (for local testing) |

Set filters by editing the `env:` block in `news.yml` (or switch them to `${{ vars.TICKERS }}`
repository variables).

## Local test
```
DISCORD_WEBHOOK_URL=http://127.0.0.1:8765/hook STATE_FILE=/tmp/seen.json RUN_MINUTES=0 python stocktitan_discord.py
```
