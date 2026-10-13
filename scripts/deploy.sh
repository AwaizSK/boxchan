#!/usr/bin/env bash
# Admin-only (needs real sudo, not in the devteam sudoers rule).
# Copies reviewed code from the repo into root-owned /opt/tts-bot and restarts the service.
set -euo pipefail

[ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/tts-bot
PY="$APP/env/bin/python"

[ -x "$PY" ] || { echo "Run scripts/setup_system.sh first."; exit 1; }
"$PY" -m py_compile "$SRC/bot.py" "$SRC/tts_engine.py"

install -m 644 -o root -g root "$SRC/bot.py" "$SRC/tts_engine.py" "$SRC/requirements.txt" "$APP/"
install -d -m 755 -o root -g root "$APP/models"
install -m 644 -o root -g root "$SRC"/models/*.onnx "$SRC"/models/*.onnx.json "$APP/models/"
rm -rf "$APP/__pycache__"

systemctl restart tts_bot
sleep 2
systemctl --no-pager --lines=15 status tts_bot || true
