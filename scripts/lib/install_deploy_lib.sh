# Pure helpers for scripts/install_deploy.sh (sourced; also exercised by tests/test_deploy_validation.sh).
# No side effects on source.

DEPLOY_WRAPPER_PATH=/usr/local/sbin/tts-bot-deploy

# valid_user_name NAME: conservative unix name, never root.
valid_user_name() {
  local n="${1-}"
  [ "$n" != "root" ] || return 1
  [[ "$n" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]
}

# valid_src_dir DIR: absolute, existing directory, not / and not /opt/tts-bot (or inside).
valid_src_dir() {
  local d="${1-}" r
  case "$d" in /*) ;; *) return 1 ;; esac
  r="$(cd -- "$d" 2>/dev/null && pwd -P)" || return 1
  [ "$r" != "/" ] || return 1
  case "$r" in /opt/tts-bot|/opt/tts-bot/*) return 1 ;; esac
  return 0
}

# render_sudoers_line NAME: the single sudoers rule; the trailing "" means NO arguments are allowed.
render_sudoers_line() {
  valid_user_name "${1-}" || return 1
  printf '%s ALL=(root) NOPASSWD: %s ""\n' "$1" "$DEPLOY_WRAPPER_PATH"
}
