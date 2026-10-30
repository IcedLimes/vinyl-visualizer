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

try:
    import cairo
    import gi

    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    gi.require_version("Playerctl", "2.0")
    gi.require_foreign("cairo")
except (ImportError, ValueError) as missing:
    sys.exit(f"vinyl: {missing}. Run ./install.sh to see which packages to install.")
try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell
except (ImportError, ValueError):  # no gtk-layer-shell: it runs as a borderless window instead
    GtkLayerShell = None
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import hypr  # noqa: E402
import settings as config  # noqa: E402
from audio import Analyzer, Capture, display_delay  # noqa: E402
from disc import ARM_REST, PULSE_MAX, Disc, Layout, View, arm_angle  # noqa: E402
from dock import Dock  # noqa: E402
from prefs import Prefs  # noqa: E402
from spotify import Spotify  # noqa: E402

APP_ID = "io.github.vinyl_visualizer"
NAMESPACE = "vinyl-visualizer"  # layer-shell namespace, for Hyprland layer rules
CENTER = 0.3                    # the click-to-pause area, in record radii
MIN_SIZE, MAX_SIZE = 120, 1400
EDGE_GAP = 48                   # first placement: this far in from the right edge
FPS, IDLE_FPS = 60, 10
GLIDE = 0.6                     # seconds to glide into (or back out of) a dock


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
        self._new_cover = False   # a cover waiting to be uploaded at the next draw
        self.prefs = None
        self.speed = 0.0          # platter speed, rad/s
        self.info = 0.0           # track info opacity
        self.over_record = False
        self._holding = False     # takes the keyboard on demand (see _hold_pointer)
        self.over_center = False
        self.interactive = None   # what currently takes clicks (see _update_input)
        self._menu_open = False   # the right-click menu is showing
        self._press = None        # left-button drag in progress
        self.dock = Dock()        # a circle on the desktop to drop the record into
        self._glide = None        # size/position animation into or out of the dock
        self._layer_on_drop = None  # a layer change held back until the drag ends
        self._present = None      # last told: in the dock and on screen
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
        if _layer_shell():
            GtkLayerShell.init_for_window(win)
            GtkLayerShell.set_namespace(win, NAMESPACE)
            GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.LEFT, True)
            GtkLayerShell.set_exclusive_zone(win, -1)  # place it by the screen, not around bars
            # Don't take the keyboard: keys stay with the window you're using.
            # (Taking it on a click kept it: Hyprland lets layers hold on to
            # keyboard focus, so clicks on your windows stopped focusing them.)
            # Only while it's pointed at or dragged, and given back (see
            # _hold_pointer).
            if hasattr(GtkLayerShell, "set_keyboard_mode"):  # gtk-layer-shell 0.6+
                GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
        else:  # no layer shell (GNOME, X11): a bare window kept on top
            win.set_decorated(False)
            win.set_keep_above(True)
            win.set_skip_taskbar_hint(True)
            win.set_skip_pager_hint(True)
            win.set_accept_focus(False)  # clicks work, but keys stay with your window
            win.set_focus_on_map(False)

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
        if _layer_shell():
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
        if _layer_shell():
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
        While hidden, nothing does; while the menu is open, all of it does."""
        interactive = self.view.opacity > 0.05
        state = (interactive, self._menu_open)
        if state == self.interactive:
            return
        self.interactive = state
        lay = self.layout
        if self._menu_open:
            region = cairo.Region(cairo.RectangleInt(0, 0, lay.width, lay.height))
        elif interactive:
            reach = lay.radius * (1 + PULSE_MAX * self.settings["pulse"]) + 1  # room to swell
            region = _circle(lay.cx, lay.cy, reach)
        else:
            region = cairo.Region()
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
        keep = ("follow_tracks",) if position else ("x", "y", "monitor", "follow_tracks")
        kept = {k: self.settings[k] for k in keep}  # per-song choices always survive a reset
        if position:
            self.layout = None  # no centre to keep
        self.settings = dict(config.defaults(), **kept)
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
            self._sync_presence()
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
        if self._present:
            self.dock.set_present(False)
        if self._save_id:
            GLib.source_remove(self._save_id)
        config.save(self.settings)
        self.capture.stop()

    # ---- Spotify -------------------------------------------------------

    def _on_track(self):
        self._update_label()
        self._run_at(FPS)

    def _on_cover(self, image):
        # Uploaded at the next draw, where the GLArea's context is current: GL
        # calls made here would land in whatever context GTK left current (its
        # compositor's), which showed as a black square for a frame or two.
        self.cover = image
        self._new_cover = True
        self._run_at(FPS)

    # ---- drawing -------------------------------------------------------

    def _realize(self, area):
        area.make_current()
        if area.get_error():
            print("vinyl: no OpenGL:", area.get_error().message)
            return
        self.disc = Disc(_gl_context(), self.cover)
        self._new_cover = False

    def _unrealize(self, area):
        area.make_current()
        if self.disc:
            self.disc.release()
            self.disc = None

    def _render(self, area, _context):
        if self.disc:
            if self._new_cover:
                self.disc.set_cover(self.cover)
                self._new_cover = False
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
        self._sync_presence(targets["opacity"] > 0.5)
        taus = {"arm": 0.4, "lift": 0.25, "label": 0.25, "hover": 0.08, "opacity": 0.3}
        for key, goal in targets.items():
            setattr(v, key, _ease(getattr(v, key), goal, dt, taus[key]))
        v.playing = sp.playing

        # Show the music as it's heard: Bluetooth output plays it ~100-300 ms
        # after we capture it, so the ring and the pulse wait for it.
        delay = display_delay(self.capture.output_latency, s["sync_offset"] / 1000)
        self.analyzer.mode = self._follow()
        self.analyzer.vocals_first = s["vocals_first"]
        self.analyzer.process(self.capture, now, lookahead=delay > 0.06)
        v.bands = self.analyzer.levels_at(now - delay)
        v.scale = 1.0 + PULSE_MAX * s["pulse"] * self.analyzer.pulse_at(now - delay)
        if self.disc:
            self.disc.update(dt)

        show_info = {"always": 1.0, "hover": float(self.over_record), "off": 0.0}[s["track_info"]]
        self.info = _ease(self.info, show_info, dt, 0.15)
        self.label.set_opacity(self.info * v.opacity)
        self._update_input()
        self.area.queue_render()

        moving = any(abs(getattr(v, k) - goal) > 1e-3 for k, goal in targets.items())
        if self._glide:
            self._step_glide(now)
        busy = (sp.playing or self.speed > 1e-3 or moving or not self.analyzer.quiet or self._glide
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
            s, lay, mon = self.settings, self.layout, self._monitor()
            self._press = {"x": event.x, "y": event.y, "cursor": hypr.cursor(),
                           "pos": (s["x"], s["y"]), "monitor": mon, "moved": False,
                           # the record's middle on screen; dragging moves it, so a
                           # size change mid-drag keeps the record under the pointer
                           "center": (mon["x"] + s["x"] + lay.cx, mon["y"] + s["y"] + lay.cy)}
        return True

    def _motion(self, _area, event):
        reach = self.layout.reach(event.x, event.y)
        self.over_record = reach <= 1.0
        self.over_center = reach <= CENTER
        self._hold_pointer(self.over_record or bool(self._press))
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
            if not _layer_shell():  # a plain window: let the desktop move it
                self.window.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
                self._press = None
                return
            if self.dock.spec:
                self.dock.tell("grab")
                p["told_dock"] = True
            GLib.timeout_add(16, self._catch_up)
        self._drag_to(hypr.cursor(), event)

    def _catch_up(self):
        """While dragging, follow the cursor every frame even when no motion
        reaches the record (a fast flick can outrun it for a moment)."""
        if not self._dragging:
            return GLib.SOURCE_REMOVE
        now = hypr.cursor()
        if now and now != self._press.get("last"):
            self._drag_to(now, None)
        return GLib.SOURCE_CONTINUE

    def _drag_to(self, now, event):
        """Put the record's middle where the drag has taken it."""
        p, s = self._press, self.settings
        if p["cursor"] and now:
            p["last"] = now
            # Hyprland reports the cursor on screen, so the move can't feed back on itself.
            gx = p["center"][0] + now[0] - p["cursor"][0]
            gy = p["center"][1] + now[1] - p["cursor"][1]
            mon = self._monitor(at=now)  # dragging onto another screen moves it there
            s["monitor"] = mon["name"]
            lx, ly = gx - mon["x"], gy - mon["y"]   # the record's middle on this screen
            s["x"], s["y"] = round(lx - self.layout.cx), round(ly - self.layout.cy)
            self._place(mon)
            self._track_dock(mon["name"], lx, ly)
        elif event:  # elsewhere: follow the pointer relative to the overlay
            s["x"] += round(event.x - p["x"])
            s["y"] += round(event.y - p["y"])
            self._place(p["monitor"])
            self._track_dock(p["monitor"]["name"], s["x"] + self.layout.cx, s["y"] + self.layout.cy)

    def _button_release(self, _area, event):
        if event.button == 1 and self._press:
            if self._press["moved"]:
                self._drop()
            elif self.layout.reach(event.x, event.y) <= CENTER:
                self.spotify.play_pause()
            self._press = None
            self._hold_pointer(False)
            self._motion(_area, event)
        return True

    def _hold_pointer(self, hold):
        """Hyprland keeps the pointer on the surface you pressed while the
        button is held, but only if some surface has the keyboard: with
        nothing focused (an empty desktop) a fast drag outran the record and
        it slipped out from under the cursor. So the record takes the keyboard
        on demand while the pointer is over it or dragging it, which makes a
        press focus it, and gives it back (Hyprland refocuses your window)
        once it's let go or the pointer leaves."""
        if not _layer_shell() or not hasattr(GtkLayerShell, "set_keyboard_mode") or self._holding == hold:
            return
        self._holding = hold
        mode = GtkLayerShell.KeyboardMode.ON_DEMAND if hold else GtkLayerShell.KeyboardMode.NONE
        GtkLayerShell.set_keyboard_mode(self.window, mode)

    # ---- dock: a circle on the desktop to drop the record into ------------

    def _track_dock(self, monitor, x, y):
        """While dragging: tells the desktop when the record's middle enters or
        leaves the dock, and gives the record back its size when it's pulled out."""
        d = self.dock
        inside = d.contains(monitor, x, y)
        if inside != d.hovering:
            d.hovering = inside
            d.tell("hover" if inside else "leave")
        if d.docked and not inside:
            self._undock()

    def _sync_presence(self, shown=None):
        """Tells the desktop when the record starts or stops sitting in the dock
        on screen (Rei shows only then): docked, its window up, and not faded
        out for want of Spotify."""
        if shown is None:
            shown = self.spotify.running or not self.settings["hide_when_closed"]
        present = bool(self.dock.docked and self.dock.spec and self.window.get_visible() and shown)
        if present != self._present:
            self._present = present
            self.dock.set_present(present)

    def _undock(self):
        """Pulled out of the dock: back to the size, layer, ring and repeat mode
        it had before. The layer changes when it's let go: switching layers
        while the button is held loses the drag."""
        d = self.dock
        d.docked = False
        d.tell("undock")
        before = d.forget()
        self._sync_presence()
        if before.get("layer") in ("top", "bottom"):
            if self._dragging:
                self._layer_on_drop = before["layer"]
            else:
                self.change(layer=before["layer"])
        if before.get("loop"):
            self.spotify.set_loop(before["loop"])
        if before.get("size"):
            self._glide_to(size=before["size"], visualizer=before.get("visualizer"))

    def _drop(self):
        d = self.dock
        layer, self._layer_on_drop = self._layer_on_drop, None  # dropped back in the dock: moot
        if d.hovering and d.spec:
            first = not d.docked
            s = self.settings
            if first:
                d.remember({"size": s["size"], "layer": s["layer"], "visualizer": s["visualizer"],
                            "loop": self.spotify.loop})
            d.docked, d.hovering = True, False
            # moon form: the ring swells as it glides in; under windows once it lands
            self._glide_to(size=d.size, center=d.center(), visualizer=1.0, layer="bottom")
            if first:
                uri = d.spec.get("uri")
                if self.spotify.has_track(uri):   # already on: carry on from where it is
                    if not self.spotify.playing:
                        self.spotify.play()
                else:
                    self.spotify.open_uri(uri)
                self.spotify.set_loop("Track")
                GLib.timeout_add(2000, self._keep_looping)
            d.tell("dock")
            self._sync_presence()
        else:
            if self._press.get("told_dock"):
                d.tell("release")
            d.hovering = False
            if layer and layer != self.settings["layer"]:
                self.change(layer=layer)
            self._save_soon()

    def _keep_looping(self):
        """Once the docked song has started: repeat it, in case opening it reset that."""
        if self.dock.docked:
            self.spotify.set_loop("Track")
        return GLib.SOURCE_REMOVE

    def _glide_to(self, size, center=None, visualizer=None, layer=None):
        """Animates the record's size, its middle to `center` (monitor px) and
        its spectrum ring to `visualizer`; switches to `layer` on landing."""
        s, lay = self.settings, self.layout
        self._glide = {"t0": time.monotonic(), "size0": s["size"], "size1": size,
                       "c0": (s["x"] + lay.cx, s["y"] + lay.cy), "c1": center,
                       "vis0": s["visualizer"], "vis1": visualizer, "layer": layer,
                       "monitor": self._monitor()}
        self._run_at(FPS)

    def _step_glide(self, now):
        g, s = self._glide, self.settings
        t = min(1.0, (now - g["t0"]) / GLIDE)
        e = 1 - (1 - t) ** 3   # ease out: quick start, soft landing
        size = max(MIN_SIZE, min(MAX_SIZE, round(g["size0"] + (g["size1"] - g["size0"]) * e)))
        if size != s["size"]:
            s["size"] = size
            self._apply()   # keeps the record's middle in place
        if g["vis1"] is not None:
            s["visualizer"] = round(g["vis0"] + (g["vis1"] - g["vis0"]) * e, 4)
            self.view.ring = s["visualizer"]
        if g["c1"] and not self._dragging:
            cx = g["c0"][0] + (g["c1"][0] - g["c0"][0]) * e
            cy = g["c0"][1] + (g["c1"][1] - g["c0"][1]) * e
            s["x"], s["y"] = round(cx - self.layout.cx), round(cy - self.layout.cy)
            self._place(g["monitor"])
        if t >= 1.0:
            self._glide = None
            if g["layer"] and g["layer"] != s["layer"]:
                self.change(layer=g["layer"])
            if self.prefs:
                self.prefs.sync()
            self._save_soon()

    def _leave(self, _area, _event):
        if not self._press:
            self.over_record = self.over_center = False
            self._set_cursor(None)
            self._hold_pointer(False)
        return True

    def _scroll(self, _area, event):
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            steps = -event.get_scroll_deltas()[2]
        else:
            steps = {Gdk.ScrollDirection.UP: 1, Gdk.ScrollDirection.DOWN: -1}.get(event.direction, 0)
        if steps:
            self.change(size=round(self.settings["size"] * 1.06 ** steps))
        return True

    def _set_cursor(self, name):
        if name == self._cursor or not self.area.get_window():
            return
        self._cursor = name
        cursor = Gdk.Cursor.new_from_name(self.area.get_display(), name) if name else None
        self.area.get_window().set_cursor(cursor)

    def _follow(self):
        """What the pulse follows for the current song: its own choice, else the default."""
        return self.settings["follow_tracks"].get(self.spotify.track.id, self.settings["follow"])

    def _set_follow(self, mode):
        track = self.spotify.track.id
        if track:
            self.change(follow_tracks=dict(self.settings["follow_tracks"], **{track: mode}))

    def _show_menu(self, event):
        """The right-click menu, drawn inside the overlay as a popover. A
        separate popup menu takes the keyboard, and when it closed Hyprland
        handed focus back to your last window, even a minimized one, which
        flashed every minimized window on screen. A popover takes nothing."""
        pop = Gtk.Popover(relative_to=self.area, modal=True)
        pop.get_style_context().add_class("menu")
        box = self._menu_contents(pop)
        pop.add(box)
        box.show_all()
        natural = box.get_preferred_size()[1]
        lay = self.layout
        if natural.width + 32 > lay.width or natural.height + 32 > lay.height:
            pop.destroy()  # too small a record to hold it: use a popup menu after all
            return self._show_popup_menu(event)
        where = Gdk.Rectangle()
        where.x, where.y = (int(event.x), int(event.y)) if event else (int(lay.cx), int(lay.cy))
        where.width = where.height = 1
        pop.set_pointing_to(where)
        pop.set_position(Gtk.PositionType.BOTTOM)
        pop.connect("closed", self._menu_closed)
        self._menu_open = True
        self._update_input()
        pop.popup()
        self._menu = pop
        self._menu_away = 0
        GLib.timeout_add(250, self._menu_watch)

    def _menu_contents(self, pop):
        s, sp = self.settings, self.spotify

        def run(action):  # close first, then act (some actions resize the overlay)
            pop.popdown()
            GLib.idle_add(lambda: (action(), False)[1])

        def button(action, label=None, icon=None):
            b = Gtk.Button(label=label, relief=Gtk.ReliefStyle.NONE)
            if icon:
                b.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
                b.set_always_show_image(True)
            b.connect("clicked", lambda _: run(action))
            return b

        def check(label, active, action):
            c = Gtk.CheckButton(label=label, active=active)
            c.connect("toggled", lambda _: run(action))
            return c

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=8)
        if sp.running:
            row = Gtk.Box(spacing=2, homogeneous=True)
            row.add(button(sp.previous, icon="media-skip-backward-symbolic"))
            row.add(button(sp.play_pause, icon="media-playback-pause-symbolic" if sp.playing
                           else "media-playback-start-symbolic"))
            row.add(button(sp.next, icon="media-skip-forward-symbolic"))
            box.add(row)
            box.add(Gtk.Separator())
        if sp.track.id:  # remembered for this song only
            row = Gtk.Box(spacing=8)
            row.add(Gtk.Label(label="This song pulses on"))
            linked = Gtk.Box()
            linked.get_style_context().add_class("linked")
            hits = Gtk.RadioButton(label="Hits", draw_indicator=False)
            notes = Gtk.RadioButton(label="Every note", group=hits, draw_indicator=False)
            (notes if self._follow() == "notes" else hits).set_active(True)
            for item, mode in ((hits, "hits"), (notes, "notes")):
                item.connect("toggled", lambda i, m=mode: i.get_active() and run(lambda: self._set_follow(m)))
                linked.add(item)
            row.pack_end(linked, False, False, 0)
            box.add(row)
            box.add(Gtk.Separator())
        box.add(check("Vocals first", s["vocals_first"],
                      lambda: self.change(vocals_first=not s["vocals_first"])))
        box.add(check("Black vinyl", s["style"] == "label",
                      lambda: self.change(style="picture" if s["style"] == "label" else "label")))
        box.add(check("Tonearm", s["tonearm"], lambda: self.change(tonearm=not s["tonearm"])))
        box.add(check("Above windows", s["layer"] == "top",
                      lambda: self.change(layer="bottom" if s["layer"] == "top" else "top")))
        box.add(Gtk.Separator())
        row = Gtk.Box(spacing=2, homogeneous=True)
        row.add(button(self.open_settings, label="Settings…"))
        row.add(button(self.toggle, label="Hide"))
        row.add(button(self.app.quit, label="Quit"))
        box.add(row)
        return box

    def _menu_closed(self, pop):
        self._menu_open = False
        self._update_input()
        GLib.idle_add(lambda: (pop.destroy(), False)[1])

    def _menu_watch(self):
        """Clicks outside the overlay never reach it, so close the menu once the
        pointer has been away from the overlay for about a second."""
        if not self._menu_open:
            return GLib.SOURCE_REMOVE
        cursor, s, lay = hypr.cursor(), self.settings, self.layout
        if cursor:
            mon = self._monitor()
            x, y = cursor[0] - mon["x"] - s["x"], cursor[1] - mon["y"] - s["y"]
            away = not (0 <= x < lay.width and 0 <= y < lay.height)
            self._menu_away = self._menu_away + 1 if away else 0
            if self._menu_away >= 4:
                self._menu.popdown()
                return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _show_popup_menu(self, event):
        """The old popup menu: only for records too small to hold the popover."""
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
        if sp.track.id:  # remembered for this song only
            hits = Gtk.RadioMenuItem(label="This song pulses on hits")
            notes = Gtk.RadioMenuItem(label="This song pulses on every note", group=hits)
            (notes if self._follow() == "notes" else hits).set_active(True)
            for item, mode in ((hits, "hits"), (notes, "notes")):
                item.connect("toggled", lambda i, m=mode: i.get_active() and self._set_follow(m))
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())
        add("Black vinyl", lambda: self.change(style="picture" if s["style"] == "label" else "label"),
            checked=s["style"] == "label")
        add("Vocals first", lambda: self.change(vocals_first=not s["vocals_first"]),
            checked=s["vocals_first"])
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


def _layer_shell():
    """Whether the overlay can be a layer surface: Wayland compositors with
    wlr-layer-shell, such as Hyprland, Sway, KDE Plasma and river."""
    return GtkLayerShell is not None and GtkLayerShell.is_supported()


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
