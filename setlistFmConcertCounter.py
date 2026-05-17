"""
setlistFmConcertCounter.py  —  CLI version

Run: python setlistFmConcertCounter.py

For a GUI version see setlistFmConcertCounterGUI.py
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
username = input("Enter your setlist.fm username: ").strip()

# ---------------------- PATHS ---------------------------
ATTENDED_URL = f"https://www.setlist.fm/attended/{username}"
CONCERTS_URL = f"https://www.setlist.fm/concerts/{username}"

# ---------------------- CONFIG --------------------------
REQUEST_DELAY         = 1.2
CONCERT_ITEM_SELECTOR = "li.setlist"
NEXT_BTN_SELECTOR     = "a[title='Go to next page']"

# ---------------------- HELPERS -------------------------
def normalize_text(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().replace("\u2013", "-").replace("\u2014", "-")

def normalize_date(date_text):
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

def parse_attended_page(html, festival_names):
    soup    = BeautifulSoup(html, "html.parser")
    results = []
    for li in soup.select(CONCERT_ITEM_SELECTOR):
        try:
            content_link = li.select_one("div.column.content a[href]")
            href     = content_link["href"] if content_link else ""
            name_tag = li.select_one("div.column.content a strong")
            artist   = normalize_text(name_tag.get_text()) if name_tag else ""
            date_text = None
            date_span = li.select_one("span.smallDateBlock")
            if date_span:
                date_text = date_span.get_text(separator=" ", strip=True)
            venue = city = state = country = ""
            loc_tag  = li.select_one("span.subline > span")
            loc_text = normalize_text(loc_tag.get_text()) if loc_tag else ""
            if loc_text:
                parts = [p.strip() for p in loc_text.split(",") if p.strip()]
                if len(parts) >= 1: venue   = parts[0]
                if len(parts) >= 2: city    = parts[1]
                if len(parts) >= 3: state   = parts[2]
                if len(parts) >= 4: country = parts[3]
            venue_lower = venue.lower()
            is_fest = any(fn in venue_lower or venue_lower in fn for fn in festival_names)
            raw = normalize_text(li.get_text(separator=" "))
            results.append({
                "raw_text": raw, "href": href, "date_text": date_text,
                "artist": artist, "venue": venue, "city": city,
                "state": state, "country": country, "is_festival": is_fest,
            })
        except Exception:
            continue
    return results

def dedupe_to_events(entries):
    events = {}
    order  = []
    for e in entries:
        date_iso = normalize_date(e.get("date_text"))
        if not date_iso:
            continue
        venue   = e.get("venue",   "")
        city    = e.get("city",    "")
        state   = e.get("state",   "")
        country = e.get("country", "")
        key = (date_iso, venue.lower(), city.lower())
        if key not in events:
            events[key] = {
                "event_id": make_event_id(date_iso, venue, city),
                "date": date_iso, "venue": venue, "city": city,
                "state_province": state, "country": country,
                "artists": [], "sets_seen": 0,
                "is_festival": False, "raw_setlists": [],
            }
            order.append(key)
        ev = events[key]
        ev["sets_seen"] += 1
        artist = e.get("artist", "")
        if artist and artist not in ev["artists"]:
            ev["artists"].append(artist)
        raw = e.get("raw_text", "")
        if raw and raw not in ev["raw_setlists"]:
            ev["raw_setlists"].append(raw)
        if e.get("is_festival"):
            ev["is_festival"] = True
    return [events[k] for k in order]

def build_summary(events, festivals):
    total_concerts = len(events)
    total_sets     = sum(e["sets_seen"] for e in events)
    festival_days  = sum(1 for e in events if e["is_festival"])
    all_artists    = set()
    for e in events:
        all_artists.update(e["artists"])
    return {
        "total_concerts":            total_concerts,
        "total_sets_seen":           total_sets,
        "unique_artists_seen":       len(all_artists),
        "non_festival_concerts":     total_concerts - festival_days,
        "festival_event_days":       festival_days,
        "unique_festivals_attended": len(festivals),
    }

# ===================== SCRAPE 1: FESTIVALS =====================
def scrape_festivals_page(pw_page):
    print(f"\n[1/2] Loading your festival history...")
    pw_page.goto(CONCERTS_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        btn = pw_page.locator("button:has-text('Accept')")
        if btn.count():
            btn.first.click()
            time.sleep(0.6)
    except Exception:
        pass
    try:
        pw_page.wait_for_selector("table.statsTable", timeout=15_000)
    except Exception:
        print("  Could not load festival data — continuing without it")
        return []
    perpage = pw_page.locator("select[name*='festivals:perPage']")
    if perpage.count():
        try:
            perpage.select_option(label="500")
            time.sleep(2.5)
        except Exception:
            pass
    html = pw_page.content()
    soup = BeautifulSoup(html, "html.parser")
    festivals = []
    h2 = None
    for tag in soup.find_all("h2"):
        if tag.get_text(strip=True).lower() == "festivals":
            h2 = tag
            break
    if not h2:
        return []
    table = h2.find_next("table", class_="statsTable")
    if not table:
        return []
    for tr in table.select("tr"):
        name_td = tr.select_one("td.songName")
        if not name_td:
            continue
        name_span = name_td.select_one("a span")
        if not name_span:
            continue
        name = normalize_text(name_span.get_text())
        if not name:
            continue
        count = 1
        chart = tr.select_one("span.barChart span")
        if chart:
            try:
                count = int(chart.get_text(strip=True))
            except ValueError:
                pass
        years = []
        next_tr = tr.find_next_sibling("tr")
        if next_tr:
            for link in next_tr.select("a.twoLineLink"):
                strong = link.select_one("strong")
                if strong:
                    years.append(normalize_text(strong.get_text()))
        festivals.append({"name": name, "times_attended": count, "years": years})
    print(f"    Found {len(festivals)} festival(s) in your history")
    return festivals

# ===================== SCRAPE 2: ATTENDED =====================
def scrape_attended(pw_page, festival_names):
    print(f"\n[2/2] Loading your attended shows...")
    pw_page.goto(ATTENDED_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        btn = pw_page.locator("button:has-text('Accept')")
        if btn.count():
            btn.first.click()
            time.sleep(0.6)
    except Exception:
        pass
    pw_page.wait_for_selector(CONCERT_ITEM_SELECTOR, timeout=60_000)
    nums = []
    for el in pw_page.query_selector_all(".pageLink"):
        try:
            t = el.inner_text().strip()
            if t.isdigit():
                nums.append(int(t))
        except Exception:
            pass
    total_pages = max(nums) if nums else 1
    all_entries = []
    page_num    = 0
    progress    = tqdm(total=total_pages, desc="    Scraping pages", unit="page", ncols=60)
    while True:
        page_num += 1
        html   = pw_page.content()
        parsed = parse_attended_page(html, festival_names)
        all_entries.extend(parsed)
        progress.update(1)
        progress.set_postfix({"sets": len(all_entries)})
        next_btn = pw_page.locator(NEXT_BTN_SELECTOR)
        if next_btn.count() == 0:
            break
        try:
            first_before = pw_page.locator(f"{CONCERT_ITEM_SELECTOR}:first-child").inner_text(timeout=5_000)
        except Exception:
            first_before = ""
        next_btn.click()
        pw_page.wait_for_load_state("networkidle", timeout=30_000)
        try:
            pw_page.wait_for_function(
                """(before) => {
                    const el = document.querySelector('li.setlist:first-child');
                    return el && el.innerText !== before;
                }""",
                arg=first_before, timeout=10_000,
            )
        except Exception:
            pass
        time.sleep(REQUEST_DELAY)
    progress.close()
    return all_entries

# ---------------------- MAIN ----------------------------
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        pw_page = context.new_page()
        festivals      = scrape_festivals_page(pw_page)
        festival_names = {f["name"].lower() for f in festivals}
        entries        = scrape_attended(pw_page, festival_names)
        browser.close()

    events  = dedupe_to_events(entries)
    summary = build_summary(events, festivals)

    if not events:
        print("\n  No events found — check that the username is correct")
        return

    w = 52
    print(f"\n{'─' * w}")
    print(f"  Concert stats for: {username}")
    print(f"{'─' * w}")
    print(f"  Total concerts attended            : {summary['total_concerts']}")
    print(f"  Total individual sets seen         : {summary['total_sets_seen']}")
    print(f"  Unique artists seen                : {summary['unique_artists_seen']}")
    print(f"{'─' * w}")
    print(f"  Regular (non-festival) concerts    : {summary['non_festival_concerts']}")
    print(f"  Days spent at festivals            : {summary['festival_event_days']}")
    print(f"  Unique festivals attended          : {summary['unique_festivals_attended']}")
    print(f"{'─' * w}")

    # Optional JSON save
    save = input("\nSave full data as JSON? [y/n]: ").strip().lower()
    if save == "y":
        default = os.path.join(os.path.expanduser("~/Downloads"), f"{username.lower()}_concert_events.json")
        path = input(f"Save path [{default}]: ").strip() or default
        output = {"username": username, "summary": summary, "festivals": festivals, "events": events}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"  Saved → {path}")
    print()

if __name__ == "__main__":
    main()
