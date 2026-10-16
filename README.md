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

## v3 features (speed first)
Main loop polls every `POLL_SECONDS` (default 10). Only the fast news sources run every tick;
everything else self-throttles: SEC ≥60 s per form, Form 4 job every 30 s (≤`INSIDER_MAX_FETCH`
XML fetches, all sec.gov ≤1 req/s), halts 20 s, unusual volume 120 s, social 600 s, recap and
earnings-calendar checks 60 s, Yahoo price cache 60 s, float/short cache 24 h (in state).

Sources, fastest first:
1. **StockTitan websocket** (`STOCKTITAN_WS`, default **0 / experimental**): the live page exposes an
   anonymous URL (`wss://ws.stocktitan.net:9011//<id>`, no token for logged-out users; frames are
   `{"header":{"type":"news"|"ping"},"payload":…}`, reply `{"type":"pong"}`). A background thread
   connects with exponential backoff (5 s → 5 min) and feeds a queue drained each tick. It could not be
   verified from the dev box (outbound port 9011 blocked), so it is off by default; enable it and watch
   the `ws …` log line. Requires `websocket-client`.
2. **StockTitan live.html** (`STOCKTITAN_LIVE=1`): parses the `jsGlobals.newsList` JSON embedded in
   https://www.stocktitan.net/news/live.html (~65 latest items with ticker, exchange, title, UTC time,
   float, short %, one-line summary). Deduped with the RSS by URL. Its float/short data is reused
   instead of calling Yahoo.
3. **Direct wires** (`WIRES=1`; toggles `WIRE_PRN`, `WIRE_BW`, `WIRE_GNW`, `WIRE_ACCESSWIRE`):
   PR Newswire `news-releases-list.rss` (works, 20 latest), Business Wire all-news RSS
   (works but only a handful of items), GlobeNewswire public-companies RSS (HTTP 403 / Akamai from
   the test box and fetch service – may or may not work from GitHub), Accesswire (no public RSS;
   set `ACCESSWIRE_RSS_URL` if you obtain one). Only items with an exchange ticker
   (`(NASDAQ: ABCD)`, `NYSE American:`, `OTCQB:` …) are posted; non-English Business Wire copies skipped.
4. **StockTitan RSS** (as before; slowest).
5. **newsfilter.io** (optional `NEWSFILTER_API_KEY` secret): polls the Query API every
   `NEWSFILTER_POLL` s. Note: free Starter = 100 calls/day, 5 articles/request, **1-hour delay**
   (useless for speed; free-key signup is reported broken). Real-time needs Basic ($35/mo);
   websocket stream needs Professional ($65/mo).

**Cross-source dedupe**: every story is remembered (ticker + normalized title tokens, 12 h).
A later copy with the same ticker and token similarity ≥ 0.6 is dropped; wire-first cards say
`⚡ via PR Newswire`. Lead times are logged into `state.latency`.

Card enrichment (each step optional and failure-tolerant):
- **AI take + sentiment**: with `AI_API_KEY` (Groq by default, `AI_BASE_URL` for any
  OpenAI-compatible endpoint, e.g. Gemini `https://generativelanguage.googleapis.com/v1beta/openai`),
  `AI_MODEL` default `openai/gpt-oss-20b` (Groq retired its free Llama 3.x models in Aug 2026;
  set e.g. `meta-llama/llama-4-scout-17b-16e-instruct` if you prefer Llama), ≤20-word take,
  4 s timeout, `AI_PER_MIN` cap. Otherwise / on failure: keyword lexicon → 🟢/🟡/🔴 only.
- **Float & short** (`FLOAT_DATA`): Yahoo quoteSummary `defaultKeyStatistics` via cookie+crumb;
  tags `🧊 Low float` (< `LOW_FLOAT`) and `🩳 High short` (> `HIGH_SHORT` %).
- **Mini chart** (`CHARTS`): intraday 5m line (≤60 points, prev-close dashed) rendered by
  QuickChart's free `/chart/create` short URL.
- **🗣️ Trending on Reddit** tag for tickers in ApeWisdom's current top 20.

Other alerts:
- **Earnings calendar** (`EARNINGS_CAL`): weekdays 08:00–11:00 ET, once/day, Nasdaq calendar API,
  top 20 by market cap with EPS estimates, before-open / after-close. → `WEBHOOK_EARNINGS_CAL` →
  `WEBHOOK_EARNINGS` → main.
- **Unusual volume** (`UNUSUAL_VOLUME`): every 2 min in regular + after-hours, Yahoo predefined
  screeners (most_actives, day_gainers, day_losers, small_cap_gainers ≈ 250 tickers); alert when
  volume ≥ `VOL_MULT` × 10-day average scaled by a time-of-day volume curve, price ≥ `VOL_MIN_PRICE`,
  volume ≥ `VOL_MIN_SHARES`; once per ticker per day. → `WEBHOOK_VOLUME`.
  (Pre-market is skipped: Yahoo's screener only has regular-session volume.)
- **Social buzz** (`SOCIAL`): ApeWisdom every 10 min; alert when rank rose ≥ 20 places in 24 h and
  mentions ≥ 2× the 24h-ago count and ≥ `SOCIAL_MIN_MENTIONS`; once per ticker per day. → `WEBHOOK_SOCIAL`.

Every new source is seeded silently on its first poll (no flood). State stays bounded
(8000 seen ids, 800 stories, 400 day items, 400 float entries, 100 latency samples; ≈50–150 KB).

### Future option: watchlist DMs
Per-user watchlists with DMs need a real Discord bot (bot token + gateway connection, slash
commands, DM permission). Webhooks cannot DM users, so this is not implemented. It would require a
persistent host (not a GitHub Actions loop) running e.g. discord.py, reading the same state.

## Env vars
| var | default | meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | (secret) | webhook; bot exits if empty |
| `POLL_SECONDS` | 10 | poll interval |
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
