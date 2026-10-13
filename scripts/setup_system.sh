#!/usr/bin/env bash
# One-time root setup. Run by an admin: sudo scripts/setup_system.sh
#
# Layout (trust boundary: devs edit the repo, only an admin deploys + holds the token):
#   /opt/tts-bot/        root-owned code, models, private conda env   (service reads, devs cannot write)
#   /etc/tts-bot/token   root:root 600, handed to service via systemd LoadCredential
#   /var/lib/tts-bot/    state.json, owned by the unprivileged 'ttsbot' user
#   sudoers              devteam may ONLY start/stop/restart tts_bot
set -euo pipefail

[ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/tts-bot
CONF=/etc/tts-bot
CONDA="${CONDA:-/home/jb/miniconda3/bin/conda}"
GROUP="${TEAM_GROUP:-devteam}"

getent group "$GROUP" >/dev/null || { echo "group $GROUP missing"; exit 1; }
[ -x "$CONDA" ] || { echo "conda not found at $CONDA (set CONDA=...)"; exit 1; }

# service account: no login, no home
id ttsbot &>/dev/null || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin ttsbot

install -d -m 755 -o root -g root "$APP"
install -d -m 700 -o root -g root "$CONF"

# private, root-owned interpreter (so nobody can swap site-packages under the token)
if [ ! -x "$APP/env/bin/python" ]; then
  CONDA_PKGS_DIRS=/var/cache/tts-bot-conda "$CONDA" create -y -p "$APP/env" --override-channels -c conda-forge python=3.11 pip ffmpeg libopus
fi
# env may predate pip (conda-forge python ships without it)
if ! "$APP/env/bin/python" -m pip --version &>/dev/null; then
  CONDA_PKGS_DIRS=/var/cache/tts-bot-conda "$CONDA" install -y -p "$APP/env" --override-channels -c conda-forge pip
fi
"$APP/env/bin/python" -m pip install -r "$SRC/requirements.txt"
# piper-tts drags in CPU onnxruntime; keep only the GPU build
"$APP/env/bin/python" -m pip uninstall -y onnxruntime onnxruntime-gpu
"$APP/env/bin/python" -m pip install "onnxruntime-gpu>=1.18,<1.21"

# sudoers: exact commands only (no wildcards, no pagers -> no shell escapes)
tmp="$(mktemp)"
cat > "$tmp" <<EOF
Cmnd_Alias TTSBOT = /usr/bin/systemctl start tts_bot, /usr/bin/systemctl stop tts_bot, /usr/bin/systemctl restart tts_bot
%$GROUP ALL=(root) NOPASSWD: TTSBOT
EOF
visudo -cf "$tmp"
install -m 440 -o root -g root "$tmp" /etc/sudoers.d/tts-bot
rm -f "$tmp"

# logs without sudo
for u in $(getent group "$GROUP" | cut -d: -f4 | tr ',' ' '); do
  usermod -aG systemd-journal "$u"
done

install -m 644 -o root -g root "$SRC/tts_bot.service" /etc/systemd/system/tts_bot.service
systemctl daemon-reload

echo
echo "Next:"
echo "  1) sudo $SRC/scripts/install_token.sh --system"
echo "  2) sudo bash $SRC/scripts/install_deploy.sh --src <runner checkout> --runner-user <ci user>"
echo "  3) sudo tts-bot-deploy"
echo "  4) sudo systemctl enable tts_bot"
