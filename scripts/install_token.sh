#!/usr/bin/env bash
# Prompts for the token (hidden), validates its shape, stores it with mode 600.
#   scripts/install_token.sh            -> ~/.config/tts-bot/token (personal dev bot)
#   sudo scripts/install_token.sh --system -> /etc/tts-bot/token, root:root (production service)
# Discord tokens have no fixed length: <base64 user id>.<6-7 char timestamp>.<27-38 char HMAC>.
set -euo pipefail

if [ "${1:-}" = "--system" ]; then
  [ "$EUID" -eq 0 ] || { echo "--system needs sudo."; exit 1; }
  dir=/etc/tts-bot
else
  dir="${TOKEN_DIR:-$HOME/.config/tts-bot}"
fi
read -rsp "Discord bot token: " tok; echo

# Common paste mistakes (never echo the token itself)
case "$tok" in
  *[[:space:]]*)    echo "Rejected: contains whitespace."; exit 1 ;;
  DISCORD_TOKEN=*)  echo "Rejected: remove the 'DISCORD_TOKEN=' prefix."; exit 1 ;;
  Bot.*|Bot\ *)     echo "Rejected: remove the 'Bot ' prefix."; exit 1 ;;
  \"*|\'*|*\"|*\')  echo "Rejected: remove surrounding quotes."; exit 1 ;;
esac

if ! [[ "$tok" =~ ^[A-Za-z0-9_-]{23,30}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,38}$ ]]; then
  echo "Rejected: not a Discord bot token shape (len=${#tok}; expect 3 dot-separated parts, ~59-72 chars)."
  echo "Use Developer Portal -> Bot -> Reset Token (NOT client secret or public key)."
  exit 1
fi

mkdir -p "$dir" && chmod 700 "$dir"
umask 077
printf '%s' "$tok" > "$dir/token"
chmod 600 "$dir/token"
[ "${1:-}" = "--system" ] && chown root:root "$dir" "$dir/token"
echo "OK: token shape valid (len=${#tok}). Saved to $dir/token"
