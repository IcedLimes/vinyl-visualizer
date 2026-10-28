#!/usr/bin/env python3
"""Vinyl: what Spotify is playing, as a black-and-white record slowly spinning
over your desktop.

Click the middle of the record to play or pause, drag it to move it, scroll on
it to resize, right-click for the menu and settings. Running vinyl-visualizer again shows or
hides it; --with-spotify keeps it opening and closing along with Spotify.
"""

import argparse
import math
import os
import signal
import sys
import time

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkLayerShell, Pango  # noqa: E402

import hypr  # noqa: E402
import settings as config  # noqa: E402
from audio import Analyzer, Capture  # noqa: E402
from disc import ARM_REST, PULSE_MAX, Disc, Layout, View, arm_angle  # noqa: E402
from prefs import Prefs  # noqa: E402
from spotify import Spotify  # noqa: E402

APP_ID = "io.github.vinyl_visualizer"
NAMESPACE = "vinyl-visualizer"  # layer-shell namespace, for Hyprland layer rules
CENTER = 0.3                    # the click-to-pause area, in record radii
MIN_SIZE, MAX_SIZE = 120, 1400
EDGE_GAP = 48                   # first placement: this far in from the right edge
FPS, IDLE_FPS = 60, 10


def parser():
    ap = argparse.ArgumentParser(prog="vinyl-visualizer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", type=int, metavar="PX", help="record diameter")
    ap.add_argument("--rpm", type=float, help="spin speed (default 8)")
    ap.add_argument("--pulse", type=float, metavar="0-1",
                    help="how much the record swells with the beat (default 0.5; 0 is off)")
    ap.add_argument("--style", choices=config.CHOICES["style"],
                    help="picture: the cover across the record; label: black vinyl, cover label")
    ap.add_argument("--layer", choices=config.CHOICES["layer"],
                    help="top: above windows; bottom: on the desktop, under windows")
    ap.add_argument("--colors", choices=list(config.presets()), metavar="PRESET",
                    help="color preset: " + ", ".join(config.presets()))
    ap.add_argument("--settings", action="store_true", help="open the settings window")
    ap.add_argument("--reset", action="store_true", help="default settings and position")
    ap.add_argument("--show", action="store_true", help="open or show it (never hides)")
    ap.add_argument("--quit", action="store_true", help="close the running overlay")
    ap.add_argument("--with-spotify", action="store_true",
                    help="stay in the background, opening the overlay when Spotify starts "
                         "and closing it when Spotify quits")
    ap.add_argument("--audio", choices=["spotify", "system"], default="spotify",
                    help="react to Spotify only (default) or to everything you hear")
    ap.add_argument("--player", default="spotify", help="MPRIS player to follow (see playerctl -l)")
    return ap


class App(Gtk.Application):
    """One overlay per session: launching again hands the options to the
    running one, and with no options shows or hides it."""

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.overlay = None

    def do_command_line(self, command_line):
        args = parser().parse_args(command_line.get_arguments()[1:])
        first = self.overlay is None
        if args.quit:
            GLib.idle_add(self.quit)  # after answering, so the caller doesn't see an error
            return 0
        if first:
            self.overlay = Overlay(self, args)
        changes = {key: getattr(args, key) for key in ("size", "rpm", "pulse", "style", "layer")
                   if getattr(args, key) is not None}
        if args.colors:
            dark, light, accent = config.presets()[args.colors]
            changes.update(preset=args.colors, dark=dark, light=light, accent=accent)
        if args.reset:
            self.overlay.reset_settings(position=True)
        if changes:
            self.overlay.change(**changes)
        if args.settings:
            self.overlay.open_settings()
        if args.show:
            self.overlay.show()
        elif not (first or changes or args.reset or args.settings):
            self.overlay.toggle()
        return 0

    def do_shutdown(self):
        if self.overlay:
            self.overlay.shutdown()
        Gtk.Application.do_shutdown(self)


class Overlay:
    def __init__(self, app, args):
        self.app = app
        self.settings = config.load()
        self.view = View()
        self.layout = None
        self.disc = None
        self.cover = None         # the current cover, kept so the GL side can be rebuilt
        self.prefs = None
        self.speed = 0.0          # platter speed, rad/s
        self.info = 0.0           # track info opacity
        self.over_record = False
        self.over_center = False
        self.interactive = None   # whether the record currently takes clicks
        self._press = None        # left-button drag in progress
        self._cursor = None
        self._menu = None
        self._save_id = 0
        self._timer = 0
        self._fps = 0
        self._last = time.monotonic()

        self.window = self._build_window()
        self.analyzer = Analyzer()
        self.capture = Capture(args.audio)
        self.capture.start()
        self.spotify = Spotify(args.player, on_change=self._on_track, on_cover=self._on_cover)
        self._apply()
        self.window.show_all()
        self._run_at(FPS)

    # ---- window --------------------------------------------------------

    def _build_window(self):
        win = Gtk.Window(application=self.app, title="Vinyl")
        win.set_name("vinyl")
        win.set_visual(win.get_screen().get_rgba_visual())
        win.set_app_paintable(True)
        css = Gtk.CssProvider()
        css.load_from_data(b"#vinyl { background-color: transparent; }")
        win.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if GtkLayerShell.is_supported():
            GtkLayerShell.init_for_window(win)
            GtkLayerShell.set_namespace(win, NAMESPACE)
            GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.LEFT, True)
            GtkLayerShell.set_exclusive_zone(win, -1)  # place it by the screen, not around bars
            GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.ON_DEMAND)
        else:  # no layer shell (X11, some compositors): a bare window kept on top
            win.set_decorated(False)
            win.set_keep_above(True)
            win.set_skip_taskbar_hint(True)
            win.set_skip_pager_hint(True)
        win.connect("key-press-event", self._key)

        self.area = area = Gtk.GLArea(has_alpha=True)
        area.set_required_version(3, 3)
        area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
                        | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        area.connect("realize", self._realize)
        area.connect("unrealize", self._unrealize)
        area.connect("render", self._render)
        area.connect("button-press-event", self._button_press)
        area.connect("button-release-event", self._button_release)
        area.connect("motion-notify-event", self._motion)
        area.connect("leave-notify-event", self._leave)
        area.connect("scroll-event", self._scroll)

        self.label = Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.END,
                               justify=Gtk.Justification.CENTER, max_width_chars=1,
                               ellipsize=Pango.EllipsizeMode.END)
        stack = Gtk.Overlay()
        stack.add(area)
        stack.add_overlay(self.label)
        stack.set_overlay_pass_through(self.label, True)
        win.add(stack)
        return win

    def _apply(self):
        """Lays out, places and colors the overlay from the settings."""
        s = self.settings
        old = self.layout
        radius = s["size"] / 2
        text_height = 0 if s["track_info"] == "off" else round(_font_px(radius) * 2.9) + 6
        self.layout = lay = Layout.for_record(s["size"], s["tonearm"], text_height)
        if old and s["x"] is not None:  # keep the record's centre where it was on screen
            s["x"] += round(old.cx - lay.cx)
            s["y"] += round(old.cy - lay.cy)

        self.area.set_size_request(lay.width, lay.height)
        self.window.resize(lay.width, lay.height)
        self.label.set_size_request(round(2.4 * lay.radius), -1)
        self.label.set_margin_start(max(0, round(lay.cx - 1.2 * lay.radius)))
        self.label.set_margin_bottom(2)
        if GtkLayerShell.is_supported():
            layer = GtkLayerShell.Layer.TOP if s["layer"] == "top" else GtkLayerShell.Layer.BOTTOM
            GtkLayerShell.set_layer(self.window, layer)
        self._place()

        v = self.view
        v.dark, v.light, v.accent = (config.rgb(s[k]) for k in ("dark", "light", "accent"))
        v.ring = s["visualizer"]
        v.arm_show = 1.0 if s["tonearm"] else 0.0
        self._update_label()
        self.interactive = None  # redo the input region for the new size

    def _place(self, monitor=None):
        """Moves the overlay to settings x/y, kept on screen."""
        s, lay = self.settings, self.layout
        mon = monitor or self._monitor()
        if s["x"] is None or s["y"] is None:
            s["x"] = mon["width"] - lay.width - EDGE_GAP
            s["y"] = (mon["height"] - lay.height) // 2
        s["x"] = max(0, min(s["x"], mon["width"] - lay.width))
        s["y"] = max(0, min(s["y"], mon["height"] - lay.height))
        if GtkLayerShell.is_supported():
            gdk_monitor = _gdk_monitor(mon)
            if gdk_monitor:
                GtkLayerShell.set_monitor(self.window, gdk_monitor)
            GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.LEFT, s["x"])
            GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.TOP, s["y"])
        else:
            self.window.move(mon["x"] + s["x"], mon["y"] + s["y"])

    def _monitor(self, at=None):
        """The overlay's monitor (or the one containing point `at`), in layout px."""
        monitors = hypr.monitors()
        if monitors:
            if at:
                mon = next((m for m in monitors if m["x"] <= at[0] < m["x"] + m["width"]
                            and m["y"] <= at[1] < m["y"] + m["height"]), None)
                if mon:
                    return mon
            mon = (next((m for m in monitors if m["name"] == self.settings["monitor"]), None)
                   or next((m for m in monitors if m["focused"]), monitors[0]))
            self.settings["monitor"] = mon["name"]
            return mon
        display = Gdk.Display.get_default()
        geo = (display.get_primary_monitor() or display.get_monitor(0)).get_geometry()
        return {"name": None, "x": geo.x, "y": geo.y, "width": geo.width, "height": geo.height}

    def _update_input(self):
        """Only the record takes clicks; everything around it passes through.
        While hidden, nothing does."""
        interactive = self.view.opacity > 0.05
        if interactive == self.interactive:
            return
        self.interactive = interactive
        lay = self.layout
        reach = lay.radius * (1 + PULSE_MAX * self.settings["pulse"]) + 1  # room to swell
        region = _circle(lay.cx, lay.cy, reach) if interactive else cairo.Region()
        self.window.input_shape_combine_region(region)

    def _update_label(self):
        track = self.spotify.track
        if not track.title or self.settings["track_info"] == "off":
            self.label.set_markup("")
            return
        pt = _font_px(self.layout.radius) * 0.75
        accent = self.settings["accent"]
        title = GLib.markup_escape_text(track.title)
        artist = GLib.markup_escape_text(track.artist.upper())
        self.label.set_markup(
            f'<span size="{round(pt * 1024)}" foreground="{accent}" alpha="88%">{title}</span>\n'
            f'<span size="{round(pt * 0.78 * 1024)}" foreground="{accent}" alpha="50%" '
            f'letter_spacing="{round(pt * 0.12 * 1024)}">{artist}</span>')

    # ---- settings ------------------------------------------------------

    def change(self, **values):
        for key, value in values.items():
            if key in config.DEFAULTS and config.valid(key, value):
                self.settings[key] = value
        self.settings["size"] = max(MIN_SIZE, min(MAX_SIZE, int(self.settings["size"])))
        self._apply()
        if self.prefs:
            self.prefs.sync()
        self._save_soon()
        self._run_at(FPS)

    def reset_settings(self, position=False):
        kept = {} if position else {k: self.settings[k] for k in ("x", "y", "monitor")}
        if position:
            self.layout = None  # no centre to keep
        self.settings = dict(config.DEFAULTS, **kept)
        self.change()

    def open_settings(self):
        if not self.prefs:
            self.prefs = Prefs(self)
            self.prefs.connect("destroy", lambda _: setattr(self, "prefs", None))
            self.prefs.show_all()
        self.prefs.present()

    def toggle(self):
        if self.window.get_visible():
            self.window.hide()
        else:
            self.show()

    def show(self):
        if not self.window.get_visible():
            self.window.show_all()
            self.interactive = None
            self._run_at(FPS)

    def _save_soon(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(500, self._save)

    def _save(self):
        self._save_id = 0
        config.save(self.settings)
        return GLib.SOURCE_REMOVE

    def shutdown(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
        config.save(self.settings)
        self.capture.stop()

    # ---- Spotify -------------------------------------------------------

    def _on_track(self):
        self._update_label()
        self._run_at(FPS)

    def _on_cover(self, image):
        self.cover = image
        if self.disc:
            self.disc.set_cover(image)
        self._run_at(FPS)

    # ---- drawing -------------------------------------------------------

    def _realize(self, area):
        area.make_current()
        if area.get_error():
            print("vinyl: no OpenGL:", area.get_error().message)
            return
        self.disc = Disc(_gl_context(), self.cover)

    def _unrealize(self, area):
        area.make_current()
        if self.disc:
            self.disc.release()
            self.disc = None

    def _render(self, area, _context):
        if self.disc:
            self.disc.ctx.detect_framebuffer().use()  # the GLArea's own framebuffer
            scale = area.get_scale_factor()
            size = (area.get_allocated_width() * scale, area.get_allocated_height() * scale)
            self.disc.render(size, scale, self.layout, self.view)
        return True

    def _run_at(self, fps):
        if fps != self._fps:
            if self._timer:
                GLib.source_remove(self._timer)
            self._fps = fps
            self._timer = GLib.timeout_add(1000 // fps, self._frame)

    def _frame(self):
        now = time.monotonic()
        dt = min(now - self._last, 0.1)
        self._last = now
        s, v, sp = self.settings, self.view, self.spotify

        # Like a turntable: a gentle spin-up on play, a longer coast down on pause.
        target = s["rpm"] * math.tau / 60 if sp.playing else 0.0
        self.speed = _ease(self.speed, target, dt, 0.6 if target > self.speed else 1.5)
        v.rot = (v.rot + self.speed * dt) % math.tau

        # The tonearm follows the track inward, lifts on pause, parks with no track.
        on_record = sp.running and sp.track.length > 0
        targets = {
            "arm": arm_angle(sp.progress) if on_record else ARM_REST,
            "lift": 0.0 if sp.playing else 1.0,
            "label": 1.0 if s["style"] == "label" else 0.0,
            "hover": 1.0 if self.over_center and not self._dragging else 0.0,
            "opacity": 1.0 if sp.running or not s["hide_when_closed"] else 0.0,
        }
        taus = {"arm": 0.4, "lift": 0.25, "label": 0.25, "hover": 0.08, "opacity": 0.3}
        for key, goal in targets.items():
            setattr(v, key, _ease(getattr(v, key), goal, dt, taus[key]))
        v.playing = sp.playing

        self.analyzer.update(self.capture.latest(), dt)
        v.bands = self.analyzer.levels
        v.scale = 1.0 + PULSE_MAX * s["pulse"] * self.analyzer.pulse
        if self.disc:
            self.disc.update(dt)

        show_info = {"always": 1.0, "hover": float(self.over_record), "off": 0.0}[s["track_info"]]
        self.info = _ease(self.info, show_info, dt, 0.15)
        self.label.set_opacity(self.info * v.opacity)
        self._update_input()
        self.area.queue_render()

        moving = any(abs(getattr(v, k) - goal) > 1e-3 for k, goal in targets.items())
        busy = (sp.playing or self.speed > 1e-3 or moving or not self.analyzer.quiet
                or abs(self.info - show_info) > 1e-3 or (self.disc and not self.disc.settled))
        fps = FPS if busy else IDLE_FPS
        if fps != self._fps:
            self._timer = 0
            self._fps = 0
            self._run_at(fps)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    # ---- input ---------------------------------------------------------

    @property
    def _dragging(self):
        return bool(self._press and self._press["moved"])

    def _button_press(self, _area, event):
        if event.type != Gdk.EventType.BUTTON_PRESS:  # skip GTK's extra double-click events
            return True
        if event.button == 3:
            self._show_menu(event)
        elif event.button == 1:
            s = self.settings
            self._press = {"x": event.x, "y": event.y, "cursor": hypr.cursor(),
                           "pos": (s["x"], s["y"]), "monitor": self._monitor(), "moved": False}
        return True

    def _motion(self, _area, event):
        reach = self.layout.reach(event.x, event.y)
        self.over_record = reach <= 1.0
        self.over_center = reach <= CENTER
        if self._press:
            self._drag(event)
        if self._dragging:
            self._set_cursor("grabbing")
        else:
            self._set_cursor("pointer" if self.over_center else "grab" if self.over_record else None)
        self._run_at(FPS)
        return True

    def _drag(self, event):
        p, s = self._press, self.settings
        if not p["moved"]:
            if math.hypot(event.x - p["x"], event.y - p["y"]) < 4:
                return
            p["moved"] = True
        now = hypr.cursor()
        if p["cursor"] and now:
            # Hyprland reports the cursor on screen, so the move can't feed back on itself.
            start = p["monitor"]
            gx = start["x"] + p["pos"][0] + now[0] - p["cursor"][0]
            gy = start["y"] + p["pos"][1] + now[1] - p["cursor"][1]
            mon = self._monitor(at=now)  # dragging onto another screen moves it there
            s["monitor"] = mon["name"]
            s["x"], s["y"] = round(gx - mon["x"]), round(gy - mon["y"])
            self._place(mon)
        else:  # elsewhere: follow the pointer relative to the overlay
            s["x"] += round(event.x - p["x"])
            s["y"] += round(event.y - p["y"])
            self._place(p["monitor"])

    def _button_release(self, _area, event):
        if event.button == 1 and self._press:
            if self._press["moved"]:
                self._save_soon()
            elif self.layout.reach(event.x, event.y) <= CENTER:
                self.spotify.play_pause()
            self._press = None
            self._motion(_area, event)
        return True

    def _leave(self, _area, _event):
        if not self._press:
            self.over_record = self.over_center = False
            self._set_cursor(None)
        return True

    def _scroll(self, _area, event):
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            steps = -event.get_scroll_deltas()[2]
        else:
            steps = {Gdk.ScrollDirection.UP: 1, Gdk.ScrollDirection.DOWN: -1}.get(event.direction, 0)
        if steps:
            self.change(size=round(self.settings["size"] * 1.06 ** steps))
        return True

    def _key(self, _window, event):
        key = Gdk.keyval_name(event.keyval)
        actions = {
            "space": self.spotify.play_pause,
            "Right": self.spotify.next,
            "Left": self.spotify.previous,
            "plus": lambda: self.change(size=round(self.settings["size"] * 1.1)),
            "equal": lambda: self.change(size=round(self.settings["size"] * 1.1)),
            "minus": lambda: self.change(size=round(self.settings["size"] / 1.1)),
        }
        if key in actions:
            actions[key]()
            return True
        return False

    def _set_cursor(self, name):
        if name == self._cursor or not self.area.get_window():
            return
        self._cursor = name
        cursor = Gdk.Cursor.new_from_name(self.area.get_display(), name) if name else None
        self.area.get_window().set_cursor(cursor)

    def _show_menu(self, event):
        s, sp = self.settings, self.spotify
        menu = Gtk.Menu()

        def add(label, action, checked=None):
            item = (Gtk.CheckMenuItem(label=label, active=checked) if checked is not None
                    else Gtk.MenuItem(label=label))
            item.connect("activate", lambda _: action())
            menu.append(item)

        if sp.running:
            add("Pause" if sp.playing else "Play", sp.play_pause)
            add("Next track", sp.next)
            add("Previous track", sp.previous)
            menu.append(Gtk.SeparatorMenuItem())
        add("Black vinyl", lambda: self.change(style="picture" if s["style"] == "label" else "label"),
            checked=s["style"] == "label")
        add("Tonearm", lambda: self.change(tonearm=not s["tonearm"]), checked=s["tonearm"])
        add("Above windows", lambda: self.change(layer="bottom" if s["layer"] == "top" else "top"),
            checked=s["layer"] == "top")
        add("Settings…", self.open_settings)
        menu.append(Gtk.SeparatorMenuItem())
        add("Hide", self.toggle)
        add("Quit", self.app.quit)
        menu.attach_to_widget(self.area, None)
        menu.show_all()
        menu.popup_at_pointer(event)
        self._menu = menu  # keep it alive while it's open


def _ease(value, target, dt, tau):
    value += (target - value) * (1 - math.exp(-dt / tau))
    return target if abs(target - value) < 1e-4 else value


def _font_px(radius):
    return max(10.0, min(20.0, radius * 0.075))


def _circle(cx, cy, r, strip=2):
    """A circle as a stack of thin rectangles, for the input region."""
    rects = []
    y = math.floor(cy - r)
    while y < cy + r:
        nearest = 0 if y <= cy <= y + strip else min(abs(y - cy), abs(y + strip - cy))
        half = math.sqrt(max(0.0, r * r - nearest * nearest))
        rects.append(cairo.RectangleInt(math.floor(cx - half), y, math.ceil(2 * half) + 1, strip))
        y += strip
    return cairo.Region(rects)


def _gdk_monitor(mon):
    display = Gdk.Display.get_default()
    for i in range(display.get_n_monitors()):
        monitor = display.get_monitor(i)
        geo = monitor.get_geometry()
        if (geo.x, geo.y) == (mon["x"], mon["y"]):
            return monitor
    return None


def _gl_context():
    import moderngl
    try:
        return moderngl.create_context()
    except Exception:  # a GL context moderngl can't detect on its own: say it's EGL
        return moderngl.create_context(backend="egl")


def main():
    args = parser().parse_args()  # --help and bad options are handled here, before the app starts
    if args.with_spotify:  # hand over to the small watcher, freeing everything loaded here
        follow = os.path.join(os.path.dirname(os.path.abspath(__file__)), "follow.py")
        os.execv(sys.executable, [sys.executable, follow, "--player", args.player,
                                  "--audio", args.audio])
    app = App()
    try:
        from gi.repository import GLibUnix
        add_signal = GLibUnix.signal_add
    except ImportError:  # PyGObject before 3.52
        add_signal = GLib.unix_signal_add
    for sig in (signal.SIGINT, signal.SIGTERM):
        add_signal(GLib.PRIORITY_DEFAULT, sig, lambda *_: app.quit())
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
