"""
setlistFmScrapePlaywright.py

Scrapes a user's setlist.fm "attended" pages and produces
EVENT-LEVEL concert data.

Definition of ONE event:
- One calendar date
- One venue
- One city

Multiple artists, co-headliners, and festivals on the same
day at the same venue are collapsed into ONE event.
Festivals count ONCE PER DAY.
"""

import json
import re
import time
import os
import hashlib
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from tqdm import tqdm
from playwright.sync_api import sync_playwright

# ---------------------- USER INPUT ----------------------
username = input("Enter the setlist.fm username: ").strip()

# ---------------------- PATHS ---------------------------
SCRIPT_DIR = "/Users/dieter/python/setlistFmConcertCounter"
os.makedirs(SCRIPT_DIR, exist_ok=True)

RESULT_FILE = os.path.join(
    SCRIPT_DIR,
    f"{username.lower()}_concert_events.json"
)

BASE_PROFILE = f"https://www.setlist.fm/attended/{username}"

# ---------------------- CONFIG --------------------------
REQUEST_DELAY = 1.0
CONCERT_ITEM_SELECTOR = "li.setlist"

# ---------------------- HELPERS -------------------------
def normalize_text(s: str) -> str:
    if not s:
        return ""
    return (
        re.sub(r"\s+", " ", s)
        .strip()
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )

def normalize_date(date_text: str):
    if not date_text:
        return None
    try:
        d = dateparser.parse(date_text, fuzzy=True)
        if d:
            return d.date().isoformat()
    except Exception:
        pass
    return None

def make_event_id(date_iso, venue, city):
    base = f"{date_iso}|{venue.lower()}|{city.lower()}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]

def is_festival_entry(raw_text, venue):
    text = f"{raw_text} {venue}".lower()
    festival_hints = [
        "festival",
        "fest",
        "weekend",
        "open air",
        "day "
    ]
    return any(h in text for h in festival_hints)

# ---------------------- PARSE PAGE ----------------------
def parse_attended_page(html):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for li in soup.select(CONCERT_ITEM_SELECTOR):
        try:
            text = li.get_text(separator=" ", strip=True)

            # ---- Date (robust) ----
            date_text = None
            date_tag = li.find("time")

            if date_tag:
                date_text = date_tag.get("datetime") or date_tag.get_text(strip=True)

            if not date_text:
                m = re.search(
                    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
                    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
                    r"\s+\d{1,2},?\s+\d{4}",
                    text
                )
                if m:
                    date_text = m.group(0)

            # ---- Artists ----
            artists = []
            for a in li.find_all("a", href=True):
                if "/artist/" in a["href"] or "/setlists/" in a["href"]:
                    name = normalize_text(a.get_text())
                    if name:
                        artists.append(name)

            # ---- Location ----
            venue = city = state = country = ""
            sub = li.select_one(".subline")
            loc_text = sub.get_text(" ", strip=True) if sub else text
            parts = [p.strip() for p in loc_text.split(",") if p.strip()]

            if len(parts) >= 1: venue = parts[0]
            if len(parts) >= 2: city = parts[1]
            if len(parts) >= 3: state = parts[2]
            if len(parts) >= 4: country = parts[3]

            results.append({
                "raw_text": text,
                "date_text": date_text,
                "artists": artists,
                "venue": venue,
                "city": city,
                "state": state,
                "country": country
            })

        except Exception:
            continue

    return results

# ---------------------- EVENT DEDUPE --------------------
def dedupe_to_events(entries):
    events = {}
    order = []

    for e in entries:
        date_iso = normalize_date(e.get("date_text"))
        if not date_iso:
            continue

        venue = normalize_text(e.get("venue"))
        city = normalize_text(e.get("city"))
        state = normalize_text(e.get("state"))
        country = normalize_text(e.get("country"))

        # Fallback parse from raw text if venue missing
        if not venue:
            m = re.search(r"at\s+([^,]+),\s*([^,]+)", e.get("raw_text",""), re.I)
            if m:
                venue = normalize_text(m.group(1))
                city = normalize_text(m.group(2))

        key = (date_iso, venue.lower(), city.lower())

        if key not in events:
            event_id = make_event_id(date_iso, venue, city)
            events[key] = {
                "event_id": event_id,
                "date": date_iso,
                "venue": venue,
                "city": city,
                "state_province": state,
                "country": country,
                "artists": [],
                "is_festival": False,
                "raw_setlists": []
            }
            order.append(key)

        # Merge artists
        for a in e.get("artists", []):
            if a not in events[key]["artists"]:
                events[key]["artists"].append(a)

        # Raw audit trail
        raw = e.get("raw_text","")
        if raw and raw not in events[key]["raw_setlists"]:
            events[key]["raw_setlists"].append(raw)

        if is_festival_entry(raw, venue):
            events[key]["is_festival"] = True

    return [events[k] for k in order]

# ---------------------- MAIN ----------------------------
def main():
    all_entries = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/114.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        print(f"Fetching profile: {BASE_PROFILE}")
        page.goto(BASE_PROFILE, wait_until="domcontentloaded", timeout=60000)

        # Cookie consent (best effort)
        try:
            consent = page.locator("button:has-text('Accept')")
            if consent.count():
                consent.first.click()
        except Exception:
            pass

        page.wait_for_selector(CONCERT_ITEM_SELECTOR, timeout=60000)

        # Detect total pages
        try:
            last = page.query_selector("a.pageLink[title='Go to last page']")
            TOTAL_PAGES = int(last.inner_text())
        except Exception:
            TOTAL_PAGES = 1

        print(f"Detected {TOTAL_PAGES} pages")

        for page_num in tqdm(range(1, TOTAL_PAGES + 1), desc="Scraping pages"):
            if page_num > 1:
                try:
                    page.click(f"a.pageLink[title='Go to page {page_num}']")
                    page.wait_for_selector(CONCERT_ITEM_SELECTOR, timeout=60000)
                except Exception:
                    break

            html = page.content()
            parsed = parse_attended_page(html)
            all_entries.extend(parsed)
            time.sleep(REQUEST_DELAY)

        browser.close()

    print(f"Collected {len(all_entries)} raw setlist entries")
    events = dedupe_to_events(all_entries)

    if not events:
        print("❌ No events collapsed — check date parsing or selectors")
    else:
        print(f"Collapsed to {len(events)} true concert events")

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

    print(f"Wrote event data to:\n{RESULT_FILE}")

# ---------------------- RUN -----------------------------
if __name__ == "__main__":
    main()