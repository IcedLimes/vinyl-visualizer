#!/bin/sh
# Installs Vinyl for the current user: a Python environment in .venv, the
# `vinyl-visualizer` command in ~/.local/bin and a "Vinyl" app-menu entry.
#
#   ./install.sh               install, or update after a git pull
#   ./install.sh --autostart   also start with your session (via XDG autostart),
#                              opening and closing along with Spotify
#   ./install.sh --uninstall   remove everything this script added
set -e
cd "$(dirname "$0")"
DIR=$(pwd)
BIN="$HOME/.local/bin/vinyl-visualizer"
MENU="${XDG_DATA_HOME:-$HOME/.local/share}/applications/vinyl-visualizer.desktop"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/vinyl-visualizer.desktop"

say() { printf '%s\n' "$*"; }

if [ "$1" = "--uninstall" ]; then
    [ -x "$BIN" ] && "$BIN" --quit 2>/dev/null || true
    pkill -f "$DIR/follow.py" 2>/dev/null || true
    rm -f "$BIN" "$MENU" "$AUTOSTART"
    rm -rf "$DIR/.venv"
    say "Uninstalled. Your settings are still in ~/.config/vinyl-visualizer; delete that folder to forget them."
    exit 0
fi

# ---- system packages ---------------------------------------------------
# Each check is a key that maps to a package name per distro below.
missing=""
optional=""
check() { python3 -c "$2" >/dev/null 2>&1 || missing="$missing $1"; }

if ! command -v python3 >/dev/null 2>&1; then
    missing=" python"
elif ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    say "Vinyl needs Python 3.9 or newer (found $(python3 -V))."
    exit 1
else
    check venv 'import venv, ensurepip'
    check gtk 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk'
    check cairo 'import cairo, gi; gi.require_foreign("cairo")'
    check playerctl 'import gi; gi.require_version("Playerctl", "2.0"); from gi.repository import Playerctl'
    python3 -c 'import gi; gi.require_version("GtkLayerShell", "0.1")' >/dev/null 2>&1 \
        || optional="$optional layershell"
fi
command -v parec >/dev/null 2>&1 && command -v pactl >/dev/null 2>&1 || missing="$missing pulse"

packages() {  # $1: distro family, $2: keys
    for key in $2; do
        case "$1:$key" in
            arch:python) printf ' python' ;;
            arch:venv) ;;
            arch:gtk) printf ' python-gobject gtk3' ;;
            arch:cairo) printf ' python-cairo' ;;
            arch:playerctl) printf ' playerctl' ;;
            arch:layershell) printf ' gtk-layer-shell' ;;
            arch:pulse) printf ' libpulse' ;;
            debian:python) printf ' python3' ;;
            debian:venv) printf ' python3-venv' ;;
            debian:gtk) printf ' python3-gi gir1.2-gtk-3.0' ;;
            debian:cairo) printf ' python3-gi-cairo python3-cairo' ;;
            debian:playerctl) printf ' playerctl gir1.2-playerctl-2.0' ;;
            debian:layershell) printf ' gir1.2-gtklayershell-0.1' ;;
            debian:pulse) printf ' pulseaudio-utils' ;;
            fedora:python) printf ' python3' ;;
            fedora:venv) ;;
            fedora:gtk) printf ' python3-gobject gtk3' ;;
            fedora:cairo) printf ' python3-cairo' ;;
            fedora:playerctl) printf ' playerctl' ;;
            fedora:layershell) printf ' gtk-layer-shell' ;;
            fedora:pulse) printf ' pulseaudio-utils' ;;
            *) printf ' %s' "$key" ;;
        esac
    done
}

install_hint() {  # $1: keys
    family=other
    if [ -r /etc/os-release ]; then
        ids=$(. /etc/os-release; echo "$ID $ID_LIKE")
        case " $ids " in
            *" arch "*) family=arch ;;
            *" debian "* | *" ubuntu "*) family=debian ;;
            *" fedora "* | *" rhel "*) family=fedora ;;
        esac
    fi
    list=$(packages "$family" "$1")
    case $family in
        arch) say "    sudo pacman -S --needed$list" ;;
        debian) say "    sudo apt install$list" ;;
        fedora) say "    sudo dnf install$list" ;;
        *) say "    Python 3 with venv, PyGObject + GTK 3, pycairo, playerctl (with its"
           say "    GObject bindings), PulseAudio's parec/pactl (pipewire-pulse is fine),"
           say "    and optionally gtk-layer-shell." ;;
    esac
}

if [ -n "$missing" ]; then
    say "Vinyl needs a few system packages first. Install them with:"
    say ""
    install_hint "$missing$optional"
    say ""
    say "then run ./install.sh again."
    exit 1
fi

# ---- Python environment ------------------------------------------------
say "Setting up the Python environment in .venv ..."
python3 -m venv --system-site-packages .venv  # system site packages: GTK's Python bindings
.venv/bin/pip install -q --disable-pip-version-check --upgrade -r requirements.txt

# ---- command, menu entry, autostart -------------------------------------
mkdir -p "$(dirname "$BIN")" "$(dirname "$MENU")"
cat > "$BIN" <<SH
#!/bin/sh
exec "$DIR/.venv/bin/python" "$DIR/vinyl.py" "\$@"
SH
chmod +x "$BIN"

cat > "$MENU" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Vinyl
Comment=A spinning record for what Spotify is playing (launch again to show or hide)
Exec=$BIN
Icon=media-optical-audio
Terminal=false
Categories=AudioVideo;Audio;
DESKTOP

if [ "$1" = "--autostart" ]; then
    mkdir -p "$(dirname "$AUTOSTART")"
    cat > "$AUTOSTART" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Vinyl (with Spotify)
Comment=Opens the Vinyl overlay when Spotify starts and closes it when Spotify quits
Exec=$BIN --with-spotify
Icon=media-optical-audio
Terminal=false
X-GNOME-Autostart-enabled=true
DESKTOP
fi

say ""
say "Installed. Run vinyl-visualizer, or open Vinyl from your app menu."
if [ -n "$optional" ]; then
    say ""
    say "Optional: without gtk-layer-shell, Vinyl runs as a borderless window instead"
    say "of an overlay. For the overlay (Hyprland, Sway, KDE Plasma, river...):"
    install_hint "$optional"
fi
if [ "$1" = "--autostart" ]; then
    say ""
    say "It will start with your session and open whenever Spotify does."
    say "(Hyprland and Sway don't run XDG autostart; see the README for their one-line setup.)"
fi
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) say ""; say "Note: ~/.local/bin isn't on your PATH, so run it as $BIN" ;;
esac
