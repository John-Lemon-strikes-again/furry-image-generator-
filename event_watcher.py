"""
MOGO Wiki -> Discord event watcher

Watches the Golden Blitz and Partner Events tag pages. When a NEW post appears
it sends ONE Discord message (one ping) with every new event, including the
event's START DATE (read from the title / blurb text, e.g. "Starts September 29, 2026"
or "Coming June 30th").

It can also send a second "starts TODAY" reminder ping on the day an event begins.

Setup:
    pip install requests beautifulsoup4
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    export DISCORD_PING="<@YOUR_USER_ID>"      # or "@here" / "<@&ROLE_ID>"
    python event_watcher.py                    # runs forever, checks every 10 min
    python event_watcher.py --once             # single check (cron / GitHub Actions)

Optional:
    DAY_OF_REMINDER=0     # set to 0 to turn off the "starts today" reminder
    CHECK_EVERY_SECONDS=600

First run saves everything already on the pages as "seen" WITHOUT posting.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SOURCES = {
    "Golden Blitz": "https://monopolygo.wiki/tag/golden-blitz",
    "Partner Event": "https://monopolygo.wiki/tag/partner-events",
}

STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_events.json"))
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
PING = os.environ.get("DISCORD_PING") or "@here"
CHECK_EVERY_SECONDS = int(os.environ.get("CHECK_EVERY_SECONDS", "600"))
DAY_OF_REMINDER = os.environ.get("DAY_OF_REMINDER", "1") != "0"

HEADERS = {"User-Agent": "Mozilla/5.0 (event-watcher; personal Discord alerts)"}
COLORS = {"Golden Blitz": 0xF5B800, "Partner Event": 0x2E86DE}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# "September 29, 2026" | "Sep 29" | "June 30th" | "August 7, 2026"
DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
POST_DATE_RE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$")  # "Sep 25, 2026"


# ---------- date helpers ----------
def parse_post_date(text):
    try:
        return datetime.strptime(text, "%b %d, %Y").date()
    except (TypeError, ValueError):
        return None


def parse_start_date(title, summary, post_date):
    """Find the event start date in the title, then the blurb. Returns date or None."""
    for text in (title, summary):
        if not text:
            continue
        m = DATE_RE.search(text)
        if not m:
            continue
        month = MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else (post_date.year if post_date else date.today().year)
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        # "Coming January 3rd" posted in December -> next year
        if not m.group(3) and post_date and (post_date - d).days > 180:
            d = date(year + 1, month, day)
        return d
    return None


def discord_date(d):
    """Discord renders <t:...:D> in each viewer's own timezone (noon UTC avoids off-by-one)."""
    ts = int(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp())
    return f"<t:{ts}:D> (<t:{ts}:R>)"


# ---------- state ----------
def load_state():
    if not STATE_FILE.exists():
        return None  # first run
    data = json.loads(STATE_FILE.read_text())
    if isinstance(data, list):  # old format
        data = {"seen": data, "pending": {}}
    return data


def save_state(state):
    state["seen"] = sorted(set(state["seen"]))
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------- scraping ----------
def scrape(label, url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    posts = []
    for h2 in soup.select("h2"):
        a = h2.find("a", href=True)
        if not a:
            continue
        link = urljoin(url, a["href"])
        title = a.get_text(strip=True)

        card = h2.parent
        image = summary = None
        post_date = None
        if card:
            img = card.find("img", src=True)
            if img and img["src"].startswith("http"):
                image = img["src"]
            for p in card.find_all("p"):
                text = p.get_text(" ", strip=True)
                if text:
                    summary = text
                    break
            for s in card.find_all(string=True):
                s = s.strip()
                if POST_DATE_RE.match(s):
                    post_date = parse_post_date(s)
                    break

        start = parse_start_date(title, summary, post_date)
        posts.append({
            "label": label, "title": title, "url": link, "image": image,
            "summary": summary,
            "post_date": post_date.isoformat() if post_date else None,
            "start_date": start.isoformat() if start else None,
        })
    return posts


# ---------- discord ----------
def post_webhook(payload):
    r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)))
        r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    time.sleep(1)


def build_embed(p, today_reminder=False):
    fields = []
    if p["start_date"]:
        sd = date.fromisoformat(p["start_date"])
        fields.append({"name": "📅 Starts", "value": discord_date(sd), "inline": False})
    elif p["post_date"]:
        pd = date.fromisoformat(p["post_date"])
        fields.append({"name": "📅 Posted", "value": discord_date(pd), "inline": False})

    embed = {
        "title": p["title"],
        "url": p["url"],
        "color": COLORS.get(p["label"], 0x2ECC71),
        "fields": fields,
        "footer": {"text": p["label"]},
    }
    if p["image"]:
        embed["thumbnail"] = {"url": p["image"]}
    return embed


def send_new(new_posts):
    """One message, one ping, up to 10 embeds."""
    for i in range(0, len(new_posts), 10):
        batch = new_posts[i:i + 10]
        post_webhook({
            "content": f"{PING} 🚨 New event alert!" if i == 0 else "",
            "embeds": [build_embed(p) for p in batch],
            "allowed_mentions": {"parse": ["everyone", "users", "roles"]},
        })


def send_today(posts):
    for i in range(0, len(posts), 10):
        batch = posts[i:i + 10]
        post_webhook({
            "content": f"{PING} ⏰ Starting TODAY!" if i == 0 else "",
            "embeds": [build_embed(p, True) for p in batch],
            "allowed_mentions": {"parse": ["everyone", "users", "roles"]},
        })


# ---------- main logic ----------
def check_once():
    state = load_state()
    first_run = state is None
    if first_run:
        state = {"seen": [], "pending": {}}
    seen = set(state["seen"])
    pending = state.setdefault("pending", {})  # url -> post, waiting for start day

    all_posts = []
    for label, url in SOURCES.items():
        try:
            all_posts.extend(scrape(label, url))
        except Exception as e:
            print(f"[warn] couldn't read {url}: {e}", file=sys.stderr)

    new_posts, urls = [], set()
    for p in all_posts:
        if p["url"] not in seen and p["url"] not in urls:
            urls.add(p["url"])
            new_posts.append(p)

    if first_run:
        print(f"First run: saved {len(new_posts)} existing posts, nothing sent.")
        # still queue upcoming events for a day-of reminder
        for p in new_posts:
            if p["start_date"] and date.fromisoformat(p["start_date"]) >= date.today():
                pending[p["url"]] = p
    elif new_posts:
        if not WEBHOOK_URL:
            sys.exit("Set DISCORD_WEBHOOK_URL first.")
        send_new(list(reversed(new_posts)))  # oldest first
        print(f"Sent {len(new_posts)} new event(s) to Discord.")
        for p in new_posts:
            if p["start_date"] and date.fromisoformat(p["start_date"]) >= date.today():
                pending[p["url"]] = p
    else:
        print("No new events.")

    seen.update(p["url"] for p in new_posts)

    # day-of reminders (one ping per run for everything starting today)
    if DAY_OF_REMINDER and not first_run:
        today = date.today()
        due = [p for p in pending.values() if date.fromisoformat(p["start_date"]) <= today]
        starting_today = [p for p in due if date.fromisoformat(p["start_date"]) == today]
        if starting_today:
            send_today(starting_today)
            print(f"Sent 'starts today' reminder for {len(starting_today)} event(s).")
        for p in due:  # also drops any that were missed (past start)
            pending.pop(p["url"], None)

    state["seen"] = list(seen)
    state["pending"] = pending
    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="check one time and exit")
    args = ap.parse_args()
    if args.once:
        check_once()
        return
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
