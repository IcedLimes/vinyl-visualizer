"""Hyprland IPC: the cursor position and monitor layout, which dragging the
overlay needs. Layer surfaces only see the pointer relative to themselves,
which shifts as they move. Everything returns None outside Hyprland."""

import json
import os
import socket

from gi.repository import Gio, GLib

_watches = []  # event connections, kept open for as long as the program runs


def _socket_path(name):
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    instance = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not runtime or not instance:
        return None
    return os.path.join(runtime, "hypr", instance, name)


def command(text):
    """A request to Hyprland; its reply as text, or None outside Hyprland."""
    path = _socket_path(".socket.sock")
    if not path:
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            sock.connect(path)
            sock.sendall(text.encode())
            chunks = []
            while data := sock.recv(65536):
                chunks.append(data)
        return b"".join(chunks).decode(errors="replace")
    except OSError:
        return None


def request(name):
    """A query answered in JSON (cursorpos, monitors, clients...), or None."""
    reply = command(f"j/{name}")
    try:
        return json.loads(reply) if reply else None
    except ValueError:
        return None


def cursor():
    pos = request("cursorpos")
    return (pos["x"], pos["y"]) if isinstance(pos, dict) else None


def monitors():
    """Monitors in layout coordinates: name, x, y, width, height, focused."""
    found = []
    for m in request("monitors") or []:
        w, h = m["width"] / m["scale"], m["height"] / m["scale"]
        if m.get("transform", 0) % 2:  # rotated a quarter turn
            w, h = h, w
        found.append({"name": m["name"], "x": m["x"], "y": m["y"],
                      "width": round(w), "height": round(h), "focused": m.get("focused", False)})
    return found


def active_window():
    """The focused window: class, at, size, fullscreen..., or None."""
    win = request("activewindow")
    return win if isinstance(win, dict) and win.get("at") and win.get("size") else None


def watch(on_event):
    """Calls on_event(name) for each event Hyprland reports (focus moving,
    windows opening, closing, going fullscreen...), read from the GLib main
    loop without blocking it. False outside Hyprland."""
    path = _socket_path(".socket2.sock")
    if not path:
        return False
    try:
        conn = Gio.SocketClient().connect(Gio.UnixSocketAddress.new(path), None)
    except GLib.Error:
        return False
    stream = Gio.DataInputStream.new(conn.get_input_stream())

    def got(_stream, result):
        try:
            line, _length = stream.read_line_finish(result)
        except GLib.Error:
            return
        if line is None:  # Hyprland closed it
            return
        on_event(bytes(line).decode(errors="replace").partition(">>")[0])
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, got)

    stream.read_line_async(GLib.PRIORITY_DEFAULT, None, got)
    _watches.append((conn, stream))
    return True

