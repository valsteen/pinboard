#!/bin/sh
set -eu

plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
pinboard_launcher="$plugin_root/scripts/pinboard"

exec python3 "$plugin_root/scripts/permission-hook.py" "$pinboard_launcher"
