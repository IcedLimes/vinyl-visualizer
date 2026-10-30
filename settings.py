"""Settings: defaults, color presets and the JSON file they're saved in."""

import copy
import json
import os
import re
import sys
from pathlib import Path


def _folder():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


PATH = _folder() / "vinyl-visualizer" / "settings.json"

# shadows, highlights, accent. The cover is drawn in shadows → highlights
# (a duotone), the spectrum ring, tonearm and text in the accent.
PRESETS = {
    "Mono": ("#000000", "#ffffff", "#d9d9d9"),
    "Sepia": ("#140d06", "#f0e0c4", "#cdb48c"),
    "Slate": ("#090d12", "#dbe5ef", "#93a8bd"),
    "Crimson": ("#100304", "#f3d3cf", "#e04a42"),
}

DEFAULTS = {
    "size": 380,              # record diameter, px
    "x": None,                # overlay's top-left on its monitor; None = right edge, centred
    "y": None,
    "monitor": None,          # the monitor's name (Hyprland's, or the screen's on Windows and macOS)
    "layer": "top",           # "top": above windows; "bottom": on the desktop, under windows
    "style": "picture",       # "picture": cover across the record; "label": black vinyl, cover label
    "rpm": 8.0,
    "visualizer": 0.4,        # spectrum ring strength; 0 turns it off
    "pulse": 0.5,             # how much the record swells with the beat; 0 turns it off
    "sync_offset": 0,         # ms on top of the automatic output-latency delay (+ shows later)
    "follow": "hits",         # what the pulse follows: "hits", or "notes" (every note of the lead)
    "follow_tracks": {},      # per-song choice: track id -> "hits" or "notes"
    "vocals_first": True,     # while someone sings, the pulse follows their syllables
    "tonearm": True,
    "track_info": "always",   # "always", "hover" or "off"
    "hide_when_closed": True,  # fade out while Spotify isn't running
    "preset": "Mono",
    "dark": "#000000",
    "light": "#ffffff",
    "accent": "#d9d9d9",
}

CHOICES = {
    "layer": ("top", "bottom"),
    "style": ("picture", "label"),
    "track_info": ("always", "hover", "off"),
    "follow": ("hits", "notes"),
}


def presets():
    """The built-in presets, plus the current eva theme when there is one."""
    found = dict(PRESETS)
    eva = eva_colors()
    if eva:
        found["Eva theme"] = (eva.get("bg", "#000000"), eva.get("text", "#ffffff"),
                              eva.get("primary", "#d9d9d9"))
    return found


def eva_colors():
    try:
        text = (Path.home() / ".config/eva/current/colors.lua").read_text()
    except OSError:
        return {}
    return {k: "#" + v for k, v in re.findall(r'(\w+)\s*=\s*"([0-9a-fA-F]{6})"', text)}


def defaults():
    return copy.deepcopy(DEFAULTS)


def load():
    settings = defaults()
    try:
        saved = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return settings
    for key, value in saved.items() if isinstance(saved, dict) else ():
        if key in DEFAULTS and valid(key, value):
            settings[key] = value
    return settings


def save(settings):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(PATH)


def valid(key, value):
    default = DEFAULTS[key]
    if key in CHOICES:
        return value in CHOICES[key]
    if key == "follow_tracks":
        return isinstance(value, dict) and all(
            isinstance(k, str) and v in CHOICES["follow"] for k, v in value.items())
    if key in ("dark", "light", "accent"):
        return isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None
    if default is None:
        return value is None or isinstance(value, (int, float, str))
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def rgb(hex_color):
    """'#rrggbb' → (r, g, b) floats."""
    return tuple(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
