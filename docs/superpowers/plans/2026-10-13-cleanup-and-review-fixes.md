# Cleanup + Review-Fix Plan (watcher removal, voice-swap and Messages-tab review findings)

**Spec:** user instructions in the session: (1) remove the file watcher — "I'll have them rerun the app when needed"; (2) fix the remaining open findings from the voice-swap and Messages-tab security/quality reviews; (3) docs/tests stay accurate. `repomix-output.xml` stays (user uses it for agent awareness). No separate spec file; rulings are provisional.

## Global Constraints

- Python 3.11 at `~/miniconda3/envs/tts-bot/bin/python`; no new dependencies (stdlib + aiohttp + existing discord.py/piper/onnxruntime). Tests: `unittest` only. Run: `~/miniconda3/envs/tts-bot/bin/python -m unittest discover -s tests -v`, `bash tests/test_deploy_validation.sh`, `node --check static/app.js`, `node tests/static_logic.js`, `bash -n scripts/*.sh`.
- Read CLAUDE.md and docs/ARCHITECTURE.md first. Hard rules from CLAUDE.md apply: never read/print/open `.env`, `~/.config/tts-bot/*`, `/etc/tts-bot/*` or any file named `token`/`gui_token`; never run `deploy.sh`/`setup_system.sh`/`install_voice.sh` against real paths; never use sudo; do not dispatch subagents.
- The GUI has NO token; the boundary is the network (Tailscale ACLs) plus the peer/Host/Origin guards. Do not weaken any guard. Frontend stays vanilla JS, `textContent` only, strict CSP, static payload cap 57344 bytes (`tests/test_static.py`).
- Some findings below may already be fixed by commit b6d21d8. For EACH listed item first check the current code: if already satisfied, say so in the report and add a test only if one is missing; do not rewrite working code.
- Commit with a conventional message ending with the line `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Honesty: no browser, live Discord or GPU is available; do not claim UI/Discord/GPU behavior was verified.

## Rationale

1. Removed watcher: a root daemon that auto-deploys whatever lands in the group-writable repo lets any `devteam` member (or an agent running as one) run code as the bot with the Discord token credential, and as root through the repo-sourced `scripts/lib/deploy_validate.sh`, with no admin review. Production deploys stay manual: `sudo scripts/deploy.sh` after review.
2. Voice swap must never silently degrade GPU -> CPU: on the 6 GB GTX 1060 the new model is built while the old session is still resident, so a CUDA OOM during a swap is the likely failure; refusing the swap is safer than a 10x slower voice with a "success" toast.
3. Anything the GUI sends must be what the confirmation line says: the "Will ping" summary must be derived from the exact final text sent, including raw `<@id>` tokens.

## File Structure (touched)

```
scripts/watch_and_deploy.py, scripts/install_watcher.sh   DELETE (Task 1)
scripts/deploy.sh                                          MOD   (Task 1: revert SRC argument)
tts_engine.py, voices.py, bot.py, webui.py, static/app.js, scripts/install_voice.sh   MOD (Task 2)
messages.py, bot.py, webui.py, static/app.js, static/app.css   MOD (Task 3)
README.md, docs/ARCHITECTURE.md, CLAUDE.md, .gitignore, tests/*   MOD (Tasks 1-4)
```

---

### Task 1: Remove the file watcher

**Files:** delete `scripts/watch_and_deploy.py` and `scripts/install_watcher.sh`; modify `scripts/deploy.sh`; docs.

1. `git rm` both watcher files.
2. `scripts/deploy.sh`: restore the original source resolution — `SRC="$(cd "$(dirname "$0")/.." && pwd -P)"` — removing the `"${1:-...}"` argument form and the second `SRC="$(cd "$SRC" && pwd -P)"` line added by commit 360835e (`git show 360835e -- scripts/deploy.sh` shows the exact change). Keep everything else in deploy.sh unchanged, including the header comment lines about pruning.
3. Search the whole repo (excluding `.git`, `.superpowers`, `docs/superpowers/plans`, `repomix-output.xml`) for `watch_and_deploy`, `install_watcher`, `tts_bot_watcher`, `auto-deploy`, "watcher" (the unrelated `config_watcher` in bot.py is the config-event task and MUST stay) and remove/adjust any mention in README.md, docs/ARCHITECTURE.md, CLAUDE.md, tests, scripts. If a test references `deploy.sh` taking an argument, update it.
4. Add a short note to docs/ARCHITECTURE.md (decision log, new entry with Why / Alternatives rejected / Cost) recording that auto-deploy was deliberately NOT adopted and why (Rationale 1 above), and what to do instead for fast iteration (run the bot as your own user against a dev bot token with `python bot.py`; production deploy stays `sudo scripts/deploy.sh` after review). Mention it in Known limitations: deploys are manual.
5. Add `repomix-output.xml` to `.gitignore` (the user keeps the file locally for agent awareness; it must not be committed).
6. Verify: `bash -n scripts/*.sh`, the full unittest suite, `bash tests/test_deploy_validation.sh`; `git grep -n -i "watch_and_deploy\|install_watcher\|tts_bot_watcher"` returns nothing outside historical plan files.

Commit: `chore: remove auto-deploy file watcher (trust-boundary regression)`.

---

### Task 2: Voice-swap review findings

**Files:** `tts_engine.py`, `voices.py`, `bot.py`, `webui.py`, `static/app.js`, `scripts/install_voice.sh`, tests, docs.

Items (verify each against the current code first, see Global Constraints):

1. **[Important] No silent GPU -> CPU downgrade on swap.** In `TTSEngine.reload`: if the CURRENTLY loaded voice runs on CUDA (`cuda_active` True) and the candidate could only be built on CPU (CUDA init/OOM fell back), raise `VoiceLoadError` (safe message, e.g. "GPU unavailable for this voice") and keep the old voice and old `cuda_active` — do not swap. Startup behavior (first load may fall back to CPU) is unchanged. Also expose `cuda` (bool) in the voice summary returned by `Controller.list_voices()` and in the `voice` SSE event payloads, and show a small "CPU" badge next to the Active voice in the Voice tab when `cuda` is false. Tests: fake PiperVoice factory where the CUDA build raises -> swap refused, old voice still serves, `loading` returns to False, `cuda_active` unchanged; CPU-to-CPU swap allowed.
2. **State assignment atomicity.** Event-loop readers can briefly see the new `voice` with the old `model_path`/`model_name`. Hold `voice`, `sample_rate`, `model_path`, `config_path`, `cuda_active` in one small immutable object (e.g. a frozen dataclass or a tuple) assigned with a single statement, with the existing properties/attributes (`engine.voice`, `engine.sample_rate`, `engine.model_name`, `engine.cuda_active`, `engine.model_path`) kept working for callers and tests. Also make `load()` assign model/config paths only after a successful build.
3. **Unlogged exception after client disconnect.** The shielded `apply_voice` call (and the shielded `say`/other write paths in webui.py that use the same pattern, if any) must run as a task with a done-callback that logs any non-expected exception (no exception text in responses; log with `log.exception`-style output on the server) so "Task exception was never retrieved" cannot occur. Test it (cancel the handler mid-apply with an engine that then raises; assertLogs).
4. **Symlinked voice files.** In `voices.py` `discover_voices`/`resolve_voice`: skip any `.onnx` or `.onnx.json` that is a symlink or whose `os.path.realpath` is not inside the realpath of the models dir (debug log, basename only). Tests with real symlinks in a temp dir.
5. **Loading-state ordering.** `mergeVoiceSummary` in `static/app.js`: a `GET /api/voices` response arriving after a newer SSE `voice` event must not overwrite the live `loading` flag. Implement with a monotonic local sequence/timestamp (count of `voice` SSE events seen; a GET response started before the latest event must not override `loading`/`current`), or ignore `loading` from GET while the SSE stream is connected. Add a pure helper and cover it in `tests/static_logic.js`.
6. **install_voice.sh no-overwrite race.** When `--force` is not given, use `mv -n` (or `ln` + `rm`) so a concurrent install cannot be overwritten between the `-e` check and the move; fail with a clear message if the target appeared. Add/extend a bash test for the non-force path in `tests/test_deploy_validation.sh` style (temp dirs only; never run against `/opt`).
7. **Doc corrections** (docs/ARCHITECTURE.md): the voice is persisted by `ConfigStore.set_voice`, not `_mutate` (~line 162); the route error-code list must include `bad_voice` and `too_long` (~line 101); the "scripts source repo files as root" bullet must also name `install_voice.sh` (it sources `scripts/lib/deploy_validate.sh` from the repo as root); document the CUDA-refusal behavior and the `cuda` field; update the test counts if they appear.

Commit(s): `fix: refuse GPU->CPU voice swaps, atomic engine state, symlink-safe voice discovery` and `fix: voice loading-state ordering, install_voice no-clobber, docs`.

---

### Task 3: Messages-tab review findings

**Files:** `messages.py`, `bot.py`, `webui.py`, `static/app.js`, `static/app.css`, tests, docs.

Items (verify each against the current code first):

1. **[Important] "Will ping" and the preview must reflect the exact final text.** In `static/app.js`: compute the ping summary and the preview from the SAME string the client will send (after `substituteTags`), by scanning it with `/<@!?(\d+)>/g`; map each id to a chip label (show label and `@username`), falling back to "unknown user <id>"; raw `<@id>` tokens typed or pasted by the user must appear in the summary and be highlighted in the preview. Include the case where a member's display name is itself `<@victimId>` glued to a word character (e.g. `x@<@5> hi`): the summary must list user 5. Add pure helpers + tests in `tests/static_logic.js` (including that exact case, duplicates, `<@!id>` form).
2. **[Important] Bounded member scan.** `Controller.members_for`: cap permission checks per request (e.g. at most 2000 `permissions_for` evaluations) and `await asyncio.sleep(0)` every ~250 members; when the cap is hit return `truncated: true` with what was found; do not sort the entire guild before filtering when `q` is empty if it can be avoided cheaply (keep results deterministic: non-bots first, then display name). Correct docs/ARCHITECTURE.md flow "(e) step 6" which claims large guilds cost "one sort plus a few permission checks". Tests with a fake guild of 5,000 members where almost none can view the channel: the scan stops at the cap, yields, and returns `truncated`.
3. **Lookbehind fallback.** `static/app.js` tag substitution uses a regex lookbehind that throws on Safari < 16.4 and breaks the whole render path. Replace with a manual preceding-character check (`/[\p{L}\p{N}_]/u.test(prevChar)`), or wrap in try/catch with a safe fallback that disables Send with a visible message; keep behavior identical for supported browsers (existing tests must still pass; add one for the glued-to-word-character case).
4. **Cap mentions per message.** Server (`messages.py`/`Controller.send_message`): reject with 400 `bad_mention` when a message contains more than 10 distinct user mentions; the UI shows the count and disables Send over the limit. Tests + docs (README Messages section, ARCHITECTURE).
5. **Chip shows the username.** Chips, the chip `title`, and the "Will ping" line show `@Display Name (@username)` so two members with the same display name are distinguishable; the member picker rows already show username.
6. **Audit failed sends.** `Controller.send_message`: `discord.Forbidden` also logs a WARNING (`gui message failed guild=%s channel=%s editor=%s reason=forbidden`, no content); same for HTTPException (already warns — keep consistent format). Tests with assertLogs asserting no message content appears.
7. **Unlogged exception after client disconnect** for the shielded send in `webui.py`: same done-callback pattern as Task 2 item 3 (shared helper if Task 2 created one — reuse it, do not duplicate). Test.
8. **`guild.me` can be None** during outages (`permissions_for` raises): map to 409 `no_channel` (send) / empty result (members) instead of a generic 500. Tests.
9. **Chunk timeout memo.** `members_for`: if `guild.chunk()` times out, remember the failure per guild for ~60 s and skip the wait on subsequent searches (use the available cache); injectable clock for the test. Add a concurrency test (two simultaneous searches on an unchunked guild call `chunk()` once).
10. **UI polish (cheap ones only):** do not rebuild the chip row and the member list on every keystroke/status tick when nothing changed (skip re-render if the data is identical); keep scroll position and focus of the member list; arrow-key highlight scrolls the item into view; `m-dest`/`m-ping` regions use `aria-live="polite"` only when their text actually changes; ignore a member-search error/response whose guild id no longer matches the selected guild; reset the member cache when the configured channel changes.
11. **Docs:** README and docs/ARCHITECTURE.md "Web UI trust boundary" and "Accepted limitation" sections must say anyone who can reach the GUI can also post as the bot in the configured channel (and queue test phrases), not just change the channel; fix D41's rationale (browsers send raw UTF-8; worst case ~8 KB emoji / ~12 KB for escaped control characters; the 16 KB cap conclusion still holds); the CLAUDE.md `py_compile` line must list `messages.py` and `voices.py`; update test counts.

Commit(s): `fix(messages): exact-final-text ping summary, bounded member scan, mention cap, safer substitution` and `fix(messages): audit logging, error handling, UI polish, docs`.

---

### Task 4: Final documentation consistency pass

**Files:** README.md, docs/ARCHITECTURE.md, CLAUDE.md, .env.example, tests (counts only).

Read the final code and bring every document into agreement with it: module map (include `voices.py`, `messages.py`), route table (all routes with guards and error codes), data-flow walkthroughs (voice swap, GUI message), security model (network-layer boundary, peer/Host/Origin guards, what a local process / any tailnet-ACL-permitted peer can do: change channel, pause/skip/clear, swap voice, speak test phrases, post as the bot), runbook (fresh install order: `sudo scripts/setup_system.sh` -> `sudo scripts/install_token.sh --system` -> `sudo scripts/deploy.sh` -> `sudo systemctl enable tts_bot`; upgrade; voice install; firewall rules; manual deploy only), Known limitations (refresh the list: no browser/GPU/live-Discord tests, self-declared editor names, one channel per guild, global voice, in-memory rate limits, manual deploys), the current test count (run the suite and use the real number), CLAUDE.md file map and key facts. Remove any statement that is no longer true. Verify every path, env var, function name, route, default and limit you mention by grepping the code. No secrets. Do not change code in this task.

Commit: `docs: refresh architecture, README and agent notes after cleanup`.
