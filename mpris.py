"""The music side: follows Spotify and YouTube Music (see sources.py) over
MPRIS, through libplayerctl, and loads each new cover as a black-and-white
image. With both open, it follows whichever one is playing.

The apps publish the track, its cover and the playback position themselves,
so this needs no developer app, login or API key.
"""

import threading
import time

import gi

gi.require_version("Playerctl", "2.0")
from gi.repository import GLib, Playerctl  # noqa: E402

import sources  # noqa: E402
from media import Track, load_cover  # noqa: E402  (Track is re-exported for callers)

_LOOPS = {"None": Playerctl.LoopStatus.NONE, "Track": Playerctl.LoopStatus.TRACK,
          "Playlist": Playerctl.LoopStatus.PLAYLIST}


class Player:
    """Watches the music apps' MPRIS players and follows one of them: the
    one playing, or the one that played last. Runs on the GLib main loop, so
    every callback arrives on the GTK thread. on_change() fires when the
    track, play state or open/closed state changes, or another app takes
    over; on_cover(image) when a cover is ready (None means there's no cover
    to show)."""

    def __init__(self, name="auto", on_change=None, on_cover=None):
        self.sources = sources.wanted(name)
        self.on_change = on_change or (lambda: None)
        self.on_cover = on_cover or (lambda image: None)
        self.players = {}         # every matching player that's open: Playerctl.Player -> Source
        self.player = None        # the one followed
        self.source = self.sources[0]  # ...and which app it is
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
        """Plays a URI right away: a spotify: one (a track, album or playlist)
        in Spotify, whichever app is being followed."""
        player = self.player
        if uri and uri.startswith("spotify:"):
            player = next((p for p, s in self.players.items() if s is sources.SPOTIFY), player)
        if player and uri:
            try:
                player.open(uri)
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

    # ---- which player -----------------------------------------------------

    def _scan(self):
        for name in self.manager.props.player_names:
            self._appeared(self.manager, name)
        return GLib.SOURCE_REMOVE

    def _appeared(self, manager, name, retry=True):
        source = sources.source_for(name.instance, self.sources)
        if source is None:
            if retry and not sources.identity(name.instance):
                # just appeared and not answering yet: ask again in a moment
                GLib.timeout_add(1000, lambda: self._appeared(manager, name, False) and False)
            return
        if any(p.props.player_instance == name.instance for p in self.players):
            return
        try:
            player = Playerctl.Player.new_from_name(name)
        except GLib.Error:
            return
        player.connect("playback-status", self._status_changed)
        player.connect("metadata", lambda p, *_: p is self.player and self._refresh())
        player.connect("seeked", lambda p, *_: p is self.player and self._read_position())
        manager.manage_player(player)
        self.players[player] = source
        if self.player is None or (_is_playing(player) and not self.playing):
            self._follow(player)

    def _vanished(self, manager, player):
        if self.players.pop(player, None) is not None and player is self.player:
            # carry on with another open one, playing if any is
            others = sorted(self.players, key=_is_playing, reverse=True)
            self._follow(others[0] if others else None)

    def _status_changed(self, player, *_):
        if player is self.player:
            self._refresh()
        elif _is_playing(player):  # another app started playing: follow it
            self._follow(player)

    def _follow(self, player):
        self.player = player
        self.source = self.players.get(player, self.source)
        self._refresh()

    # ---- the followed player ----------------------------------------------

    def _refresh(self):
        player = self.player
        if player:
            self.playing = _is_playing(player)
            title = _read(player.get_title)
            artist = _read(player.get_artist)
            # an object path, printed in quotes: '/com/spotify/track/...'
            track_id = _read(lambda: player.print_metadata_prop("mpris:trackid")).strip("'")
            if track_id.startswith("/org/chromium/"):
                # Electron's generic player numbers tracks as it goes: name
                # the song instead, so per-song choices stick.
                track_id = f"{self.source.key}:{artist}\n{title}"
            self.track = Track(
                id=track_id,
                title=title,
                artist=artist,
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


def _is_playing(player):
    try:
        return player.props.playback_status == Playerctl.PlaybackStatus.PLAYING
    except GLib.Error:
        return False


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
