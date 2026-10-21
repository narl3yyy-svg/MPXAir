#!/usr/bin/env bash
# MPXAir installer for Ubuntu (tested on 25.10).
#   ./install.sh            install for this user (~/.local), no password needed
#   ./install.sh --system   install for every user (/opt/mpxair), asks for your password
#   ./install.sh --uninstall [--system]   remove MPXAir again (your settings and logs are kept)
set -e
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
MODE=user
ACTION=install
for a in "$@"; do
    case "$a" in
        --system) MODE=system ;;
        --uninstall) ACTION=uninstall ;;
        -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
        *) echo "Unknown option: $a" >&2; exit 2 ;;
    esac
done

if [ "$MODE" = system ]; then
    PREFIX=/opt/mpxair; BIN=/usr/local/bin; APPS=/usr/share/applications
    ICONS=/usr/share/icons/hicolor/scalable/apps; SUDO=sudo
else
    PREFIX="$HOME/.local/share/mpxair"; BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"
    ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"; SUDO=
fi

if [ "$ACTION" = uninstall ]; then
    echo "Removing MPXAir from $PREFIX"
    $SUDO rm -rf "$PREFIX"
    $SUDO rm -f "$BIN/mpxair" "$APPS/io.github.narl3yyy_svg.MPXAir.desktop" "$APPS/io.github.mpxair.MPXAir.desktop" "$ICONS/mpxair.svg"
    command -v update-desktop-database >/dev/null && $SUDO update-desktop-database -q "$APPS" 2>/dev/null || true
    echo "Done. Your settings (~/.config/mpxair), map cache (~/.cache/mpxair) and"
    echo "logs (~/Documents/MPXAir) were left in place; delete them by hand if you want."
    exit 0
fi

echo "== MPXAir installer =="

# 1. system libraries (all from Ubuntu's own repositories)
NEED=""
for p in python3 python3-gi python3-cairo gir1.2-gtk-4.0 gir1.2-adw-1; do
    dpkg -s "$p" >/dev/null 2>&1 || NEED="$NEED $p"
done
if [ -n "$NEED" ]; then
    echo "MPXAir needs these Ubuntu packages:$NEED"
    read -r -p "Install them now with apt? (needs your password) [Y/n] " ans
    case "$ans" in n|N) echo "Cancelled."; exit 1 ;; esac
    sudo apt-get update && sudo apt-get install -y $NEED
fi
# optional: location service support for the ground position
dpkg -s gir1.2-geoclue-2.0 >/dev/null 2>&1 || echo "Note: install gir1.2-geoclue-2.0 to use this computer's location service."

# 2. copy the program
echo "Installing to $PREFIX"
$SUDO mkdir -p "$PREFIX" "$BIN" "$APPS" "$ICONS"
$SUDO rm -rf "$PREFIX/mpxair"
$SUDO cp -r "$HERE/mpxair" "$PREFIX/"
$SUDO find "$PREFIX/mpxair" -name '__pycache__' -prune -exec rm -rf {} +
$SUDO cp "$HERE/run.sh" "$HERE/README.md" "$HERE/LICENSE" "$PREFIX/"
$SUDO chmod 755 "$PREFIX/run.sh"

# 3. launcher command
TMP=$(mktemp)
cat > "$TMP" <<LAUNCH
#!/usr/bin/env bash
exec "$PREFIX/run.sh" "\$@"
LAUNCH
$SUDO install -m 755 "$TMP" "$BIN/mpxair"
rm -f "$TMP"

# 4. desktop entry and icon
$SUDO cp "$HERE/mpxair/data/icons/mpxair.svg" "$ICONS/mpxair.svg"
TMP=$(mktemp)
cat > "$TMP" <<DESK
[Desktop Entry]
Type=Application
Name=MPXAir
GenericName=Ground Control Station
Comment=Plan, fly and set up ArduPilot drones, planes and rovers
Exec=$BIN/mpxair
Icon=mpxair
Terminal=false
Categories=Science;Engineering;Geography;
Keywords=drone;ardupilot;mavlink;mission;planner;uav;gcs;
StartupWMClass=io.github.narl3yyy_svg.MPXAir
X-Geoclue-Reason=Shows the ground station position for Follow Me and antenna tracking
DESK
$SUDO install -m 644 "$TMP" "$APPS/io.github.narl3yyy_svg.MPXAir.desktop"
# older builds used a different app ID: remove their launcher so it doesn't show twice
$SUDO rm -f "$APPS/io.github.mpxair.MPXAir.desktop"
rm -f "$TMP"
command -v update-desktop-database >/dev/null && $SUDO update-desktop-database -q "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && $SUDO gtk-update-icon-cache -q "$(dirname "$(dirname "$(dirname "$ICONS")")")" 2>/dev/null || true

# 5. USB serial access for flight controllers and telemetry radios
if ! id -nG "$USER" | grep -qw dialout; then
    echo
    echo "To talk to flight controllers over USB, your user must be in the 'dialout' group."
    read -r -p "Add $USER to 'dialout' now? (needs your password; log out and back in afterwards) [Y/n] " ans
    case "$ans" in
        n|N) echo "Skipped. You can do it later with: sudo usermod -aG dialout $USER" ;;
        *) sudo usermod -aG dialout "$USER" && echo "Done — log out and back in for it to take effect." ;;
    esac
fi
# ModemManager probes new USB serial devices and can upset flight controllers
if systemctl is-active --quiet ModemManager 2>/dev/null; then
    echo "Note: ModemManager is running. If connections to a flight controller fail, stop it with:"
    echo "  sudo systemctl disable --now ModemManager"
fi

echo
echo "MPXAir is installed. Start it from the app grid (search for MPXAir) or run: mpxair"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "($BIN is not on your PATH yet; it will be after you log in again.)";; esac
