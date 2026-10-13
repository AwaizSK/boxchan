#!/usr/bin/env bash
# Admin-only (needs real sudo, not in the devteam sudoers rule).
# Copies reviewed code from the repo into root-owned /opt/tts-bot and restarts the service.
set -euo pipefail

[ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd -P)"
APP=/opt/tts-bot
PY="$APP/env/bin/python"

[ -x "$PY" ] || { echo "Run scripts/setup_system.sh first."; exit 1; }

# Validate + snapshot everything before touching /opt: all later steps read only from $snap
# (closes the check/copy race; root never follows repo symlinks).
snap="$(mktemp -d)"
trap 'rm -rf "$snap"' EXIT
chmod 700 "$snap"
# shellcheck source=lib/deploy_validate.sh
. "$SRC/scripts/lib/deploy_validate.sh"
snapshot_sources "$SRC" "$snap" || exit 1

# Compile from the snapshot (output to the snapshot's own throwaway dir, nothing lands in the repo)
mkdir "$snap/.pyc"
for f in "$snap"/*.py; do
  "$PY" -c 'import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)' "$f" "$snap/.pyc/$(basename "$f")c"
done

install -d -m 755 -o root -g root "$APP" "$APP/models" "$APP/static"
install -m 644 -o root -g root "$snap"/*.py "$snap/requirements.txt" "$APP/"
install -m 644 -o root -g root "$snap"/static/* "$APP/static/"
install -m 644 -o root -g root "$snap"/models/* "$APP/models/"

# Remove deployed files that no longer exist in the repo
for f in "$APP"/*.py; do
  [ -e "$f" ] || continue
  [ -e "$snap/$(basename "$f")" ] || { echo "removing stale $f"; rm -f "$f"; }
done
for f in "$APP"/static/*; do
  [ -e "$f" ] || continue
  [ -e "$snap/static/$(basename "$f")" ] || { echo "removing stale $f"; rm -rf "$f"; }
done
for f in "$APP"/models/*; do
  [ -e "$f" ] || continue
  [ -e "$snap/models/$(basename "$f")" ] || { echo "removing stale $f"; rm -rf "$f"; }
done
rm -rf "$APP/__pycache__"

systemctl restart tts_bot
sleep 2
systemctl --no-pager --lines=15 status tts_bot || true
