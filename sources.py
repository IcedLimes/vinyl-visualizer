"""The music apps Vinyl follows, and how to recognise each one: its MPRIS
player (for the track and controls) and its audio stream (for the beat).

Spotify's player is always called "spotify". YouTube Music is the Pear
Desktop app (formerly th-ch's YouTube Music): with its "Shortcuts (& MPRIS)"
plugin it has a player of its own called "YoutubeMusic", and without it
Electron's generic one, "chromium.instance<pid>", which only its Identity
tells apart from a browser's.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    key: str
    names: tuple = ()        # MPRIS player names (the part after org.mpris.MediaPlayer2.)
    identities: tuple = ()   # MPRIS Identity values, for players without a fixed name
    streams: tuple = ()      # words in its audio stream's binary or application name


SPOTIFY = Source("spotify", names=("spotify",), streams=("spotify",))
YOUTUBE_MUSIC = Source(
    "youtube-music",
    names=("YoutubeMusic", "youtube-music"),
    identities=("com.github.th-ch.youtube-music", "YouTube Music", "Pear Desktop"),
    streams=("youtube-music", "pear-desktop"))
SOURCES = (SPOTIFY, YOUTUBE_MUSIC)


def wanted(player):
    """The sources to follow: all of them ("auto"), or just the MPRIS player
    named on the command line."""
    if player in (None, "", "auto"):
        return SOURCES
    known = next((s for s in SOURCES if player == s.key or player in s.names), None)
    return (known or Source(player, names=(player,), streams=(player.lower(),)),)


def source_for(instance, sources=SOURCES):
    """Which source an MPRIS player (its bus name after org.mpris.MediaPlayer2.)
    belongs to, or None."""
    for source in sources:
        if any(instance == n or instance.startswith(n + ".") for n in source.names):
            return source
    if any(source.identities for source in sources):
        ident = identity(instance)
        for source in sources:
            if ident and ident in source.identities:
                return source
    return None


def identity(instance):
    """A player's MPRIS Identity, e.g. "Spotify" or "com.github.th-ch.youtube-music"."""
    from gi.repository import Gio, GLib  # here: the Windows and macOS builds use the rest
    try:
        reply = Gio.bus_get_sync(Gio.BusType.SESSION).call_sync(
            f"org.mpris.MediaPlayer2.{instance}", "/org/mpris/MediaPlayer2",
            "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.mpris.MediaPlayer2", "Identity")),
            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 1000, None)
        return reply.unpack()[0] or ""
    except GLib.Error:
        return ""


def plays(source, stream):
    """Whether a PulseAudio sink input (pactl's JSON) is this source's sound."""
    props = stream.get("properties", {})
    found = " ".join(props.get(key, "") for key in
                     ("application.process.binary", "application.name")).lower()
    return any(word in found for word in source.streams)
