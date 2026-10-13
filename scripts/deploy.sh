#!/usr/bin/env bash
# Admin-only (needs real sudo, not in the devteam sudoers rule).
# Copies reviewed code from the repo into root-owned /opt/tts-bot and restarts the service.
set -euo pipefail

[ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/tts-bot
PY="$APP/env/bin/python"

[ -x "$PY" ] || { echo "Run scripts/setup_system.sh first."; exit 1; }
[ -f /etc/tts-bot/gui_token ] || { echo "/etc/tts-bot/gui_token missing; the unit would fail to start. Run: sudo scripts/gen_gui_token.sh --system"; exit 1; }

shopt -s nullglob
pyfiles=("$SRC"/*.py)
staticfiles=("$SRC"/static/*)
models=("$SRC"/models/*.onnx "$SRC"/models/*.onnx.json)
shopt -u nullglob
[ "${#pyfiles[@]}" -gt 0 ] || { echo "No *.py files found in $SRC"; exit 1; }
[ "${#staticfiles[@]}" -gt 0 ] || { echo "No files found in $SRC/static"; exit 1; }
[ "${#models[@]}" -gt 0 ] || { echo "No model files found in $SRC/models"; exit 1; }

# Refuse symlinks / non-regular files: root would dereference them and publish the target world-readable
for f in "${pyfiles[@]}" "${staticfiles[@]}" "${models[@]}" "$SRC/requirements.txt"; do
  if [ -L "$f" ] || [ ! -f "$f" ]; then
    echo "Refusing to deploy: $f is a symlink or not a regular file."
    exit 1
  fi
done

# Compile everything before touching /opt (output to a throwaway dir, no __pycache__ in the repo)
chk="$(mktemp -d)"
trap 'rm -rf "$chk"' EXIT
for f in "${pyfiles[@]}"; do
  "$PY" -c 'import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)' "$f" "$chk/$(basename "$f")c"
done

install -d -m 755 -o root -g root "$APP" "$APP/models" "$APP/static"
install -m 644 -o root -g root "${pyfiles[@]}" "$SRC/requirements.txt" "$APP/"
install -m 644 -o root -g root "${staticfiles[@]}" "$APP/static/"
install -m 644 -o root -g root "${models[@]}" "$APP/models/"

# Remove deployed files that no longer exist in the repo
for f in "$APP"/*.py; do
  [ -e "$f" ] || continue
  [ -e "$SRC/$(basename "$f")" ] || { echo "removing stale $f"; rm -f "$f"; }
done
for f in "$APP"/static/*; do
  [ -e "$f" ] || continue
  [ -e "$SRC/static/$(basename "$f")" ] || { echo "removing stale $f"; rm -rf "$f"; }
done
for f in "$APP"/models/*; do
  [ -e "$f" ] || continue
  [ -e "$SRC/models/$(basename "$f")" ] || { echo "removing stale $f"; rm -rf "$f"; }
done
rm -rf "$APP/__pycache__"

systemctl restart tts_bot
sleep 2
systemctl --no-pager --lines=15 status tts_bot || true
