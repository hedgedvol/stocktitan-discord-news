import json, os, sys, time
import feedparser, requests

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
FEED = os.environ.get("STOCKTITAN_FEED_URL", "https://www.stocktitan.net/rss").strip()
STATE = "state/seen.json"

if not WEBHOOK:
    print("DISCORD_WEBHOOK_URL not set; skipping.")
    sys.exit(0)

seen = []
if os.path.exists(STATE):
    with open(STATE) as f:
        seen = json.load(f)
first_run = not os.path.exists(STATE)
seen_set = set(seen)

feed = feedparser.parse(FEED)
entries = list(reversed(feed.entries))  # oldest first
posted = 0
for e in entries:
    eid = e.get("id") or e.get("link")
    if not eid or eid in seen_set:
        continue
    seen.append(eid); seen_set.add(eid)
    if first_run:
        continue
    payload = {"embeds": [{"title": (e.get("title") or "News")[:256],
                           "url": e.get("link"),
                           "description": e.get("published", "")}]}
    for _ in range(3):
        r = requests.post(WEBHOOK, json=payload, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        break
    posted += 1
    time.sleep(1)

os.makedirs("state", exist_ok=True)
with open(STATE, "w") as f:
    json.dump(seen[-2000:], f)
print(f"feed entries={len(feed.entries)} posted={posted} first_run={first_run}")
