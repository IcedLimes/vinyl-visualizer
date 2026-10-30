#!/usr/bin/env python3
"""Vinyl for Windows and macOS (vinyl.py is the Linux app).

What Spotify is playing, as a black-and-white record slowly spinning over
your desktop. Click the middle to play or pause, drag it to move it, scroll
on it to resize, right-click for the menu and settings.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _keep_output():
    """The packaged app has no console: what it prints goes to vinyl.log
    next to the settings, so there's something to look at when it misbehaves."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from desktop.native import log_path
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 1_000_000:
        path.replace(path.with_suffix(".old.log"))
    log = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


if __name__ == "__main__":
    _keep_output()
    from desktop.app import main
    sys.exit(main())
