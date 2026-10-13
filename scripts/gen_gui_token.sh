#!/usr/bin/env bash
# Generates the web UI bearer token and prints it ONCE.
#   scripts/gen_gui_token.sh [--rotate]           -> ~/.config/tts-bot/gui_token (600, dir 700)
#   sudo scripts/gen_gui_token.sh --system [--rotate] -> /etc/tts-bot/gui_token, root:root 600
# Refuses to overwrite an existing token unless --rotate is given.
set -euo pipefail

system=0; rotate=0
for a in "$@"; do
  case "$a" in
    --system) system=1 ;;
    --rotate) rotate=1 ;;
    *) echo "usage: $0 [--system] [--rotate]"; exit 2 ;;
  esac
done

if [ "$system" -eq 1 ]; then
  [ "$EUID" -eq 0 ] || { echo "--system needs sudo."; exit 1; }
  dir=/etc/tts-bot
else
  dir="${TOKEN_DIR:-$HOME/.config/tts-bot}"
fi
file="$dir/gui_token"

if [ -e "$file" ] && [ "$rotate" -ne 1 ]; then
  echo "Refusing to overwrite existing $file (use --rotate)."
  exit 1
fi

tok="$(openssl rand -base64 36 | tr '+/' '-_' | tr -d '=\n')"
[ "${#tok}" -ge 48 ] || { echo "token generation failed"; exit 1; }

umask 077
mkdir -p "$dir" && chmod 700 "$dir"
printf '%s' "$tok" > "$file"
chmod 600 "$file"
if [ "$system" -eq 1 ]; then chown root:root "$dir" "$file"; fi

echo "Saved to $file. Token (shown once, store it in your password manager):"
echo "$tok"
