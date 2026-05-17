"""
setup.py  —  builds a standalone macOS .app from the GUI script

Usage:
    pip install py2app
    python setup.py py2app

Output:  dist/setlistFmConcertCounter.app
    → drag to /Applications or share with anyone on macOS

Notes:
  - Playwright's Chromium browser is NOT bundled (it's too large, ~500MB).
    The first time someone runs the .app they'll need:
      pip install playwright && playwright install chromium
    Future versions of this project could switch to requests + BeautifulSoup
    server-side to remove this dependency entirely.

  - If py2app complains about missing modules, add them to packages= below.
"""

from setuptools import setup

APP     = ["setlistFmConcertCounterGUI.py"]
OPTIONS = {
    "argv_emulation": False,          # True can cause issues on macOS 12+
    "packages": [
        "bs4",
        "dateutil",
        "playwright",
        "certifi",
    ],
    "includes": [
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "queue",
        "threading",
        "hashlib",
    ],
    "plist": {
        "CFBundleName":             "setlist.fm Concert Counter",
        "CFBundleDisplayName":      "Concert Counter",
        "CFBundleIdentifier":       "com.yourname.setlistcounter",
        "CFBundleVersion":          "1.0.0",
        "CFBundleShortVersionString": "1.0",
        "NSHighResolutionCapable":  True,
    },
}

setup(
    app=APP,
    name="setlist.fm Concert Counter",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
