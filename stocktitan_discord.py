import json, os, sys, time
import feedparser, requests

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
FEED = os.environ.get("STOCKTITAN_FEED_URL", "https://www.stocktitan.net/rss").strip()
STATE = "state/seen.json"
POLL = int(os.environ.get("POLL_SECONDS", "30"))
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "0"))  # 0 = single pass

if not WEBHOOK:
    print("DISCORD_WEBHOOK_URL not set; skipping.")
    sys.exit(0)

first_run = not os.path.exists(STATE)
seen = json.load(open(STATE)) if not first_run else []
seen_set = set(seen)

def save():
    os.makedirs("state", exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(seen[-2000:], f)

def post(e):
    payload = {"embeds": [{"title": (e.get("title") or "News")[:256],
                           "url": e.get("link"),
                           "description": e.get("published", "")}]}
    for _ in range(3):
        r = requests.post(WEBHOOK, json=payload, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        break
    time.sleep(1)

def poll_once():
    global first_run
    feed = feedparser.parse(FEED)
    posted = 0
    for e in reversed(feed.entries):
        eid = e.get("id") or e.get("link")
        if not eid or eid in seen_set:
            continue
        seen.append(eid); seen_set.add(eid)
        if not first_run:
            post(e); posted += 1
    first_run = False
    save()
    print(f"{time.strftime('%H:%M:%S')} feed entries={len(feed.entries)} posted={posted}", flush=True)

end = time.time() + RUN_MINUTES * 60
while True:
    try:
        poll_once()
    except Exception as ex:
        print("error:", ex, flush=True)
    if time.time() + POLL >= end:
        break
    time.sleep(POLL)
