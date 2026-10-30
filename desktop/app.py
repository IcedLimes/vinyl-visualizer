"""The overlay for Windows and macOS: a frameless, see-through window holding
the record. The record is drawn off screen with the same shader as on Linux
(disc.frag) and shown as an image; clicks pass through everywhere but the
record itself, and it never takes the keyboard."""

import argparse
import getpass
import math
import sys
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import (QAction, QActionGroup, QColor, QCursor, QFont, QFontMetrics,
                           QGuiApplication, QIcon, QImage, QPainter, QPen, QPixmap)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon, QWidget

import settings as config
from audio import Analyzer, display_delay
from desktop import native
from desktop.player import open_player
from desktop.prefs import Prefs
from disc import ARM_REST, PULSE_MAX, Disc, Layout, View, arm_angle

CENTER = 0.3                    # the click-to-pause area, in record radii
MIN_SIZE, MAX_SIZE = 120, 1400
EDGE_GAP = 48                   # first placement: this far in from the right edge
FPS, IDLE_FPS = 60, 10
POINTER_HZ = 30                 # how often to check whether the pointer is over the record
SERVER = f"vinyl-visualizer-{getpass.getuser()}"


def parser():
    ap = argparse.ArgumentParser(prog="Vinyl", description=__doc__)
    ap.add_argument("--settings", action="store_true", help="open the settings window")
    ap.add_argument("--reset", action="store_true", help="default settings and position")
    ap.add_argument("--hide", action="store_true", help="hide the running overlay")
    ap.add_argument("--quit", action="store_true", help="close the running overlay")
    ap.add_argument("--audio", choices=["spotify", "system"], default="spotify",
                    help="react to Spotify only (default; macOS) or everything you hear")
    ap.add_argument("--self-test", metavar="FILE", help=argparse.SUPPRESS)
    return ap


def open_capture(source):
    if sys.platform == "win32":
        from desktop.capture_windows import LoopbackCapture
        return LoopbackCapture(source)
    if sys.platform == "darwin":
        from desktop.capture_macos import SpotifyCapture
        return SpotifyCapture(source)
    from audio import Capture
    return Capture(source)


class Renderer:
    """Draws the record into an off-screen framebuffer and reads it back."""

    def __init__(self):
        import moderngl
        try:
            self.ctx = moderngl.create_context(standalone=True, require=330)
        except Exception:
            if not sys.platform.startswith("linux"):
                raise
            self.ctx = moderngl.create_context(standalone=True, require=330, backend="egl")
        self.disc = Disc(self.ctx)
        self.fbo = None
        self._pixels = None  # the newest frame's bytes, kept alive under its QImage

    def render(self, size, scale, layout, view):
        if self.fbo is None or self.fbo.size != size:
            if self.fbo is not None:
                self.fbo.release()
            self.fbo = self.ctx.simple_framebuffer(size, components=4)
        self.fbo.use()
        self.disc.render(size, scale, layout, view)
        w, h = size
        rows = np.frombuffer(self.fbo.read(components=4, alignment=1), np.uint8).reshape(h, w * 4)
        self._pixels = np.ascontiguousarray(rows[::-1])  # OpenGL counts rows from the bottom
        image = QImage(self._pixels.data, w, h, w * 4, QImage.Format.Format_RGBA8888_Premultiplied)
        image.setDevicePixelRatio(scale)
        return image


class Overlay(QWidget):
    def __init__(self, app, args):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.NoDropShadowWindowHint)
        super().__init__(None, flags)
        self.app = app
        self.setWindowTitle("Vinyl")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setMouseTracking(True)

        self.settings = config.load()
        self.view = View()
        self.lay = None           # the record's Layout
        self.image = None         # the newest frame
        self.prefs = None
        self.speed = 0.0          # platter speed, rad/s
        self.info = 0.0           # track info opacity
        self.over_record = False
        self.over_center = False
        self._through = None      # clicks pass through (see _poll_pointer)
        self._menu_open = False
        self._press = None        # left-button drag in progress
        self._cover = None
        self._new_cover = False
        self._last = time.monotonic()

        try:
            self.renderer = Renderer()
        except Exception as e:
            self.renderer = None
            problem = f"Vinyl needs OpenGL 3.3 to draw the record, which this computer's " \
                      f"graphics driver doesn't offer.\n\n{e}"
            print(f"vinyl: {problem}")
            QTimer.singleShot(0, lambda: QMessageBox.critical(None, "Vinyl", problem))
        self.analyzer = Analyzer()
        self.capture = open_capture(args.audio)
        self.capture.start()
        self.player = open_player()
        self.player.changed.connect(self._on_track)
        self.player.cover.connect(self._on_cover)

        self._apply()
        self.show()
        self._timer = QTimer(self, timerType=Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._frame)
        self._run_at(FPS)
        self._pointer = QTimer(self)
        self._pointer.timeout.connect(self._poll_pointer)
        self._pointer.start(1000 // POINTER_HZ)
        self.tray = self._build_tray()

    # ---- window --------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        native.prepare(self)
        native.set_layer(self, self.settings["layer"])
        self._through = None

    def _apply(self):
        """Lays out, places and colors the overlay from the settings."""
        s = self.settings
        old = self.lay
        radius = s["size"] / 2
        self._text_height = 0 if s["track_info"] == "off" else round(_font_px(radius) * 2.9) + 6
        self.lay = lay = Layout.for_record(s["size"], s["tonearm"], self._text_height)
        if old and s["x"] is not None:  # keep the record's centre where it was on screen
            s["x"] += round(old.cx - lay.cx)
            s["y"] += round(old.cy - lay.cy)
        self.setFixedSize(lay.width, lay.height)
        self._place()
        if self.isVisible():
            native.set_layer(self, s["layer"])
        v = self.view
        v.dark, v.light, v.accent = (config.rgb(s[k]) for k in ("dark", "light", "accent"))
        v.ring = s["visualizer"]
        v.arm_show = 1.0 if s["tonearm"] else 0.0

    def _screen(self, at=None):
        if at is not None:
            screen = QGuiApplication.screenAt(at)
            if screen:
                return screen
        name = self.settings["monitor"]
        return (next((sc for sc in QGuiApplication.screens() if sc.name() == name), None)
                or QGuiApplication.primaryScreen())

    def _place(self, screen=None):
        """Moves the overlay to settings x/y on its screen, kept on screen."""
        s, lay = self.settings, self.lay
        screen = screen or self._screen()
        s["monitor"] = screen.name()
        geo = screen.geometry()
        if s["x"] is None or s["y"] is None:
            s["x"] = geo.width() - lay.width - EDGE_GAP
            s["y"] = (geo.height() - lay.height) // 2
        s["x"] = max(0, min(s["x"], geo.width() - lay.width))
        s["y"] = max(0, min(s["y"], geo.height() - lay.height))
        self.move(geo.x() + s["x"], geo.y() + s["y"])

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
            self.lay = None  # no centre to keep
        self.settings = dict(config.defaults(), **kept)
        self.change()

    def open_settings(self):
        if not self.prefs:
            self.prefs = Prefs(self)
            self.prefs.destroyed.connect(lambda: setattr(self, "prefs", None))
        self.prefs.show()
        self.prefs.raise_()
        self.prefs.activateWindow()

    def toggle(self):
        self.setVisible(not self.isVisible())
        self._run_at(FPS)

    def _save_soon(self):
        if not hasattr(self, "_saver"):
            self._saver = QTimer(self, singleShot=True, interval=500)
            self._saver.timeout.connect(lambda: config.save(self.settings))
        self._saver.start()

    def shutdown(self):
        config.save(self.settings)
        self.capture.stop()
        self.player.stop()

    # ---- Spotify -------------------------------------------------------

    def _on_track(self):
        self._run_at(FPS)
        self.update()

    def _on_cover(self, image):
        self._cover = image
        self._new_cover = True
        self._run_at(FPS)

    def _follow(self):
        """What the pulse follows for the current song: its own choice, else the default."""
        return self.settings["follow_tracks"].get(self.player.track.id, self.settings["follow"])

    def _set_follow(self, mode):
        track = self.player.track.id
        if track:
            self.change(follow_tracks=dict(self.settings["follow_tracks"], **{track: mode}))

    # ---- drawing -------------------------------------------------------

    def _run_at(self, fps):
        interval = 1000 // fps
        if self._timer.interval() != interval or not self._timer.isActive():
            self._timer.start(interval)

    def _frame(self):
        now = time.monotonic()
        dt = min(now - self._last, 0.1)
        self._last = now
        s, v, sp = self.settings, self.view, self.player

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

        # Show the music as it's heard (see audio.display_delay).
        delay = display_delay(self.capture.output_latency, s["sync_offset"] / 1000)
        self.analyzer.mode = self._follow()
        self.analyzer.vocals_first = s["vocals_first"]
        self.analyzer.process(self.capture, now, lookahead=delay > 0.06)
        v.bands = self.analyzer.levels_at(now - delay)
        v.scale = 1.0 + PULSE_MAX * s["pulse"] * self.analyzer.pulse_at(now - delay)

        show_info = {"always": 1.0, "hover": float(self.over_record), "off": 0.0}[s["track_info"]]
        self.info = _ease(self.info, show_info, dt, 0.15)

        if self.renderer and self.isVisible():
            disc = self.renderer.disc
            if self._new_cover:
                disc.set_cover(self._cover)
                self._new_cover = False
            disc.update(dt)
            scale = self.devicePixelRatioF()
            size = (max(1, round(self.lay.width * scale)), max(1, round(self.lay.height * scale)))
            self.image = self.renderer.render(size, scale, self.lay, v)
            self.update()

        moving = any(abs(getattr(v, k) - goal) > 1e-3 for k, goal in targets.items())
        settling = self.renderer and not self.renderer.disc.settled
        busy = (sp.playing or self.speed > 1e-3 or moving or not self.analyzer.quiet
                or abs(self.info - show_info) > 1e-3 or settling)
        self._run_at(FPS if busy else IDLE_FPS)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if self.image is not None:
            p.drawImage(0, 0, self.image)
        self._draw_info(p)
        p.end()

    def _draw_info(self, p):
        """Title and artist under the record."""
        track, lay = self.player.track, self.lay
        alpha = self.info * self.view.opacity
        if not track.title or not self._text_height or alpha < 0.01:
            return
        px = _font_px(lay.radius)
        width = round(2.4 * lay.radius)
        left = round(lay.cx - width / 2)
        accent = QColor(self.settings["accent"])
        title_font = QFont(self.font())
        title_font.setPixelSize(round(px))
        artist_font = QFont(self.font())
        artist_font.setPixelSize(max(8, round(px * 0.78)))
        artist_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px * 0.12)
        title_metrics, artist_metrics = QFontMetrics(title_font), QFontMetrics(artist_font)
        bottom = lay.height - 2
        artist_top = bottom - artist_metrics.height()
        title_top = artist_top - title_metrics.height()
        center = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        for font, metrics, text, top, strength in (
                (title_font, title_metrics, track.title, title_top, 0.88),
                (artist_font, artist_metrics, track.artist.upper(), artist_top, 0.5)):
            color = QColor(accent)
            color.setAlphaF(strength * alpha)
            p.setFont(font)
            p.setPen(color)
            elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)
            p.drawText(QRect(left, top, width, metrics.height()), center, elided)

    # ---- input ---------------------------------------------------------

    @property
    def _dragging(self):
        return bool(self._press and self._press["moved"])

    def _poll_pointer(self):
        """Lets clicks through everywhere but the record. Windows and macOS
        give a window all of its clicks or none, so this watches the pointer
        and switches as it crosses the record's edge."""
        if not self.isVisible():
            return
        pos = self.mapFromGlobal(QCursor.pos())
        reach = self.lay.reach(pos.x(), pos.y())
        room = 1 + PULSE_MAX * self.settings["pulse"]  # it may be swollen with the beat
        shown = self.view.opacity > 0.05
        take = shown and (reach <= room or self._press is not None or self._menu_open)
        if self._through != (not take):
            self._through = not take
            native.set_click_through(self, not take)
        over_record = shown and reach <= 1.0
        over_center = shown and reach <= CENTER
        if (over_record, over_center) != (self.over_record, self.over_center):
            self.over_record, self.over_center = over_record, over_center
            self._run_at(FPS)
        if self._dragging:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif over_center:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        elif over_record:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.globalPosition().toPoint())
        elif event.button() == Qt.MouseButton.LeftButton:
            self._press = {"at": event.globalPosition().toPoint(), "pos": self.pos(),
                           "moved": False}
            native.note_front_app()

    def mouseMoveEvent(self, event):
        p = self._press
        if not p:
            return
        delta = event.globalPosition().toPoint() - p["at"]
        if not p["moved"]:
            if math.hypot(delta.x(), delta.y()) < 4:
                return
            p["moved"] = True
        top_left = p["pos"] + delta
        center = top_left + QPoint(round(self.lay.cx), round(self.lay.cy))
        screen = self._screen(at=center)  # dragging onto another screen moves it there
        geo = screen.geometry()
        self.settings["x"], self.settings["y"] = top_left.x() - geo.x(), top_left.y() - geo.y()
        self._place(screen)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._press:
            return
        if self._press["moved"]:
            self._save_soon()
        else:
            pos = event.position()
            if self.lay.reach(pos.x(), pos.y()) <= CENTER:
                self.player.play_pause()
        self._press = None
        native.give_back_front_app()
        self._poll_pointer()

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120
        if steps:
            self.change(size=round(self.settings["size"] * 1.06 ** steps))

    # ---- menus ---------------------------------------------------------

    def _fill_menu(self, menu, tray=False):
        s, sp = self.settings, self.player

        def add(label, action, checked=None, enabled=True):
            item = QAction(label, menu)
            if checked is not None:
                item.setCheckable(True)
                item.setChecked(checked)
            item.setEnabled(enabled)
            # after the menu has closed and handed the focus back (see _show_menu)
            item.triggered.connect(lambda *_: QTimer.singleShot(0, action))
            menu.addAction(item)
            return item

        if tray:
            add("Hide Vinyl" if self.isVisible() else "Show Vinyl", self.toggle)
            menu.addSeparator()
        if sp.running:
            add("Pause" if sp.playing else "Play", sp.play_pause)
            add("Next track", sp.next)
            add("Previous track", sp.previous)
            menu.addSeparator()
        if sp.track.id:  # remembered for this song only
            group = QActionGroup(menu)
            for label, mode in (("This song pulses on hits", "hits"),
                                ("This song pulses on every note", "notes")):
                item = add(label, lambda m=mode: self._set_follow(m), checked=self._follow() == mode)
                group.addAction(item)
            menu.addSeparator()
        add("Vocals first", lambda: self.change(vocals_first=not s["vocals_first"]),
            checked=s["vocals_first"])
        add("Black vinyl", lambda: self.change(style="picture" if s["style"] == "label" else "label"),
            checked=s["style"] == "label")
        add("Tonearm", lambda: self.change(tonearm=not s["tonearm"]), checked=s["tonearm"])
        add("Above windows", lambda: self.change(layer="bottom" if s["layer"] == "top" else "top"),
            checked=s["layer"] == "top")
        login = native.starts_at_login()
        if login is not None:
            add("Start at login", lambda: native.set_starts_at_login(not login), checked=login)
        menu.addSeparator()
        add("Settings…", self.open_settings)
        if not tray:
            add("Hide", self.toggle)
        add("Quit", self.app.quit)

    def _show_menu(self, at):
        """The record never takes focus, but a menu needs it while it's open:
        without it, a click elsewhere wouldn't close it. So it takes it for
        the menu and hands it back to your window afterwards."""
        menu = QMenu(self)
        self._fill_menu(menu)
        self._menu_open = True
        menu.aboutToHide.connect(lambda: setattr(self, "_menu_open", False))
        give_back = native.focus_for_menu(self)
        menu.exec(at)
        give_back()

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(tray_icon(), self)
        tray.setToolTip("Vinyl")
        menu = QMenu()
        menu.aboutToShow.connect(lambda: (menu.clear(), self._fill_menu(menu, tray=True)))
        tray.setContextMenu(menu)
        self._tray_menu = menu  # the tray doesn't keep it alive
        tray.activated.connect(lambda reason: self.toggle()
                               if reason == QSystemTrayIcon.ActivationReason.Trigger
                               and sys.platform == "win32" else None)
        tray.show()
        return tray


ICON = Path(__file__).resolve().parent.parent / "packaging" / "icon.png"


def app_icon():
    return QIcon(str(ICON)) if ICON.exists() else tray_icon()


def tray_icon():
    """A record cut out of one gray, which reads on light and dark taskbars;
    on macOS it's a template the menu bar tints."""
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#a6a6a6") if sys.platform != "darwin" else QColor("black"))
    p.drawEllipse(QPoint(32, 32), 30, 30)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    p.setBrush(QColor("black"))
    p.drawEllipse(QPoint(32, 32), 12, 12)  # the label, cut out
    for r in (25, 20):                      # two song gaps
        p.setPen(QPen(QColor("black"), 1.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPoint(32, 32), r, r)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#a6a6a6") if sys.platform != "darwin" else QColor("black"))
    p.drawEllipse(QPoint(32, 32), 3, 3)    # spindle
    p.end()
    icon = QIcon(pix)
    if sys.platform == "darwin":
        icon.setIsMask(True)
    return icon


def _ease(value, target, dt, tau):
    value += (target - value) * (1 - math.exp(-dt / tau))
    return target if abs(target - value) < 1e-4 else value


def _font_px(radius):
    return max(10.0, min(20.0, radius * 0.075))


# ---- one copy at a time ------------------------------------------------

def _hand_over(argv):
    """Passes the options to a copy that's already running. True if there was one."""
    sock = QLocalSocket()
    sock.connectToServer(SERVER)
    if not sock.waitForConnected(500):
        return False
    sock.write(("\n".join(argv) + "\n").encode())
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def _serve(overlay):
    QLocalServer.removeServer(SERVER)  # left behind if a copy crashed
    server = QLocalServer(overlay)
    server.listen(SERVER)

    def connected():
        sock = server.nextPendingConnection()
        sock.readyRead.connect(lambda: _handle(overlay, bytes(sock.readAll()).decode().split()))

    server.newConnection.connect(connected)
    return server


def _handle(overlay, argv):
    try:
        args = parser().parse_args(argv)
    except SystemExit:
        return
    if args.quit:
        overlay.app.quit()
        return
    if args.reset:
        overlay.reset_settings(position=True)
    if args.settings:
        overlay.open_settings()
    if args.hide:
        overlay.hide()
    elif not overlay.isVisible():
        overlay.show()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(argv)
    if args.self_test:
        from desktop.selftest import run
        return run(args.self_test)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Vinyl")
    app.setQuitOnLastWindowClosed(False)  # closing the settings window isn't quitting
    app.setWindowIcon(app_icon())
    if _hand_over(argv):
        return 0
    if args.quit or args.hide:
        return 0  # nothing running to close
    overlay = Overlay(app, args)
    server = _serve(overlay)
    app.aboutToQuit.connect(overlay.shutdown)
    if args.reset:
        overlay.reset_settings(position=True)
    if args.settings:
        overlay.open_settings()
    if sys.platform == "darwin":  # opening the app again while it runs just activates it
        app.applicationStateChanged.connect(
            lambda state: overlay.show() if state == Qt.ApplicationState.ApplicationActive
            and not overlay.isVisible() else None)
    # Ctrl+C in a terminal: Qt's loop doesn't run Python's signal handlers on its own.
    import signal
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    ticker = QTimer(interval=250)
    ticker.timeout.connect(lambda: None)
    ticker.start()
    code = app.exec()
    server.close()
    return code
