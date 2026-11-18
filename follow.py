"""Opens the overlay whenever a music app (Spotify or YouTube Music, see
sources.py) starts, and closes it once the last one has quit.

Started by `vinyl-visualizer --with-spotify` (from Hyprland's start-up). It
only listens on D-Bus for media players coming and going, so it costs nothing
while idle, and it talks to the overlay through the overlay's own --show and
--quit, so it works whether or not it was the one that opened it.
"""

import argparse
import os
import signal
import sys

from gi.repository import Gio, GLib

import sources

VINYL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vinyl.py")
PREFIX = "org.mpris.MediaPlayer2."


def run_vinyl(*args):
    # Gio.Subprocess reaps the process when it exits, so nothing lingers.
    Gio.Subprocess.new([sys.executable, VINYL, *args], Gio.SubprocessFlags.NONE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", default="auto")
    ap.add_argument("--audio", default="player")
    args = ap.parse_args()
    wanted = sources.wanted(args.player)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    open_now = set()  # the music apps' players on the bus

    def update(name, present, retry=True):
        had = bool(open_now)
        instance = name[len(PREFIX):]
        if present and sources.source_for(instance, wanted):
            open_now.add(name)
        else:
            open_now.discard(name)
            if present and retry and not sources.identity(instance):
                # just appeared and not answering yet: ask again in a moment
                GLib.timeout_add(1000, lambda: update(name, name in present_now(), False) and False)
        if open_now and not had:
            run_vinyl("--show", "--player", args.player, "--audio", args.audio)
        elif had and not open_now:
            run_vinyl("--quit")

    def present_now():
        return bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                             "org.freedesktop.DBus", "ListNames", None,
                             GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, -1,
                             None).unpack()[0]

    def owner_changed(_bus, _sender, _path, _iface, _signal, params):
        name, _old, new = params.unpack()
        if name.startswith(PREFIX):
            update(name, bool(new))

    bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                         "/org/freedesktop/DBus", PREFIX.rstrip("."),
                         Gio.DBusSignalFlags.MATCH_ARG0_NAMESPACE, owner_changed)
    for name in present_now():  # the ones already open when this starts
        if name.startswith(PREFIX):
            update(name, True)

    loop = GLib.MainLoop()
    try:
        from gi.repository import GLibUnix
        add_signal = GLibUnix.signal_add
    except ImportError:  # PyGObject before 3.52
        add_signal = GLib.unix_signal_add
    for sig in (signal.SIGINT, signal.SIGTERM):
        add_signal(GLib.PRIORITY_DEFAULT, sig, loop.quit)
    loop.run()


if __name__ == "__main__":
    main()
