"""
setlistFmConcertCounterGUI.py  —  macOS GUI version

Run:             python setlistFmConcertCounterGUI.py
Build .app:      python setup.py py2app --alias

Requires: beautifulsoup4, python-dateutil, playwright, tqdm
  pip install beautifulsoup4 python-dateutil playwright tqdm
  playwright install chromium

Recommended Python: 3.11 via Homebrew
  brew install python-tk@3.11
  /opt/homebrew/bin/python3.11 setlistFmConcertCounterGUI.py

Color theme: matches Android notification panel
  #1a1a1a  dark charcoal background
  #2c2c2c  panel / tile background
  #0d0d0d  black element background
  #3ddc84  Android green (primary accent)
  #f0f0f0  primary text
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import queue
import json
import re
import time
import os
import hashlib
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright

# ── Android notification panel palette ───────────────────────────────────────
BG        = "#1a1a1a"   # notification panel background (dark charcoal)
BG_PANEL  = "#2c2c2c"   # quick settings tile background
BG_BLACK  = "#0d0d0d"   # black circle elements
BG_LOG    = "#111111"   # log area (slightly darker than BG)
FG        = "#f0f0f0"   # primary text (near white)
FG_DIM    = "#808080"   # secondary / dim text
FG_GREEN  = "#3ddc84"   # Android green — primary accent
FG_GREEN2 = "#69f0ae"   # lighter green for highlights
BORDER    = "#383838"   # separator / border

FONT_HEAD  = ("Helvetica", 16, "bold")
FONT_BODY  = ("Helvetica", 12)
FONT_SMALL = ("Helvetica", 11)
FONT_MONO  = ("Courier", 11)
FONT_STAT  = ("Helvetica", 13)
FONT_STATB = ("Helvetica", 13, "bold")


# ── Shared scraping functions ─────────────────────────────────────────────────

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
    for li in soup.select("li.setlist"):
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


# ── GUI ───────────────────────────────────────────────────────────────────────

class SetlistCounterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("setlist.fm Concert Counter")
        self.resizable(False, False)
        self.configure(bg=BG)

        self.queue       = queue.Queue()
        self.result_data = None
        self.running     = False

        self._build_ui()
        self._center()
        self.after(50, self._force_draw)

    def _force_draw(self):
        self.update_idletasks()
        self.lift()
        self.focus_force()

    def _center(self):
        self.update_idletasks()
        w  = self.winfo_reqwidth()
        h  = self.winfo_reqheight()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    def _resize_to_content(self):
        self.update_idletasks()
        w  = self.winfo_reqwidth()
        h  = self.winfo_reqheight()
        x  = self.winfo_x()
        y  = self.winfo_y()
        sh = self.winfo_screenheight()
        y  = min(y, sh - h - 40)
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = tk.Frame(self, bg=BG, padx=22, pady=18)
        root.pack(fill=tk.BOTH, expand=True)

        # Header
        header_row = tk.Frame(root, bg=BG)
        header_row.pack(fill=tk.X, anchor=tk.W)
        # Green circle dot — mimics the Android green icon style
        tk.Label(header_row, text="●", font=("Helvetica", 20),
                 bg=BG, fg=FG_GREEN).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(header_row, text="setlist.fm Concert Counter",
                 font=FONT_HEAD, bg=BG, fg=FG).pack(side=tk.LEFT)

        tk.Label(root, text="Enter a setlist.fm username to see your concert stats",
                 font=FONT_SMALL, bg=BG, fg=FG_DIM).pack(anchor=tk.W, pady=(4, 14))

        self._divider(root)

        # Username input — styled like an Android tile
        input_row = tk.Frame(root, bg=BG)
        input_row.pack(fill=tk.X, pady=(10, 6))

        tk.Label(input_row, text="Username", font=FONT_BODY,
                 bg=BG, fg=FG).pack(side=tk.LEFT, padx=(0, 10))

        self.username_var = tk.StringVar()
        self.entry = tk.Entry(
            input_row, textvariable=self.username_var,
            font=FONT_BODY, width=26,
            bg=BG_PANEL, fg=FG, insertbackground=FG_GREEN,
            relief=tk.FLAT, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=FG_GREEN,
        )
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self.entry.bind("<Return>", lambda _: self._start())
        self.entry.focus()

        # Look up button — Android black circle style with green text
        self.run_btn = tk.Button(
            input_row, text="Look up", font=FONT_BODY,
            bg=BG_BLACK, fg=FG_GREEN,
            activebackground=BG_PANEL, activeforeground=FG_GREEN2,
            relief=tk.FLAT, padx=14, pady=5, cursor="hand2",
            command=self._start,
        )
        self.run_btn.pack(side=tk.LEFT, padx=(10, 0))

        # Progress bar — green on dark
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("App.Horizontal.TProgressbar",
                        troughcolor=BG_PANEL, background=FG_GREEN,
                        bordercolor=BG, lightcolor=FG_GREEN, darkcolor=FG_GREEN)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            root, variable=self.progress_var,
            style="App.Horizontal.TProgressbar",
            maximum=100, mode="indeterminate", length=474,
        )
        self.progress_bar.pack(pady=(6, 0))

        # Activity log — black bg, green text (terminal style)
        tk.Label(root, text="Activity", font=FONT_SMALL,
                 bg=BG, fg=FG_DIM).pack(anchor=tk.W, pady=(12, 3))

        log_outer = tk.Frame(root, bg=FG_GREEN, padx=1, pady=1)   # green border
        log_outer.pack(fill=tk.X)
        log_inner = tk.Frame(log_outer, bg=BG_LOG)
        log_inner.pack(fill=tk.BOTH)

        self.log_text = tk.Text(
            log_inner, height=7, font=FONT_MONO,
            state=tk.DISABLED, wrap=tk.WORD, relief=tk.FLAT,
            bg=BG_LOG, fg=FG_GREEN,
            insertbackground=FG_GREEN, padx=8, pady=6,
        )
        sb = tk.Scrollbar(log_inner, orient=tk.VERTICAL,
                          command=self.log_text.yview,
                          bg=BG_PANEL, troughcolor=BG_LOG)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self._divider(root, top=14, bottom=14)

        # Results area
        self.results_frame = tk.Frame(root, bg=BG)
        self.results_frame.pack(fill=tk.X)
        tk.Label(self.results_frame,
                 text="Results will appear here after lookup",
                 font=FONT_BODY, bg=BG, fg=FG_DIM).pack(anchor=tk.W)

        self._divider(root, top=14, bottom=10)

        # Save button — Android tile style
        self.save_btn = tk.Button(
            root, text="Save Results as JSON…", font=FONT_BODY,
            bg=BG_PANEL, fg=FG_DIM,
            activebackground=BG_BLACK, activeforeground=FG_GREEN,
            relief=tk.FLAT, padx=12, pady=5, cursor="hand2",
            state=tk.DISABLED, command=self._save_json,
        )
        self.save_btn.pack(pady=(0, 4))

    def _divider(self, parent, top=4, bottom=4):
        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X, pady=(top, bottom))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _log(self, text):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ── Start ─────────────────────────────────────────────────────────────────

    def _start(self):
        username = self.username_var.get().strip()
        if not username:
            messagebox.showwarning("No username", "Please enter a setlist.fm username.")
            return
        if self.running:
            return

        self.running     = True
        self.result_data = None

        self.run_btn.configure(state=tk.DISABLED, bg=BG_PANEL, fg=FG_DIM)
        self.save_btn.configure(state=tk.DISABLED, bg=BG_PANEL, fg=FG_DIM)

        self._clear_log()
        for w in self.results_frame.winfo_children():
            w.destroy()
        tk.Label(self.results_frame, text="Loading…",
                 font=FONT_BODY, bg=BG, fg=FG_DIM).pack(anchor=tk.W)

        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(12)

        threading.Thread(target=self._worker, args=(username,), daemon=True).start()
        self.after(100, self._poll)

    def _poll(self):
        try:
            while True:
                msg  = self.queue.get_nowait()
                kind = msg[0]

                if kind == "log":
                    self._log(msg[1])

                elif kind == "progress_known":
                    _, current, total = msg
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate")
                    self.progress_var.set(current / total * 100)

                elif kind == "progress_update":
                    _, current, total = msg
                    self.progress_var.set(current / total * 100)

                elif kind == "done":
                    _, summary, festivals, events, username = msg
                    self.result_data = {
                        "username": username, "summary": summary,
                        "festivals": festivals, "events": events,
                    }
                    self._show_results(username, summary)
                    self.progress_bar.stop()
                    self.progress_var.set(100)
                    self.running = False
                    self.run_btn.configure(state=tk.NORMAL, bg=BG_BLACK, fg=FG_GREEN)
                    self.save_btn.configure(state=tk.NORMAL, bg=BG_PANEL, fg=FG_GREEN)
                    return

                elif kind == "error":
                    self._log(f"\n❌  {msg[1]}")
                    self.progress_bar.stop()
                    self.progress_var.set(0)
                    self.running = False
                    self.run_btn.configure(state=tk.NORMAL, bg=BG_BLACK, fg=FG_GREEN)
                    return

        except queue.Empty:
            pass

        if self.running:
            self.after(100, self._poll)

    # ── Results ───────────────────────────────────────────────────────────────

    def _show_results(self, username, s):
        for w in self.results_frame.winfo_children():
            w.destroy()

        def stat_row(label, value, highlight=False):
            f = tk.Frame(self.results_frame, bg=BG)
            f.pack(fill=tk.X, pady=3)
            tk.Label(f, text=label, font=FONT_STAT,
                     bg=BG, fg=FG).pack(side=tk.LEFT)
            color = FG_GREEN2 if highlight else FG_GREEN
            tk.Label(f, text=str(value), font=FONT_STATB,
                     bg=BG, fg=color).pack(side=tk.RIGHT)

        def mini_divider():
            tk.Frame(self.results_frame, bg=BORDER, height=1).pack(fill=tk.X, pady=6)

        # Username header styled like an Android tile label
        tk.Label(self.results_frame,
                 text=f"Stats for:  {username}",
                 font=FONT_BODY, bg=BG, fg=FG_DIM).pack(anchor=tk.W, pady=(0, 8))

        stat_row("Total concerts attended",         s["total_concerts"],        highlight=True)
        stat_row("Total individual sets seen",      s["total_sets_seen"])
        stat_row("Unique artists seen",             s["unique_artists_seen"])
        mini_divider()
        stat_row("Regular (non-festival) concerts", s["non_festival_concerts"])
        stat_row("Days spent at festivals",         s["festival_event_days"])
        stat_row("Unique festivals attended",       s["unique_festivals_attended"])

        self.after(50, self._resize_to_content)

    # ── Save JSON ─────────────────────────────────────────────────────────────

    def _save_json(self):
        if not self.result_data:
            return
        username     = self.result_data.get("username", "user")
        default_name = f"{username.lower()}_concert_events.json"
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialdir=os.path.expanduser("~/Downloads"),
            initialfile=default_name,
            title="Save concert data",
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.result_data, f, ensure_ascii=False, indent=2)
            self._log(f"✓ Saved to {path}")

    # ── Worker ────────────────────────────────────────────────────────────────

    def _worker(self, username):
        q = self.queue
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ))
                pw_page = context.new_page()
                festivals      = self._w_festivals(pw_page, username, q)
                festival_names = {f["name"].lower() for f in festivals}
                entries        = self._w_attended(pw_page, username, festival_names, q)
                browser.close()

            events  = dedupe_to_events(entries)
            summary = build_summary(events, festivals)
            q.put(("done", summary, festivals, events, username))

        except Exception as exc:
            q.put(("error", str(exc)))

    def _w_festivals(self, pw_page, username, q):
        q.put(("log", "[1/2] Loading your festival history…"))
        pw_page.goto(f"https://www.setlist.fm/concerts/{username}",
                     wait_until="domcontentloaded", timeout=60_000)
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
            q.put(("log", "  Could not load festival data"))
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
        h2 = next(
            (t for t in soup.find_all("h2") if t.get_text(strip=True).lower() == "festivals"),
            None,
        )
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
        q.put(("log", f"  Found {len(festivals)} festival(s) in your history"))
        return festivals

    def _w_attended(self, pw_page, username, festival_names, q):
        q.put(("log", "\n[2/2] Loading your attended shows…"))
        pw_page.goto(f"https://www.setlist.fm/attended/{username}",
                     wait_until="domcontentloaded", timeout=60_000)
        try:
            btn = pw_page.locator("button:has-text('Accept')")
            if btn.count():
                btn.first.click()
                time.sleep(0.6)
        except Exception:
            pass
        pw_page.wait_for_selector("li.setlist", timeout=60_000)
        nums = []
        for el in pw_page.query_selector_all(".pageLink"):
            try:
                t = el.inner_text().strip()
                if t.isdigit():
                    nums.append(int(t))
            except Exception:
                pass
        total_pages = max(nums) if nums else 1
        q.put(("log",            f"  {total_pages} page(s) to scrape"))
        q.put(("progress_known", 0, total_pages))

        all_entries = []
        page_num    = 0
        while True:
            page_num += 1
            q.put(("progress_update", page_num, total_pages))
            q.put(("log", f"  Page {page_num}/{total_pages} — {len(all_entries)} sets so far"))
            html   = pw_page.content()
            parsed = parse_attended_page(html, festival_names)
            all_entries.extend(parsed)
            next_btn = pw_page.locator("a[title='Go to next page']")
            if next_btn.count() == 0:
                break
            try:
                first_before = pw_page.locator("li.setlist:first-child").inner_text(timeout=5_000)
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
            time.sleep(1.2)
        q.put(("log", f"\n  Done — {len(all_entries)} total sets collected"))
        return all_entries


if __name__ == "__main__":
    app = SetlistCounterApp()
    app.mainloop()