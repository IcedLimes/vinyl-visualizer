"""Docks: a circle on the desktop the record can be dropped into, such as the
moon on an Evangelion desktop. The desktop describes it in a small JSON file
($XDG_RUNTIME_DIR/vinyl-dock.json, watched for changes):

    {"monitor": "eDP-1", "cx": 1793, "cy": 830, "r": 374,
     "uri": "spotify:track:...",
     "notify": ["quickshell", "-c", "eva", "ipc", "call", "eva", "moon"]}

cx, cy and r are in the monitor's layout pixels. With "open": false (windows
cover the circle) nothing docks. While you drag the record,
`notify` is run with an event appended: "grab" when the drag starts, "hover"
and "leave" as the record's middle enters or leaves the circle, "release"
when it's dropped elsewhere, "dock" when it's dropped in (the record glides
to the circle and takes its size, and `uri` starts playing, or carries on if
it's already the current track) and "undock" when
it's dragged back out. Without the file, none of this happens.

Separately it reports "present" while the record sits in the dock and is on
screen, and "away" once it isn't (pulled out, hidden, closed, or faded out with
Spotify), so the desktop can show things only then. While present it also
keeps $XDG_RUNTIME_DIR/vinyl-docked, for a desktop that starts up later.

While docked ("moon form") the record also sits under windows, its spectrum
ring is at full strength and the player repeats the track. Pulled back out, it
gets back its size, layer, ring and the player's repeat mode. What it had
before is kept in ~/.config/vinyl-visualizer/docked.json while it's docked,
so it's still docked (and can be undone) after a restart.
"""

import json
import math
import os
import subprocess

from gi.repository import Gio

PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "vinyl-dock.json")
BEFORE = os.path.join(os.path.expanduser("~"), ".config", "vinyl-visualizer", "docked.json")
PRESENT = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "vinyl-docked")


class Dock:
    def __init__(self):
        self.spec = None
        self.hovering = False
        self.before = self._read_before()   # while docked: settings and repeat mode from before
        self.docked = self.before is not None
        self._load()
        self._watch = Gio.File.new_for_path(PATH).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self._watch.connect("changed", lambda *_: self._load())

    def _load(self):
        try:
            with open(PATH) as f:
                spec = json.load(f)
            self.spec = spec if all(k in spec for k in ("cx", "cy", "r")) else None
        except (OSError, ValueError):
            self.spec = None

    def set_present(self, present):
        """The record is (or no longer is) sitting in the dock, on screen."""
        try:
            if present:
                open(PRESENT, "w").close()
            else:
                os.remove(PRESENT)
        except OSError:
            pass
        self.tell("present" if present else "away")

    def remember(self, before):
        """Docking: keeps what the record had before (a dict), across restarts."""
        self.before = before
        try:
            os.makedirs(os.path.dirname(BEFORE), exist_ok=True)
            with open(BEFORE + ".tmp", "w") as f:
                json.dump(before, f, indent=2)
            os.replace(BEFORE + ".tmp", BEFORE)
        except OSError:
            pass

    def forget(self):
        """Undocking: returns what the record had before, and drops it."""
        before, self.before = self.before or {}, None
        try:
            os.remove(BEFORE)
        except OSError:
            pass
        return before

    @staticmethod
    def _read_before():
        try:
            with open(BEFORE) as f:
                before = json.load(f)
            return before if isinstance(before, dict) else None
        except (OSError, ValueError):
            return None

    @property
    def size(self):
        return round(2 * self.spec["r"]) if self.spec else None

    def center(self):
        return (self.spec["cx"], self.spec["cy"]) if self.spec else None

    def contains(self, monitor, x, y):
        """Whether a point (monitor-local px) is inside the circle."""
        s = self.spec
        if not s or s.get("open") is False or (s.get("monitor") and monitor and s["monitor"] != monitor):
            return False
        return math.hypot(x - s["cx"], y - s["cy"]) <= s["r"]

    def tell(self, event):
        cmd = (self.spec or {}).get("notify")
        if not cmd:
            return
        try:
            subprocess.Popen(list(cmd) + [event], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass
