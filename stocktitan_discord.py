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
POLL = int(env("POLL_SECONDS", "30"))
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
INSIDER_MAX_FETCH = int(env("INSIDER_MAX_FETCH", "8"))  # XML fetches per cycle
HALTS = env("HALTS", "1") == "1"
HALTS_URL = env("HALTS_URL", "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts")
RECAP = env("RECAP", "1") == "1"

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
LABELS = {k: l for k, l, _, _ in TYPES} | {"news": "📰 News", "sec": "🏛️ SEC", "insider": "🟢 Insider buy", "halt": "⛔ Halt"}

def classify(title):
    for key, label, color, rx in TYPES:
        if rx.search(title or ""):
            return key, label, color
    return DEFAULT_TYPE

# ---------- routing ----------
ROUTES = {"earnings": "WEBHOOK_EARNINGS", "fda": "WEBHOOK_FDA", "offering": "WEBHOOK_OFFERINGS",
          "ma": "WEBHOOK_MA", "analyst": "WEBHOOK_ANALYST", "sec": "WEBHOOK_SEC",
          "insider": "WEBHOOK_SEC", "halt": "WEBHOOK_HALTS", "recap": "WEBHOOK_RECAP"}

def webhook_for(kind):
    return env(ROUTES.get(kind, "")) if kind in ROUTES and env(ROUTES[kind]) else WEBHOOK

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
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}",
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
        q = {"price": price, "pct": pct, "ext_price": None, "ext_pct": None, "ext_label": None}
        # extended-hours: last bar outside the regular period
        ts = res.get("timestamp") or []
        closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
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

def build_news_embed(p, quote=None):
    mv = move_pct(quote)
    big = mv is not None and abs(mv) >= MOVER_PCT
    title = (("🔥 " if big else "") + (p["headline"] or "News"))[:256]
    emb = {"title": title, "url": p["link"] or None, "color": int(p["color"]),
           "footer": {"text": " · ".join(x for x in (p["type"], p.get("session"), "StockTitan") if x)}}
    if p["timestamp"]:
        emb["timestamp"] = p["timestamp"]
    fields = []
    if p["ticker"]:
        t = p["ticker"]
        emb["author"] = {"name": f"${t}", "url": f"https://www.stocktitan.net/overview/{t}/"}
        fields.append({"name": "Ticker", "value": f"[${t}](https://www.stocktitan.net/overview/{t}/)", "inline": True})
        if quote:
            fields.append({"name": "Price", "value": fmt_quote(quote), "inline": True})
        fields.append({"name": "Chart", "value": f"[Yahoo](https://finance.yahoo.com/quote/{t}) · "
                                                 f"[TradingView](https://www.tradingview.com/symbols/{t}/)", "inline": True})
    fields.append({"name": "Type", "value": p["type"], "inline": True})
    if p.get("session"):
        fields.append({"name": "Session", "value": p["session"], "inline": True})
    emb["fields"] = fields
    if p["logo"]:
        emb["thumbnail"] = {"url": p["logo"]}
    return {k: v for k, v in emb.items() if v is not None}, big, mv

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
STATE_SEEN_MAX, DAY_ITEMS_MAX = 6000, 400
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
    r = requests.get(FEED, headers={"User-Agent": UA_BROWSER}, timeout=20)
    feed = feedparser.parse(r.content)
    posted = 0
    for e in reversed(feed.entries):
        eid = e.get("id") or e.get("link")
        if not eid or eid in seen_set:
            continue
        mark(eid)
        if seed:
            continue
        p = parse_stocktitan(e)
        if not passes_filters(p["headline"], [p["ticker"]]):
            continue
        quote = get_quote(p["ticker"])
        emb, big, mv = build_news_embed(p, quote)
        content = f"<@&{MOVER_ROLE_ID}> 🔥 ${p['ticker']} {fmt_pct(mv)}" if big and MOVER_ROLE_ID else None
        post(emb, p["kind"], content, MOVER_ROLE_ID if content else None)
        track(p["kind"], p["headline"], p["link"], p["ticker"], mv)
        posted += 1
    done_seed("stocktitan")
    return f"stocktitan entries={len(feed.entries)} posted={posted}" + (" (seeded)" if seed else "")

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
    order = ["earnings", "fda", "offering", "ma", "analyst", "news", "sec", "insider", "halt"]
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

# ---------- loop ----------
def poll_once():
    global first_run
    jobs = [("stocktitan", poll_stocktitan)]
    jobs += [(f"sec {f}", (lambda f=f: poll_sec(f))) for f in SEC_FORMS]
    if INSIDER_BUYS: jobs.append(("insider", poll_insiders))
    if HALTS: jobs.append(("halts", poll_halts))
    if RECAP: jobs.append(("recap", maybe_recap))
    msg = []
    for name, fn in jobs:
        try:
            res = fn()
            if res: msg.append(res)
        except Exception as ex:
            msg.append(f"{name} error: {ex}")
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
        try:
            poll_once()
        except Exception as ex:
            print("error:", ex, flush=True)
        if time.time() + POLL >= end:
            break
        time.sleep(POLL)

if __name__ == "__main__":
    main()
