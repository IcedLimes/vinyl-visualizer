"""What Spotify is playing, read on a worker thread and handed to the overlay
on the Qt thread. Each platform asks in its own way (see open_player); this
is the part they share: polling, carrying the position forward between
reads, and loading each new cover."""

import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from media import Track, load_cover


@dataclass(frozen=True)
class State:
    """One reading of the player. None stands for "Spotify isn't running"."""
    playing: bool = False
    track: Track = Track()
    position: float = 0.0   # seconds into the track when it was read


class NotYet(Exception):
    """The cover isn't available yet: try again at the next poll."""


class Player(QObject):
    """changed fires when the track, play state or open/closed state changes;
    cover(image) when a cover is ready (None: there's no cover to show)."""

    changed = Signal()
    cover = Signal(object)
    _read_now = Signal(object)

    POLL = 0.5           # seconds between readings
    COVER_TRIES = 6      # readings to wait for a cover before showing none
    SETTLE = 1.5         # seconds a player may take to show a play/pause it was asked for

    def __init__(self, poll=True):
        super().__init__()
        self.running = False
        self.playing = False
        self.track = Track()
        self._position = 0.0
        self._position_at = time.monotonic()
        self._cover_key = None   # the cover shown (or given up on): its track's art_url
        self._cover_tries = 0
        self._expect = (None, 0.0)  # (playing, until): what play_pause just asked for
        self._commands = queue.Queue()
        self._stopped = False
        self._read_now.connect(self._apply)  # queued: emitted on the worker thread
        if poll:  # (the self-test reads it once itself instead)
            threading.Thread(target=self._run, daemon=True).start()

    # ---- what the overlay uses -----------------------------------------

    @property
    def position(self):
        pos = self._position
        if self.playing:
            pos += time.monotonic() - self._position_at
        return min(pos, self.track.length) if self.track.length else pos

    @property
    def progress(self):
        return self.position / self.track.length if self.track.length else 0.0

    def play_pause(self):
        self._send("play_pause")
        if self.running:  # show it straight away, and give the player a moment to agree
            self._position, self._position_at = self.position, time.monotonic()
            self.playing = not self.playing
            self._expect = (self.playing, time.monotonic() + self.SETTLE)
            self.changed.emit()

    def next(self):
        self._send("next")

    def previous(self):
        self._send("previous")

    def stop(self):
        self._stopped = True
        self._commands.put(None)

    # ---- for each platform ---------------------------------------------

    def _setup(self):
        """Runs once on the worker thread before the first reading."""

    def _read(self):
        """The player's State now, or None while Spotify isn't running."""
        raise NotImplementedError

    def _command(self, name):
        """Carries out "play_pause", "next" or "previous"."""
        raise NotImplementedError

    def _load_cover(self, state):
        """The black-and-white cover for state.track (a PIL image), None for
        none, or raises NotYet. By default it's downloaded from its URL."""
        return load_cover(state.track.art_url)

    # ---- worker thread -------------------------------------------------

    def _send(self, name):
        self._commands.put(name)

    def _run(self):
        try:
            self._setup()
        except Exception as e:
            print(f"vinyl: can't follow Spotify: {e}")
            return
        while not self._stopped:
            try:
                name = self._commands.get(timeout=self.POLL)
            except queue.Empty:
                name = None
            if self._stopped:
                return
            try:
                if name:
                    self._command(name)
                    continue  # read it back at the next poll, once the player has caught up
                state = self._read()
            except Exception as e:  # the player went away mid-call, or answered oddly
                print(f"vinyl: couldn't read Spotify: {e}")
                state = None
            self._read_now.emit(state)
            if state and state.track.art_url != self._cover_key:
                self._fetch_cover(state)
            elif state is None and self._cover_key:
                self._cover_key = None
                self.cover.emit(None)

    def _fetch_cover(self, state):
        key = state.track.art_url
        try:
            image = self._load_cover(state) if key else None
        except NotYet:
            image = NotYet
        except Exception as e:  # network or decode errors: retry, then show a plain record
            print(f"vinyl: couldn't load the cover: {e}")
            image = NotYet
        if image is NotYet:
            self._cover_tries += 1
            if self._cover_tries < self.COVER_TRIES:
                return
            image = None
        self._cover_key, self._cover_tries = key, 0
        self.cover.emit(image)

    # ---- Qt thread -----------------------------------------------------

    def _apply(self, state):
        running = state is not None
        state = state or State()
        playing = state.playing
        expected, until = self._expect
        if running and expected is not None and playing != expected and time.monotonic() < until:
            playing = expected  # it hasn't caught up with the click yet
        before = (self.running, self.playing, self.track)
        self.running, self.playing, self.track = running, playing, state.track
        self._position, self._position_at = state.position, time.monotonic()
        if (self.running, self.playing, self.track) != before:
            self.changed.emit()


class Playerctl(Player):
    """Linux, for trying the Qt overlay there: Spotify over MPRIS through the
    playerctl command. (The Linux app proper is vinyl.py.)"""

    FORMAT = "\t".join(["{{status}}", "{{mpris:trackid}}", "{{title}}", "{{artist}}",
                        "{{mpris:artUrl}}", "{{mpris:length}}", "{{position}}"])

    def _read(self):
        out = subprocess.run(["playerctl", "-p", "spotify", "metadata", "--format", self.FORMAT],
                             capture_output=True, text=True, timeout=2)
        if out.returncode:
            return None
        status, tid, title, artist, art, length, pos = (out.stdout.rstrip("\n").split("\t") + [""] * 7)[:7]
        return State(playing=status == "Playing",
                     track=Track(id=tid, title=title, artist=artist, art_url=art,
                                 length=_number(length) / 1e6),
                     position=_number(pos) / 1e6)

    def _command(self, name):
        verb = {"play_pause": "play-pause", "next": "next", "previous": "previous"}[name]
        subprocess.run(["playerctl", "-p", "spotify", verb], capture_output=True, timeout=2)


def open_player():
    if sys.platform == "win32":
        from desktop.player_windows import SpotifyWindows
        return SpotifyWindows()
    if sys.platform == "darwin":
        from desktop.player_macos import SpotifyMac
        return SpotifyMac()
    return Playerctl()


def _number(text):
    try:
        return float(text)
    except ValueError:
        return 0.0
