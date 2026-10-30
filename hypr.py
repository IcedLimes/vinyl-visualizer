"""Hyprland IPC: the cursor position and monitor layout, which dragging the
overlay needs. Layer surfaces only see the pointer relative to themselves,
which shifts as they move. Everything returns None outside Hyprland."""

import json
import os
import socket


def command(text):
    """A request to Hyprland; its reply as text, or None outside Hyprland."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    instance = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not runtime or not instance:
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            sock.connect(os.path.join(runtime, "hypr", instance, ".socket.sock"))
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

