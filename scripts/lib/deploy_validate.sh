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

# ---- voice installation (used by scripts/install_voice.sh; needs no root) ----

# voice_stem_ok STEM: same rule as voices.py (VOICE_NAME_RE, no leading dot).
voice_stem_ok() {
  local LC_ALL=C  # byte-wise ranges: in a UTF-8 locale [A-Za-z] can match accented letters
  case "$1" in .*) return 1 ;; esac
  [[ "$1" =~ ^[A-Za-z0-9_.+-]{1,100}$ ]]
}

# validate_voice_files ONNX JSON [PYTHON]
#   ONNX must be named <stem>.onnx with a valid stem; both must be regular non-symlink files; the model
#   must be > 1 MiB and not text (a saved HTML error page is the classic bad download); the JSON must be
#   <= 1 MiB, parse, carry audio.sample_rate (positive int) and not be multi-speaker (unsupported).
#   Returns 1 with a message on any violation. Callers validate a private COPY (race-free).
validate_voice_files() {
  local onnx="$1" json="$2" py="${3:-python3}" base stem size nontext
  base="$(basename -- "$onnx")"
  case "$base" in *.onnx) stem="${base%.onnx}" ;; *) echo "Voice model must be named <name>.onnx." >&2; return 1 ;; esac
  voice_stem_ok "$stem" || { echo "Invalid voice name '$stem': use only A-Z a-z 0-9 _ . + - (max 100, no leading dot)." >&2; return 1; }
  local f
  for f in "$onnx" "$json"; do
    if [ -L "$f" ] || [ ! -f "$f" ]; then
      echo "Refusing: $f is a symlink or not a regular file." >&2; return 1
    fi
  done
  size="$(stat -c %s -- "$onnx")"
  [ "$size" -gt 1048576 ] || { echo "Refusing: $base is only $size bytes (not a plausible voice model)." >&2; return 1; }
  nontext="$(head -c 1024 -- "$onnx" | LC_ALL=C tr -d '[:print:][:space:]' | wc -c)"
  [ "$nontext" -gt 0 ] || { echo "Refusing: $base looks like a text file, not an ONNX model (failed download?)." >&2; return 1; }
  [ "$(stat -c %s -- "$json")" -le 1048576 ] || { echo "Refusing: voice config JSON is too large." >&2; return 1; }
  "$py" - "$json" <<'PYEOF' || return 1
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        cfg = json.load(f)
except (OSError, ValueError):
    sys.exit("Refusing: voice config is not valid JSON.")
audio = cfg.get("audio") if isinstance(cfg, dict) else None
rate = audio.get("sample_rate") if isinstance(audio, dict) else None
if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
    sys.exit("Refusing: voice config has no audio.sample_rate.")
spk = cfg.get("num_speakers", 1)
if isinstance(spk, bool) or not isinstance(spk, int) or spk > 1:
    sys.exit("Refusing: multi-speaker voices are not supported.")
PYEOF
}
