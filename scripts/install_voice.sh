#!/usr/bin/env bash
# Admin-only (real sudo). Adds a Piper voice to the running service WITHOUT a restart or deploy:
#   sudo scripts/install_voice.sh [--force] <voice.onnx> [<voice.onnx.json>]
# The voice appears after "Rescan" in the GUI's Voice tab. Existing voices are never overwritten
# unless --force. Voices installed here live only in /opt/tts-bot/models (deploy.sh does not prune them).
set -euo pipefail

[ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }
APP=/opt/tts-bot
MODELS="$APP/models"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
# shellcheck source=lib/deploy_validate.sh
. "$HERE/lib/deploy_validate.sh"

force=0
args=()
for a in "$@"; do
  case "$a" in
    --force) force=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) args+=("$a") ;;
  esac
done
[ "${#args[@]}" -ge 1 ] && [ "${#args[@]}" -le 2 ] || { echo "Usage: $0 [--force] <voice.onnx> [<voice.onnx.json>]" >&2; exit 2; }
src_onnx="${args[0]}"
src_json="${args[1]:-${src_onnx}.json}"

PY="$APP/env/bin/python"
[ -x "$PY" ] || PY=python3
if [ -L "$MODELS" ] || [ ! -d "$MODELS" ]; then
  echo "$MODELS is missing or a symlink; run scripts/setup_system.sh and deploy first." >&2; exit 1
fi

base="$(basename -- "$src_onnx")"
case "$base" in *.onnx) stem="${base%.onnx}" ;; *) echo "Voice model must be named <name>.onnx." >&2; exit 1 ;; esac
voice_stem_ok "$stem" || { echo "Invalid voice name '$stem': use only A-Z a-z 0-9 _ . + - (max 100, no leading dot)." >&2; exit 1; }
for f in "$src_onnx" "$src_json"; do
  if [ -L "$f" ] || [ ! -f "$f" ]; then
    echo "Refusing: $f is a symlink or not a regular file." >&2; exit 1
  fi
done
if [ "$force" -ne 1 ] && { [ -e "$MODELS/$stem.onnx" ] || [ -e "$MODELS/$stem.onnx.json" ]; }; then
  echo "Voice '$stem' is already installed. Use --force to replace it." >&2; exit 1
fi

# Copy into a private root-owned staging dir inside $MODELS (same filesystem => atomic mv; the dot name is
# invisible to the bot's rescan), then validate the COPY, so the source cannot change between check and use.
stage="$(mktemp -d "$MODELS/.install.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
chmod 700 "$stage"
cp -P --no-preserve=all -- "$src_onnx" "$stage/$stem.onnx"
cp -P --no-preserve=all -- "$src_json" "$stage/$stem.onnx.json"
validate_voice_files "$stage/$stem.onnx" "$stage/$stem.onnx.json" "$PY"

chown root:root "$stage/$stem.onnx" "$stage/$stem.onnx.json"
chmod 644 "$stage/$stem.onnx" "$stage/$stem.onnx.json"
# config first, model last: the bot lists a voice only when both exist, so a rescan never sees a half install
# Use mv -n (no-clobber) to prevent a race between the check and the move: fail if target appeared.
if [ "$force" -ne 1 ]; then
  mv -n -- "$stage/$stem.onnx.json" "$MODELS/$stem.onnx.json" || { echo "Voice '$stem' was installed by another process; aborting." >&2; exit 1; }
  mv -n -- "$stage/$stem.onnx" "$MODELS/$stem.onnx" || { echo "Voice '$stem' was installed by another process; aborting." >&2; exit 1; }
else
  mv -f -- "$stage/$stem.onnx.json" "$MODELS/$stem.onnx.json"
  mv -f -- "$stage/$stem.onnx" "$MODELS/$stem.onnx"
fi
echo "Installed voice '$stem' in $MODELS (no restart needed)."
echo "Rescan in the GUI (Voice tab -> Rescan), then select it and press Apply."
