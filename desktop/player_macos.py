"""macOS: Spotify through its AppleScript interface (osascript), which gives
the track, its cover URL, the play state and the position. The first time,
macOS asks whether Vinyl may control Spotify."""

import subprocess

from desktop.player import Player, State, _number
from media import Track

# Asks only while Spotify is running: a plain `tell` would launch it.
READ = """
if application "Spotify" is running then
    tell application "Spotify"
        set answer to (player state as text)
        try
            set t to current track
            set answer to answer & tab & (id of t) & tab & (name of t) & tab & (artist of t) ¬
                & tab & (artwork url of t) & tab & (duration of t) & tab & (player position)
        end try
        return answer
    end tell
end if
return "closed"
"""

COMMANDS = {"play_pause": "playpause", "next": "next track", "previous": "previous track"}


class SpotifyMac(Player):
    POLL = 1.0  # each reading starts osascript, so not too often

    def _read(self):
        out = _osascript(READ)
        if out is None or out == "closed":
            return None
        state, tid, title, artist, art, duration, position = (out.split("\t") + [""] * 7)[:7]
        return State(playing=state == "playing",
                     # numbers come back in the user's locale: 12,5 as well as 12.5
                     position=_number(position.replace(",", ".")),
                     track=Track(id=tid, title=title, artist=artist, art_url=art,
                                 length=_number(duration.replace(",", ".")) / 1000))

    def _command(self, name):
        _osascript(f'if application "Spotify" is running then '
                   f'tell application "Spotify" to {COMMANDS[name]}')


def _osascript(script):
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode:
        if "-1743" in out.stderr:  # not allowed to control Spotify
            print("vinyl: allow Vinyl to control Spotify in System Settings → "
                  "Privacy & Security → Automation")
        return None
    return out.stdout.rstrip("\n")
