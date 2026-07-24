#!/usr/bin/env python3
"""Locate a Chromium for the Playwright browser suites.

The suites originally hardcoded `/opt/pw-browsers/chromium-1194/chrome-linux/chrome` — the path
inside one Linux sandbox. On a Mac checkout that path does not exist, so EVERY browser suite
failed to launch and silently stopped being a check at all. This resolves the pinned path first
(so nothing changes where it exists), then Playwright's own download cache on either platform.
"""
import os
from pathlib import Path

PINNED = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

_SEARCH_ROOTS = [
    Path("/opt/pw-browsers"),
    Path.home() / "Library/Caches/ms-playwright",     # macOS
    Path.home() / ".cache/ms-playwright",             # Linux
]

_GLOBS = [
    "chromium-*/chrome-linux/chrome",
    "chromium-*/chrome-mac*/*.app/Contents/MacOS/Google Chrome for Testing",
    "chromium-*/chrome-mac*/*.app/Contents/MacOS/Chromium",
]


def chrome_path():
    """Absolute path to a usable Chromium, or None if this checkout has none installed."""
    if os.path.exists(PINNED):
        return PINNED
    for root in _SEARCH_ROOTS:
        if not root.exists():
            continue
        for pattern in _GLOBS:
            for exe in sorted(root.glob(pattern), reverse=True):   # newest build first
                if exe.is_file() and os.access(exe, os.X_OK):
                    return str(exe)
    return None


if __name__ == "__main__":
    print(chrome_path() or "no chromium found")
