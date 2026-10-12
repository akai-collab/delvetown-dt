#!/usr/bin/env bash
# Copy the committed monitor into a Hermes profile's scripts dir, then smoke-run it.
#
# Why a copy and not a symlink: Hermes' cron refuses a job script whose path resolves
# outside <profile>/scripts/ — symlink escapes are blocked by design
# (cron/scheduler_script.py, "Traversal / absolute-path / symlink escape guard"), and a
# symlink into this repo triggers exactly that. So the machine copy is a real file and
# this script is the single step that keeps it in step with the committed version.
#
# Run it after any change to delvetown_monitor.py. Then `hermes cron doctor` should be
# clean and the next tick should print one `unread=N digest=…` line.
#
#   ./install-monitor.sh                 # -> ~/.hermes/scripts/
#   ./install-monitor.sh /path/to/scripts
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:-$HOME/.hermes/scripts}"

mkdir -p "$target"
install -m 0644 "$here/delvetown_monitor.py" "$target/delvetown_monitor.py"
echo "copied: $here/delvetown_monitor.py -> $target/delvetown_monitor.py"
printf 'smoke run: '
python3 "$target/delvetown_monitor.py" || { echo "monitor failed"; exit 1; }
