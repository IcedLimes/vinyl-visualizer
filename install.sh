#!/bin/sh
# Sets up the Python environment, then adds a `vinyl-visualizer` command and an
# app-menu entry. GTK, gtk-layer-shell and playerctl come from the system:
#   pacman -S python-gobject python-cairo gtk3 gtk-layer-shell playerctl libpulse
set -e
cd "$(dirname "$0")"
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -q --upgrade -r requirements.txt

mkdir -p ~/.local/bin ~/.local/share/applications
cat > ~/.local/bin/vinyl-visualizer <<SH
#!/bin/sh
exec "$PWD/.venv/bin/python" "$PWD/vinyl.py" "\$@"
SH
chmod +x ~/.local/bin/vinyl-visualizer

cat > ~/.local/share/applications/vinyl-visualizer.desktop <<DESKTOP
[Desktop Entry]
Type=Application
Name=Vinyl
Comment=A spinning record for what Spotify is playing (launch again to show or hide)
Exec=$HOME/.local/bin/vinyl-visualizer
Icon=media-optical-audio
Terminal=false
Categories=AudioVideo;Audio;
DESKTOP

echo "Installed. Run vinyl-visualizer, or open Vinyl from the launcher."
