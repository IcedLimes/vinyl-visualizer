"""What Qt can't do on its own: letting clicks through the overlay around the
record, never taking focus, sitting above windows or on the desktop, and
starting at login. Elsewhere (Linux, for trying it out) these do nothing."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

APP_NAME = "Vinyl"
LABEL = "io.github.vinyl-visualizer"


def command():
    """How to start this copy of Vinyl: the packaged app, or the script."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "vinyl_desktop.py")]


if sys.platform == "win32":
    import ctypes
    import winreg
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    HWND_TOPMOST, HWND_NOTOPMOST, HWND_BOTTOM = -1, -2, 1
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    _get_style = getattr(_user32, "GetWindowLongPtrW", _user32.GetWindowLongW)
    _get_style.restype = ctypes.c_ssize_t
    _get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    _set_style = getattr(_user32, "SetWindowLongPtrW", _user32.SetWindowLongW)
    _set_style.restype = ctypes.c_ssize_t
    _set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_uint]

    def _hwnd(widget):
        return wintypes.HWND(int(widget.winId()))

    def _style(widget, add=0, remove=0):
        hwnd = _hwnd(widget)
        old = _get_style(hwnd, GWL_EXSTYLE)
        new = (old | add) & ~remove
        if new != old:
            _set_style(hwnd, GWL_EXSTYLE, new)

    def prepare(widget):
        """No focus when clicked (keys stay with your window), no taskbar button."""
        _style(widget, add=WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

    def set_click_through(widget, through):
        _style(widget, add=WS_EX_TRANSPARENT if through else 0,
               remove=0 if through else WS_EX_TRANSPARENT)

    def set_layer(widget, layer):
        hwnd = _hwnd(widget)
        flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
        if layer == "top":
            _user32.SetWindowPos(hwnd, wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0, flags)
        else:
            _user32.SetWindowPos(hwnd, wintypes.HWND(HWND_NOTOPMOST), 0, 0, 0, 0, flags)
            _user32.SetWindowPos(hwnd, wintypes.HWND(HWND_BOTTOM), 0, 0, 0, 0, flags)

    def starts_at_login():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        except OSError:
            return False

    def set_starts_at_login(on):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                line = subprocess.list2cmdline(command())
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, line)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except OSError:
                    pass

elif sys.platform == "darwin":
    import objc
    from AppKit import NSPanel

    # NSWindow levels and behaviours
    FLOATING = 3                      # NSFloatingWindowLevel: above app windows
    DESKTOP_ICONS = -2147483603       # kCGDesktopIconWindowLevel: on the desktop
    ALL_SPACES, STATIONARY, IGNORES_CYCLE, FULL_SCREEN_AUX = 1, 1 << 4, 1 << 6, 1 << 8
    NONACTIVATING_PANEL = 1 << 7
    AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def _window(widget):
        view = objc.objc_object(c_void_p=int(widget.winId()))
        return view.window()

    def prepare(widget):
        """On every Space and beside full-screen apps, out of the window
        cycle, and a click doesn't bring Vinyl to the front."""
        win = _window(widget)
        if win is None:
            return
        win.setCollectionBehavior_(ALL_SPACES | STATIONARY | IGNORES_CYCLE | FULL_SCREEN_AUX)
        win.setHasShadow_(False)
        if win.isKindOfClass_(NSPanel):
            win.setStyleMask_(win.styleMask() | NONACTIVATING_PANEL)
            win.setHidesOnDeactivate_(False)

    def set_click_through(widget, through):
        win = _window(widget)
        if win is not None and bool(win.ignoresMouseEvents()) != through:
            win.setIgnoresMouseEvents_(through)

    def set_layer(widget, layer):
        win = _window(widget)
        if win is not None:
            win.setLevel_(FLOATING if layer == "top" else DESKTOP_ICONS + 1)

    def starts_at_login():
        return AGENT.exists()

    def set_starts_at_login(on):
        if on:
            AGENT.parent.mkdir(parents=True, exist_ok=True)
            AGENT.write_bytes(plistlib.dumps({
                "Label": LABEL, "ProgramArguments": command(), "RunAtLoad": True,
                "ProcessType": "Interactive"}))
        elif AGENT.exists():
            AGENT.unlink()

else:
    def prepare(widget):
        pass

    def set_click_through(widget, through):
        pass

    def set_layer(widget, layer):
        pass

    def starts_at_login():
        return None  # not offered here

    def set_starts_at_login(on):
        pass


def log_path():
    """Where the packaged app writes what it would print (it has no console)."""
    import settings
    return settings.PATH.with_name("vinyl.log")


def open_log_folder():
    folder = str(log_path().parent)
    if sys.platform == "win32":
        os.startfile(folder)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", folder])
    else:
        subprocess.Popen(["xdg-open", folder])
