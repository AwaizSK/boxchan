# Architecture Guide

Audience: developers and LLMs answering questions about this repo. Everything here was checked against the code at the commit that added this file. Where the original plan (`docs/superpowers/plans/2026-10-13-channel-gui.md`) differs from the code, the code is documented and the divergence is flagged with "Divergence".

No secret values appear in this document. Token *locations* are described; contents never are.

## Contents
1. Purpose and summary
2. System diagram
3. Module map
4. Data-flow walkthroughs
5. State and persistence
6. Concurrency model
7. Security model and trust boundaries
8. Decision log
9. Nuances and gotchas
10. Operations runbook
11. Testing
12. Known limitations
13. Asking an LLM about this repo

---

## 1. Purpose and summary

A Discord bot that reads messages from a chosen set of users ("watched" users) aloud in a voice channel, using the Piper neural TTS model on a local GPU (GTX 1060, CUDA, FP32). One process does everything: the discord.py client, a per-guild queue and playback worker, a GPU-serialized synthesis engine, and a small aiohttp web UI. The web UI lets several developers pick which text channel the bot listens to (per guild), pause/resume it, and skip or clear the queue, live, without restarting the bot. Channel selection lives in `config.json` (the source of truth, edited via the GUI); the watch list lives in `state.json` (edited via chat commands). Production runs as a hardened systemd service under an unprivileged user with root-owned code; the Discord token never touches the dev-writable checkout.

---

## 2. System diagram

```
                       +----------------------------- bot process (bot.py, one asyncio event loop) -----------------------------+
                       |                                                                                                        |
 Discord gateway  <--->|  discord.py client (TTSBot)                                                                            |
 (WebSocket+REST)      |    on_message --> ConfigStore.is_listen_channel? --> WatchStore.is_watched? --> clean_discord_text      |
                       |         |                                                                          |                   |
                       |         |  prefix/slash commands (global check = ConfigStore.is_command_channel)   v                   |
                       |         |                                                          GuildAudio.queue (asyncio.Queue,      |
                       |         v                                                           maxsize MAX_QUEUE=20, per guild)     |
                       |   WatchStore --> state.json (atomic)                                         |                             |
                       |                                                                              v                             |
                       |   ConfigStore --> config.json (atomic)      audio_worker (one task per guild; waits for voice)         |
                       |      |  (rev, asyncio.Lock)                        |                                                   |
                       |      +--> EventHub (pub/sub, Queue(64) per subscriber)                                                  |
                       |             ^   |                                  v                                                   |
                       |             |   +--> config_watcher task      TTSEngine.synthesize  (asyncio.Lock + 1-thread pool)      |
                       |             |        (drains guild queue            |                                                  |
                       |             |         on channel change)            v                                                  |
                       |             |                                 Piper (onnxruntime-gpu, CUDA, FP32) --> temp .wav        |
                       |   Controller (sync view/actions for web UI)         |       in <tmp>/tts_bot (/tmp/tts_bot in service)  |
                       |             ^                                       v                                                  |
                       |             |                              ffmpeg (discord.FFmpegPCMAudio) --> Opus --> voice UDP      |
                       |   aiohttp web UI (webui.py)  127.0.0.1:8765         |                                                  |
                       +-------------|-------------------------------------- | ------------------------------------------------+
                                     |  REST (JSON) + SSE (/api/events)      v
 Browsers (devs) <--- SSH tunnel ----+                               Discord voice server
   static/index.html, app.js, app.css
   fetch() streaming, Bearer token, X-Editor header
```

Key points the diagram encodes: one process and one event loop; the web UI never talks to Discord or Piper directly (only through `Controller` and `ConfigStore`); `EventHub` is the only fan-out path from config changes to browsers and to `config_watcher`; Piper inference is the only code that runs off-loop (single dedicated thread).

---

## 3. Module map

| File | Responsibility | Public interface |
|---|---|---|
| `bot.py` | Entry point. Discord events and commands, per-guild queue/worker, WatchStore, Controller, config watcher, lifecycle (startup, signals, shutdown). | Run `python bot.py`. Module-level: `config` (ConfigStore), `store` (WatchStore), `engine` (TTSEngine), `audio` (dict guild_id -> GuildAudio), `controller` (Controller), `load_token()`, `parse_port()`, `maybe_start_webui()`, `shutdown()`, `main()`. |
| `tts_engine.py` | Piper wrapper (CUDA with CPU fallback) and Discord-text normalizer. | `TTSEngine(model_path, config_path)`: `load() -> bool` (never raises), `async synthesize(text) -> path\|None`, `purge_tmp()`, `close()`, attrs `ready`, `cuda_active`. `clean_discord_text(message, max_chars=300) -> str\|None`. |
| `config_store.py` | Runtime config with revision counter, atomic persistence, pub/sub. | `atomic_write_json(data, path)`, `ConflictError(snapshot)`, `EventHub` (`subscribe/unsubscribe/publish/subscriber_count`), `ConfigStore` (`snapshot`, `channel_for`, `is_enabled`, `is_listen_channel`, `is_command_channel`, `async set_channel`, `async set_enabled`, `async seed_channel`, `.rev`, `.hub`). |
| `webui.py` | aiohttp backend: bearer auth, throttle, REST, SSE, static serving. Duck-typed on the controller; never imports `bot.py`. | `load_gui_token() -> str\|None`, `make_app(config, controller, token, *, status_interval=3.0, keepalive_interval=15.0, static_dir=None, clock=time.monotonic)`, `async start_webui(app, host, port) -> AppRunner`, `sanitize_editor()`, `wire_snapshot()`, `wire_event()`. |
| `static/index.html`, `app.js`, `app.css` | Vanilla-JS single-page UI. No framework, no CDN, no inline script/style (CSP). `app.js` also exports pure helpers under node for tests. | Served at `/`, `/app.js`, `/app.css`. |
| `tts_bot.service` | systemd unit: user `ttsbot`, credentials, env, sandboxing. | Installed to `/etc/systemd/system/` by `setup_system.sh`. |
| `scripts/setup_system.sh` | One-time root setup: service user, `/opt/tts-bot`, private conda env, sudoers rule, journal group, unit install. | `sudo scripts/setup_system.sh` (env: `CONDA`, `TEAM_GROUP`). |
| `scripts/deploy.sh` | Admin deploy: validate, snapshot, compile, install, prune, restart. | `sudo scripts/deploy.sh`. |
| `scripts/lib/deploy_validate.sh` | Sourced function `snapshot_sources SRC SNAP` (validation + snapshot). | Tested by `tests/test_deploy_validation.sh`. |
| `scripts/install_token.sh` | Hidden prompt, shape-checks and stores the Discord token (mode 600). | `[--system]`; `TOKEN_DIR` env override (undocumented in README). |
| `scripts/gen_gui_token.sh` | Generates GUI token (openssl, >= 48 chars), prints it once. | `[--system] [--rotate]`; refuses to overwrite without `--rotate`. |
| `tests/` | See section 11. | `python -m unittest discover -s tests` |
| `requirements.txt` | `discord.py[voice]>=2.4`, `PyNaCl>=1.5`, `piper-tts==1.2.0`, `onnxruntime-gpu>=1.18,<1.21`, `python-dotenv>=1.0`. | |
| `.env.example` | Non-secret dev settings template. | |

### Web API (all under `webui.py`)

| Route | Auth | Purpose |
|---|---|---|
| `GET /`, `/app.js`, `/app.css` | none (no secrets in them) | Three explicit routes; files read once at app creation. No directory serving, so no path traversal. |
| `GET /api/state` | Bearer | `{"config": snapshot, "guilds": guilds_view}` |
| `PUT /api/guilds/{gid}/channel` | Bearer + `X-Editor` | Body `{"channel_id": "<digits>"\|null, "rev": int\|null}`. 400 `bad_channel`, 409 `conflict` (with snapshot), 200 `{"snapshot"}`. |
| `PUT /api/guilds/{gid}/enabled` | Bearer + `X-Editor` | Body `{"enabled": bool, "rev": int\|null}`. |
| `POST /api/guilds/{gid}/skip` | Bearer + `X-Editor` | `{"skipped": bool}` |
| `POST /api/guilds/{gid}/clear` | Bearer + `X-Editor` | `{"cleared": int}` |
| `GET /api/events` | Bearer | SSE: `hello` (snapshot+guilds) on connect, then `config`, `channels`, `status` (only when changed), `: keepalive` comment every 15 s. |

Error codes: 401 `unauthorized`, 429 `too_many_attempts`, 400 `editor_required` / `bad_request` / `bad_channel`, 404 (unknown or non-digit guild id; body is not JSON), 409 `conflict`, 413 for bodies > 4096 bytes (aiohttp `client_max_size`). Bad JSON, non-object body, or a `rev` that is not an int (bools rejected) -> 400 `bad_request`.

All IDs on the wire are strings (JS numbers lose precision above 2^53). `wire_snapshot`/`wire_event` convert `channel_id` and `change.guild_id/old/new`; internally the store uses ints.

### Event shapes
- Hub `config` event (internal, ints): `{"type":"config","snapshot":{...},"change":{"guild_id":int,"field":"channel_id"|"enabled","old":..,"new":..}}`.
- Hub `channels` event: `{"type":"channels","guild_id":"<str>"}` published by `bot.py` on channel create/delete/update and guild join/remove; the browser reacts by refetching `/api/state`.
- Snapshot: `{"rev","updated_by","updated_at","guilds":{"<gid>":{"channel_id":int|null,"enabled":bool}}}`.

---

## 4. Data-flow walkthroughs

### (a) A watched user's message becomes speech
1. Discord delivers `MESSAGE_CREATE`; `on_message` (bot.py) ignores bots and DMs.
2. If the text starts with `COMMAND_PREFIX`, it goes to `bot.process_commands` only (never spoken). The global check `only_tts_channel` runs `ConfigStore.is_command_channel(guild, channel)`; failure raises `CheckFailure` (silent for prefix commands).
3. Otherwise `ConfigStore.is_listen_channel` must be true: configured channel equals this channel AND `enabled`. Unconfigured guild -> false (fail closed). Paused -> false.
4. `WatchStore.is_watched(guild, author)` must be true.
5. The bot must be in voice (`guild.voice_client`) and the guild must have a `GuildAudio` (created by `join`).
6. `clean_discord_text` normalizes (URLs -> "link", mentions -> names, code blocks -> "code block", etc., truncation at word boundary to `MAX_CHARS`). Returns `None` for empty or symbol-only text, which stops here (never reaches queue/GPU).
7. `queue.put_nowait(text)`. On `QueueFull`: log, add the hourglass reaction, drop the message.
8. `audio_worker` (one task per guild) pops the text, waits up to `RECONNECT_WAIT`=10 s for the voice client to be connected, calls `engine.synthesize(text)`.
9. `synthesize` takes an `asyncio.Lock`, then runs `_synth_to_wav` on the one-thread pool: Piper `synthesize_stream_raw` -> PCM -> temp `.wav` in `<tempdir>/tts_bot`.
10. Worker re-checks the voice client, clears `play_done`, calls `vc.play(FFmpegPCMAudio(path), after=after)`. Ownership of the temp file moves to `after`, which deletes it and does `loop.call_soon_threadsafe(play_done.set)`.
11. Worker awaits `play_done` with `PLAY_TIMEOUT`=180 s (safety net: `vc.stop()` on timeout), then loops.

### (b) A dev switches the channel in the GUI
1. Browser: user clicks a row (or Enter). `app.js` updates the UI optimistically and sends `PUT /api/guilds/{gid}/channel` with `Authorization: Bearer`, `X-Editor: <percent-encoded name>`, body `{channel_id, rev}`.
2. Server middleware order: `security_headers` -> `auth` (throttle, constant-time compare). Handler: validates guild (404), editor (400), body (400), channel via `Controller.is_text_channel` (400 `bad_channel`).
3. `ConfigStore.set_channel` is invoked inside `asyncio.shield`. Under the store lock: rev check, no-op check, mutate, `rev += 1`, persist via `atomic_write_json` in a thread, then `hub.publish`.
4. The hub event reaches (i) every SSE stream (all browsers get `config`, flash the row and toast "X switched to #name"), and (ii) `config_watcher` in bot.py, which sees `change.field == "channel_id"` and `drain`s that guild's queue.
5. Effect on the live bot: from the next `on_message`, the old channel is ignored and the new one is listened to (`is_listen_channel` reads the in-memory dict directly; there is no cache to invalidate). Pending queued clips are dropped; the clip currently playing is NOT interrupted; a clip already mid-synthesis will still play (accepted).
6. The HTTP response returns the new snapshot; the requester's UI confirms it.

Toggling `enabled` follows the same path but `config_watcher` ignores it (the queue is NOT drained on pause; clips already queued finish unless Clear/Skip is used).

### (c) Two devs edit simultaneously (conflict path)
1. Alice and Bob both see `rev 7`. Alice's PUT (`rev: 7`) wins: rev becomes 8, persisted, broadcast.
2. Bob's PUT (`rev: 7`) arrives (before or after he saw the SSE event): under the lock `expected_rev (7) != self.rev (8)` -> `ConflictError(snapshot)` -> HTTP 409 `{"error":"conflict","snapshot":<rev 8>}`. Nothing is mutated or published.
3. Bob's UI had changed optimistically; on 409 it applies the returned snapshot (reverting), and toasts "Changed by Alice — your change was not applied". The server is the single source of truth; clients never merge.
4. Edge: the `rev` is global per config file, not per guild, so a change to guild A also makes a stale write to guild B return 409 (the UI wording is the same). `rev: null` skips the check and always applies (API clients only; the browser always sends `rev`).
5. Edge: even without 409, `applySnapshot(..., force=false)` ignores snapshots older than the held one, so out-of-order SSE events cannot roll the UI back.

---

## 5. State and persistence

| Item | Where | Format / semantics | Written by |
|---|---|---|---|
| Listen channel + pause flag | `config.json` (`CONFIG_FILE`; default: next to `STATE_FILE`; `/var/lib/tts-bot/config.json` in production) | The snapshot dict: `rev`, `updated_by`, `updated_at` (epoch float), `guilds{gid: {channel_id, enabled}}`. Pretty-printed JSON. | `ConfigStore._mutate` |
| Watch list | `state.json` (`STATE_FILE`; default next to `bot.py`; `/var/lib/tts-bot/state.json` in production) | `{"watched": {"<gid>": [user ids]}}` | `WatchStore.mutate` |
| Queues, workers, voice connections, playing clip | memory | `GuildAudio` per guild | |
| EventHub subscribers, throttle counters, SSE tasks | memory | | |
| Temp `.wav` clips | `<tempdir>/tts_bot` (`/tmp/tts_bot`; under `PrivateTmp` in the service) | deleted by `after`; `purge_tmp()` at start and in `engine.close()` | |

**`rev` semantics.** Starts at 0 for a fresh store; +1 for every effective change (channel set to a different value, enabled flipped, or first seed of a guild); persisted, so it survives restarts. No-op writes (same value on an existing guild entry) return the current snapshot without bumping rev or publishing. Quirk: the first `set_*` on a guild with no entry always counts as a change, even when the value equals the default (`enabled=True`, `channel_id=None`), so it bumps rev. `expected_rev=None` bypasses conflict detection.

**Atomic writes.** `atomic_write_json` creates a temp file in the same directory (`.cfg-*.tmp`), writes, flushes, `fsync`s, then `os.replace`s over the target; on any failure the temp file is unlinked and the error re-raised. A crash leaves either the old or the new file, never a torn one. The same function backs `state.json` (the older private `save_state` was removed).

**Load behavior.** Both stores tolerate a missing file (start empty) and a corrupt file (log the exception, start empty; the corrupt file is overwritten on the next write, no backup). `ConfigStore` never raises from its constructor.

**Persist failure.** `_mutate` restores in-memory state and rev and re-raises; nothing is published. `WatchStore.mutate` instead logs "state save failed" and keeps the in-memory change (memory ahead of disk until the next successful write).

**Seed.** `TTS_CHANNEL_ID` (comma-separated ids allowed) is only a seed: on `on_ready`, for each guild with no entry in config, the first listed id that is a `TextChannel` in that guild is stored with editor `env-seed`. Once a guild has an entry, the env value is ignored. Clearing the channel in the GUI sets `channel_id: null` but keeps the entry, so the seed does NOT re-apply.

**Lost on restart:** queued-but-unspoken messages, the voice connection (the bot does not rejoin; someone must run `join` again), which also means no `GuildAudio` exists until `join`. **Kept:** channel selection, pause flag, rev, watch list.

Other state-changing behaviors: `leave` and auto-leave (no humans left in the bot's voice channel) both flush that guild's watch list.

---

## 6. Concurrency model

- **One asyncio event loop** runs discord.py, all per-guild workers, `config_watcher`, the aiohttp server, and all `ConfigStore`/`WatchStore` logic. `Controller` methods are synchronous and non-blocking and must be called on the loop (aiohttp handlers are).
- **Locks.**
  - `ConfigStore._lock` (`asyncio.Lock`): serializes all config mutations including the thread-offloaded disk write, so rev check + mutate + persist + publish is atomic with respect to other writers. Reads (`channel_for`, `is_*`, `snapshot`) take no lock; they read in-memory dicts on the loop and so never see a half-applied mutation except for the brief window in which a not-yet-persisted change is visible (and may be rolled back if persist fails).
  - `WatchStore.lock`: serializes watch-list mutations plus their persist.
  - `TTSEngine._lock` (created lazily on first use so it binds to the running loop): ensures only one synthesis is submitted at a time.
- **Single GPU worker thread.** `TTSEngine._pool = ThreadPoolExecutor(max_workers=1)`. Why: the GTX 1060 has 6 GB and one CUDA context; ONNX Runtime sessions are not worth sharing across threads; parallel synthesis would only add VRAM pressure and contention without latency gain for short clips. The thread keeps inference off the event loop so the Discord gateway heartbeat is never blocked. The `asyncio.Lock` in front of the pool means cancellation of a waiting caller does not queue stale work behind a long job. Do not add a second thread/executor for GPU work.
- **Playback.** discord.py plays in its own audio thread. `after` runs on that thread, so it signals the worker with `loop.call_soon_threadsafe(ga.play_done.set)`; the worker awaits an `asyncio.Event` (no polling). `vc.stop()` (skip, timeout, teardown) also triggers `after`. `PLAY_TIMEOUT` (180 s) prevents a wedged clip from blocking the guild forever.
- **Worker lifecycle.** `ensure_audio` creates the `GuildAudio` and restarts a dead worker task. `audio_worker` catches all exceptions per item and continues; only `CancelledError` ends it. `teardown(guild)` pops the `GuildAudio`, drains the queue, cancels the worker, stops playback and disconnects (`force=True`). Temp-file ownership: the worker's `finally` deletes the file unless it was handed to `after`.
- **Voice reconnect tolerance.** `wait_connected` polls `is_connected()` every 0.5 s for up to 10 s (`RECONNECT_WAIT`), so a UDP drop or region move does not discard the queue; if the voice client is gone (`None`) or the wait expires, the current item is skipped. `on_voice_state_update` tears down only if the bot itself ends up in no channel.
- **Shutdown** (`shutdown()`, guarded by `_shutting_down`; SIGINT/SIGTERM): cancel the config watcher, `teardown` every guild with a voice client or audio state (no orphan ffmpeg), `await runner.cleanup()` for the web UI, `engine.close()` (pool shutdown, purge tmp), `bot.close()`. SSE handler tasks are tracked in a `streams` set and cancelled by an `app.on_shutdown` hook; otherwise aiohttp would wait the default 60 s for them. `start_webui` also sets `shutdown_timeout=5.0` as a backstop.
- **Web write path.** `write()` wraps the store coroutine in `asyncio.shield`: if the HTTP client disconnects mid-request aiohttp cancels the handler, but the shielded persist runs to completion, so memory and disk cannot diverge by cancellation. Underlying caveat: `asyncio.to_thread` cannot be cancelled once the thread started; without the shield a cancelled caller would trigger rollback of memory while the thread still landed the new file.
- **EventHub back-pressure.** Each subscriber has `Queue(maxsize=64)`; `publish` never blocks: if a queue is full its oldest item is dropped. A slow browser may miss intermediate events but always gets the latest; the client self-heals because each `config` event carries the full snapshot and a reconnect starts with a full `hello`.

---

## 7. Security model and trust boundaries

### Actors and boundaries
| Actor | Can | Cannot |
|---|---|---|
| Admin (real sudo) | run `setup_system.sh`, `deploy.sh`, `install_token.sh --system`, `gen_gui_token.sh --system`; read both tokens | |
| Dev (group `devteam`) | edit the repo; `sudo systemctl start\|stop\|restart tts_bot` (exact commands, no wildcards); `systemctl status`, `journalctl -u tts_bot` (via `systemd-journal` group); use the GUI with the GUI token if given it | read `/etc/tts-bot/*`; modify `/opt/tts-bot`; use any other sudo command |
| Service (`ttsbot` user) | read `/opt/tts-bot` (root-owned), write `/var/lib/tts-bot`, `/tmp` (private) | write code, see home dirs, gain privileges |
| GUI user (token holder) | change channel, pause, skip, clear for any guild the bot is in | read the Discord token; read/write anything on the host |

### Token locations and flow (names only)
- **Discord token**: production `/etc/tts-bot/token` (root:root 600, dir `/etc/tts-bot` 700) -> systemd `LoadCredential=discord_token:/etc/tts-bot/token` -> process sees `$CREDENTIALS_DIRECTORY/discord_token`. Dev: `~/.config/tts-bot/token` (mode 600), override `DISCORD_TOKEN_FILE`. Lookup order in `load_token()`: credential, file, then `DISCORD_TOKEN` env (last resort). Shape-validated against `TOKEN_RE` at import; on mismatch the process exits with a message containing only the length.
- **GUI token**: production `/etc/tts-bot/gui_token` (root:root 600) -> `LoadCredential=gui_token:/etc/tts-bot/gui_token` -> `$CREDENTIALS_DIRECTORY/gui_token`. Dev: `~/.config/tts-bot/gui_token` or `WEBUI_TOKEN_FILE` (no `~` expansion there) or `WEBUI_TOKEN` env. Order in `load_gui_token()`: credential, file, env. The first source that is found and non-empty wins even if it is too short (no fallthrough to later sources); a token under 24 chars is rejected (warning logs only the length) and the GUI stays off.
- Neither token is ever logged or returned by an endpoint (`test_token_never_in_body`). The browser keeps the GUI token in `sessionStorage` (per tab, gone on close); the editor name is in `localStorage`.
- `.env`, `~/.config/tts-bot/`, and `/etc/tts-bot/` are off-limits for agents and must never be committed (`.gitignore` covers `.env`).

### Web UI hardening
- **Loopback bind** by default (`WEBUI_HOST=127.0.0.1`, port 8765). Remote use: `ssh -L 8765:127.0.0.1:8765 <host>` or a TLS reverse proxy. Traffic is plain HTTP; do not expose it directly.
- **Bearer auth** via `Authorization: Bearer <token>` header only, compared with `hmac.compare_digest`. No cookies means no CSRF surface. All `/api/*` routes need it; static files do not (they contain no secrets).
- **Fail closed**: no usable token -> server not started (warning). `make_app` raises `ValueError` for an empty or < 24 char token, so a programming or config error can never produce an open server. A guild without a configured channel reads nothing.
- **CSP** on every response: `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` for `/api/*` and `no-cache` for static. The UI uses `textContent`/DOM APIs only (channel names are attacker-influenced); `tests/test_static.py` forbids `innerHTML`, `eval`, inline handlers/styles and external URLs.
- **Auth throttle**: 5 failures within 60 s from one `request.remote` -> 429 for 60 s. The block applies before the token check, so a blocked IP is refused even with a valid token. See the caveats in section 9.
- **Body limit** 4096 bytes. Guild ids and channel ids must be ASCII digits; `is_text_channel` confirms the channel exists in that guild and the bot can view it.
- **Editor attribution** (`X-Editor`) is self-declared: it names who changed what in the UI and logs; it is not authentication. The client percent-encodes it (`encodeURIComponent`) because a raw non-Latin-1 name would make `fetch` throw; the server `unquote`s then sanitizes (strip control chars, collapse whitespace, truncate to 40, reject empty).
- **Never crash the bot**: `maybe_start_webui` wraps token loading, `make_app` and the bind; any failure (bad token, bad port, port in use) logs and the bot continues without the GUI. `parse_port` falls back to 8765 on an invalid or out-of-range `WEBUI_PORT`.

### systemd sandbox (`tts_bot.service`)
`User=ttsbot`, `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict` (everything read-only except `StateDirectory=tts-bot` -> `/var/lib/tts-bot`), `ProtectHome=true`, `ProtectKernelModules`, `ProtectControlGroups`, `RestrictSUIDSGID`.

### Deploy trust boundary (important)
- `scripts/deploy.sh` runs as **root** from a **group-writable checkout**. That is a privilege-escalation surface, so it is defended in depth by `scripts/lib/deploy_validate.sh`: every source file (root `*.py`, `static/*`, `models/*.onnx` and `*.onnx.json`, `requirements.txt`) must be a regular file, not a symlink; its `realpath` must lie inside the physically-resolved repo; `static/` and `models/` must be real directories; no subdirectories. Files are copied into a root-owned `mktemp -d` snapshot (mode 700), the snapshot is re-verified (no symlinks or special files), and everything after that (py_compile, install, stale pruning) reads only from the snapshot. The compiled output goes to the snapshot's own `.pyc` dir so nothing is written into the repo.
- **Accepted residual risk**: a microsecond race between the `realpath` check and the `cp` into the snapshot (the later re-verification catches a swapped-in symlink but not a swapped-in regular file with different content, which is the same as "dev edited the file", i.e. legitimately deployable code).
- `deploy.sh` and `setup_system.sh` also `source`/execute files from the dev-writable repo as root (`scripts/lib/deploy_validate.sh`, `tts_bot.service`, `requirements.txt`). **The admin must review `scripts/` (and the diff in general) before running them.**
- **Anyone who can get code deployed can read the Discord token**, because deployed code runs with it. The boundary protects the token from devs who can only edit/run things as themselves, not from a malicious deployed change. Hence "review before `deploy.sh`".
- The sudoers rule is limited to three exact commands: `systemctl start tts_bot`, `stop`, `restart` (`/etc/sudoers.d/tts-bot`, validated with `visudo -cf`, mode 440). No wildcards, no pagers, no shell escapes.
- Why code is root-owned in `/opt/tts-bot` including the conda env: a dev who could write site-packages or the code could exfiltrate the token via the process.

---

## 8. Decision log

Format: **Decision** / Why / Alternatives rejected / Cost if wrong.

### D1. GUI served inside the bot process (aiohttp)
- **Decision:** `webui.py` runs on the same event loop as the bot.
- **Why:** hot-swap must change the in-memory state the bot reads *right now*; a separate process would need IPC or file watching and could race. aiohttp already ships with discord.py: zero new dependencies, one process, one loop, nothing extra to deploy or restart.
- **Rejected:** separate web service + file watching/IPC; Flask/FastAPI (new deps).
- **Cost:** the GUI is down whenever the bot is down; a GUI bug shares a process with the bot (mitigated: startup failure is contained, handlers are small).

### D2. SSE instead of WebSockets or polling
- **Why:** only server->client push is needed (config changed, channel list changed, status). SSE is plain HTTP, simple to proxy, lighter than a WS stack, and reconnect-friendly.
- **Client detail:** `app.js` uses `fetch` streaming plus a hand-written parser (`createSSEParser`), not `EventSource`, because `EventSource` cannot set the `Authorization` header. Putting the token in a query string or cookie was rejected (leaks in logs/history; cookies would add CSRF).
- **Rejected:** WebSockets, polling.
- **Cost:** proxies that buffer responses break live updates (the server sends `X-Accel-Buffering: no`); a 45 s client-side idle watchdog reconnects dead streams.

### D3. Optimistic concurrency with a monotonic `rev`
- **Why:** several devs edit at once. Last-writer-wins silently clobbers; locks need unlock/expiry semantics. Rev check + 409 + returned snapshot is stateless per client and makes conflicts visible.
- **Rejected:** last-writer-wins, pessimistic locks, client-side merging.
- **Cost:** `rev` is global across guilds so unrelated edits can conflict (harmless; user retries).

### D4. One active channel per guild plus a pause flag
- **Why:** "hot-swap" means replace, not accumulate. Pause (`enabled=false`) stops speech during live tests without losing the selection. `TTS_CHANNEL_ID` became a first-boot seed only.
- **Rejected:** multiple listen channels per guild; the env var as the permanent source of truth.
- **Cost:** supporting several channels per guild later requires a schema change.

### D5. On channel swap the pending queue is cleared; the playing clip is not interrupted
- **Why:** queued items belong to the old channel and would be spoken out of context; killing a clip mid-word is jarring and Skip exists. Pausing does not drain the queue for the same "don't surprise people" reason (use Clear).
- **Cost:** an item already being synthesized at the moment of the swap still plays.

### D6. Loopback bind + bearer token + systemd credential
- **Why:** the GUI controls a production bot, so it is not on the network by default; the token is delivered like the Discord token (`LoadCredential`) to preserve the "devs can't read secrets" boundary.
- **Rejected:** binding 0.0.0.0, no auth, basic auth over plain HTTP, cookie sessions.
- **Cost:** remote use needs an SSH tunnel or a TLS proxy.

### D7. Self-declared editor name
- **Why:** small, known team; the token is the real gate; names give "who changed this" without building accounts.
- **Cost:** anyone with the token can impersonate a name. Attribution only.

### D8. Three explicit static routes and strict CSP
- **Why:** reading three files at startup removes any traversal surface; CSP with no inline code limits XSS impact; channel names are untrusted strings, so `textContent` only.
- **Rejected:** `add_static` directory serving; CDN assets.
- **Cost:** adding a static file requires a code change in `STATIC_FILES`; a missing file at startup yields a 500 for that route (logged), not a crash.

### D9. `unittest`, not pytest
- **Why:** no extra dev dependency in the production conda env.
- **Cost:** less ergonomic tests (`IsolatedAsyncioTestCase`, `aiohttp.test_utils`).

### D10. Percent-encoded `X-Editor` (review finding)
- **Why:** HTTP header values given to `fetch` must be Latin-1; a non-Latin-1 name (e.g. CJK) makes `fetch` throw. Client sends `encodeURIComponent(name)`, server `unquote`s then sanitizes. `normalizeEditor` in JS mirrors the server's `sanitize_editor` (so the "is this my own change?" comparison works).
- **Rejected:** restricting names to ASCII.
- **Cost:** header value is URL-encoded on the wire; JS/Python whitespace normalization differs for U+001C..U+001F (negligible).

### D11. SSE streams cancelled on shutdown; `shutdown_timeout=5`
- **Why:** aiohttp waits up to 60 s for open handlers on `runner.cleanup()`; a connected browser would stall systemd stop (`TimeoutStopSec=20`).
- **Cost:** a stream that is inside `resp.prepare()` at that instant is not yet tracked and is bounded only by the 5 s timeout.

### D12. `make_app` refuses tokens < 24 chars (fail closed)
- **Why:** defense in depth behind `load_gui_token`; an empty-string token would otherwise authenticate an empty bearer header.

### D13. Config writes shielded from handler cancellation
- See section 6. **Cost:** a disconnecting client may leave a completed write the user did not see confirmed (harmless; the SSE event/refetch shows it).

### D14. Per-IP auth throttle keyed on `request.remote`
- **Why:** cheap brute-force limit with no dependencies. **Cost:** behind a reverse proxy all clients share one IP; any local process can lock the GUI out with 5 bad requests per 60 s (the bot itself is unaffected). Valid tokens are also refused while blocked. Throttle dicts are not pruned (entries only per distinct IP).

### D15. Unconfigured guild reads nothing
- **Why:** fail closed; a bot invited to a new server must not start reading channels. `is_listen_channel`/`is_command_channel` return false without an entry. Slash replies say "No channel configured - set one in the web UI."

### D16. `maybe_start_webui` never crashes the bot
- **Why:** voice TTS is the product; the GUI is optional. Bad port, bad token, bind failure -> log and run without GUI. Tests: `tests/test_wiring.py`.

### D17. Deploy: validate, snapshot, install from snapshot
- See section 7. **Alternatives rejected:** copying straight from the repo as root (symlink dereference by root could publish any secret file, e.g. a symlinked token, world-readable in `/opt`). **Cost:** more script complexity; the residual microsecond race is accepted.

### D18. `LoadCredential` for the GUI token and no optional-credential syntax
- **Why:** consistent with the Discord token. systemd 249 on this host has no optional-credential (`:`-prefixed/`-`) syntax, so a missing `/etc/tts-bot/gui_token` makes the unit refuse to start. `deploy.sh` and `setup_system.sh` therefore refuse earlier with a hint.

### Pre-existing decisions

### D19. piper-tts pinned to 1.2.0
- **Why:** `tts_engine.py` uses `PiperVoice.load(..., use_cuda=...)` and `voice.synthesize_stream_raw(text)`; later piper releases changed the API. **Cost:** upgrading piper requires rewriting `_synth`.

### D20. onnxruntime-gpu `>=1.18,<1.21`; uninstall CPU `onnxruntime`
- **Why:** the GPU wheel must match the CUDA 12.x stack that Pascal supports; `piper-tts` depends on the CPU `onnxruntime`, which shadows/conflicts with the GPU build (both provide the `onnxruntime` module). `setup_system.sh` installs requirements, then `pip uninstall -y onnxruntime onnxruntime-gpu`, then reinstalls the pinned GPU wheel. **Cost:** forgetting this silently leaves CPU-only inference (`CUDAExecutionProvider` missing; the engine logs an error and falls back to CPU).

### D21. FP32 / CUDA only (no FP16, no TensorRT)
- **Why:** GTX 1060 is Pascal (no tensor cores): FP16 gives no speedup and risks accuracy. `load()` warms up the CUDA session; on failure it falls back to CPU rather than dying.

### D22. Hybrid prefix + slash commands
- **Why:** `@bot.hybrid_command` gives `!cmd` for quick typing and `/cmd` for discoverability from one definition. Slash needs the `applications.commands` invite scope; `setup_hook` syncs the tree (failure logged). A `CheckFailure` on a slash invocation must be answered (ephemeral message naming the current channel) or Discord shows "did not respond"; for prefix commands it is silent.

### D23. Intents: `message_content` + `members` (plus voice_states, default)
- **Why:** `message_content` is required to read text (also enable "Message Content Intent" in the Developer Portal); `members` resolves mentions and display names for speech and `status`. **Cost:** both are privileged intents; missing Portal toggles cause login failure or empty content.

### D24. Per-guild queue, hourglass reaction on overflow
- **Why:** bounded memory (`MAX_QUEUE`=20) and isolation between guilds; a flood must not grow the backlog or starve other guilds. A full queue drops the message and reacts with the hourglass so the author sees it was not spoken.

### D25. Temp `.wav` in `<tmp>/tts_bot`, purged at start and stop
- **Why:** ffmpeg reads from a path; clips are deleted in `after`. Crashes can orphan files, so `purge_tmp()` runs in `main()` before load and in `engine.close()`. Under the service `PrivateTmp=true` the directory is private and vanishes with the unit's namespace anyway.

### D26. conda-forge only (`--override-channels`) and explicit `pip`
- **Why:** Anaconda's default channels require a ToS acceptance per user, and `sudo`/root uses its own conda config; `--override-channels -c conda-forge` avoids that. conda-forge's python ships without pip, so `setup_system.sh` includes `pip` in `create` and installs it into pre-existing envs that lack it.

### D27. `PrivateTmp`, `ProtectHome`, `ProtectSystem=strict`
- **Why:** minimal filesystem exposure for a network-facing process. `ProtectHome=true` also means nothing in `~` is reachable, so production must not depend on `~/.config/tts-bot` (hence credentials and `/etc/tts-bot`). `StateDirectory` provides the single writable path.

### D28. `LD_LIBRARY_PATH=/opt/tts-bot/env/lib`
- **Why:** pip-installed CUDA/cuDNN libraries and conda's `libopus`/ffmpeg libs live in the env, not system paths; systemd does not source a shell profile. `bot.py` additionally prepends the env's `bin` to `PATH` (for ffmpeg) and loads `libopus.so.0` from the env.

### D29. Text normalization rules and the spoiler preference
- Rules (`clean_discord_text`, in order): URL -> "link"; code block -> "code block"; inline code keeps its text; spoilers `||x||` -> `x` (**preference: read spoilers aloud, just drop the pipes**); user mention -> "at <display name>" or "someone"; role -> "at <role>" or "role"; channel -> "channel <name>"; custom emoji -> its name; timestamp -> "timestamp"; markdown characters `* _ ~ > # ` |` -> space; runs of 3+ of `! ? , . : ; ~` collapse to one; 4+ repeated letters collapse to three; whitespace collapse; truncate at a word boundary with "..." beyond `MAX_CHARS`; if no alphanumeric remains return `None`.
- **Why:** TTS reads punctuation and URLs badly and slowly; spoken URLs are noise.

### D30. Deploy copies all root `*.py`, `static/*`, `models/*`, `requirements.txt`
- **Divergence:** the plan did not mention models or requirements; the code also installs `models/*` and `requirements.txt`, and prunes stale deployed files. **Cost:** a stray `*.py` in the repo root ships to production (the admin should review).

---

## 9. Nuances and gotchas

- **Dual onnxruntime install.** `piper-tts` pulls CPU `onnxruntime`; it must be uninstalled and `onnxruntime-gpu` reinstalled (D20). Symptom of getting it wrong: log line "CUDAExecutionProvider unavailable ... using CPU" and slow synthesis.
- **conda ToS under sudo.** Without `--override-channels -c conda-forge`, root conda prompts for/needs a Terms-of-Service acceptance and ignores the invoking user's config (D26).
- **`sudo` resets the environment.** `CONDA`, `TEAM_GROUP` must be passed as `sudo CONDA=... scripts/setup_system.sh`; `~` and `$HOME` under sudo may not be the dev's. The default `CONDA` in `setup_system.sh` is a host-specific path.
- **Message Content intent.** Must be enabled in the Developer Portal as well as in code, or `message.content` is empty and nothing is ever spoken (D23).
- **Unconfigured guild reads nothing, and its commands are ignored.** After inviting the bot to a new server, set a channel in the GUI (or seed via `TTS_CHANNEL_ID` at first boot), otherwise even `!join` is rejected (silent for prefix; slash says "No channel configured").
- **Paused is not disconnected.** `enabled=false` only stops *reading* new messages. The bot stays in voice, commands still work in the configured channel (`is_command_channel` ignores `enabled`), already queued clips still play. Unpausing needs no rejoin.
- **Deleting or hiding the configured channel does not clear it.** `on_guild_channel_delete` only publishes a `channels` event; config keeps the dead id and the UI shows a "Configured channel missing" row. The bot then listens to nothing in that guild until someone picks another channel.
- **`channels` events are noisy.** `on_guild_channel_update` fires for any channel edit (permissions, voice channels too), causing extra client refetches. Harmless.
- **`rev` is global, bumps on first-touch of a guild, and `null` skips the check** (section 5).
- **Slash vs prefix errors differ.** A wrong-channel prefix command is silently ignored; a wrong-channel slash command gets an ephemeral reply.
- **A message that starts with the prefix is never spoken**, even from a watched user in the listen channel (it is routed to command processing).
- **`join` follows the caller.** Running `join` elsewhere moves the bot (`move_to`); it is not tied to the text channel's guild beyond membership.
- **Voice state is lost on restart.** After a restart/deploy the bot is not in voice; someone must `!join` again. The watch list persists but `leave`/auto-leave flush it.
- **SSE through proxies needs buffering off.** Nginx honors `X-Accel-Buffering: no`; other proxies need explicit config. Symptom: UI stuck on "Connecting" or updates arrive in bursts.
- **Reverse proxy and the throttle.** All clients share the proxy IP; five bad requests lock everyone out for a minute (D14).
- **`WEBUI_TOKEN_FILE` has no `~` expansion** (`load_gui_token` uses it verbatim), unlike `DISCORD_TOKEN_FILE` which is `expanduser`ed. Use an absolute path.
- **`EnvironmentFile=/etc/tts-bot/bot.env` has no `-` prefix**, so the file must exist (it may be empty) or the unit fails to start. No script creates it; the unit comment shows how.
- **Unit refuses to start without `/etc/tts-bot/gui_token`** (D18). A too-short token does not stop the unit; it just disables the GUI.
- **Upgrading: `deploy.sh` alone ships new code with the OLD unit.** The old unit lacks `LoadCredential=gui_token`, `CONFIG_FILE` and `WEBUI_*`, so the GUI silently stays disabled and `config.json` lands in the default location. Generate the token first, re-run `setup_system.sh` (or copy the unit and `daemon-reload`), then deploy.
- **Config rollback vs disk.** `_mutate` catches `BaseException`: if a caller is cancelled while the thread-offloaded write is in flight, memory rolls back while the write may still land (disk ahead of memory). The web layer's `asyncio.shield` closes this for HTTP callers; `seed_channel` in `on_ready` is not shielded (acceptable; runs once).
- **Persist errors surface differently.** Config: the HTTP call fails (500) and state is rolled back. Watch list: logged, memory keeps the change.
- **Editor-name comparison.** The "X switched to ..." toast is suppressed when the event's `updated_by` equals the local `normalizeEditor(editor)`; two people using the same name will not see each other's toasts.
- **`known_guild` calls `controller.guilds_view()`** (all guilds' channels) on every write request; fine for a handful of guilds.
- **Static files are read at app creation.** Editing `static/*` requires a bot restart (deploy restarts it anyway).
- **Tests import `bot.py`**, which runs side effects (token lookup, opus load, `Bot` construction). `test_controller.py` and `test_wiring.py` set env vars before import for that reason. `DISCORD_TOKEN` must be unset or well-formed.
- **Stale state in `__pycache__`.** `deploy.sh` removes `/opt/tts-bot/__pycache__`; the repo is compiled to a throwaway dir during validation.

---

## 10. Operations runbook

### Fresh install (order matters)
```
sudo scripts/gen_gui_token.sh --system   # 1) prints GUI token once; creates /etc/tts-bot/gui_token
sudo scripts/setup_system.sh             # 2) user, /opt/tts-bot, conda env, sudoers, unit (refuses without the gui_token file)
sudo scripts/install_token.sh --system   # 3) prompts for the Discord token (hidden)
sudo scripts/deploy.sh                   # 4) validate, snapshot, install, restart
sudo systemctl enable tts_bot            # 5)
```
Also create `/etc/tts-bot/bot.env` (may be empty; can hold `TTS_CHANNEL_ID=<id>` for first-boot seeding) because the unit requires it. Put the Piper voice (`.onnx` + `.onnx.json`) in `models/` first; `deploy.sh` refuses when no model files exist. Review `scripts/` before running any of this as root (section 7).

### Upgrade an existing deployment
Generate the GUI token first, re-run `setup_system.sh` (or copy `tts_bot.service` to `/etc/systemd/system/` and `sudo systemctl daemon-reload`), then `deploy.sh`.

### Deploy a code change
`git pull`/edit in the repo, review, run tests, then `sudo scripts/deploy.sh` (restarts the service; the bot leaves voice, so re-run `!join`).

### Rotate the Discord token
Developer Portal -> Bot -> Reset Token, then `sudo scripts/install_token.sh --system`, then `sudo systemctl restart tts_bot`. A leaked token: reset it first.

### Rotate the GUI token
`sudo scripts/gen_gui_token.sh --system --rotate` (prints the new token once), then `sudo systemctl restart tts_bot` (the credential is loaded at unit start). Existing browser tabs get 401 and re-prompt.

### Change the listen channel (no restart)
Open the GUI (`ssh -L 8765:127.0.0.1:8765 <host>`, then `http://127.0.0.1:8765`), enter the GUI token and your name, search, click/Enter. Takes effect immediately; the guild's pending queue is cleared. Keyboard: `/` search, Up/Down, Enter, Esc. Use Pause/Resume to mute reading while testing.

### Logs
`journalctl -u tts_bot -f` (devs are in `systemd-journal` after re-login). Useful lines: `Logged in as ...`, `Piper ready (cuda=True, ...)`, `web UI listening on ...`, `Web UI disabled: ...`, `queue full`, `worker error (continuing)`, `config watcher error`.

### Common failures
| Symptom | Likely cause | Fix |
|---|---|---|
| Unit fails to start immediately | `/etc/tts-bot/gui_token` or `/etc/tts-bot/bot.env` missing | create them (section 10 install order) |
| "Web UI disabled: no usable GUI token" | token missing/short (< 24) or unreadable | `gen_gui_token.sh --system --rotate`, restart |
| GUI works locally, not remotely | loopback bind | SSH tunnel or TLS proxy (disable buffering) |
| GUI keeps "Reconnecting" behind a proxy | response buffering | `proxy_buffering off` for `/api/events` |
| 429 in the GUI | 5 bad auth requests within 60 s from that IP | wait 60 s; fix token; check for local scanners |
| Bot silent, no errors | guild has no channel / paused / user not watched / not joined / Message Content intent off | check GUI, `!status`, Portal intents |
| "CUDAExecutionProvider unavailable" | CPU `onnxruntime` installed over GPU build, or CUDA libs not on `LD_LIBRARY_PATH` | section 8 D20, D28 |
| "Token malformed" at start | quotes/prefix/newline in token file | re-run `install_token.sh` |
| GUI shows new code missing / no GUI after upgrade | old unit installed | upgrade order above |
| `deploy.sh` "Refusing to deploy" | symlink, subdir, or file escaping the repo | remove it; it is intentional |
| Clips not played, `libopus not loaded` warning | opus missing from env | conda-forge `libopus` in the env |

---

## 11. Testing

Run from the repo root:
```
~/miniconda3/envs/tts-bot/bin/python -m unittest discover -s tests -v   # 62 tests at time of writing
bash tests/test_deploy_validation.sh                                    # deploy validation, no root needed
```
`unittest` only; no pytest.

| File | Covers |
|---|---|
| `tests/test_config_store.py` | atomic write (no `.tmp` leftovers on success/failure), EventHub (fan-out, idempotent unsubscribe, drop-oldest), ConfigStore (rev/no-op, conflict leaves state untouched, `expected_rev=None`, round-trip, corrupt file, persist-failure rollback, truth table of `is_*`, event publishing, seed, 20 concurrent writes). |
| `tests/test_webui.py` | auth (missing/wrong/right), throttle with injected clock, static headers/CSP, token never in bodies, PUT channel/enabled incl. persistence and hub events, conflict, bad inputs, editor required/sanitized/percent-encoded, skip/clear, handler cancellation mid-write stays consistent, SSE (hello, config, status only on change, keepalive, subscriber cleanup), header parsing, shutdown with an open SSE, token loader order and short tokens, start/cleanup. |
| `tests/test_controller.py` | `Controller` views and actions with fake guilds, `config_watcher` drain behavior (channel change drains; `enabled` change does not; other guilds and malformed events are survived). |
| `tests/test_wiring.py` | `maybe_start_webui` never raises (no/short token, bad `make_app`, out-of-range port, port in use, load error), `parse_port`, good token serves and cleans up. |
| `tests/test_static.py` | static payload < 40 KB, CSP-safe content (no `innerHTML`/`eval`/inline handlers/external URLs), served bytes equal files, and runs `tests/static_logic.js` under node if available. |
| `tests/static_logic.js` | pure JS helpers: SSE parser, `matchChannel`, `relTime`, `backoffDelay`, `groupChannels`, `normalizeEditor`, `shouldRollback`. |
| `tests/test_deploy_validation.sh` | deploy validator: clean tree accepted; symlinked dirs/files, in-repo symlinks, subdirectories, missing `.py` rejected. |

**Not covered:** anything needing live Discord, a GPU, ffmpeg/voice, or a browser. `engine.synthesize`/playback/`audio_worker`/`on_message` end to end, the real systemd unit, `setup_system.sh`/`deploy.sh` as root, and DOM behavior of `app.js` (rendering, optimistic rollback wiring, keyboard handling) have no automated tests. The frontend was verified by node logic tests, static content checks and code review only. Run the manual smoke checklist in a real browser (and a real Discord test guild) before relying on it: sign-in prompt, token rejection, channel list/search/keyboard, select + toast + undo, pause/resume, skip/clear, two browsers (conflict toast, live toast), kill/restart the bot and watch reconnect, 360 px width, dark mode.

---

## 12. Known limitations

- No live Discord, GPU or browser tests (section 11).
- Editor names are self-declared attribution, not authentication; the token is the only gate and it is shared.
- One channel per guild (plus pause).
- Per-IP throttle caveats: shared behind a proxy; any local process can lock the GUI out; valid tokens refused while blocked; counters not pruned.
- `WEBUI_TOKEN_FILE` has no `~` expansion; `load_gui_token` does not fall through to later sources when an earlier source holds a short token.
- Deploy copies every root `*.py` (stray files ship) plus `static/*`, `models/*`, `requirements.txt`; `deploy.sh` and `setup_system.sh` run repo-supplied scripts as root (admin review required); a microsecond check/copy race is accepted; `setup_system.sh` resolves `SRC` with `pwd`, not `pwd -P`.
- The GUI is down whenever the bot is down; all plain HTTP (tunnel or TLS proxy required for remote use).
- The bot does not rejoin voice after a restart.
- `config.json` and `state.json` corruption: starts empty, no backup.
- Minor, deferred review items: first-set-on-unknown-guild bumps rev; `RecursionError` on pathologically nested JSON gives 500; 404 bodies are not JSON; `on_guild_channel_update` is chatty; shutdown cancels `_watcher_task` without awaiting it; DM `NoPrivateMessage` is unreachable on the prefix path; clicks while a request is in flight are silently ignored by the UI.

---

## 13. Asking an LLM about this repo

Give it first, in this order: `CLAUDE.md`, this file, then as needed `bot.py`, `config_store.py`, `webui.py`, `tts_engine.py`, `tts_bot.service`, `scripts/deploy.sh` + `scripts/lib/deploy_validate.sh`, `static/app.js`. For rationale history: `docs/superpowers/plans/2026-10-13-channel-gui.md` and `git log --oneline`.

Example questions it should be able to answer from these files:
- Why is the GUI inside the bot process and what happens to it when the bot stops?
- What happens to queued messages when someone changes the channel? To the clip currently playing?
- Two people click different channels at the same time: what does each see?
- Why does the browser not use `EventSource`?
- Why do deploys run from a snapshot, and who can read the Discord token?
- Why is there only one Piper worker thread?
- Why does the unit refuse to start after upgrading with only `deploy.sh`?
- What does pause do and not do?
- How do I rotate the GUI token? The Discord token?
- Why does `TTS_CHANNEL_ID` stop having any effect after first boot?
- What are the throttle's failure modes behind nginx?
- What is not tested?
