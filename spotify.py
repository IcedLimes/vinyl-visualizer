"""Spotify side: follows the desktop client over MPRIS (through libplayerctl)
and loads each new cover as a black-and-white image.

The client publishes the track, its cover URL and the playback position
itself, so this needs no Spotify developer app or login.
"""

import functools
import io
import threading
import time
import urllib.request
from dataclasses import dataclass

import gi

gi.require_version("Playerctl", "2.0")
from gi.repository import GLib, Playerctl  # noqa: E402

_LOOPS = {"None": Playerctl.LoopStatus.NONE, "Track": Playerctl.LoopStatus.TRACK,
          "Playlist": Playerctl.LoopStatus.PLAYLIST}
from PIL import Image, ImageOps  # noqa: E402

COVER_SIZE = 1024

# Spotify's CDN serves each cover at several sizes. MPRIS reports the 640px
# one; this prefix is the full-size original.
SIZE_640 = "ab67616d0000b273"
SIZE_FULL = "ab67616d000082c1"


@dataclass(frozen=True)
class Track:
    id: str = ""          # MPRIS track id, e.g. /com/spotify/track/...
    title: str = ""
    artist: str = ""
    art_url: str = ""
    length: float = 0.0  # seconds; 0 when unknown


class Spotify:
    """Watches one MPRIS player. Runs on the GLib main loop, so every callback
    arrives on the GTK thread. on_change() fires when the track, play state or
    open/closed state changes; on_cover(image) when a cover is ready (None
    means there's no cover to show)."""

    def __init__(self, name="spotify", on_change=None, on_cover=None):
        self.name = name
        self.on_change = on_change or (lambda: None)
        self.on_cover = on_cover or (lambda image: None)
        self.player = None
        self.track = Track()
        self.playing = False
        self._position = 0.0
        self._position_at = time.monotonic()
        self._cover_url = None

        self.manager = Playerctl.PlayerManager()
        self.manager.connect("name-appeared", self._appeared)
        self.manager.connect("player-vanished", self._vanished)
        GLib.idle_add(self._scan)  # so callbacks start once the caller is set up
        GLib.timeout_add_seconds(2, self._resync)

    @property
    def running(self):
        return self.player is not None

    @property
    def position(self):
        """Seconds into the track, carried forward between MPRIS reads."""
        pos = self._position
        if self.playing:
            pos += time.monotonic() - self._position_at
        return min(pos, self.track.length) if self.track.length else pos

    @property
    def progress(self):
        return self.position / self.track.length if self.track.length else 0.0

    def play_pause(self):
        self._call("play_pause")

    def next(self):
        self._call("next")

    def previous(self):
        self._call("previous")

    def has_track(self, uri):
        """Whether a spotify:track: URI is the current track (MPRIS gives its id
        as /com/spotify/track/<id>, older clients as spotify:track:<id>)."""
        want = uri.rsplit(":", 1)[-1] if uri else ""
        have = self.track.id.replace(":", "/").rsplit("/", 1)[-1] if self.track.id else ""
        return bool(want) and want == have

    def play(self):
        self._call("play")

    def open_uri(self, uri):
        """Plays a spotify: URI (a track, album or playlist) right away."""
        if self.player and uri:
            try:
                self.player.open(uri)
            except GLib.Error:
                pass

    @property
    def loop(self):
        """The player's repeat mode: "None", "Track" or "Playlist" (None if unknown)."""
        if self.player:
            try:
                status = self.player.props.loop_status
            except GLib.Error:
                return None
            return next((k for k, v in _LOOPS.items() if v == status), None)
        return None

    def set_loop(self, mode):
        if self.player and mode in _LOOPS:
            try:
                self.player.set_loop_status(_LOOPS[mode])
            except GLib.Error:
                pass

    def _call(self, method):
        if self.player:
            try:
                getattr(self.player, method)()
            except GLib.Error:
                pass

    def _scan(self):
        for name in self.manager.props.player_names:
            self._appeared(self.manager, name)
        return GLib.SOURCE_REMOVE

    def _appeared(self, manager, name):
        if name.name != self.name or self.player:
            return
        try:
            player = Playerctl.Player.new_from_name(name)
        except GLib.Error:
            return
        player.connect("playback-status", lambda *_: self._refresh())
        player.connect("metadata", lambda *_: self._refresh())
        player.connect("seeked", lambda *_: self._read_position())
        manager.manage_player(player)
        self.player = player
        self._refresh()

    def _vanished(self, manager, player):
        if player is self.player:
            self.player = None
            self._refresh()

    def _refresh(self):
        player = self.player
        if player:
            self.playing = player.props.playback_status == Playerctl.PlaybackStatus.PLAYING
            self.track = Track(
                id=_read(lambda: player.print_metadata_prop("mpris:trackid")),
                title=_read(player.get_title),
                artist=_read(player.get_artist),
                art_url=_read(lambda: player.print_metadata_prop("mpris:artUrl")),
                length=_number(_read(lambda: player.print_metadata_prop("mpris:length"))) / 1e6,
            )
        else:
            self.playing = False
            self.track = Track()
        self._read_position()
        if self.track.art_url != self._cover_url:
            self._cover_url = self.track.art_url
            threading.Thread(target=self._load_cover, args=(self._cover_url,), daemon=True).start()
        self.on_change()

    def _read_position(self):
        self._position = 0.0
        if self.player:
            try:
                self._position = self.player.get_position() / 1e6
            except GLib.Error:
                pass
        self._position_at = time.monotonic()

    def _resync(self):
        if self.playing:  # MPRIS doesn't announce position, so re-read it now and then
            self._read_position()
        return GLib.SOURCE_CONTINUE

    def _load_cover(self, url):  # runs on a worker thread
        image = None
        for attempt in range(3 if url else 0):
            try:
                image = load_cover(url)
                break
            except Exception as e:  # network or decode errors: retry, then show a plain record
                print(f"vinyl: couldn't load cover {url}: {e}")
                time.sleep(1 + 2 * attempt)
                if url != self._cover_url:
                    return
        GLib.idle_add(self._deliver_cover, url, image)

    def _deliver_cover(self, url, image):
        if url == self._cover_url:  # skip it if the track changed meanwhile
            self.on_cover(image)
        return GLib.SOURCE_REMOVE


def _read(getter):
    try:
        return getter() or ""
    except GLib.Error:
        return ""


def _number(text):
    try:
        return float(text)
    except ValueError:
        return 0.0


@functools.lru_cache(maxsize=16)  # skipping back and forth stays instant
def load_cover(url):
    data = None
    if SIZE_640 in url:
        try:
            data = download(url.replace(SIZE_640, SIZE_FULL))
        except Exception:  # fall back to the size MPRIS gave us
            pass
    if data is None:
        data = download(url)
    return to_black_and_white(Image.open(io.BytesIO(data)))


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "vinyl-visualizer"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def to_black_and_white(image):
    image = ImageOps.exif_transpose(image).convert("L")
    image = ImageOps.fit(image, (COVER_SIZE, COVER_SIZE), Image.LANCZOS)
    return ImageOps.autocontrast(image, cutoff=0.5)
