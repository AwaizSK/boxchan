#!/usr/bin/env bash
# tts-bot deploy wrapper. INSTALLED root-owned at /usr/local/sbin/tts-bot-deploy by scripts/install_deploy.sh
# (an admin with real sudo runs that after reviewing this file and scripts/lib/deploy_validate.sh).
# Running it from the repo checkout is refused by design: the checkout is group-writable.
#
# Trust boundary: everything root executes lives in root-owned paths (this file, /usr/local/lib/tts-bot).
# The caller can influence ONLY the data deployed: the source directory named in /etc/tts-bot/deploy-src
# (root-owned), validated and snapshotted (symlink/containment checks) before anything lands in /opt/tts-bot.
# Paths and PATH are hard-coded; env vars are ignored; the sudoers rule allows NO arguments.
# An optional $1 (source dir) is usable only by a real root/admin.
# Prunes stale *.py and static/ files in /opt/tts-bot; never prunes models/.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 022

LIB=/usr/local/lib/tts-bot
APP=/opt/tts-bot
CONF=/etc/tts-bot/deploy-src

# assert_trusted_path PATH [OWNER_UID]: regular file or directory (not a symlink), owned by OWNER_UID
# (default 0), not group/other-writable.
assert_trusted_path() {
  local p="$1" want="${2:-0}" st uid mode
  [ -n "$p" ] && [ ! -L "$p" ] && { [ -f "$p" ] || [ -d "$p" ]; } || return 1
  st="$(stat -c '%u %a' -- "$p")" || return 1
  uid="${st% *}"; mode="${st#* }"
  [ "$uid" = "$want" ] || return 1
  [ $(( 8#$mode & 8#022 )) -eq 0 ] || return 1
}

# validate_src_dir DIR: absolute, resolves to an existing directory, not / and not /opt/tts-bot (or inside).
# Prints the resolved path.
validate_src_dir() {
  local d="$1" r
  case "$d" in /*) ;; *) return 1 ;; esac
  r="$(cd -- "$d" 2>/dev/null && pwd -P)" || return 1
  [ "$r" != "/" ] || return 1
  case "$r" in /opt/tts-bot|/opt/tts-bot/*) return 1 ;; esac
  printf '%s\n' "$r"
}

main() {
  local self="${BASH_SOURCE[0]}"
  if ! { assert_trusted_path "$self" && assert_trusted_path "$LIB" && assert_trusted_path "$LIB/deploy_validate.sh"; }; then
    echo "deploy: refusing — wrapper/lib not root-owned (install with scripts/install_deploy.sh)"
    exit 1
  fi
  [ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }

  local SRC PY="$APP/env/bin/python" snap
  if [ "$#" -gt 1 ]; then echo "deploy: too many arguments"; exit 1; fi
  if [ "$#" -eq 1 ]; then
    SRC="$1"
  else
    SRC="$(grep -m1 -v '^[[:space:]]*$' "$CONF" 2>/dev/null || true)"
  fi
  SRC="$(validate_src_dir "$SRC")" || { echo "deploy: invalid source directory (see $CONF)"; exit 1; }

  [ -x "$PY" ] || { echo "Run scripts/setup_system.sh first."; exit 1; }

  # Validate + snapshot everything before touching /opt: all later steps read only from $snap
  # (closes the check/copy race; root never follows repo symlinks).
  snap="$(mktemp -d)"
  trap 'rm -rf "$snap"' EXIT
  chmod 700 "$snap"
  # Sourced from the fixed root-owned LIB, never from the source dir.
  # shellcheck source=lib/deploy_validate.sh
  . "$LIB/deploy_validate.sh"
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
  # models/ is deliberately NOT pruned: voices added with scripts/install_voice.sh are not in the repo and
  # must survive a deploy. (Repo models are copied/updated above; a repo model with the same name as an
  # installed voice replaces it.)
  rm -rf "$APP/__pycache__"

  systemctl restart tts_bot
  sleep 2
  systemctl --no-pager --lines=15 status tts_bot || true
}

# Only run when executed, not when sourced (tests source this file for the functions).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
