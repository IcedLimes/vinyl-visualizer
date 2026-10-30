# PyInstaller spec for the Windows and macOS packages. From the repository:
#
#   pip install -r requirements-desktop.txt pyinstaller
#   pyinstaller --noconfirm packaging/vinyl.spec
#
# Windows: dist/Vinyl/Vinyl.exe. macOS: dist/Vinyl.app. VINYL_VERSION sets
# the version shown in the app's properties.
import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
VERSION = os.environ.get("VINYL_VERSION", "0.0.0")
NUMBER = re.match(r"\d+(\.\d+)*", VERSION).group()  # macOS wants plain numbers: 0.1.0-beta.1 → 0.1.0
ICON = ROOT / "packaging" / ("icon.ico" if sys.platform == "win32" else "icon.png")

datas = [(str(ROOT / "disc.frag"), "."), (str(ROOT / "packaging" / "icon.png"), "packaging")]
hidden = collect_submodules("glcontext")  # moderngl picks its OpenGL backend at run time
if sys.platform == "win32":
    datas += collect_data_files("soundcard")  # its C declarations, read at import
    hidden += ["_cffi_backend"] + collect_submodules("winrt")
elif sys.platform == "darwin":
    for package in ("objc", "Foundation", "AppKit", "CoreMedia", "ScreenCaptureKit", "dispatch"):
        hidden += collect_submodules(package)

a = Analysis(
    [str(ROOT / "vinyl_desktop.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "gi", "cairo", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Vinyl",
    console=False,
    icon=str(ICON) if ICON.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Vinyl")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Vinyl.app",
        icon=str(ICON),
        bundle_identifier="io.github.vinyl-visualizer",
        version=NUMBER,
        info_plist={
            "CFBundleDisplayName": "Vinyl",
            "CFBundleShortVersionString": NUMBER,
            "CFBundleVersion": NUMBER,
            "LSMinimumSystemVersion": "13.0",
            "LSUIElement": True,  # no Dock icon: it lives in the menu bar
            "NSHighResolutionCapable": True,
            "NSAppleEventsUsageDescription":
                "Vinyl reads what Spotify is playing, and plays or pauses it when you click the record.",
        },
    )
