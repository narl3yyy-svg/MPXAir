#!/usr/bin/env bash
# Remove MPXAir. Add --system if you installed it with ./install.sh --system.
exec "$(dirname "$(readlink -f "$0")")/install.sh" --uninstall "$@"
