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

[ $fails -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
