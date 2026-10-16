"""StockTitan (+ optional SEC EDGAR) news -> Discord webhook bot."""
import calendar, html, json, os, re, sys, time
from datetime import datetime, timezone
import feedparser, requests

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
FEED = os.environ.get("STOCKTITAN_FEED_URL", "https://www.stocktitan.net/rss").strip()
STATE = os.environ.get("STATE_FILE", "state/seen.json")
POLL = int(os.environ.get("POLL_SECONDS", "30"))
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "0"))  # 0 = single pass
UA_BROWSER = "Mozilla/5.0 (compatible; stocktitan-discord-news)"
SEC_UA = os.environ.get("SEC_USER_AGENT", "hedgedvol-news-bot hedgedvol@gmail.com")
SEC_MIN_INTERVAL = 60  # seconds between polls of the same form


def _list(name, upper=False):
    items = [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]
    return [x.upper() if upper else x.lower() for x in items]

TICKERS = set(_list("TICKERS", upper=True))
KEYWORDS = _list("KEYWORDS")
EXCLUDE = _list("EXCLUDE")
SEC_FORMS = _list("SEC_FORMS", upper=True)

# ---------- classification ----------
TYPES = [  # (label, color, regex) – first match wins
    ("🔴 Offering", 0xE74C3C, r"\b(offering|dilution|dilutive|priced|pricing of|registered direct|warrants?|private placement|at-the-market|ATM program)\b"),
    ("📈 Earnings", 0x2ECC71, r"\b(earnings|results|EPS|revenue[s]? of|quarterly report|fiscal (year|quarter))\b"),
    ("💊 FDA / Clinical", 0x9B59B6, r"\b(FDA|EMA|(regulatory|marketing|drug|clearance and) approval|clinical|trials?|phase [1-3I]+[ab]?|NDA|BLA|IND|510\(k\))\b"),
    ("🤝 M&A", 0x3498DB, r"\b(merger|acquisition|acquire[sd]?|business combination|to be acquired|takeover)\b"),
    ("🎯 Analyst", 0xE67E22, r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiates coverage)\b"),
]
DEFAULT_TYPE = ("📰 News", 0x95A5A6)
SEC_COLOR = 0x1F3A93
TYPES = [(l, c, re.compile(p, re.I)) for l, c, p in TYPES]

def classify(title):
    for label, color, rx in TYPES:
        if rx.search(title or ""):
            return label, color
    return DEFAULT_TYPE

# ---------- ticker extraction ----------
TITLE_RX = re.compile(r"^(?P<head>.*?)\s*\|\s*(?P<tick>[A-Z0-9.\-]{1,12})\s+Stock News\s*$")
LINK_RX = re.compile(r"stocktitan\.net/news/(?P<tick>[A-Za-z0-9.\-]+)/")

def parse_stocktitan(e):
    """Feed has no <category>/exchange; ticker is in the title suffix and link path."""
    raw = (e.get("title") or "").strip()
    link = e.get("link") or ""
    m = TITLE_RX.match(raw)
    headline, ticker = (m.group("head"), m.group("tick")) if m else (raw, None)
    lm = LINK_RX.search(link)
    if not ticker and lm:
        ticker = lm.group("tick").upper()
    label, color = classify(headline)
    ts = None
    if e.get("published_parsed"):
        ts = datetime.fromtimestamp(calendar.timegm(e.published_parsed), timezone.utc).isoformat()
    logo = None
    for mc in e.get("media_content", []) or []:
        if mc.get("url"):
            logo = mc["url"]; break
    return {"headline": headline, "ticker": ticker, "link": link, "type": label,
            "color": color, "timestamp": ts, "logo": logo}

def passes_filters(headline, tickers):
    h = (headline or "").lower()
    if TICKERS and not (set(t.upper() for t in tickers if t) & TICKERS):
        return False
    if KEYWORDS and not any(k in h for k in KEYWORDS):
        return False
    if EXCLUDE and any(k in h for k in EXCLUDE):
        return False
    return True

def build_news_embed(p):
    emb = {"title": p["headline"][:256] or "News", "url": p["link"] or None,
           "color": int(p["color"]), "footer": {"text": f"{p['type']} · StockTitan"}}
    if p["timestamp"]:
        emb["timestamp"] = p["timestamp"]
    if p["ticker"]:
        t = p["ticker"]
        emb["author"] = {"name": f"${t}", "url": f"https://www.stocktitan.net/overview/{t}/"}
        emb["fields"] = [
            {"name": "Ticker", "value": f"[${t}](https://www.stocktitan.net/overview/{t}/)", "inline": True},
            {"name": "Chart", "value": f"[Yahoo](https://finance.yahoo.com/quote/{t}) · "
                                       f"[TradingView](https://www.tradingview.com/symbols/{t}/)", "inline": True},
            {"name": "Type", "value": p["type"], "inline": True},
        ]
    if p["logo"]:
        emb["thumbnail"] = {"url": p["logo"]}
    return {k: v for k, v in emb.items() if v is not None}

# ---------- SEC ----------
SEC_TITLE_RX = re.compile(r"^(?P<form>\S+)\s+-\s+(?P<name>.*?)\s+\((?P<cik>\d{10})\)\s+\((?P<role>[^)]*)\)\s*$")
_cik_tickers = None  # cik(int) -> [tickers]

def cik_map():
    """SEC's official CIK->ticker map, used to filter filings by TICKERS."""
    global _cik_tickers
    if _cik_tickers is None:
        _cik_tickers = {}
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers.json",
                             headers={"User-Agent": SEC_UA}, timeout=20)
            r.raise_for_status()
            for row in r.json().values():
                _cik_tickers.setdefault(int(row["cik_str"]), []).append(row["ticker"].upper())
        except Exception as ex:
            print("sec ticker map error:", ex, flush=True)
    return _cik_tickers

def parse_sec(e):
    m = SEC_TITLE_RX.match(e.get("title") or "")
    form, name, cik = (m.group("form"), m.group("name"), int(m.group("cik"))) if m else ("SEC", e.get("title", ""), None)
    summary = re.sub(r"<[^>]+>", " ", html.unescape(e.get("summary", "")))
    summary = re.sub(r"\s+", " ", summary).strip()
    ts = None
    if e.get("updated_parsed"):
        ts = datetime.fromtimestamp(calendar.timegm(e.updated_parsed), timezone.utc).isoformat()
    tickers = cik_map().get(cik, []) if cik else []
    return {"form": form, "company": name, "cik": cik, "link": e.get("link"),
            "summary": summary, "timestamp": ts, "tickers": tickers}

def build_sec_embed(p):
    emb = {"title": f"🏛️ SEC {p['form']} — {p['company']}"[:256], "url": p["link"],
           "color": SEC_COLOR, "description": p["summary"][:1000],
           "footer": {"text": f"SEC EDGAR · CIK {p['cik']}"}}
    if p["tickers"]:
        emb["author"] = {"name": " ".join("$" + t for t in p["tickers"][:5])}
    if p["timestamp"]:
        emb["timestamp"] = p["timestamp"]
    return emb

# ---------- state / posting ----------
first_run = not os.path.exists(STATE)
seen = json.load(open(STATE)) if not first_run else []
seen_set = set(seen)

def save():
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(seen[-5000:], f)

def mark(eid):
    seen.append(eid); seen_set.add(eid)

def post(embed):
    payload = {"embeds": [embed]}
    for _ in range(5):
        r = requests.post(WEBHOOK, json=payload, timeout=15)
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2
            time.sleep(wait if wait < 100 else wait / 1000)  # handle ms-style values
            continue
        if r.status_code >= 400:
            print("discord error", r.status_code, r.text[:200], flush=True)
        break
    time.sleep(1)

def poll_stocktitan(seeding):
    r = requests.get(FEED, headers={"User-Agent": UA_BROWSER}, timeout=20)
    feed = feedparser.parse(r.content)
    posted = 0
    for e in reversed(feed.entries):
        eid = e.get("id") or e.get("link")
        if not eid or eid in seen_set:
            continue
        mark(eid)
        if seeding:
            continue
        p = parse_stocktitan(e)
        if passes_filters(p["headline"], [p["ticker"]]):
            post(build_news_embed(p)); posted += 1
    return len(feed.entries), posted

_sec_last = {}
_sec_seeded = set()

def poll_sec(form, seeding):
    if time.time() - _sec_last.get(form, 0) < SEC_MIN_INTERVAL:
        return None
    _sec_last[form] = time.time()
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&count=40&output=atom"
    r = requests.get(url, headers={"User-Agent": SEC_UA}, timeout=20)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    # A form added later is seeded silently on its first poll too (no flood).
    seed = seeding or (form not in _sec_seeded and not any(s.startswith(f"sec:{form}:") for s in seen))
    _sec_seeded.add(form)
    posted = 0
    for e in reversed(feed.entries):
        eid = f"sec:{form}:{e.get('id') or e.get('link')}"
        if eid in seen_set:
            continue
        mark(eid)
        if seed:
            continue
        # getcurrent&type=4 also matches 4/A etc.; keep them
        p = parse_sec(e)
        if passes_filters(f"{p['form']} {p['company']} {p['summary']}", p["tickers"]):
            post(build_sec_embed(p)); posted += 1
    return len(feed.entries), posted

def poll_once():
    global first_run
    seeding = first_run
    msg = []
    try:
        n, k = poll_stocktitan(seeding); msg.append(f"stocktitan entries={n} posted={k}")
    except Exception as ex:
        msg.append(f"stocktitan error: {ex}")
    for form in SEC_FORMS:
        try:
            res = poll_sec(form, seeding)
            if res:
                msg.append(f"sec {form} entries={res[0]} posted={res[1]}")
        except Exception as ex:
            msg.append(f"sec {form} error: {ex}")
    first_run = False
    save()
    print(time.strftime("%H:%M:%S"), ("seeding " if seeding else "") + " | ".join(msg), flush=True)

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
