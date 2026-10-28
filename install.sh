#!/usr/bin/env bash
# Installs System Inspector for the current user (no root, no pip needed).
set -e
cd "$(dirname "$0")"

python3 -c "import tkinter" 2>/dev/null || {
  echo "Tkinter is missing. Install it first:"
  echo "  Debian/Ubuntu: sudo apt install python3-tk"
  echo "  Fedora:        sudo dnf install python3-tkinter"
  echo "  Arch:          sudo pacman -S tk"
  exit 1
}

APP="$HOME/.local/share/system-inspector"
BIN="$HOME/.local/bin"
mkdir -p "$APP" "$BIN" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"

rm -rf "$APP/system_inspector"
cp -r system_inspector "$APP/"

cat > "$BIN/system-inspector" <<LAUNCH
#!/usr/bin/env bash
PYTHONPATH="$APP" exec python3 -m system_inspector "\$@"
LAUNCH
chmod +x "$BIN/system-inspector"

cp packaging/system-inspector.svg "$HOME/.local/share/icons/hicolor/scalable/apps/"
cp packaging/system-inspector.desktop "$HOME/.local/share/applications/"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "Installed. Launch it from your app menu, or run: system-inspector"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: add $BIN to your PATH to use the command in a terminal." ;; esac
