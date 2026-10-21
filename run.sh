#!/usr/bin/env bash
# MPXAir portable launcher. Runs straight from this folder; nothing is installed.
#   ./run.sh              start MPXAir (settings in ~/.config/mpxair, files in ~/Documents/MPXAir)
#   ./run.sh --portable   keep settings, map cache and logs inside this folder (for a USB stick)
#   ./run.sh --demo       start the built-in demo drone in this terminal (no window)
set -e
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

check() {
    python3 - <<'PY' 2>/dev/null
import sys
assert sys.version_info >= (3, 10)
import gi, cairo
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
assert Adw.get_minor_version() >= 4
PY
}

if ! command -v python3 >/dev/null || ! check; then
    echo "MPXAir needs Python 3, GTK 4, libadwaita and PyCairo." >&2
    echo "On Ubuntu they are installed with:" >&2
    echo "  sudo apt install python3-gi python3-cairo gir1.2-gtk-4.0 gir1.2-adw-1" >&2
    echo "(./install.sh can do this for you.)" >&2
    exit 1
fi

case "$1" in
    --portable)
        shift
        export MPXAIR_HOME="$HERE/userdata"
        mkdir -p "$MPXAIR_HOME"
        ;;
    --demo)
        shift
        cd "$HERE"
        exec python3 -m mpxair.sim.demo "$@"
        ;;
esac

cd "$HERE"
exec python3 -m mpxair "$@"
