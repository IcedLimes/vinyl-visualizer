"""Windows: Spotify through the system media controls (the ones behind the
volume flyout), which give the track, play state, position and cover."""

import asyncio
from datetime import datetime, timezone

from desktop.player import NotYet, Player, State
from media import Track, cover_from_bytes

PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING


class SpotifyWindows(Player):
    def _setup(self):
        # Imported here so the Windows Runtime starts on this thread, apart from Qt's.
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager)
        self._loop = asyncio.new_event_loop()
        self._manager = self._loop.run_until_complete(Manager.request_async())
        self._last_cover = None  # the previous track's cover bytes

    def _session(self):
        """Spotify's session: "Spotify.exe" for the installer version,
        "SpotifyAB.SpotifyMusic_...!Spotify" for the Microsoft Store one."""
        for session in self._manager.get_sessions():
            if "spotify" in (session.source_app_user_model_id or "").lower():
                return session
        return None

    def _read(self):
        return self._loop.run_until_complete(self._read_async())

    async def _read_async(self):
        session = self._session()
        if session is None:
            return None
        props = await session.try_get_media_properties_async()
        playing = int(session.get_playback_info().playback_status) == PLAYING
        timeline = session.get_timeline_properties()
        length = (timeline.end_time - timeline.start_time).total_seconds()
        position = timeline.position.total_seconds()
        if playing:  # the timeline is only updated now and then: add the time since
            since = (datetime.now(timezone.utc) - _utc(timeline.last_updated_time)).total_seconds()
            if 0 <= since < max(length, 1):
                position += since
        title, artist = props.title or "", props.artist or ""
        # No track ids here, so title and artist stand in for one (it keys the
        # per-song pulse choice and tells when the cover needs loading).
        key = f"{artist}\n{title}" if title else ""
        return State(playing=playing, position=position,
                     track=Track(id=key, title=title, artist=artist, art_url=key, length=length))

    def _command(self, name):
        session = self._session()
        if session:
            call = {"play_pause": session.try_toggle_play_pause_async,
                    "next": session.try_skip_next_async,
                    "previous": session.try_skip_previous_async}[name]
            self._loop.run_until_complete(call())

    def _load_cover(self, state):
        data = self._loop.run_until_complete(self._thumbnail())
        # Spotify updates the cover a moment after the title: the old one, or
        # none, means it hasn't yet.
        if not data or data == self._last_cover:
            raise NotYet
        self._last_cover = data
        return cover_from_bytes(data)

    async def _thumbnail(self):
        from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions
        session = self._session()
        if session is None:
            return None
        props = await session.try_get_media_properties_async()
        if props.thumbnail is None:
            return None
        stream = await props.thumbnail.open_read_async()
        buffer = Buffer(stream.size)
        await stream.read_async(buffer, buffer.capacity, InputStreamOptions.READ_AHEAD)
        try:
            return bytes(memoryview(buffer))
        except TypeError:  # an older pywinrt without the buffer protocol
            data = bytearray(buffer.length)
            DataReader.from_buffer(buffer).read_bytes(data)
            return bytes(data)


def _utc(when):
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
