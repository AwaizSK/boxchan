#!/usr/bin/env bash
# Tests scripts/lib/deploy_validate.sh as a normal user, on throwaway temp trees with fake files.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
. "$HERE/scripts/lib/deploy_validate.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0

mk() {  # fresh clean repo tree at $T/repo
  rm -rf "$T/repo" "$T/outside" "$T/snap"; mkdir -p "$T/repo/static" "$T/repo/models" "$T/outside" "$T/snap"
  echo x > "$T/repo/bot.py"; echo x > "$T/repo/requirements.txt"; echo x > "$T/repo/static/a.js"
  echo x > "$T/repo/models/m.onnx"; echo x > "$T/repo/models/m.onnx.json"
  echo secret > "$T/outside/s.txt"
  SRC="$(cd "$T/repo" && pwd -P)"
}
expect() {  # name want(0|1)
  snapshot_sources "$SRC" "$T/snap" 2>/dev/null; local rc=$?
  if { [ "$2" -eq 0 ] && [ $rc -eq 0 ]; } || { [ "$2" -ne 0 ] && [ $rc -ne 0 ]; }; then echo "ok   $1"
  else echo "FAIL $1 (rc=$rc)"; fails=$((fails+1)); fi
}

mk; expect "clean tree accepted" 0
[ -f "$T/snap/static/a.js" ] && [ -f "$T/snap/models/m.onnx" ] && [ -f "$T/snap/bot.py" ] || { echo "FAIL snapshot layout"; fails=$((fails+1)); }

mk; rm -rf "$T/repo/static"; ln -s "$T/outside" "$T/repo/static"; expect "symlinked static dir rejected" 1
mk; rm -rf "$T/repo/models"; ln -s "$T/outside" "$T/repo/models"; expect "symlinked models dir rejected" 1
mk; ln -s "$T/outside/s.txt" "$T/repo/static/leak.txt"; expect "leaf symlink (outside) rejected" 1
mk; ln -s "$T/repo/bot.py" "$T/repo/static/inrepo.js"; expect "symlink pointing inside repo rejected" 1
mk; mkdir "$T/repo/static/sub"; expect "subdirectory in static rejected" 1
mk; ln -sf "$T/outside/s.txt" "$T/repo/requirements.txt"; expect "symlinked requirements.txt rejected" 1
mk; ln -s "$T/outside/s.txt" "$T/repo/evil.py"; expect "symlinked root .py rejected" 1
mk; rm "$T/repo/models/m.onnx"; ln -s "$T/outside/s.txt" "$T/repo/models/m.onnx"; expect "symlinked model rejected" 1
mk; rm "$T/repo/bot.py"; expect "no .py files rejected" 1

# ---- voice validation (scripts/install_voice.sh uses validate_voice_files on a private copy) ----
PY="${PYTHON:-python3}"
GOODJSON='{"audio":{"sample_rate":22050,"quality":"medium"},"num_speakers":1,"espeak":{"voice":"en-us"}}'
mkv() {  # fresh voice dir with a plausible binary model + good config
  rm -rf "$T/v"; mkdir -p "$T/v"
  { printf '\x08\x07'; head -c 1200000 /dev/urandom; } > "$T/v/en_US-t-medium.onnx"
  echo "$GOODJSON" > "$T/v/en_US-t-medium.onnx.json"
}
vexpect() {  # name want(0|1) onnx json
  validate_voice_files "$3" "$4" "$PY" 2>/dev/null; local rc=$?
  if { [ "$2" -eq 0 ] && [ $rc -eq 0 ]; } || { [ "$2" -ne 0 ] && [ $rc -ne 0 ]; }; then echo "ok   $1"
  else echo "FAIL $1 (rc=$rc)"; fails=$((fails+1)); fi
}
V="$T/v/en_US-t-medium.onnx"; VJ="$V.json"
mkv; vexpect "valid voice accepted" 0 "$V" "$VJ"
mkv; echo '{"audio":{"sample_rate":16000}}' > "$VJ"; vexpect "config without num_speakers accepted" 0 "$V" "$VJ"
for bad in "has space" "semi;colon" ".hidden" $'uni\xc3\xa9' "$(printf 'x%.0s' $(seq 101))"; do
  mkv; cp "$V" "$T/v/$bad.onnx" 2>/dev/null && vexpect "bad stem '${bad:0:12}' rejected" 1 "$T/v/$bad.onnx" "$VJ"
done
mkv; cp "$V" "$T/v/-rf.onnx"; vexpect "leading dash is a legal name (all tools use --)" 0 "$T/v/-rf.onnx" "$VJ"
mkv; cp "$V" "$T/v/model.bin"; vexpect "non-.onnx name rejected" 1 "$T/v/model.bin" "$VJ"
mkv; head -c 1000 "$V" > "$T/v/small.onnx"; vexpect "tiny model rejected" 1 "$T/v/small.onnx" "$VJ"
mkv; yes "<html>404 not found</html>" | head -c 1500000 > "$T/v/text.onnx"; vexpect "text file posing as model rejected" 1 "$T/v/text.onnx" "$VJ"
mkv; ln -s "$V" "$T/v/link.onnx"; vexpect "symlinked model rejected" 1 "$T/v/link.onnx" "$VJ"
mkv; ln -s "$VJ" "$T/v/lj.json"; vexpect "symlinked config rejected" 1 "$V" "$T/v/lj.json"
mkv; vexpect "missing config rejected" 1 "$V" "$T/v/nope.json"
mkdir -p "$T/v/dir.onnx"; vexpect "directory rejected" 1 "$T/v/dir.onnx" "$VJ"
mkv; echo '{nope' > "$VJ"; vexpect "invalid JSON rejected" 1 "$V" "$VJ"
mkv; echo '{"audio":{}}' > "$VJ"; vexpect "missing sample_rate rejected" 1 "$V" "$VJ"
mkv; echo '{"audio":{"sample_rate":"22050"}}' > "$VJ"; vexpect "string sample_rate rejected" 1 "$V" "$VJ"
mkv; echo '{"audio":{"sample_rate":22050},"num_speakers":904}' > "$VJ"; vexpect "multi-speaker rejected" 1 "$V" "$VJ"
mkv; echo '[1]' > "$VJ"; vexpect "non-object JSON rejected" 1 "$V" "$VJ"

# ---- deploy.sh must not prune models/ (installed voices survive a deploy) ----
if grep -nE 'rm .*models|"\$APP"/models/\*' "$HERE/scripts/deploy.sh" | grep -v '^[0-9]*:#' | grep -vq 'install -m 644'; then
  echo "FAIL deploy.sh appears to prune models/"; fails=$((fails+1)); else echo "ok   deploy.sh does not prune models"; fi
bash -n "$HERE/scripts/install_voice.sh" && echo "ok   install_voice.sh syntax" || { echo "FAIL install_voice.sh syntax"; fails=$((fails+1)); }
grep -q 'set -euo pipefail' "$HERE/scripts/install_voice.sh" && echo "ok   install_voice.sh strict mode" || { echo "FAIL strict mode"; fails=$((fails+1)); }

# ---- install_voice.sh race condition tests (temp dir only, no sudo, no /opt) ----
INSTALL_TEST_DIR="$T/install_test"
mkdir -p "$INSTALL_TEST_DIR/models" "$INSTALL_TEST_DIR/src"
# Create test voice files
{ printf '\x08\x07'; head -c 1200000 /dev/urandom; } > "$INSTALL_TEST_DIR/src/test_voice.onnx"
echo '{"audio":{"sample_rate":22050,"quality":"medium"},"num_speakers":1,"espeak":{"voice":"en-us"}}' > "$INSTALL_TEST_DIR/src/test_voice.onnx.json"

# Test 1: Fresh install succeeds
if INSTALL_VOICE_SKIP_ROOT_CHECK=1 PIPER_MODELS_DIR_OVERRIDE="$INSTALL_TEST_DIR/models" bash "$HERE/scripts/install_voice.sh" "$INSTALL_TEST_DIR/src/test_voice.onnx" "$INSTALL_TEST_DIR/src/test_voice.onnx.json" >/dev/null 2>&1; then
  [ -f "$INSTALL_TEST_DIR/models/test_voice.onnx" ] && [ -f "$INSTALL_TEST_DIR/models/test_voice.onnx.json" ] && echo "ok   install_voice.sh fresh install succeeds" || { echo "FAIL fresh install files missing"; fails=$((fails+1)); }
else
  echo "FAIL install_voice.sh fresh install failed"; fails=$((fails+1));
fi

# Test 2: Second install without --force fails and leaves original untouched
ORIG_ONNX_MD5="$(md5sum "$INSTALL_TEST_DIR/models/test_voice.onnx" | cut -d' ' -f1)"
if INSTALL_VOICE_SKIP_ROOT_CHECK=1 PIPER_MODELS_DIR_OVERRIDE="$INSTALL_TEST_DIR/models" bash "$HERE/scripts/install_voice.sh" "$INSTALL_TEST_DIR/src/test_voice.onnx" "$INSTALL_TEST_DIR/src/test_voice.onnx.json" >/dev/null 2>&1; then
  echo "FAIL install_voice.sh second install should have failed"; fails=$((fails+1));
else
  CURR_ONNX_MD5="$(md5sum "$INSTALL_TEST_DIR/models/test_voice.onnx" | cut -d' ' -f1)"
  if [ "$ORIG_ONNX_MD5" = "$CURR_ONNX_MD5" ]; then
    echo "ok   install_voice.sh second install fails and preserves original"
  else
    echo "FAIL second install overwrote the file"; fails=$((fails+1));
  fi
fi

# Test 3: With --force it overwrites (re-install the original after the failed attempt above)
# Recreate the original good voice files
{ printf '\x08\x07'; head -c 1200000 /dev/urandom; } > "$INSTALL_TEST_DIR/src/test_voice.onnx"
if INSTALL_VOICE_SKIP_ROOT_CHECK=1 PIPER_MODELS_DIR_OVERRIDE="$INSTALL_TEST_DIR/models" bash "$HERE/scripts/install_voice.sh" --force "$INSTALL_TEST_DIR/src/test_voice.onnx" "$INSTALL_TEST_DIR/src/test_voice.onnx.json" >/dev/null 2>&1; then
  [ -f "$INSTALL_TEST_DIR/models/test_voice.onnx" ] && echo "ok   install_voice.sh --force overwrites" || { echo "FAIL --force did not create file"; fails=$((fails+1)); }
else
  echo "FAIL install_voice.sh --force install failed"; fails=$((fails+1));
fi

# Test 4: No staging files left behind
if [ -z "$(find "$INSTALL_TEST_DIR/models" -name '.install.*' -type d 2>/dev/null)" ]; then
  echo "ok   install_voice.sh no staging files left behind"
else
  echo "FAIL staging files left behind"; fails=$((fails+1));
fi

[ $fails -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
