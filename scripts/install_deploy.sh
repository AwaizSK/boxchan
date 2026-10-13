#!/usr/bin/env bash
# Root-only, run by an admin with real sudo:
#   sudo bash scripts/install_deploy.sh --src DIR --runner-user NAME [--remove-only]
# Installs the deploy wrapper where only root can change it:
#   /usr/local/sbin/tts-bot-deploy            (copy of scripts/deploy.sh)
#   /usr/local/lib/tts-bot/deploy_validate.sh (copy of scripts/lib/deploy_validate.sh)
#   /etc/tts-bot/deploy-src                   (the directory to deploy, one line)
#   /etc/sudoers.d/tts-bot-deploy             (NAME may run the wrapper with NO arguments; replaces the old rule)
# Re-run after every change to deploy.sh or the lib. Idempotent.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/install_deploy_lib.sh
. "$HERE/lib/install_deploy_lib.sh"

usage() { echo "usage: sudo bash scripts/install_deploy.sh --src DIR --runner-user NAME [--remove-only]"; exit 2; }

SRC="" RUNNER="" REMOVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --src) [ $# -ge 2 ] || usage; SRC="$2"; shift 2 ;;
    --runner-user) [ $# -ge 2 ] || usage; RUNNER="$2"; shift 2 ;;
    --remove-only) REMOVE=1; shift ;;
    *) usage ;;
  esac
done

[ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }

BIN=/usr/local/sbin/tts-bot-deploy
LIBDIR=/usr/local/lib/tts-bot
CONFDIR=/etc/tts-bot
SUDOERS=/etc/sudoers.d/tts-bot-deploy

if [ "$REMOVE" -eq 1 ]; then
  rm -f "$SUDOERS" "$BIN" "$LIBDIR/deploy_validate.sh" "$CONFDIR/deploy-src"
  rmdir "$LIBDIR" 2>/dev/null || true
  echo "Removed: sudoers rule, wrapper, lib, deploy-src. Nobody can deploy via sudo now."
  exit 0
fi

[ -n "$SRC" ] && [ -n "$RUNNER" ] || usage
valid_src_dir "$SRC" || { echo "invalid --src (must be an absolute existing directory, not / or inside /opt/tts-bot)"; exit 1; }
valid_user_name "$RUNNER" || { echo "invalid --runner-user (must match ^[a-z_][a-z0-9_-]{0,31}\$ and not be root)"; exit 1; }
id -u "$RUNNER" >/dev/null 2>&1 || { echo "user $RUNNER does not exist"; exit 1; }
SRC="$(cd "$SRC" && pwd -P)"
[ -f "$HERE/deploy.sh" ] && [ -f "$HERE/lib/deploy_validate.sh" ] || { echo "deploy.sh / lib/deploy_validate.sh missing next to this script"; exit 1; }

echo "WARNING: $HERE/deploy.sh and $HERE/lib/deploy_validate.sh are about to become code that runs as root."
echo "         Review them first (and the git history of the checkout you are installing from)."

# atomic_install MODE SRCFILE DEST: temp file in the destination dir, then mv.
atomic_install() {
  local mode="$1" from="$2" to="$3" tmp
  tmp="$(mktemp "$(dirname "$to")/.install.XXXXXX")"
  install -m "$mode" -o root -g root "$from" "$tmp"
  mv -f "$tmp" "$to"
}

install -d -m 755 -o root -g root "$LIBDIR"
install -d -m 700 -o root -g root "$CONFDIR"
atomic_install 755 "$HERE/deploy.sh" "$BIN"
atomic_install 644 "$HERE/lib/deploy_validate.sh" "$LIBDIR/deploy_validate.sh"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
printf '%s\n' "$SRC" > "$tmp"
atomic_install 644 "$tmp" "$CONFDIR/deploy-src"

render_sudoers_line "$RUNNER" > "$tmp"
visudo -cf "$tmp" >/dev/null
atomic_install 440 "$tmp" "$SUDOERS"

echo "Installed:"
echo "  $BIN (root:root 0755)"
echo "  $LIBDIR/deploy_validate.sh (root:root 0644)"
echo "  $CONFDIR/deploy-src -> $SRC"
echo "  $SUDOERS: $(render_sudoers_line "$RUNNER")"
echo "devteam members can no longer run deploy.sh via sudo; only $RUNNER can run $BIN (no arguments)."
echo "The /etc/sudoers.d/tts-bot rule (start/stop/restart) is untouched."
