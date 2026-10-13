# CLAUDE.md

Discord TTS bot: reads watched users' messages aloud in a voice channel with Piper
(ONNX Runtime GPU, CUDA, FP32, GTX 1060). One Python 3.11 process: discord.py client,
per-guild queue + worker, single-thread GPU engine, and an aiohttp web UI (REST + SSE)
that hot-swaps the listened-to text channel and the Piper voice at runtime. Full detail and the "why" of
every decision: `docs/ARCHITECTURE.md` (read it before non-trivial changes).

## Hard rules
- NEVER read, print, log, commit or paste secrets. Off-limits: `.env`, `~/.config/tts-bot/*`,
  `/etc/tts-bot/*`, any file named `token` or `gui_token` (old hosts keep it until shredded). Do not echo token values or lengths
  beyond what existing code already logs.
- Do not add dependencies (pip/conda) without asking. Stdlib + aiohttp (ships with discord.py) only.
- All Piper/GPU work stays on the single worker thread in `TTSEngine` (`max_workers=1` + lock), including
  voice (re)loading (`TTSEngine.reload`). Never run inference on the event loop or add a second executor.
- Channel/pause/voice changes go through `ConfigStore` (`set_channel`, `set_enabled`, `set_voice`, `seed_channel`).
  Never edit `config.json` or `ConfigStore._guilds` directly; writes use `atomic_write_json`.
- The web UI talks to the bot only via `Controller` (bot.py); `webui.py` must not import `bot.py`.
- Frontend: vanilla JS, no CDN/framework, no inline script/style, `textContent` only
  (strict CSP; `tests/test_static.py` enforces it). Static payload stays < 56 KB (cap in `tests/test_static.py`).
- Fail closed: unconfigured guild reads nothing; a GUI bind address outside loopback/Tailscale disables the GUI;
  `maybe_start_webui` must never crash the bot.
- Do not run `scripts/deploy.sh`, `setup_system.sh` or anything with sudo unless asked.
- Commits: conventional style, end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Commands
```
~/miniconda3/envs/tts-bot/bin/python -m unittest discover -s tests -v   # unittest, no pytest
bash tests/test_deploy_validation.sh                                    # deploy validator
~/miniconda3/envs/tts-bot/bin/python -m py_compile bot.py webui.py config_store.py tts_engine.py
~/miniconda3/envs/tts-bot/bin/python bot.py                             # dev run (needs your own dev token)
```
No live Discord/GPU/browser tests exist; say so rather than claiming they passed.

## File map
- `bot.py`: entry, events, hybrid commands, `WatchStore` (state.json), `GuildAudio` + `audio_worker`,
  `Controller`, `config_watcher`, `maybe_start_webui`, `shutdown`.
- `tts_engine.py`: `TTSEngine` (Piper, CUDA->CPU fallback, `reload` hot swap), `clean_discord_text`, `clean_plain_text`.
- `voices.py`: `discover_voices`, `resolve_voice`, `VoiceLoadError`/`BadVoiceError`/`SayError` (pure stdlib).
- `messages.py`: `MessageError`, `parse_mentions`, `SendLimiter` (pure stdlib; used by `Controller.send_message` and `webui.py`).
- `config_store.py`: `ConfigStore` (rev, optimistic concurrency, `ConflictError`), `EventHub`, `atomic_write_json`.
- `webui.py`: `make_app`, `start_webui`, `validate_bind_hosts`, `detect_tailscale_addrs`; peer/Host/Origin checks, REST, SSE. No token auth.
- `static/{index,app}.{html,js}`, `static/app.css`: the UI (`app.js` helpers are node-testable).
- `tts_bot.service`: hardened systemd unit (`ttsbot` user, `LoadCredential` for the Discord token).
- `scripts/`: `setup_system.sh`, `deploy.sh` (+ `lib/deploy_validate.sh`), `install_token.sh`, `install_voice.sh`.
- `tests/`: unittest suites + `static_logic.js` + `test_deploy_validation.sh`.
- `docs/ARCHITECTURE.md`: architecture, decision log, gotchas, runbook.

## Key facts
- `config.json` (`CONFIG_FILE`) = channel + pause per guild, global `rev`; writes carry `rev`, mismatch -> 409.
  `TTS_CHANNEL_ID` is only a first-boot seed. `state.json` (`STATE_FILE`) = watch list.
- The Piper voice is GLOBAL (`config.json` top-level `voice`, null = `PIPER_MODEL_PATH`). `Controller.apply_voice` order: resolve name -> `expected_rev` check (409) -> `engine.reload` (old voice stays on failure, 422) -> `config.set_voice(expected_rev=None)` (OSError -> engine reverted, 500). Voices are scanned from `PIPER_MODELS_DIR` on demand (Rescan), addressed only by file stem; multi-speaker models are skipped. `deploy.sh` never prunes `/opt/tts-bot/models`; add voices with `scripts/install_voice.sh` (no restart). A swap briefly holds two sessions on the GPU; restart clears growth.
- Messages tab: `POST /api/guilds/{gid}/send` posts as the bot ONLY in `config.channel_for(gid)`; user mentions only (`allowed_mentions` users=validated ids, roles/everyone off, `<@&id>` rejected); every tagged user must be able to view the channel; 10 per 60 s + 2 s gap per guild (`SendLimiter`, reserved before the await); body limit 16 KB app-wide; audit log has editor + counts, never content. `GET .../members?q=` feeds the picker (max 25, `guild.chunk()` once if not chunked).
- Channel swap drains that guild's queue (not the playing clip); pause only stops reading new messages.
- Wire IDs are strings (JS precision); internal IDs are ints. `X-Editor` is percent-encoded by the client.
- GUI has NO token: network layer is the boundary. Binds loopback + auto-detected Tailscale addrs (re-detected every 15 s until found) on `WEBUI_PORT` (8765); every request's TCP peer must be loopback/Tailscale (`bad_peer`, weak-host model); `WEBUI_HOSTS` overrides (loopback/Tailscale only, else GUI disabled); Host allowlist + `WEBUI_ALLOWED_HOSTS` (MagicDNS/domain) and Origin check on writes; SSE needs proxy buffering off. Any local process can reach loopback (accepted).
- piper-tts is pinned to 1.2.0 (`synthesize_stream_raw`); CPU `onnxruntime` must stay uninstalled.
- Prod layout: code root-owned `/opt/tts-bot`, state `/var/lib/tts-bot`, tokens in `/etc/tts-bot`.
  Anyone who can get code deployed can read the Discord token: review changes before deploy.
- `bot.env` is optional (`EnvironmentFile=-`, only a first-boot `TTS_CHANNEL_ID` seed). Fresh install: setup_system -> install_token --system -> deploy -> enable. Upgrade: setup_system (new unit) then deploy.
- Importing `bot.py` has side effects; tests set env vars (`STATE_FILE`, `CONFIG_FILE`, ...) first.
