"""StockTitan (+ SEC EDGAR, insider buys, Nasdaq halts, prices, daily recap) -> Discord."""
import calendar, html, json, os, re, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import feedparser, requests

ET_TZ = ZoneInfo("America/New_York")
env = lambda k, d="": os.environ.get(k, d).strip()

WEBHOOK = env("DISCORD_WEBHOOK_URL")
FEED = env("STOCKTITAN_FEED_URL", "https://www.stocktitan.net/rss")
STATE = env("STATE_FILE", "state/seen.json")
POLL = int(env("POLL_SECONDS", "10"))
RUN_MINUTES = float(env("RUN_MINUTES", "0"))  # 0 = single pass
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
SEC_UA = env("SEC_USER_AGENT", "hedgedvol-news-bot hedgedvol@gmail.com")
SEC_MIN_INTERVAL = 60  # seconds between polls of the same EDGAR form
MOVER_PCT = float(env("MOVER_PCT", "10"))
MOVER_ROLE_ID = env("MOVER_ROLE_ID")
PRICES = env("PRICES", "1") == "1"
INSIDER_BUYS = env("INSIDER_BUYS", "0") == "1"
INSIDER_MIN_USD = float(env("INSIDER_MIN_USD", "50000"))
INSIDER_MAX_FETCH = int(env("INSIDER_MAX_FETCH", "5"))  # XML fetches per cycle
HALTS = env("HALTS", "1") == "1"
HALTS_URL = env("HALTS_URL", "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts")
RECAP = env("RECAP", "1") == "1"
WIRES = env("WIRES", "1") == "1"
WIRE_FEEDS = {  # name -> (toggle env, url)
    "PR Newswire": ("WIRE_PRN", "https://www.prnewswire.com/rss/news-releases-list.rss"),
    "Business Wire": ("WIRE_BW", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFtRWA=="),
    "GlobeNewswire": ("WIRE_GNW", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    "Accesswire": ("WIRE_ACCESSWIRE", env("ACCESSWIRE_RSS_URL")),  # no public RSS found; set URL to enable
}
AI_API_KEY = env("AI_API_KEY")
AI_BASE_URL = env("AI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
AI_MODEL = env("AI_MODEL", "openai/gpt-oss-20b")
AI_PER_MIN = int(env("AI_PER_MIN", "20"))
AI_TIMEOUT = float(env("AI_TIMEOUT", "4"))
FLOAT_DATA = env("FLOAT_DATA", "1") == "1"
LOW_FLOAT = float(env("LOW_FLOAT", "20000000"))
HIGH_SHORT = float(env("HIGH_SHORT", "20"))
CHARTS = env("CHARTS", "1") == "1"
EARNINGS_CAL = env("EARNINGS_CAL", "1") == "1"
UNUSUAL_VOLUME = env("UNUSUAL_VOLUME", "1") == "1"
VOL_MULT = float(env("VOL_MULT", "5"))
VOL_MIN_PRICE = float(env("VOL_MIN_PRICE", "1"))
VOL_MIN_SHARES = float(env("VOL_MIN_SHARES", "500000"))
SOCIAL_MIN_MENTIONS = int(env("SOCIAL_MIN_MENTIONS", "15"))
SOCIAL = env("SOCIAL", "1") == "1"

def _list(name, upper=False):
    items = [x.strip() for x in env(name).split(",") if x.strip()]
    return [x.upper() if upper else x.lower() for x in items]

TICKERS = set(_list("TICKERS", upper=True))
KEYWORDS = _list("KEYWORDS")
EXCLUDE = _list("EXCLUDE")
SEC_FORMS = _list("SEC_FORMS", upper=True)

# ---------- classification ----------
TYPES = [  # (key, label, color, regex) – first match wins
    ("offering", "🔴 Offering", 0xE74C3C, r"\b(offering|dilution|dilutive|priced|pricing of|registered direct|warrants?|private placement|at-the-market|ATM program)\b"),
    ("earnings", "📈 Earnings", 0x2ECC71, r"\b(earnings|results|EPS|revenue[s]? of|quarterly report|fiscal (year|quarter))\b"),
    ("fda", "💊 FDA / Clinical", 0x9B59B6, r"\b(FDA|EMA|(regulatory|marketing|drug|clearance and) approval|clinical|trials?|phase [1-3I]+[ab]?|NDA|BLA|IND|510\(k\))\b"),
    ("ma", "🤝 M&A", 0x3498DB, r"\b(merger|acquisition|acquire[sd]?|business combination|to be acquired|takeover)\b"),
    ("analyst", "🎯 Analyst", 0xE67E22, r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiates coverage)\b"),
]
TYPES = [(k, l, c, re.compile(p, re.I)) for k, l, c, p in TYPES]
DEFAULT_TYPE = ("news", "📰 News", 0x95A5A6)
SEC_COLOR, INSIDER_COLOR, HALT_RED, RESUME_GREEN = 0x1F3A93, 0x1ABC9C, 0xC0392B, 0x27AE60
LABELS = {k: l for k, l, _, _ in TYPES} | {"news": "📰 News", "sec": "🏛️ SEC", "insider": "🟢 Insider buy", "halt": "⛔ Halt",
                                         "volume": "📊 Unusual volume", "social": "🗣️ Social buzz"}

def classify(title):
    for key, label, color, rx in TYPES:
        if rx.search(title or ""):
            return key, label, color
    return DEFAULT_TYPE

# ---------- routing ----------
ROUTES = {"earnings": "WEBHOOK_EARNINGS", "fda": "WEBHOOK_FDA", "offering": "WEBHOOK_OFFERINGS",
          "ma": "WEBHOOK_MA", "analyst": "WEBHOOK_ANALYST", "sec": "WEBHOOK_SEC",
          "insider": "WEBHOOK_SEC", "halt": "WEBHOOK_HALTS", "recap": "WEBHOOK_RECAP",
          "volume": "WEBHOOK_VOLUME", "social": "WEBHOOK_SOCIAL"}

def webhook_for(kind):
    if kind == "earnings_cal":
        return env("WEBHOOK_EARNINGS_CAL") or env("WEBHOOK_EARNINGS") or WEBHOOK
    return env(ROUTES[kind]) if kind in ROUTES and env(ROUTES[kind]) else WEBHOOK

# ---------- session ----------
def session_tag(dt_utc):
    t = dt_utc.astimezone(ET_TZ)
    if t.weekday() >= 5:
        return "🌙 Closed (weekend)"
    m = t.hour * 60 + t.minute
    if 240 <= m < 570: return "🌅 Pre-market"
    if 570 <= m < 960: return "🔔 Regular"
    if 960 <= m < 1200: return "🌆 After-hours"
    return "🌙 Overnight"

# ---------- filters ----------
def passes_filters(text, tickers, use_keywords=True):
    h = (text or "").lower()
    if TICKERS and not (set(t.upper() for t in tickers if t) & TICKERS):
        return False
    if use_keywords and KEYWORDS and not any(k in h for k in KEYWORDS):
        return False
    if use_keywords and EXCLUDE and any(k in h for k in EXCLUDE):
        return False
    return True

# ---------- prices (Yahoo, free, unofficial) ----------
_price_cache = {}
PRICE_TTL = 60

def get_quote(ticker):
    """Returns dict(price, pct, ext_price, ext_pct, ext_label) or None. Never raises."""
    if not PRICES or not ticker:
        return None
    hit = _price_cache.get(ticker)
    if hit and time.time() - hit[0] < PRICE_TTL:
        return hit[1]
    q = None
    try:
        for host in ("query1", "query2"):
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker.replace('.', '-')}",
                             params={"interval": "5m", "range": "1d", "includePrePost": "true"},
                             headers={"User-Agent": UA_BROWSER}, timeout=8)
            if r.status_code == 200:
                break
        res = r.json()["chart"]["result"][0]
        m = res["meta"]
        price, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
        if price is None:
            raise ValueError("no price")
        pct = (price / prev - 1) * 100 if prev else m.get("regularMarketChangePercent")
        q = {"price": price, "pct": pct, "ext_price": None, "ext_pct": None, "ext_label": None, "prev": prev}
        # extended-hours: last bar outside the regular period
        ts = res.get("timestamp") or []
        closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        q["series"] = [(t, c) for t, c in zip(ts, closes) if c is not None]
        q["reg"] = (m.get("currentTradingPeriod", {}).get("regular", {}).get("start"),
                    m.get("currentTradingPeriod", {}).get("regular", {}).get("end"))
        per = m.get("currentTradingPeriod", {})
        reg = per.get("regular", {})
        for t, c in reversed(list(zip(ts, closes))):
            if c is None:
                continue
            if t >= reg.get("end", 1e18):
                q.update(ext_price=c, ext_pct=(c / price - 1) * 100, ext_label="Post")
            elif t < reg.get("start", 0):
                q.update(ext_price=c, ext_pct=(c / prev - 1) * 100 if prev else None, ext_label="Pre")
            break
    except Exception as ex:
        print(f"price {ticker} unavailable: {ex}", flush=True)
    _price_cache[ticker] = (time.time(), q)
    return q

def fmt_pct(p):
    return "" if p is None else f"{'▲' if p >= 0 else '▼'} {abs(p):.1f}%"

def fmt_quote(q):
    s = f"${q['price']:,.2f} {fmt_pct(q['pct'])}"
    if q.get("ext_price") is not None:
        s += f"\n{q['ext_label']}: ${q['ext_price']:,.2f} {fmt_pct(q['ext_pct'])}"
    return s

def move_pct(q):
    """Largest relevant move (regular day change or extended vs. prior close)."""
    if not q: return None
    vals = [q["pct"]]
    if q.get("ext_price") is not None and q.get("pct") is not None and q["ext_label"] == "Post":
        vals.append(((1 + q["pct"] / 100) * (1 + (q["ext_pct"] or 0) / 100) - 1) * 100)
    elif q.get("ext_pct") is not None:
        vals.append(q["ext_pct"])
    vals = [v for v in vals if v is not None]
    return max(vals, key=abs) if vals else None


# ---------- Yahoo session (cookie + crumb) for quoteSummary / quote ----------
_yh = {"s": None, "crumb": None, "t": 0}

def yahoo_session(refresh=False):
    if refresh or not _yh["crumb"] or time.time() - _yh["t"] > 3600:
        s = requests.Session()
        s.headers["User-Agent"] = UA_BROWSER
        try:
            s.get("https://fc.yahoo.com", timeout=8)  # 404 is normal; it sets the A3 cookie
        except Exception:
            pass
        r = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=8)
        crumb = r.text.strip() if r.status_code == 200 and len(r.text) < 40 and "<" not in r.text else None
        _yh.update(s=s, crumb=crumb, t=time.time())
    return _yh["s"], _yh["crumb"]

def yahoo_json(url, params):
    for attempt in (0, 1):
        s, crumb = yahoo_session(refresh=attempt == 1)
        p = dict(params, **({"crumb": crumb} if crumb else {}))
        r = s.get(url, params=p, timeout=8)
        if r.status_code in (401, 403) and attempt == 0:
            continue
        r.raise_for_status()
        return r.json()

FLOAT_TTL = 24 * 3600

def get_float_short(ticker):
    """{'float': shares, 'short_pct': % of float} cached 24h in state; None on failure."""
    if not FLOAT_DATA or not ticker:
        return None
    c = state.setdefault("fs", {})
    hit = c.get(ticker)
    if hit and time.time() - hit["t"] < FLOAT_TTL:
        return hit
    out = {"t": time.time(), "float": None, "short_pct": None}
    try:
        d = yahoo_json(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker.replace('.', '-')}",
                       {"modules": "defaultKeyStatistics"})
        ks = d["quoteSummary"]["result"][0]["defaultKeyStatistics"]
        out["float"] = (ks.get("floatShares") or {}).get("raw")
        sp = (ks.get("shortPercentOfFloat") or {}).get("raw")
        out["short_pct"] = sp * 100 if sp is not None else None
    except Exception as ex:
        print(f"float {ticker} unavailable: {ex}", flush=True)
    c[ticker] = out
    if len(c) > 400:
        for k in sorted(c, key=lambda k: c[k]["t"])[:len(c) - 400]:
            c.pop(k)
    return out

def human(n):
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suf}"
    return f"{n:.0f}"

# ---------- QuickChart mini chart ----------
_chart_cache = {}

def chart_url(ticker, q):
    """Short QuickChart URL for the intraday 5m series (downsampled), or None."""
    if not CHARTS or not q or len(q.get("series") or []) < 3:
        return None
    hit = _chart_cache.get(ticker)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    url = None
    try:
        ser = q["series"]
        step = max(1, len(ser) // 60)
        pts = ser[::step]
        if pts[-1] != ser[-1]:
            pts.append(ser[-1])
        labels = [datetime.fromtimestamp(t, ET_TZ).strftime("%H:%M") for t, _ in pts]
        data = [round(c, 4) for _, c in pts]
        up = data[-1] >= (q.get("prev") or data[0])
        col = "rgb(46,204,113)" if up else "rgb(231,76,60)"
        cfg = {"type": "line", "data": {"labels": labels, "datasets": [
                  {"data": data, "borderColor": col, "borderWidth": 2, "pointRadius": 0, "fill": False}]},
               "options": {"legend": {"display": False}, "title": {"display": True, "text": f"{ticker} 5m (ET)"},
                           "scales": {"xAxes": [{"ticks": {"maxTicksLimit": 6}}]}}}
        if q.get("prev"):
            cfg["data"]["datasets"].append({"data": [q["prev"]] * len(data), "borderColor": "rgba(150,150,150,0.8)",
                                            "borderDash": [4, 4], "borderWidth": 1, "pointRadius": 0, "fill": False})
        r = requests.post("https://quickchart.io/chart/create",
                          json={"chart": cfg, "width": 500, "height": 250, "backgroundColor": "white"}, timeout=6)
        if r.status_code == 200 and r.json().get("success"):
            url = r.json()["url"]
    except Exception as ex:
        print(f"chart {ticker} unavailable: {ex}", flush=True)
    _chart_cache[ticker] = (time.time(), url)
    return url

# ---------- sentiment: AI (OpenAI-compatible) with lexicon fallback ----------
BULL = ("beat", "beats", "record", "surge", "approval", "approved", "positive", "raises", "raised", "upgrade",
        "awarded", "contract", "partnership", "exceeds", "growth", "breakthrough", "acquire", "launches",
        "expands", "strong", "profit", "wins", "buyback", "repurchase", "dividend increase", "milestone")
BEAR = ("offering", "dilution", "priced", "misses", "miss", "downgrade", "lawsuit", "investigation", "class action",
        "delist", "deficiency", "bankruptcy", "chapter 11", "resigns", "terminated", "halt", "recall", "loss",
        "decline", "warning", "cuts", "lowers", "suspends", "default", "going concern", "reverse split", "fraud")
_ai_calls = []

def lexicon_sentiment(text):
    t = (text or "").lower()
    score = sum(t.count(w) for w in BULL) - sum(t.count(w) for w in BEAR)
    return "Bullish" if score > 0 else "Bearish" if score < 0 else "Neutral"

def ai_take(title, summary):
    """(take or None, sentiment, source). AI only if AI_API_KEY; never raises."""
    if AI_API_KEY:
        now = time.time()
        _ai_calls[:] = [t for t in _ai_calls if now - t < 60]
        if len(_ai_calls) < AI_PER_MIN:
            _ai_calls.append(now)
            try:
                body = {"model": AI_MODEL, "temperature": 0.2, "max_tokens": 300,
                        "messages": [{"role": "system", "content":
                            "You are a terse markets news analyst. Reply ONLY with JSON: "
                            '{"take": "<=20 words on why it matters to the stock", "sentiment": "Bullish|Neutral|Bearish"}'},
                            {"role": "user", "content": f"Headline: {title}\nSummary: {(summary or '')[:800]}"}]}
                if "gpt-oss" in AI_MODEL:
                    body["reasoning_effort"] = "low"
                r = requests.post(f"{AI_BASE_URL}/chat/completions", json=body, timeout=AI_TIMEOUT,
                                  headers={"Authorization": f"Bearer {AI_API_KEY}"})
                r.raise_for_status()
                txt = r.json()["choices"][0]["message"]["content"]
                j = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
                sent = str(j.get("sentiment", "")).capitalize()
                if sent not in ("Bullish", "Neutral", "Bearish"):
                    sent = lexicon_sentiment(title)
                take = " ".join(str(j.get("take", "")).split()[:20]) or None
                return take, sent, "ai"
            except Exception as ex:
                print("ai unavailable:", str(ex)[:120], flush=True)
    return None, lexicon_sentiment(f"{title} {summary or ''}"[:600]), "lexicon"

SENT_ICON = {"Bullish": "🟢", "Neutral": "🟡", "Bearish": "🔴"}

# ---------- social (ApeWisdom) ----------
_social = {"top": {}, "t": 0}

def social_tag(ticker):
    r = _social["top"].get(ticker)
    return f"🗣️ Trending on Reddit (#{r})" if r else None

# ---------- StockTitan ----------
TITLE_RX = re.compile(r"^(?P<head>.*?)\s*\|\s*(?P<tick>[A-Z0-9.\-]{1,12})\s+Stock News\s*$")
LINK_RX = re.compile(r"stocktitan\.net/news/(?P<tick>[A-Za-z0-9.\-]+)/")

def _dt(struct):
    return datetime.fromtimestamp(calendar.timegm(struct), timezone.utc) if struct else None

def parse_stocktitan(e):
    """Feed has no <category>/exchange; ticker is in the title suffix and link path."""
    raw = (e.get("title") or "").strip()
    link = e.get("link") or ""
    m = TITLE_RX.match(raw)
    headline, ticker = (m.group("head"), m.group("tick")) if m else (raw, None)
    lm = LINK_RX.search(link)
    if not ticker and lm:
        ticker = lm.group("tick").upper()
    key, label, color = classify(headline)
    dt = _dt(e.get("published_parsed"))
    logo = next((mc["url"] for mc in (e.get("media_content") or []) if mc.get("url")), None)
    return {"headline": headline, "ticker": ticker, "link": link, "kind": key, "type": label,
            "color": color, "timestamp": dt.isoformat() if dt else None,
            "session": session_tag(dt) if dt else None, "logo": logo}

def build_news_embed(p, quote=None, extra=None):
    """p: parsed item; extra: dict(fs, sentiment, take, chart, social, via). Stays within Discord limits."""
    extra = extra or {}
    mv = move_pct(quote)
    big = mv is not None and abs(mv) >= MOVER_PCT
    title = (("🔥 " if big else "") + (p["headline"] or "News"))[:256]
    src = extra.get("via") or "StockTitan"
    emb = {"title": title, "url": p["link"] or None, "color": int(p["color"]),
           "footer": {"text": " · ".join(x for x in (p["type"], p.get("session"),
                                                    f"⚡ via {src}" if extra.get("via") else src) if x)[:2048]}}
    if p["timestamp"]:
        emb["timestamp"] = p["timestamp"]
    if extra.get("sentiment"):
        s = extra["sentiment"]
        emb["description"] = (f"{SENT_ICON.get(s, '🟡')} **{s}**" + (f" — {extra['take']}" if extra.get("take") else ""))[:400]
    fields = []
    if p["ticker"]:
        t = p["ticker"]
        emb["author"] = {"name": f"${t}" + (f" · {p['exchange']}" if p.get("exchange") else ""),
                         "url": f"https://www.stocktitan.net/overview/{t}/"}
        fields.append({"name": "Ticker", "value": f"[${t}](https://www.stocktitan.net/overview/{t}/)", "inline": True})
        if quote:
            fields.append({"name": "Price", "value": fmt_quote(quote), "inline": True})
        fields.append({"name": "Chart", "value": f"[Yahoo](https://finance.yahoo.com/quote/{t}) · "
                                                 f"[TradingView](https://www.tradingview.com/symbols/{t}/)", "inline": True})
    fields.append({"name": "Type", "value": p["type"], "inline": True})
    if p.get("session"):
        fields.append({"name": "Session", "value": p["session"], "inline": True})
    tags = []
    fs = extra.get("fs") or {}
    if fs.get("float"):
        tags.append(("🧊 Low float " if fs["float"] < LOW_FLOAT else "Float ") + human(fs["float"]))
    if fs.get("short_pct") is not None:
        tags.append(("🩳 High short " if fs["short_pct"] > HIGH_SHORT else "Short ") + f"{fs['short_pct']:.1f}%")
    if extra.get("social"):
        tags.append(extra["social"])
    if tags:
        fields.append({"name": "Tags", "value": " · ".join(tags)[:1024], "inline": False})
    emb["fields"] = fields[:25]
    if extra.get("chart"):
        emb["image"] = {"url": extra["chart"]}
    elif p.get("logo"):
        emb["thumbnail"] = {"url": p["logo"]}
    if extra.get("chart") and p.get("logo"):
        emb["thumbnail"] = {"url": p["logo"]}
    return {k: v for k, v in emb.items() if v is not None}, big, mv

# ---------- cross-source dedupe ----------
STOP = set("the a an of and to for in on with its at by from as is inc corp ltd llc co plc announces announce reports "
           "report company group holdings limited nasdaq nyse otc".split())

def tokens(title):
    return {w for w in re.findall(r"[a-z0-9]+", html.unescape(title or "").lower()) if w not in STOP and len(w) > 1}

def similar(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return max(inter / len(a | b), inter / min(len(a), len(b)) * 0.9)

def find_story(ticker, toks):
    for s in state.setdefault("stories", []):
        if s["tk"] == ticker and similar(toks, set(s["w"])) >= 0.6:
            return s
    return None

def remember_story(ticker, toks, src, pub_ts):
    st = state.setdefault("stories", [])
    st.append({"tk": ticker, "w": sorted(toks)[:40], "src": src, "seen": time.time(), "pub": pub_ts})
    cutoff = time.time() - 12 * 3600
    st[:] = [s for s in st if s["seen"] > cutoff][-800:]

def note_latency(story, src):
    lat = state.setdefault("latency", [])
    lat.append({"tk": story["tk"], "first": story["src"], "later": src, "delta_s": round(time.time() - story["seen"], 1)})
    del lat[:-100]

# ---------- news handling (shared by StockTitan and wires) ----------
def handle_news(p, source, summary=""):
    """Dedupe across sources, filter, enrich, post. Returns True if posted."""
    toks = tokens(p["headline"])
    if p["ticker"]:
        prev = find_story(p["ticker"], toks)
        if prev:
            if prev["src"] != source:
                note_latency(prev, source)
            return False
    remember_story(p["ticker"], toks, source, p.get("timestamp"))
    if not passes_filters(p["headline"], [p["ticker"]]):
        return False
    extra = {"via": source if source != "StockTitan" else None}
    quote = None
    for name, fn in (("price", lambda: get_quote(p["ticker"])),):
        try: quote = fn()
        except Exception as ex: print(name, "error", ex, flush=True)
    try: extra["fs"] = get_float_short(p["ticker"])
    except Exception as ex: print("float error", ex, flush=True)
    try: extra["chart"] = chart_url(p["ticker"], quote)
    except Exception as ex: print("chart error", ex, flush=True)
    try:
        take, sent, _ = ai_take(p["headline"], summary)
        extra.update(take=take, sentiment=sent)
    except Exception as ex: print("sentiment error", ex, flush=True)
    try: extra["social"] = social_tag(p["ticker"])
    except Exception: pass
    emb, big, mv = build_news_embed(p, quote, extra)
    content = f"<@&{MOVER_ROLE_ID}> 🔥 ${p['ticker']} {fmt_pct(mv)}" if big and MOVER_ROLE_ID else None
    post(emb, p["kind"], content, MOVER_ROLE_ID if content else None)
    track(p["kind"], p["headline"], p["link"], p["ticker"], mv)
    return True


# ---------- SEC helpers ----------
_sec_last_req = 0.0

def sec_get(url, timeout=20):
    """All sec.gov requests go through here: compliant UA, ≤1 request/sec overall."""
    global _sec_last_req
    wait = 1.0 - (time.time() - _sec_last_req)
    if wait > 0:
        time.sleep(wait)
    _sec_last_req = time.time()
    r = requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}, timeout=timeout)
    r.raise_for_status()
    return r

SEC_TITLE_RX = re.compile(r"^(?P<form>\S+)\s+-\s+(?P<name>.*?)\s+\((?P<cik>\d{10})\)\s+\((?P<role>[^)]*)\)\s*$")
ACC_RX = re.compile(r"accession-number=(\d{10}-\d{2}-\d{6})")
_cik_tickers = None

def cik_map():
    global _cik_tickers
    if _cik_tickers is None:
        _cik_tickers = {}
        try:
            for row in sec_get("https://www.sec.gov/files/company_tickers.json").json().values():
                _cik_tickers.setdefault(int(row["cik_str"]), []).append(row["ticker"].upper())
        except Exception as ex:
            print("sec ticker map error:", ex, flush=True)
    return _cik_tickers

def parse_sec(e):
    m = SEC_TITLE_RX.match(e.get("title") or "")
    form, name, cik = (m.group("form"), m.group("name"), int(m.group("cik"))) if m else ("SEC", e.get("title", ""), None)
    summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(e.get("summary", "")))).strip()
    dt = _dt(e.get("updated_parsed"))
    return {"form": form, "company": name, "cik": cik, "link": e.get("link"), "summary": summary,
            "timestamp": dt.isoformat() if dt else None, "tickers": cik_map().get(cik, []) if cik else []}

def build_sec_embed(p):
    emb = {"title": f"🏛️ SEC {p['form']} — {p['company']}"[:256], "url": p["link"], "color": SEC_COLOR,
           "description": p["summary"][:1000], "footer": {"text": f"SEC EDGAR · CIK {p['cik']}"}}
    if p["tickers"]:
        emb["author"] = {"name": " ".join("$" + t for t in p["tickers"][:5])}
    if p["timestamp"]:
        emb["timestamp"] = p["timestamp"]
    return emb

# ---------- state ----------
STATE_SEEN_MAX, DAY_ITEMS_MAX = 8000, 400
state = {"seen": [], "seeded": [], "day": {"date": None, "items": []}, "recap_posted": None, "pending_halts": {}}
first_run = not os.path.exists(STATE)
if not first_run:
    raw = json.load(open(STATE))
    if isinstance(raw, list):  # legacy format: plain list of seen ids
        state["seen"] = raw
        state["seeded"] = ["stocktitan"] + sorted({f"sec:{x.split(':')[1]}" for x in raw if x.startswith("sec:")})
    else:
        state.update(raw)
seen = state["seen"]
seen_set = set(seen)

def save():
    state["seen"] = seen[-STATE_SEEN_MAX:]
    state["day"]["items"] = state["day"]["items"][-DAY_ITEMS_MAX:]
    state["pending_halts"] = dict(list(state["pending_halts"].items())[-300:])
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, STATE)

def mark(eid):
    seen.append(eid); seen_set.add(eid)

def needs_seed(source):
    """True if this source has never been polled -> mark items seen silently (no flood)."""
    if first_run or source not in state["seeded"]:
        return True
    return False

def done_seed(source):
    if source not in state["seeded"]:
        state["seeded"].append(source)

def today_et():
    return datetime.now(ET_TZ).date().isoformat()

def track(kind, title, link, ticker=None, move=None):
    d = today_et()
    if state["day"]["date"] != d:
        state["day"] = {"date": d, "items": []}
    state["day"]["items"].append({"k": kind, "t": title[:150], "u": link, "s": ticker,
                                  "m": round(move, 2) if move is not None else None})

# ---------- Discord ----------
def post(embed, kind="news", content=None, role_id=None):
    url = webhook_for(kind)
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if content:
        payload["content"] = content
        if role_id:
            payload["allowed_mentions"] = {"parse": [], "roles": [role_id]}
    for _ in range(5):
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2
            time.sleep(wait if wait < 100 else wait / 1000)
            continue
        if r.status_code >= 400:
            print("discord error", r.status_code, r.text[:200], flush=True)
        break
    time.sleep(1)

# ---------- sources ----------
def poll_stocktitan():
    seed = needs_seed("stocktitan")
    r = requests.get(FEED, headers={"User-Agent": UA_BROWSER}, timeout=15)
    feed = feedparser.parse(r.content)
    posted = 0
    for e in reversed(feed.entries):
        eid = e.get("id") or e.get("link")
        if not eid or eid in seen_set:
            continue
        mark(eid)
        p = parse_stocktitan(e)
        if seed:
            if p["ticker"]:
                remember_story(p["ticker"], tokens(p["headline"]), "StockTitan", p["timestamp"])
            continue
        posted += handle_news(p, "StockTitan")
    done_seed("stocktitan")
    return f"stocktitan entries={len(feed.entries)} posted={posted}" + (" (seeded)" if seed else "")

# ---------- StockTitan live.html (faster than RSS) ----------
LIVE_URL = env("STOCKTITAN_LIVE_URL", "https://www.stocktitan.net/news/live.html")
STOCKTITAN_LIVE = env("STOCKTITAN_LIVE", "1") == "1"
STOCKTITAN_WS = env("STOCKTITAN_WS", "0") == "1"
JSG_RX = re.compile(r"let jsGlobals\s*=\s*(\{.*?\});\s*\n", re.S)
_live_meta = {"ws": None}

def parse_live_item(x):
    """One element of jsGlobals.newsList (or a websocket 'news' payload) -> parsed item + summary."""
    n = x.get("news", x) if isinstance(x, dict) else {}
    sym = (n.get("symbol") or "").upper() or None
    slug = n.get("internal_url") or n.get("internalUrl")
    if not sym or not slug:
        return None, None, None
    link = f"https://www.stocktitan.net/news/{sym}/{slug}.html"
    title = html.unescape(n.get("title") or "").strip()
    key, label, color = classify(title)
    dt = None
    try:
        # 'date' is true UTC; the numeric 'timestamp' field is offset by the ET/UTC difference
        dt = datetime.fromisoformat(n["date"].replace("Z", "+00:00")) if n.get("date") else None
    except Exception:
        pass
    p = {"headline": title, "ticker": sym, "exchange": n.get("exchange"), "link": link, "kind": key, "type": label,
         "color": color, "timestamp": dt.isoformat() if dt else None, "session": session_tag(dt) if dt else None,
         "logo": f"https://static.stocktitan.net/company-logo/{sym.lower()}-lg.png"}
    st = (x.get("stock") or {}) if isinstance(x, dict) else {}
    fs = None
    try:
        if st.get("sharesFloat"):
            sp = st.get("shortPercent")
            fs = {"t": time.time(), "float": float(st["sharesFloat"]),
                  "short_pct": float(sp) * 100 if sp not in (None, "") and float(sp) > 0.0001 else None}
    except (TypeError, ValueError):
        pass
    return p, (x.get("ultra_summary") or n.get("ultra_summary") or ""), fs

def poll_stocktitan_live():
    seed = needs_seed("stocktitan_live")
    r = requests.get(LIVE_URL, headers={"User-Agent": UA_BROWSER, "Accept-Encoding": "gzip, deflate"}, timeout=10)
    r.raise_for_status()
    m = JSG_RX.search(r.text)
    if not m:
        raise ValueError("jsGlobals not found (page layout changed?)")
    g = json.loads(m.group(1))
    _live_meta["ws"] = g.get("ws")
    items = g.get("newsList") or []
    posted = 0
    for x in reversed(items):
        p, summary, fs = parse_live_item(x)
        if not p or p["link"] in seen_set:
            continue
        mark(p["link"])  # same URL as the RSS <guid>/<link> -> shared dedupe
        if seed:
            remember_story(p["ticker"], tokens(p["headline"]), "StockTitan", p["timestamp"]); continue
        if fs and fs.get("float"):
            state.setdefault("fs", {})[p["ticker"]] = fs  # StockTitan already ships float/short: skip Yahoo call
        posted += handle_news(p, "StockTitan", summary)
    done_seed("stocktitan_live")
    return f"live n={len(items)} posted={posted}" + (" (seeded)" if seed else "")

# ---------- StockTitan websocket (experimental, default off) ----------
import queue, threading
_ws_q = queue.Queue(maxsize=500)
_ws_status = {"state": "off", "msgs": 0, "news": 0, "err": None}

def _ws_thread():
    try:
        import websocket  # websocket-client
    except ImportError:
        _ws_status.update(state="no websocket-client"); return
    delay = 5
    while True:
        url = _live_meta.get("ws") or env("STOCKTITAN_WS_URL")
        if not url:
            time.sleep(5); continue
        try:
            _ws_status["state"] = "connecting"
            ws = websocket.create_connection(url, timeout=15, origin="https://www.stocktitan.net",
                                             header=[f"User-Agent: {UA_BROWSER}"])
            _ws_status["state"] = "open"; delay = 5
            ws.settimeout(90)
            while True:
                raw = ws.recv()
                _ws_status["msgs"] += 1
                d = json.loads(raw)
                h = d.get("header") or {}
                if h.get("type") == "ping":
                    ws.send(json.dumps({"type": "pong"}))
                elif h.get("type") == "news":
                    _ws_status["news"] += 1
                    pl = d.get("payload")
                    for it in (pl if isinstance(pl, list) else [pl]):
                        try: _ws_q.put_nowait(it)
                        except queue.Full: pass
        except Exception as ex:
            _ws_status.update(state="reconnecting", err=str(ex)[:100])
            time.sleep(delay); delay = min(delay * 2, 300)

def start_ws():
    if STOCKTITAN_WS and _ws_status["state"] == "off":
        _ws_status["state"] = "starting"
        threading.Thread(target=_ws_thread, daemon=True).start()

def drain_ws():
    posted = n = 0
    while not _ws_q.empty():
        x = _ws_q.get_nowait(); n += 1
        try:
            p, summary, fs = parse_live_item(x)
        except Exception:
            continue
        if not p or p["link"] in seen_set:
            continue
        mark(p["link"])
        if needs_seed("stocktitan_live"):
            continue
        if fs and fs.get("float"):
            state.setdefault("fs", {})[p["ticker"]] = fs
        posted += handle_news(p, "StockTitan")
    return f"ws {_ws_status['state']} q={n} posted={posted}" if (n or _ws_status["state"] != "open") else None

# ---------- newsfilter.io (optional, needs NEWSFILTER_API_KEY) ----------
NEWSFILTER_API_KEY = env("NEWSFILTER_API_KEY")
NEWSFILTER_POLL = int(env("NEWSFILTER_POLL", "60"))  # free Starter = 100 calls/day and 1h-delayed

def poll_newsfilter():
    seed = needs_seed("newsfilter")
    r = requests.post("https://api.newsfilter.io/search", headers={"Authorization": NEWSFILTER_API_KEY},
                      json={"queryString": "symbols:* AND publishedAt:[now-1h TO *]", "from": 0, "size": 50},
                      timeout=10)
    r.raise_for_status()
    arts = r.json().get("articles") or []
    posted = 0
    for a in reversed(arts):
        eid = "nf:" + str(a.get("id") or a.get("url"))
        syms = [s.upper() for s in a.get("symbols") or []]
        if eid in seen_set or not syms:
            continue
        mark(eid)
        title = html.unescape(a.get("title") or "")
        key, label, color = classify(title)
        try: dt = datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00"))
        except Exception: dt = None
        p = {"headline": title, "ticker": syms[0], "exchange": None, "link": a.get("url"), "kind": key, "type": label,
             "color": color, "timestamp": dt.isoformat() if dt else None, "session": session_tag(dt) if dt else None, "logo": None}
        if seed:
            remember_story(p["ticker"], tokens(title), "newsfilter", p["timestamp"]); continue
        src = ((a.get("source") or {}).get("name") or "newsfilter.io")
        posted += handle_news(p, src, a.get("description") or "")
    done_seed("newsfilter")
    return f"newsfilter n={len(arts)} posted={posted}" + (" (seeded)" if seed else "")

# ---------- direct wires ----------
EXCH_RX = re.compile(r"\(?\b((?i:NASDAQ(?:\s+(?:GS|GM|CM|Global Select|Global Market|Capital Market))?|NYSE(?:\s+American|\s+Arca|\s+MKT)?|"
                     r"AMEX|OTCQB|OTCQX|OTC\s?Pink|OTC\s?Markets|OTC|Cboe(?:\s+BZX)?))\s*:\s*\"?([A-Z][A-Z0-9]{0,4}(?:[.\-][A-Z])?)\b")

def extract_exchange_ticker(text):
    m = EXCH_RX.search(html.unescape(text or ""))
    if not m:
        return None, None
    ex = re.sub(r"\s+", " ", m.group(1)).upper().replace("OTC MARKETS", "OTC")
    return m.group(2).upper(), ex

def parse_wire(e, wire):
    title = html.unescape(re.sub(r"<[^>]+>", "", e.get("title") or "")).strip()
    summary = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", e.get("summary") or ""))).strip()
    ticker, ex = extract_exchange_ticker(f"{title} {summary}")
    key, label, color = classify(title)
    dt = _dt(e.get("published_parsed"))
    return {"headline": title, "ticker": ticker, "exchange": ex, "link": e.get("link"), "kind": key, "type": label,
            "color": color, "timestamp": dt.isoformat() if dt else None,
            "session": session_tag(dt) if dt else None, "logo": None}, summary

def poll_wires():
    out = []
    for wire, (toggle, url) in WIRE_FEEDS.items():
        if not url or env(toggle, "1") != "1":
            continue
        src = f"wire:{wire}"
        try:
            r = requests.get(url, headers={"User-Agent": UA_BROWSER}, timeout=8)
            if r.status_code != 200:
                out.append(f"{wire} HTTP {r.status_code}"); continue
            feed = feedparser.parse(r.content)
            seed = needs_seed(src)
            posted = 0
            for e in reversed(feed.entries):
                link = e.get("link") or ""
                eid = "w:" + (e.get("id") or link)
                if eid in seen_set:
                    continue
                mark(eid)
                if wire == "Business Wire" and "/en/" not in link:  # skip translated duplicates
                    continue
                p, summary = parse_wire(e, wire)
                if not p["ticker"]:
                    continue  # post only items with an exchange ticker
                if seed:
                    remember_story(p["ticker"], tokens(p["headline"]), wire, p["timestamp"]); continue
                posted += handle_news(p, wire, summary)
            done_seed(src)
            out.append(f"{wire} n={len(feed.entries)} posted={posted}" + (" (seeded)" if seed else ""))
        except Exception as ex:
            out.append(f"{wire} error: {str(ex)[:80]}")
    return " ".join(out) or None

_form_last = {}

def fetch_form_feed(form, count=40):
    if time.time() - _form_last.get(form, 0) < SEC_MIN_INTERVAL:
        return None
    _form_last[form] = time.time()
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&count={count}&output=atom"
    return feedparser.parse(sec_get(url).content)

def poll_sec(form):
    src = f"sec:{form}"
    feed = fetch_form_feed(form)
    if feed is None:
        return None
    seed = needs_seed(src)
    posted = 0
    for e in reversed(feed.entries):
        eid = f"sec:{form}:{e.get('id') or e.get('link')}"
        if eid in seen_set:
            continue
        mark(eid)
        if seed:
            continue
        p = parse_sec(e)
        if passes_filters(f"{p['form']} {p['company']} {p['summary']}", p["tickers"]):
            post(build_sec_embed(p), "sec"); posted += 1
            track("sec", f"{p['form']} {p['company']}", p["link"], (p["tickers"] or [None])[0])
    done_seed(src)
    return f"sec {form} entries={len(feed.entries)} posted={posted}" + (" (seeded)" if seed else "")

# insider buys (Form 4)
_insider_backlog = []  # accession tuples waiting for XML fetch (bounded per cycle)

def _x(node, path):
    el = node.find(path) if node is not None else None
    return (el.text or "").strip() if el is not None and el.text else ""

def parse_form4(xml_text, link):
    root = ET.fromstring(xml_text)
    sym = _x(root, "issuer/issuerTradingSymbol").upper()
    issuer = _x(root, "issuer/issuerName")
    owners = []
    for ro in root.findall("reportingOwner"):
        name = _x(ro, "reportingOwnerId/rptOwnerName")
        rel = ro.find("reportingOwnerRelationship")
        roles = []
        if _x(rel, "isDirector") in ("1", "true"): roles.append("Director")
        if _x(rel, "isOfficer") in ("1", "true"): roles.append(_x(rel, "officerTitle") or "Officer")
        if _x(rel, "isTenPercentOwner") in ("1", "true"): roles.append("10% owner")
        if _x(rel, "isOther") in ("1", "true"): roles.append(_x(rel, "otherText") or "Other")
        owners.append((name, ", ".join(roles)))
    buys = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _x(tx, "transactionCoding/transactionCode") != "P":
            continue
        try:
            sh = float(_x(tx, "transactionAmounts/transactionShares/value") or 0)
            px = float(_x(tx, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        buys.append({"shares": sh, "price": px, "value": sh * px, "date": _x(tx, "transactionDate/value"),
                     "after": _x(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")})
    if not buys:
        return None
    tot_sh = sum(b["shares"] for b in buys); tot_v = sum(b["value"] for b in buys)
    return {"ticker": sym, "issuer": issuer, "owners": owners, "shares": tot_sh, "value": tot_v,
            "avg": tot_v / tot_sh if tot_sh else 0, "date": buys[-1]["date"], "after": buys[-1]["after"],
            "n": len(buys), "link": link}

def build_insider_embed(b):
    who = "; ".join(f"{n} ({r})" if r else n for n, r in b["owners"]) or "Insider"
    emb = {"title": f"🟢 Insider buy: ${b['ticker'] or '?'} — {b['issuer']}"[:256], "url": b["link"],
           "color": INSIDER_COLOR,
           "author": {"name": f"${b['ticker']}", "url": f"https://finance.yahoo.com/quote/{b['ticker']}"} if b["ticker"] else None,
           "fields": [{"name": "Insider", "value": who[:1024], "inline": False},
                      {"name": "Shares", "value": f"{b['shares']:,.0f}", "inline": True},
                      {"name": "Avg price", "value": f"${b['avg']:,.2f}", "inline": True},
                      {"name": "Value", "value": f"${b['value']:,.0f}", "inline": True},
                      {"name": "Trade date", "value": b["date"] or "?", "inline": True},
                      {"name": "Owned after", "value": f"{float(b['after']):,.0f}" if b["after"] else "?", "inline": True}],
           "footer": {"text": f"SEC Form 4 · open-market purchase (P){' · %d txns' % b['n'] if b['n'] > 1 else ''}"},
           "timestamp": datetime.now(timezone.utc).isoformat()}
    return {k: v for k, v in emb.items() if v is not None}

def poll_insiders():
    src = "insider"
    msg = []
    feed = fetch_form_feed("4", count=100)
    if feed is not None:
        seed = needs_seed(src)
        new = 0
        for e in reversed(feed.entries):
            m = ACC_RX.search(e.get("id", ""))
            lm = re.search(r"/edgar/data/(\d+)/", e.get("link", ""))
            if not m or not lm:
                continue
            eid = f"f4:{m.group(1)}"  # each filing appears twice (Reporting + Issuer)
            if eid in seen_set or any(a[0] == eid for a in _insider_backlog):
                continue
            if seed:
                mark(eid); continue
            _insider_backlog.append((eid, lm.group(1), m.group(1), e.get("link")))
            new += 1
        done_seed(src)
        msg.append(f"form4 entries={len(feed.entries)} new={new}" + (" (seeded)" if seed else ""))
    fetched = posted = 0
    while _insider_backlog and fetched < INSIDER_MAX_FETCH:
        eid, cik, acc, link = _insider_backlog.pop(0)
        fetched += 1
        try:
            txt = sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{acc}.txt").text
            mark(eid)
            mx = re.search(r"<XML>\s*(.*?)\s*</XML>", txt, re.S)
            if not mx:
                continue
            b = parse_form4(mx.group(1).strip(), link)
            if not b or b["value"] < INSIDER_MIN_USD:
                continue
            if not passes_filters("", [b["ticker"]], use_keywords=False):
                continue
            post(build_insider_embed(b), "insider"); posted += 1
            track("insider", f"${b['ticker']} insider buy ${b['value']:,.0f}", link, b["ticker"])
        except Exception as ex:
            print("form4 fetch error", acc, ex, flush=True)
            mark(eid)  # don't retry forever
    if len(_insider_backlog) > 300:  # bound memory if SEC flow outpaces fetch cap
        for a in _insider_backlog[:-300]:
            mark(a[0])
        del _insider_backlog[:-300]
    if fetched or msg:
        msg.append(f"form4 fetched={fetched} posted={posted} backlog={len(_insider_backlog)}")
    return " ".join(msg) or None

# trading halts
HALT_CODES = {
    "T1": "News pending", "T2": "News released", "T3": "News & resumption times",
    "T5": "Single-stock trading pause in effect", "T6": "Extraordinary market activity",
    "T7": "Quotation-only period (T5 pause)", "T8": "ETF halt", "T12": "Additional info requested by Nasdaq",
    "H4": "Non-compliance", "H9": "Not current in required filings", "H10": "SEC trading suspension",
    "H11": "Regulatory concern", "O1": "Operations halt", "IPO1": "IPO not yet trading",
    "M1": "Corporate action", "M2": "Quotation not available", "LUDP": "Volatility trading pause (LULD)",
    "LUDS": "Volatility pause – straddle", "MWC1": "Market-wide circuit breaker L1",
    "MWC2": "Market-wide circuit breaker L2", "MWC3": "Market-wide circuit breaker L3",
    "MWC0": "Market-wide circuit breaker carry-over", "M": "Volatility trading pause",
    "D": "Security deletion", "R1": "New issue available", "R4": "Qualifications issues resolved; resumption",
    "C3": "Issuer news not forthcoming; resumption", "C4": "Qualifications halt ended; resumption",
    "C9": "Qualifications halt concluded; resumption", "C11": "Trade halt concluded by other authority",
}

def build_halt_embed(h, resumed):
    code = h["code"]
    emb = {"title": f"{'✅ Resumed' if resumed else '⛔ Halted'}: ${h['sym']} — {h['name']}"[:256],
           "url": f"https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts",
           "color": RESUME_GREEN if resumed else HALT_RED,
           "fields": [{"name": "Reason", "value": f"`{code}` {HALT_CODES.get(code, 'See Nasdaq halt codes')}", "inline": False},
                      {"name": "Halt time (ET)", "value": f"{h['hdate']} {h['htime']}", "inline": True},
                      {"name": "Resumption (ET)", "value": (f"{h['rdate']} {h['rtime']}".strip()
                                                            + (f" (quote {h['qtime']})" if h['qtime'] else "")) or "TBD", "inline": True},
                      {"name": "Market", "value": h["market"] or "?", "inline": True}],
           "author": {"name": f"${h['sym']}", "url": f"https://finance.yahoo.com/quote/{h['sym']}"},
           "footer": {"text": "Nasdaq Trader halts"}, "timestamp": datetime.now(timezone.utc).isoformat()}
    return emb

def poll_halts():
    src = "halts"
    r = requests.get(HALTS_URL, headers={"User-Agent": UA_BROWSER}, timeout=20)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    seed = needs_seed(src)
    posted = 0
    pend = state["pending_halts"]
    for e in reversed(feed.entries):
        h = {"sym": (e.get("ndaq_issuesymbol") or e.get("title") or "").strip().upper(),
             "name": (e.get("ndaq_issuename") or "").strip(), "market": (e.get("ndaq_market") or "").strip(),
             "code": (e.get("ndaq_reasoncode") or "").strip(), "hdate": (e.get("ndaq_haltdate") or "").strip(),
             "htime": (e.get("ndaq_halttime") or "").strip(), "rdate": (e.get("ndaq_resumptiondate") or "").strip(),
             "rtime": (e.get("ndaq_resumptiontradetime") or "").strip(),
             "qtime": (e.get("ndaq_resumptionquotetime") or "").strip()}
        if not h["sym"]:
            continue
        key = f"halt:{h['sym']}:{h['hdate']}:{h['htime']}"
        resumed = bool(h["rtime"])
        if key in seen_set and not (resumed and key in pend):
            continue
        if key not in seen_set:
            mark(key)
        if seed:
            continue
        if not passes_filters("", [h["sym"]], use_keywords=False):
            pend.pop(key, None); continue
        if resumed:
            pend.pop(key, None)
        else:
            pend[key] = 1  # watch for its resumption
        post(build_halt_embed(h, resumed), "halt"); posted += 1
        track("halt", f"${h['sym']} {'resumed' if resumed else 'halted'} ({h['code']})", None, h["sym"])
    done_seed(src)
    return f"halts entries={len(feed.entries)} posted={posted}" + (" (seeded)" if seed else "")

# daily recap
def maybe_recap(force=False):
    now = datetime.now(ET_TZ)
    d = now.date().isoformat()
    if not force and (now.weekday() >= 5 or (now.hour, now.minute) < (16, 15)):
        return None
    if state.get("recap_posted") == d or state["day"]["date"] != d or not state["day"]["items"]:
        return None
    emb = build_recap_embed(state["day"]["items"], d)
    post(emb, "recap")
    state["recap_posted"] = d
    return f"recap posted ({len(state['day']['items'])} items)"

def build_recap_embed(items, d):
    order = ["earnings", "fda", "offering", "ma", "analyst", "news", "sec", "insider", "halt", "volume", "social"]
    fields = []
    for k in order:
        grp = [i for i in items if i["k"] == k]
        if not grp:
            continue
        lines = []
        for i in grp[-5:][::-1]:
            t = (f"${i['s']} " if i.get("s") and k in ("earnings", "fda", "offering", "ma", "analyst", "news") else "") + i["t"]
            t = t[:90].replace("[", "(").replace("]", ")")
            lines.append(f"• [{t}]({i['u']})" if i.get("u") else f"• {t}")
        val = ""
        for ln in lines:
            if len(val) + len(ln) + 1 > 1000: break
            val += ln + "\n"
        fields.append({"name": f"{LABELS.get(k, k)} ({len(grp)})", "value": val or "—", "inline": False})
    movers = sorted([i for i in items if i.get("m") is not None], key=lambda i: -abs(i["m"]))
    seen_t, mv = set(), []
    for i in movers:
        if i["s"] in seen_t: continue
        seen_t.add(i["s"]); mv.append(f"${i['s']} {fmt_pct(i['m'])}")
        if len(mv) == 8: break
    if mv:
        fields.append({"name": "🔥 Biggest movers", "value": " · ".join(mv)[:1024], "inline": False})
    emb = {"title": f"📊 Daily recap — {d}", "color": 0xF1C40F,
           "description": f"{len(items)} items posted today.", "fields": fields[:25],
           "footer": {"text": "StockTitan news bot"}, "timestamp": datetime.now(timezone.utc).isoformat()}
    while len(json.dumps(emb)) > 5800 and emb["fields"]:  # Discord 6000-char embed limit
        emb["fields"].pop(-2 if len(emb["fields"]) > 1 else -1)
    return emb

# ---------- earnings calendar (Nasdaq) ----------
def _mcap(s):
    try: return float(re.sub(r"[^0-9.]", "", s or "") or 0)
    except ValueError: return 0

def maybe_earnings_cal(force=False):
    now = datetime.now(ET_TZ)
    d = now.date().isoformat()
    if not force and (now.weekday() >= 5 or not ((8, 0) <= (now.hour, now.minute) < (11, 0))):
        return None
    if state.get("ecal_posted") == d:
        return None
    r = requests.get("https://api.nasdaq.com/api/calendar/earnings", params={"date": d},
                     headers={"User-Agent": UA_BROWSER, "Accept": "application/json"}, timeout=15)
    r.raise_for_status()
    rows = ((r.json().get("data") or {}).get("rows")) or []
    if TICKERS:
        rows = [x for x in rows if x.get("symbol", "").upper() in TICKERS]
    rows.sort(key=lambda x: -_mcap(x.get("marketCap")))
    groups = {"🌅 Before open": [], "🌆 After close": [], "❔ Time not supplied": []}
    for x in rows[:20]:
        g = {"time-pre-market": "🌅 Before open", "time-after-hours": "🌆 After close"}.get(x.get("time"), "❔ Time not supplied")
        eps = x.get("epsForecast") or "n/a"
        groups[g].append(f"[${x['symbol']}](https://finance.yahoo.com/quote/{x['symbol']}) {x.get('name', '')[:28]} · "
                         f"EPS est {eps} · {human(_mcap(x.get('marketCap')))}")
    fields = [{"name": f"{k} ({len(v)})", "value": "\n".join(v)[:1024], "inline": False} for k, v in groups.items() if v]
    emb = {"title": f"📅 Earnings today — {d}", "color": 0x2ECC71,
           "description": f"{len(rows)} companies report today; top {min(20, len(rows))} by market cap." if rows else "No earnings found for today.",
           "fields": fields, "url": f"https://www.nasdaq.com/market-activity/earnings",
           "footer": {"text": "Nasdaq earnings calendar"}, "timestamp": datetime.now(timezone.utc).isoformat()}
    post(emb, "earnings_cal")
    state["ecal_posted"] = d
    return f"earnings calendar posted ({len(rows)} rows)"

# ---------- unusual volume (Yahoo predefined screeners) ----------
# Cumulative share of a regular session's volume by minutes since 09:30 (rough U-shape).
VOL_CURVE = [(0, 0), (30, .14), (60, .22), (120, .34), (180, .44), (240, .53), (300, .63), (360, .80), (390, 1.0)]

def expected_frac(now_et):
    m = (now_et.hour * 60 + now_et.minute) - 570
    if m >= 390: return 1.0
    if m <= 0: return None
    for (m0, f0), (m1, f1) in zip(VOL_CURVE, VOL_CURVE[1:]):
        if m <= m1:
            return max(0.03, f0 + (f1 - f0) * (m - m0) / (m1 - m0))
    return 1.0

def poll_volume():
    now = datetime.now(ET_TZ)
    sess = session_tag(datetime.now(timezone.utc))
    if "Regular" not in sess and "After-hours" not in sess:
        return None  # regular-session volume is meaningless before the open
    frac = expected_frac(now) or 1.0
    d = now.date().isoformat()
    vs = state.setdefault("vol", {"date": d, "alerted": []})
    if vs["date"] != d:
        vs.update(date=d, alerted=[])
    seed = needs_seed("volume")
    quotes = {}
    for scr in ("most_actives", "day_gainers", "day_losers", "small_cap_gainers"):
        try:
            dj = yahoo_json("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                            {"scrIds": scr, "count": 100})
            for q in dj["finance"]["result"][0]["quotes"]:
                quotes[q["symbol"]] = q
        except Exception as ex:
            print("screener", scr, "error", str(ex)[:80], flush=True)
    posted = 0
    for sym, q in quotes.items():
        vol, avg = q.get("regularMarketVolume") or 0, q.get("averageDailyVolume10Day") or q.get("averageDailyVolume3Month") or 0
        price = q.get("regularMarketPrice") or 0
        if not avg or price < VOL_MIN_PRICE or vol < VOL_MIN_SHARES or sym in vs["alerted"]:
            continue
        ratio = vol / (avg * frac)
        if ratio < VOL_MULT:
            continue
        vs["alerted"].append(sym)
        if seed or not passes_filters("", [sym], use_keywords=False):
            continue
        pct = q.get("regularMarketChangePercent")
        emb = {"title": f"📊 Unusual volume: ${sym} — {q.get('shortName', '')}"[:256],
               "url": f"https://finance.yahoo.com/quote/{sym}", "color": 0xF39C12,
               "fields": [{"name": "Price", "value": f"${price:,.2f} {fmt_pct(pct)}", "inline": True},
                          {"name": "Volume", "value": f"{human(vol)} ({ratio:.1f}× normal for time of day)", "inline": True},
                          {"name": "10-day avg vol", "value": human(avg), "inline": True}],
               "footer": {"text": f"Yahoo screener · {sess}"}, "timestamp": datetime.now(timezone.utc).isoformat()}
        post(emb, "volume"); posted += 1
        track("volume", f"${sym} {ratio:.1f}× volume", f"https://finance.yahoo.com/quote/{sym}", sym, pct)
    vs["alerted"] = vs["alerted"][-500:]
    done_seed("volume")
    return f"volume universe={len(quotes)} posted={posted}" + (" (seeded)" if seed else "")

# ---------- social buzz (ApeWisdom) ----------
def poll_social():
    r = requests.get("https://apewisdom.io/api/v1.0/filter/all-stocks", headers={"User-Agent": UA_BROWSER}, timeout=10)
    r.raise_for_status()
    res = r.json().get("results") or []
    _social["top"] = {x["ticker"].upper(): x["rank"] for x in res if x.get("rank", 999) <= 20}
    _social["t"] = time.time()
    d = today_et()
    ss = state.setdefault("social", {"date": d, "alerted": []})
    if ss["date"] != d:
        ss.update(date=d, alerted=[])
    seed = needs_seed("social")
    posted = 0
    for x in res:
        sym = (x.get("ticker") or "").upper()
        try:
            rank, r24 = int(x["rank"]), int(x.get("rank_24h_ago") or 9999)
            m, m24 = int(x.get("mentions") or 0), int(x.get("mentions_24h_ago") or 0)
        except (KeyError, ValueError, TypeError):
            continue
        if not (r24 - rank >= 20 and m >= max(2 * m24, SOCIAL_MIN_MENTIONS)) or sym in ss["alerted"]:
            continue
        ss["alerted"].append(sym)
        if seed or not passes_filters("", [sym], use_keywords=False):
            continue
        emb = {"title": f"🗣️ Social buzz: ${sym} — {html.unescape(x.get('name', ''))}"[:256],
               "url": f"https://apewisdom.io/stocks/{sym}/", "color": 0xFF4500,
               "fields": [{"name": "Rank", "value": f"#{rank} (was #{r24 if r24 < 9999 else '—'} 24h ago)", "inline": True},
                          {"name": "Mentions (24h)", "value": f"{m} vs {m24}", "inline": True},
                          {"name": "Upvotes", "value": str(x.get("upvotes", "?")), "inline": True}],
               "footer": {"text": "ApeWisdom · Reddit mentions"}, "timestamp": datetime.now(timezone.utc).isoformat()}
        post(emb, "social"); posted += 1
        track("social", f"${sym} buzz #{rank} ({m} mentions)", f"https://apewisdom.io/stocks/{sym}/", sym)
    ss["alerted"] = ss["alerted"][-300:]
    done_seed("social")
    return f"social n={len(res)} top20={len(_social['top'])} posted={posted}" + (" (seeded)" if seed else "")

# ---------- loop / scheduler ----------
_last_run = {}

def due(name, every):
    if time.time() - _last_run.get(name, 0) >= every:
        _last_run[name] = time.time()
        return True
    return False

def poll_once():
    global first_run
    jobs = []
    if SOCIAL and due("social", 600): jobs.append(("social", poll_social))  # first so top-20 tags are ready
    if STOCKTITAN_WS: start_ws(); jobs.append(("ws", drain_ws))  # fastest: pushed items
    if STOCKTITAN_LIVE: jobs.append(("stocktitan_live", poll_stocktitan_live))
    jobs.append(("stocktitan", poll_stocktitan))
    if WIRES: jobs.append(("wires", poll_wires))
    if NEWSFILTER_API_KEY and due("newsfilter", NEWSFILTER_POLL): jobs.append(("newsfilter", poll_newsfilter))
    jobs += [(f"sec {f}", (lambda f=f: poll_sec(f))) for f in SEC_FORMS]  # self-throttled 60 s/form
    if INSIDER_BUYS and due("insider", 30): jobs.append(("insider", poll_insiders))
    if HALTS and due("halts", 20): jobs.append(("halts", poll_halts))
    if UNUSUAL_VOLUME and due("volume", 120): jobs.append(("volume", poll_volume))
    if RECAP and due("recap", 60): jobs.append(("recap", maybe_recap))
    if EARNINGS_CAL and due("ecal", 60): jobs.append(("earnings_cal", maybe_earnings_cal))
    msg = []
    for name, fn in jobs:
        try:
            res = fn()
            if res: msg.append(res)
        except Exception as ex:
            msg.append(f"{name} error: {str(ex)[:120]}")
    try:
        save()
    except Exception as ex:
        print("save error:", ex, flush=True)
    first_run = False
    print(time.strftime("%H:%M:%S"), " | ".join(msg), flush=True)

def main():
    if not WEBHOOK:
        print("DISCORD_WEBHOOK_URL not set; skipping.")
        sys.exit(0)
    end = time.time() + RUN_MINUTES * 60
    while True:
        t0 = time.time()
        try:
            poll_once()
        except Exception as ex:
            print("error:", ex, flush=True)
        if time.time() + POLL >= end:
            break
        time.sleep(max(0.5, POLL - (time.time() - t0)))

if __name__ == "__main__":
    main()
