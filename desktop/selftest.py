"""A quick check that a build has everything it needs, for CI: with
--self-test FILE it writes what it found to FILE and exits 0 if all is well."""

import importlib
import io
import sys
import traceback

import numpy as np


def run(path):
    lines, failed = [], []

    def check(name, test, required=True):
        try:
            lines.append(f"ok    {name}: {test() or ''}")
        except Exception:
            lines.append(f"{'FAIL' if required else 'warn'}  {name}:\n{traceback.format_exc()}")
            if required:
                failed.append(name)

    check("settings", _settings)
    check("beat analysis", _analysis)
    check("cover", _cover)
    check("player", lambda: _module({"win32": "desktop.player_windows",
                                     "darwin": "desktop.player_macos"}, "desktop.player"))
    check("audio capture", lambda: _module({"win32": "desktop.capture_windows",
                                            "darwin": "desktop.capture_macos"}, "audio"))
    check("platform libraries", _platform)
    check("settings window", _qt)
    check("overlay", _overlay)
    check("Spotify reader, live", _player_live, required=False)  # no Spotify on CI: None is fine
    check("OpenGL", _opengl, required=False)  # CI machines often have no OpenGL 3.3
    lines.append("passed" if not failed else "failed: " + ", ".join(failed))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 1 if failed else 0


def _settings():
    import settings
    settings.load()
    return str(settings.PATH)


def _analysis():
    """Clicks over quiet noise, fed in as if live: they should pulse."""
    from audio import RATE, Analyzer, Capture
    cap, analyzer = Capture(), Analyzer()
    rng = np.random.default_rng(1)
    t, block, peak = 100.0, 1024, 0.0
    for i in range(int(4 * RATE / block)):
        frames = rng.normal(0, 0.003, (block, 2)).astype(np.float32)
        start = i * block
        for click in range(0, 4 * RATE, RATE // 2):  # one every half second
            if start <= click < start + block:
                n = min(480, start + block - click)
                frames[click - start:click - start + n] += (
                    rng.normal(0, 0.5, (n, 1)) * np.exp(-np.arange(n) / 80)[:, None])
        cap._push(frames.ravel())
        t += block / RATE
        cap._last_write = t
        analyzer.process(cap, t)
        peak = max(peak, analyzer.pulse_at(t))
    if peak < 0.2:
        raise AssertionError(f"clicks barely pulsed ({peak:.2f})")
    return f"peak pulse {peak:.2f}"


def _cover():
    from PIL import Image
    from media import cover_from_bytes
    data = io.BytesIO()
    Image.new("RGB", (300, 300), (200, 30, 30)).save(data, "PNG")
    image = cover_from_bytes(data.getvalue())
    return f"{image.mode} {image.size}"


def _module(by_platform, default):
    name = by_platform.get(sys.platform, default)
    importlib.import_module(name)
    return name


def _platform():
    if sys.platform == "win32":
        import soundcard  # noqa: F401
        from winrt.windows.media.control import (  # noqa: F401
            GlobalSystemMediaTransportControlsSessionManager)
        from winrt.windows.storage.streams import Buffer  # noqa: F401
        return "soundcard, winrt"
    if sys.platform == "darwin":
        import AppKit  # noqa: F401
        import CoreMedia  # noqa: F401
        import ScreenCaptureKit  # noqa: F401
        from desktop.capture_macos import VinylAudioReceiver, _protocols
        if len(_protocols()) != 2:
            raise AssertionError("ScreenCaptureKit's stream protocols weren't found")
        # The buffer callback's last argument is the output type, an NSInteger ("q").
        signature = VinylAudioReceiver.stream_didOutputSampleBuffer_ofType_.signature
        if not signature.endswith(b"q"):
            raise AssertionError(f"the audio callback has the wrong signature: {signature}")
        return f"AppKit, CoreMedia, ScreenCaptureKit; audio callback {signature.decode()}"
    return "none needed"


def _player_live():
    """Asks the real thing once: the media controls on Windows, osascript on macOS."""
    if sys.platform == "win32":
        from desktop.player_windows import SpotifyWindows as Reader
    elif sys.platform == "darwin":
        from desktop.player_macos import SpotifyMac as Reader
    else:
        from desktop.player import Playerctl as Reader
    reader = Reader(poll=False)
    reader._setup()
    return repr(reader._read())


def _qt():
    from PySide6.QtWidgets import QApplication
    import settings as config
    from desktop.app import app_icon
    from desktop.prefs import Prefs
    app = QApplication.instance() or QApplication(sys.argv[:1])

    class Stub:
        settings = config.defaults()

        def change(self, **values):
            self.settings.update(values)

    prefs = Prefs(Stub())
    app_icon()
    prefs.close()
    return f"Qt {__import__('PySide6').__version__}"


def _opengl():
    from desktop.app import Renderer
    from disc import Layout, View
    renderer = Renderer()
    layout = Layout.for_record(200, True, 40)
    image = renderer.render((layout.width, layout.height), 1.0, layout, View())
    pixels = renderer._pixels.reshape(layout.height, layout.width, 4)
    alpha = pixels[round(layout.cy), round(layout.cx) + 50, 3]
    if alpha < 200:
        raise AssertionError(f"the record didn't draw (alpha {alpha} at its middle)")
    return f"{image.width()}x{image.height()}, {renderer.ctx.info['GL_RENDERER']}"


def _overlay():
    """The whole overlay for a moment: frames, pointer checks, menus, a
    resize, hiding and showing, and the native window calls."""
    import time
    import types
    from PySide6.QtWidgets import QApplication, QMenu
    from desktop import native
    from desktop.app import Overlay
    app = QApplication.instance() or QApplication(sys.argv[:1])
    overlay = Overlay(app, types.SimpleNamespace(audio="spotify"))
    try:
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)
        overlay._frame()
        overlay._poll_pointer()
        menu, tray = QMenu(), QMenu()
        overlay._fill_menu(menu)
        overlay._fill_menu(tray, tray=True)
        overlay.change(size=300)
        overlay.toggle()
        overlay.toggle()
        app.processEvents()
        native.focus_for_menu(overlay)()
        native.set_click_through(overlay, True)
        native.set_click_through(overlay, False)
        native.set_layer(overlay, "bottom")
        native.set_layer(overlay, "top")
        native.note_front_app()
        native.give_back_front_app()
        return (f"{overlay.width()}x{overlay.height()} at {overlay.pos().toTuple()} on "
                f"{app.platformName()}, {len(menu.actions())}/{len(tray.actions())} menu items, "
                f"drawing {'on' if overlay.renderer else 'off'}")
    finally:
        overlay.capture.stop()
        overlay.player.stop()
        overlay.close()
