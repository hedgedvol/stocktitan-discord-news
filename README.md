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

## v2 features
- **Price on card** – for each StockTitan item with a ticker, a `Price` field like
  `$11.42 ▲ 3.1%` (vs. previous close) plus a `Pre:`/`Post:` line when an extended-hours
  print exists. Source: Yahoo's unofficial chart endpoint
  (`query1`/`query2.finance.yahoo.com/v8/finance/chart/TICKER`, browser User-Agent),
  cached 60 s per ticker. If Yahoo blocks or errors, the card is posted without a price.
  OTC/foreign tickers may be missing or stale on Yahoo. Disable with `PRICES=0`.
- **Big movers** – if the largest of day/extended % move ≥ `MOVER_PCT` (default 10) the
  title gets 🔥; with `MOVER_ROLE_ID` set, the message content pings `<@&ID>` and
  `allowed_mentions` only allows that role (all other mentions are always disabled).
- **Separate channels** – `WEBHOOK_EARNINGS`, `WEBHOOK_FDA`, `WEBHOOK_OFFERINGS`, `WEBHOOK_MA`,
  `WEBHOOK_ANALYST`, `WEBHOOK_SEC` (SEC filings and insider buys), `WEBHOOK_HALTS`,
  `WEBHOOK_RECAP`, each from the repo secret of the same name. Any unset one falls back to
  `DISCORD_WEBHOOK_URL`; general news always goes there.
- **Market session tag** (footer + field) from pubDate in America/New_York:
  Pre-market 04:00–09:30, Regular 09:30–16:00, After-hours 16:00–20:00, Overnight otherwise,
  Closed on weekends (exchange holidays are not detected).
- **Daily recap** – once per weekday after 16:15 ET while the loop is running: one embed with
  the day's posted items grouped by type (latest 5 per type, linked, with counts) and the
  biggest movers. Day items and `recap_posted` live in the state file (bounded: 6000 seen ids,
  400 day items), so it survives restarts and is posted only once. If no run is active
  after 16:15 ET on a day, the recap for that day is skipped. `RECAP=0` disables.
- **Insider buys** (`INSIDER_BUYS=1`, on in the workflow) – polls the EDGAR Form 4 current feed
  (≤ once/60 s, 100 latest), then fetches each new filing's full-text submission (`<acc>.txt`,
  one request, contains the ownership XML) and posts only open-market purchases
  (transactionCode `P`) with insider name/role, shares, avg price, $ value, ticker
  (`issuerTradingSymbol`) and link, if value ≥ `INSIDER_MIN_USD` (default 50 000).
  All sec.gov requests are globally throttled to ≤ 1/s with the compliant User-Agent, and XML
  fetches are capped at `INSIDER_MAX_FETCH` (default 8) per cycle; the rest wait in a backlog
  (bounded at 300). Respects `TICKERS`.
- **Trading halts** (`HALTS=1`) – Nasdaq Trader halts RSS every cycle. New halts are posted red
  with the reason code and meaning (T1 news pending, LUDP volatility pause, H10 SEC suspension…);
  when the resumption time appears, a green "Resumed" card is posted. Halts that already have a
  resumption time when first seen are posted once as green. Respects `TICKERS`.
- **No flood**: each source (stocktitan, each `sec:<form>`, insider, halts) is seeded silently
  the first time it is polled (tracked in `state.seeded`), including when you enable a source
  later. The old list-format `seen.json` is migrated automatically.

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
| `PRICES` / `MOVER_PCT` / `MOVER_ROLE_ID` | 1 / 10 / empty | price field, mover threshold, role ping |
| `WEBHOOK_*` | empty | per-type channels (see above) |
| `INSIDER_BUYS` / `INSIDER_MIN_USD` / `INSIDER_MAX_FETCH` | 0 (workflow 1) / 50000 / 8 | Form 4 buys |
| `HALTS` / `RECAP` | 1 / 1 | halts and daily recap |
| `STATE_FILE` | state/seen.json | dedupe state (for local testing) |

Set filters by editing the `env:` block in `news.yml` (or switch them to `${{ vars.TICKERS }}`
repository variables).

## Local test
```
DISCORD_WEBHOOK_URL=http://127.0.0.1:8765/hook STATE_FILE=/tmp/seen.json RUN_MINUTES=0 python stocktitan_discord.py
```
