"""Opens the overlay whenever Spotify starts and closes it when Spotify quits.

Started by `vinyl-visualizer --with-spotify` (from Hyprland's start-up). It
only waits on D-Bus for Spotify's MPRIS name, so it costs nothing while idle,
and it talks to the overlay through the overlay's own --show and --quit, so it
works whether or not it was the one that opened it.
"""

import argparse
import os
import signal
import sys

from gi.repository import Gio, GLib

VINYL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vinyl.py")


def run_vinyl(*args):
    # Gio.Subprocess reaps the process when it exits, so nothing lingers.
    Gio.Subprocess.new([sys.executable, VINYL, *args], Gio.SubprocessFlags.NONE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", default="spotify")
    ap.add_argument("--audio", default="spotify")
    args = ap.parse_args()
    opened = False

    def appeared(_bus, _name, _owner):
        nonlocal opened
        opened = True
        run_vinyl("--show", "--player", args.player, "--audio", args.audio)

    def vanished(_bus, _name):
        nonlocal opened
        if opened:  # skip the "not running" report that comes right at start-up
            opened = False
            run_vinyl("--quit")

    Gio.bus_watch_name(Gio.BusType.SESSION, f"org.mpris.MediaPlayer2.{args.player}",
                       Gio.BusNameWatcherFlags.NONE, appeared, vanished)
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
