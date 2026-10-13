#!/usr/bin/env bash
set -euo pipefail

if [ "${1:-}" = "--status" ]; then
  systemctl status --no-pager tts_bot_watcher.service
  exit 0
fi

[ "$EUID" -eq 0 ] || { echo "Run with sudo: sudo bash scripts/install_watcher.sh"; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd -P)"
SERVICE_FILE="/etc/systemd/system/tts_bot_watcher.service"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now tts_bot_watcher.service 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  rm -f /opt/tts-bot/scripts/watch_and_deploy.py /opt/tts-bot/scripts/deploy.sh
  rm -rf /opt/tts-bot/scripts/lib
  systemctl daemon-reload
  echo "tts_bot_watcher service uninstalled."
  exit 0
fi

chmod +x "$SRC/scripts/watch_and_deploy.py" "$SRC/scripts/install_watcher.sh" "$SRC/scripts/deploy.sh"

mkdir -p /opt/tts-bot/scripts/lib
install -m 755 -o root -g root "$SRC/scripts/watch_and_deploy.py" /opt/tts-bot/scripts/watch_and_deploy.py
install -m 755 -o root -g root "$SRC/scripts/deploy.sh" /opt/tts-bot/scripts/deploy.sh
install -m 644 -o root -g root "$SRC/scripts/lib/deploy_validate.sh" /opt/tts-bot/scripts/lib/deploy_validate.sh

PY="/opt/tts-bot/env/bin/python"
[ -x "$PY" ] || PY="/usr/bin/python3"

TMP_SERVICE="$(mktemp)"
trap 'rm -f "$TMP_SERVICE"' EXIT

cat > "$TMP_SERVICE" <<EOF
[Unit]
Description=Discord Piper TTS bot auto-deploy watcher
After=network.target tts_bot.service

[Service]
Type=simple
User=root
WorkingDirectory=${SRC}
ExecStart=${PY} /opt/tts-bot/scripts/watch_and_deploy.py --src "${SRC}"
Restart=always
RestartSec=5
KillSignal=SIGTERM
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

install -m 644 -o root -g root "$TMP_SERVICE" "$SERVICE_FILE"
rm -f "$TMP_SERVICE"
trap - EXIT

systemctl daemon-reload
systemctl enable --now tts_bot_watcher.service
systemctl restart tts_bot_watcher.service
systemctl status --no-pager tts_bot_watcher.service
