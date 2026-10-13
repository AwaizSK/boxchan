#!/usr/bin/env bash
# Sourced by deploy.sh (and tests/test_deploy_validation.sh). Defines functions only; needs no root.

# snapshot_sources SRC SNAP
#   SRC must already be physically resolved (pwd -P). Validates every deployable file and copies it into
#   SNAP (root files, static/, models/). Returns 1 with a message on any violation.
#   Residual race: a file could be swapped between the realpath check and the cp (microseconds);
#   the snapshot is re-verified afterwards, so a swapped-in symlink is still caught.
_dv_check_file() {  # SRC file
  local src="$1" f="$2" real
  if [ -L "$f" ] || [ ! -f "$f" ]; then
    echo "Refusing to deploy: $f is a symlink or not a regular file." >&2; return 1
  fi
  real="$(realpath -e -- "$f")" || { echo "Refusing to deploy: cannot resolve $f." >&2; return 1; }
  case "$real" in
    "$src"/*) ;;
    *) echo "Refusing to deploy: $f resolves outside the repo ($real)." >&2; return 1 ;;
  esac
}

snapshot_sources() {
  local src="$1" snap="$2" f rel
  local -a files=()
  for d in static models; do
    if [ -L "$src/$d" ] || [ ! -d "$src/$d" ]; then
      echo "Refusing to deploy: $src/$d is a symlink or not a directory." >&2; return 1
    fi
  done
  shopt -s nullglob
  local -a pyfiles=("$src"/*.py) staticfiles=("$src"/static/*) models=("$src"/models/*.onnx "$src"/models/*.onnx.json)
  shopt -u nullglob
  [ "${#pyfiles[@]}" -gt 0 ] || { echo "No *.py files found in $src" >&2; return 1; }
  [ "${#staticfiles[@]}" -gt 0 ] || { echo "No files found in $src/static" >&2; return 1; }
  [ "${#models[@]}" -gt 0 ] || { echo "No model files found in $src/models" >&2; return 1; }
  files=("${pyfiles[@]}" "${staticfiles[@]}" "${models[@]}" "$src/requirements.txt")
  for f in "${files[@]}"; do _dv_check_file "$src" "$f" || return 1; done

  mkdir -p "$snap/static" "$snap/models"
  for f in "${files[@]}"; do
    rel="${f#"$src"/}"
    cp -P --no-preserve=all -- "$f" "$snap/$rel" || return 1
  done
  if [ -n "$(find "$snap" -type l -print -quit)" ] || [ -n "$(find "$snap" ! -type f ! -type d -print -quit)" ]; then
    echo "Refusing to deploy: snapshot contains a symlink or special file." >&2; return 1
  fi
}
