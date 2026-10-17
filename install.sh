#!/bin/sh
# Installs vimeo_dl for the current user.
# The venv stays in this repository; the launcher and menu entry live under $HOME.
set -eu

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"

command -v python3 >/dev/null || { echo "missing: python3" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "missing: ffmpeg (Fedora: sudo dnf install ffmpeg)" >&2; exit 1; }
python3 -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk" 2>/dev/null \
    || { echo "missing: PyGObject with GTK 4 (Fedora: sudo dnf install python3-gobject gtk4)" >&2; exit 1; }

python3 -m venv --system-site-packages "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"

ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"
cp "$REPO/data/io.github.akosmarton.vimeo_dl.svg" "$ICON_DIR/io.github.akosmarton.vimeo_dl.svg"

cat > "$BIN_DIR/vimeo_dl" <<EOF
#!/bin/sh
exec "$REPO/.venv/bin/python" "$REPO/vimeo_dl.py" "\$@"
EOF
chmod +x "$BIN_DIR/vimeo_dl"

cat > "$APP_DIR/io.github.akosmarton.vimeo_dl.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Vimeo Downloader
Exec=$BIN_DIR/vimeo_dl
Icon=io.github.akosmarton.vimeo_dl
Terminal=false
Categories=Network;
EOF

if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$APP_DIR" || true
fi

echo "installed: $BIN_DIR/vimeo_dl"
echo "menu entry: $APP_DIR/io.github.akosmarton.vimeo_dl.desktop"
