# Architecture Guide

Audience: developers and LLMs answering questions about this repo. This guide was refreshed against the code at HEAD at the time of the last documentation pass; on any disagreement, the code is the source of truth. Where the original plan (`docs/superpowers/plans/2026-10-13-channel-gui.md`) differs from the current code, the code is documented and the divergence is flagged with "Divergence".

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

A Discord bot that reads messages from a chosen set of users ("watched" users) aloud in a voice channel, using the Piper neural TTS model on a local GPU (GTX 1060, CUDA, FP32). One process does everything: the discord.py client, a per-guild queue and playback worker, a GPU-serialized synthesis engine, and a small aiohttp web UI. The web UI lets several developers pick which text channel the bot listens to (per guild), pause/resume it, and skip or clear the queue, live, without restarting the bot. A second tab (Voice) hot-swaps the Piper voice (global, one GPU engine for all guilds) from the voices installed in the models folder and can speak a test phrase. Channel selection and the chosen voice live in `config.json` (the source of truth, edited via the GUI); the watch list lives in `state.json` (edited via chat commands). Production runs as a hardened systemd service under an unprivileged user with root-owned code; the Discord token never touches the dev-writable checkout.

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
                       |   aiohttp web UI (webui.py)  loopback+tailnet :8765   |                                                  |
                       +-------------|-------------------------------------- | ------------------------------------------------+
                                     |  REST (JSON) + SSE (/api/events)      v
 Browsers (devs) <-- Tailscale ------+                               Discord voice server
   static/index.html, app.js, app.css
   fetch() streaming, X-Editor header (no token; Host allowlist + Origin check)
```

Key points the diagram encodes: one process and one event loop; the web UI never talks to Discord or Piper directly (only through `Controller` and `ConfigStore`); `EventHub` is the only fan-out path from config changes to browsers and to `config_watcher`; Piper inference is the only code that runs off-loop (single dedicated thread).

---

## 3. Module map

| File | Responsibility | Public interface |
|---|---|---|
| `bot.py` | Entry point. Discord events and commands, per-guild queue/worker, WatchStore, Controller (incl. `list_voices`, `apply_voice`, `say`, `voice_summary`, `members_for`, `send_message`), config watcher, lifecycle (startup incl. `load_engine`, signals, shutdown). | Run `python bot.py`. Module-level: `config` (ConfigStore), `store` (WatchStore), `engine` (TTSEngine), `audio` (dict guild_id -> GuildAudio), `controller` (Controller), `load_token()`, `parse_port()`, `maybe_start_webui(cfg, ctl, port)`, `load_engine()`, `default_voice_paths()`, `shutdown()`, `main()`. Env: `PIPER_MODELS_DIR` (default: dirname of `PIPER_MODEL_PATH`). |
| `tts_engine.py` | Piper wrapper (CUDA with CPU fallback, hot reload) and text normalizers. | `TTSEngine(model_path, config_path)`: `load(model_path=None, config_path=None) -> bool` (never raises), `async reload(model_path, config_path)` (raises `VoiceLoadError`, old voice stays), `async synthesize(text) -> path\|None`, `purge_tmp()`, `close()`, attrs `ready`, `cuda_active`, `model_name`, `loading`, and `state`: an immutable `VoiceState` snapshot (`voice`, `sample_rate`, `model_path`, `config_path`, `cuda_active`); the legacy attributes are properties reading the same snapshot. `clean_discord_text(message, max_chars=300)`, `clean_plain_text(text, max_chars=300)` (same pipeline, no Discord objects). |
| `voices.py` | Pure stdlib: voice discovery and the errors shared by engine, bot and web layer. | `discover_voices(models_dir) -> list[dict]` (blocking), `resolve_voice(models_dir, name) -> (onnx, json)` (raises `BadVoiceError`, a `ValueError`), `VOICE_NAME_RE`, `VoiceLoadError`, `BadVoiceError`, `SayError(code)`. |
| `messages.py` | Pure stdlib: the "speak as the bot" building blocks shared by `bot.py` and `webui.py`. | `MAX_MESSAGE_CHARS` (2000), `MessageError(code, retry_after=None)`, `parse_mentions(content) -> list[int]` (user tokens `<@id>`/`<@!id>`, de-duplicated; role tokens `<@&id>` raise `bad_mention`; more than 10 distinct users raise `bad_mention`), `SendLimiter(clock=time.monotonic, per_window=10, window=60.0, min_gap=2.0).reserve(key)` (raises `rate_limited` with an int `retry_after`). |
| `config_store.py` | Runtime config with revision counter, atomic persistence, pub/sub. | `atomic_write_json(data, path)`, `ConflictError(snapshot)`, `EventHub` (`subscribe/unsubscribe/publish/subscriber_count`), `ConfigStore` (`snapshot`, `channel_for`, `is_enabled`, `is_listen_channel`, `is_command_channel`, `async set_channel`, `async set_enabled`, `async set_voice`, `async seed_channel`, `.rev`, `.voice`, `.hub`). |
| `webui.py` | aiohttp backend: bind-address policy (`validate_bind_hosts`, Tailscale detection), Host allowlist + Origin check, REST, SSE, static serving. No token auth. Duck-typed on the controller; never imports `bot.py`. | `validate_bind_hosts(hosts)`, `detect_tailscale_addrs()`, `parse_tailscale_ip()`, `parse_ip_addr()`, `parse_allowed_hosts()`, `host_of()`, `make_app(config, controller, *, allowed_hosts, status_interval=3.0, keepalive_interval=15.0, static_dir=None)`, `async start_webui(app, hosts, port, *, retry_interval=15.0) -> WebUIRunner` (binds each address independently, retries failures every 15 s, raises `OSError` if none bind), `sanitize_editor()`, `wire_snapshot()`, `wire_event()`. The controller is duck-typed: `guilds_view`, `status_view`, `is_text_channel`, `skip`, `clear_queue`, `voice_summary` (optional), `async list_voices`, `async apply_voice`, `say`, `async members_for(gid, q)`, `async send_message(gid, content, editor)` (both raise `MessageError`). |
| `static/index.html`, `app.js`, `app.css` | Vanilla-JS single-page UI. No framework, no CDN, no inline script/style (CSP). `app.js` also exports pure helpers under node for tests. | Served at `/`, `/app.js`, `/app.css`. |
| `tts_bot.service` | systemd unit: user `ttsbot`, Discord-token credential, env, sandboxing, ordered after `tailscaled`. | Installed to `/etc/systemd/system/` by `setup_system.sh`. |
| `scripts/setup_system.sh` | One-time root setup: service user, `/opt/tts-bot`, private conda env, sudoers rule, journal group, unit install. | `sudo scripts/setup_system.sh` (env: `CONDA`, `TEAM_GROUP`). |
| `scripts/deploy.sh` | Admin deploy: validate, snapshot, compile, install, prune stale code/static (never `models/`), restart. | `sudo scripts/deploy.sh`. |
| `scripts/lib/deploy_validate.sh` | Sourced functions `snapshot_sources SRC SNAP` (validation + snapshot) and `validate_voice_files ONNX JSON [PY]` / `voice_stem_ok`. | Tested by `tests/test_deploy_validation.sh`. |
| `scripts/install_voice.sh` | Admin: add a voice to `/opt/tts-bot/models` with no restart or deploy (validates a private copy; without `--force` it creates the files with `ln` (fails if the name exists, so no clobber race), `.onnx.json` first then `.onnx`, and if the second `ln` fails it `rm`s the first so the pair is installed or rolled back together, then `rm`s the staging links; `--force` removes the old pair then `mv`s the new one in, which is not atomic). | `sudo scripts/install_voice.sh [--force] <v.onnx> [<v.onnx.json>]`. |
| `scripts/install_token.sh` | Hidden prompt, shape-checks and stores the Discord token (mode 600). | `[--system]`; `TOKEN_DIR` env override (undocumented in README). |
| `tests/` | See section 11. | `python -m unittest discover -s tests` |
| `requirements.txt` | `discord.py[voice]>=2.4`, `PyNaCl>=1.5`, `piper-tts==1.2.0`, `onnxruntime-gpu>=1.18,<1.21`, `python-dotenv>=1.0`. | |
| `.env.example` | Non-secret dev settings template. | |

### Web API (all under `webui.py`)

| Route | Guard | Purpose |
|---|---|---|
| `GET /`, `/app.js`, `/app.css` | Host allowlist (no secrets in them) | Three explicit routes; files read once at app creation. Accessible to unauthenticated clients for rendering the UI and login page. |
| `GET /api/session` | Host | Current session status: `{"authenticated": bool, "auth_required": bool, "username": str\|null}`. |
| `POST /api/login` | Host/Origin + Rate limited (5/min per IP) | Body `{"username": str, "password": str}`. Validates against `users.json`; sets `tts_session` cookie (`HttpOnly`, `SameSite=Lax`). Returns `{"ok": true, "username": str}` (200), 400 `bad_request`, 401 `invalid_credentials`, 429 `rate_limited`. |
| `POST /api/logout` | Host/Origin | Revokes the current session and clears the `tts_session` cookie. Returns `{"ok": true}`. |
| `GET /api/state` | Host + Session | `{"config": snapshot, "guilds": guilds_view}` |
| `PUT /api/guilds/{gid}/channel` | Host/Origin + Session + `X-Editor` | Body `{"channel_id": "<digits>"\|null, "rev": int\|null}`. 400 `bad_channel`, 409 `conflict` (with snapshot), 200 `{"snapshot"}`. |
| `PUT /api/guilds/{gid}/enabled` | Host/Origin + Session + `X-Editor` | Body `{"enabled": bool, "rev": int\|null}`. |
| `POST /api/guilds/{gid}/skip` | Host/Origin + Session + `X-Editor` | `{"skipped": bool}` |
| `POST /api/guilds/{gid}/clear` | Host/Origin + Session + `X-Editor` | `{"cleared": int}` |
| `GET /api/voices` | Host + Session | Rescans the models folder every call (this is the GUI's Rescan): `{"voices":[{name,sample_rate,quality,language,size_mb}], "current": stem\|null, "configured": stem\|null, "loading": bool, "cuda": bool\|null}`. |
| `PUT /api/voice` | Host/Origin + Session + `X-Editor` | Body `{"name": "<stem>"\|null, "rev": int\|null}` (`null` = env default). 200 `{"snapshot"}`; 400 `bad_request`/`bad_voice`; 409 `conflict` (with snapshot); 422 `voice_load_failed` (nothing saved, old voice still active); 500 `persist_failed` (engine reverted best-effort). Runs under `asyncio.shield`. Takes seconds (model load + warm-up). |
| `POST /api/guilds/{gid}/say` | Host/Origin + Session + `X-Editor` | Body `{"text": str}` (1-200 chars). `{"queued": true}`; 400 `bad_request`/`too_long`; 404 unknown guild; 409 `not_connected`/`queue_full`/`tts_unavailable`. Goes through the normal guild queue. |
| `GET /api/guilds/{gid}/members?q=` | Host + Session | Tag picker. `{"members":[{id (string), name (display name), username, bot}], "truncated": bool}`: members who can view the configured channel, case-insensitive substring on display name / username / global name, empty `q` = first 25 by display name (non-bots first), max 25. 404 unknown guild, 409 `no_channel`. `q` is cut at 100 chars. |
| `POST /api/guilds/{gid}/send` | Host/Origin + Session + `X-Editor` | Body `{"content": str}` (1-2000 chars after strip). Posts as the bot in the guild's CONFIGURED channel (the caller never names a channel). `{"ok": true, "message_id": "<str>"}`; 400 `bad_request`/`bad_mention`, 403 `missing_permission`, 404 unknown guild, 409 `no_channel`, 429 `rate_limited` (`{"error":"rate_limited","retry_after":<int s>}`), 502 `discord_error`, 500 `internal`. Runs under `asyncio.shield`. |
| `GET /api/events` | Host + Session | SSE: `hello` (snapshot+guilds+`voices` summary) on connect, then `config`, `channels`, `voice`, `status` (only when changed), `: keepalive` comment every 15 s. |

Error codes: 401 `unauthorized` (when authentication is enabled and session cookie / Bearer token is missing or expired), 403 `bad_peer` (TCP peer not loopback/Tailscale, any route), 403 `bad_host` (Host not on the allowlist, any route) / `bad_origin` (write with a foreign `Origin` or `Sec-Fetch-Site: cross-site`), 500 `persist_failed` (config write raised `OSError`; no detail leaked), 400 `editor_required` / `bad_request` / `bad_channel` / `bad_voice` / `too_long`, 404 (unknown or non-digit guild id; body is not JSON), 409 `conflict`, 422 `voice_load_failed`, 409 `not_connected` / `queue_full` / `tts_unavailable`, 500 `internal` (unexpected error in the voice apply; generic body), 413 for bodies > 16384 bytes (aiohttp `client_max_size`, whole app); message endpoints add 400 `bad_mention`, 403 `missing_permission`, 409 `no_channel`, 429 `rate_limited`, 502 `discord_error`. Bad JSON, non-object body, or a `rev` that is not an int (bools rejected) -> 400 `bad_request`.

All IDs on the wire are strings (JS numbers lose precision above 2^53). `wire_snapshot`/`wire_event` convert `channel_id` and `change.guild_id/old/new`; internally the store uses ints.

### Event shapes
- Hub `config` event (internal, ints): `{"type":"config","snapshot":{...},"change":{"guild_id":int,"field":"channel_id"|"enabled","old":..,"new":..}}`.
- Hub `config` event for the voice: `change = {"guild_id": null, "field": "voice", "old": stem\|null, "new": stem\|null}` (global change; `wire_event` keeps `guild_id` as JSON null).
- Hub `voice` event (forwarded to browsers unchanged as SSE event `voice`): `{"type":"voice","loading":true}` when a swap starts, `{"type":"voice","loading":false,"current":stem\|null,"cuda":bool\|null,"error":null\|"load_failed"}` when it ends (always, also on failure). Every tab uses it to disable Apply while loading. The `cuda` field is true if the active voice runs on GPU, false if CPU, null if no voice is loaded.
- Hub `channels` event: `{"type":"channels","guild_id":"<str>"|null}` published by `bot.py` on channel create/delete/update, guild join/remove/available, and (with `guild_id: null`) at the end of every `on_ready`, so a tab that connected during the restart window (empty guild cache) refetches. The browser reacts by refetching `/api/state`, coalesced (~250 ms trailing, one request in flight).
- Snapshot: `{"rev","updated_by","updated_at","voice":stem\|null,"guilds":{"<gid>":{"channel_id":int|null,"enabled":bool}}}`.

---

### Chat commands

Every command is a hybrid command (prefix and slash), guild-only, and runs only in the configured text channel. The table matches `bot.commands`; `help` generates its list from the same registry.

| Command | Description |
|---|---|
| `help` | Show available commands and current status |
| `join` | Join your voice channel |
| `leave` | Leave voice, clear queue and watch list |
| `skip` | Skip the clip currently playing |
| `status` | Show watched users and voice state |
| `stop @user` | Stop reading this user's messages |
| `voices` | List available TTS voices (read-only; switching is GUI-only, D44) |
| `watch @user` | Read this user's messages aloud, optionally join voice (joins your channel when the bot is not connected) |

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
1. Browser: user clicks a row (or Enter). `app.js` updates the UI optimistically and sends `PUT /api/guilds/{gid}/channel` with `X-Editor: <percent-encoded name>`, body `{channel_id, rev}`.
2. Server middleware order: `security_headers` -> `peer_guard` (source address) -> `host_guard` (Host allowlist on every request; Origin / `Sec-Fetch-Site` check on non-GET/HEAD). Handler: validates guild (404), editor (400), body (400), channel via `Controller.is_text_channel` (400 `bad_channel`).
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

### (d) Swapping the voice
1. Browser (Voice tab): the user highlights a row (nothing happens yet) and presses Apply. `PUT /api/voice {name, rev}`; the handler validates the name shape (`VOICE_NAME_RE`), then calls `Controller.apply_voice` inside `asyncio.shield`.
2. `apply_voice` takes `Controller._voice_lock` (one swap at a time), resolves `name` through `resolve_voice` (the name must equal an installed stem; the path is rebuilt from the discovered entry, never from the request) or uses the env default for `null`, then checks `expected_rev` against `config.rev` (stale -> `ConflictError` -> 409) **before the engine is touched**.
3. It publishes `voice loading:true`, then `await engine.reload(onnx, json)`. On the engine's single worker thread (same `asyncio.Lock` as synthesis, so it queues behind an in-flight clip and later clips queue behind it) a candidate `PiperVoice` is built and warmed up (CUDA, CPU fallback as at startup). If the current voice is on CUDA (`cuda_active` true) and the candidate would only run on CPU (CUDA init/OOM fell back), the swap is refused with `VoiceLoadError` to prevent a silent performance downgrade. Only on successful build, a new immutable `VoiceState` (`voice`, `sample_rate`, `model_path`, `config_path`, `cuda_active`) replaces `engine._state` in one assignment, so an event-loop reader of `engine.state` never sees a mix of old and new (a failed `load()`/`reload()` and a refused swap leave the previous snapshot object untouched). The refusal applies only to swaps: the first load at startup may still fall back to CPU (`cuda` is then false), the old voice is `del`-ed and `gc.collect()` runs on the worker thread. Any failure raises `VoiceLoadError` (message without paths) and the old voice keeps serving untouched.
4. A `finally` publishes `voice loading:false` with `current` and `error` (`null` or `"load_failed"`).
5. Failure -> 422 `voice_load_failed`, nothing saved. Success -> `config.set_voice(name, expected_rev=None, editor)` persists atomically and publishes a `config` event (`field: "voice"`); every tab toasts "X switched voice to Y". If the persist raises `OSError` the engine is reloaded with the previous model (best effort) and the call returns 500 `persist_failed`.
6. Clips already synthesized keep their own WAV header rate, so a rate change between voices is safe. Queued text is synthesized with whichever voice is current at synth time.
7. At startup `load_engine()` loads the saved voice directly (single load) if it still resolves; a missing saved voice logs one WARNING and the env default is used; an unloadable one logs an error and falls back. In both cases `config.voice` is left alone, so the GUI shows `configured != current` and a warning.
8. Adding voices: `scripts/install_voice.sh` (no restart) or drop files into the repo `models/` and deploy; then Rescan. The test box posts to `/api/guilds/{gid}/say`, which cleans the text (`clean_plain_text`) and puts it on the guild's normal queue (so it needs the bot in voice).

### (e) Sending a message from the GUI
1. Browser (Messages tab): the user writes text; "Tag someone" searches `GET /api/guilds/{gid}/members?q=` (debounced ~200 ms, one request in flight). Picking a member inserts `@Display Name` at the cursor and records `{id, label}` as a chip. The preview and "Will ping" summary are computed from the exact final text that will be sent: both use `pingTargets(finalText, chips)` after `substituteTags`, which replaces each chip's label with `<@id>` (longest-first, one pass). Raw mention tokens typed by the user are listed as "unknown user <id>" if not a known member. The frontend regex `MENTION_SRC` / `mentionRe()` / `tokenizeMentions()` in `static/app.js` matches only `<@id>` and `<@!id>` (user mentions); role tokens `<@&id>` are rejected server-side by `parse_mentions` in `messages.py`. On Send the client posts the final text (already with `<@id>` substituted) to `POST /api/guilds/{gid}/send`. Member cache is reset + refetched when the configured channel changes from any snapshot path (SSE `config`/`hello`, optimistic update via `applySnapshot`, or load via `adoptConfig`), all routed through the pure `channelChanged(prevCfg, nextCfg, gid)` function.
2. `webui.py` runs the usual guards (peer, Host, Origin / `Sec-Fetch-Site`), requires `X-Editor`, parses the body (16 KB limit), cheaply re-checks type and 1-2000 length, then calls `Controller.send_message` inside `asyncio.shield`.
3. `send_message` strips the text, resolves the channel ONLY from `config.channel_for(gid)` (must be a `discord.TextChannel` of that guild, else `no_channel`), parses mentions (`<@&id>` -> `bad_mention`), allows at most 10 distinct tagged users (more -> `bad_mention`, before any send or rate-limit reservation), requires every mentioned user to be a cached member who can `view_channel` there (else `bad_mention`; nobody who cannot see the channel is ever pinged), requires `guild.me` to be known (during an outage it is None -> `no_channel`) and the bot's own `send_messages` (else `missing_permission`), builds `AllowedMentions(everyone=False, roles=False, replied_user=False, users=[Object(id) ...])`, and only then calls `SendLimiter.reserve(gid)` (at most 10 per rolling 60 s and 2 s between sends, per guild; `rate_limited` with `retry_after`). The reservation is recorded BEFORE `channel.send` is awaited, so a cancelled or duplicated request cannot bypass the limit; failed validation does not consume budget, a failed Discord call does.
4. `Forbidden` -> `missing_permission` (403), other `HTTPException` -> `discord_error` (502). Either failure logs one WARNING `gui message failed guild=... channel=... editor=... reason=forbidden` / `reason=http_error status=<code>` (never the content). Success logs one INFO line `gui message sent guild=... channel=... editor=... chars=N mentions=N` (never the content) and returns `{"ok": true, "message_id": "<str>"}`. The literal text `@everyone` / `@here` may appear in a message but `allowed_mentions` makes it inert.
5. The tab clears the text and chips, toasts "Sent" and disables Send for 2 s. On any failure the text is kept.
6. Member picker server side (`members_for`): if `guild.chunked` is false it awaits `guild.chunk()` once (under a lock, 10 s timeout; failure falls back to the cache with a WARNING and is remembered for 60 s, so later searches in that window do not call `chunk()` again), filters by name first, sorts (non-bots, then display name), and only then checks `permissions_for(member).view_channel` lazily until 25 viewers are found, with at most 2000 permission checks per request (then `truncated` is true) and a yield to the event loop every ~250 members, so a large guild does not block the event loop; if `guild.me` is None it returns an empty list.

---

## 5. State and persistence

| Item | Where | Format / semantics | Written by |
|---|---|---|---|
| Listen channel + pause flag + global voice | `config.json` (`CONFIG_FILE`; default: next to `STATE_FILE`; `/var/lib/tts-bot/config.json` in production) | The snapshot dict: `rev`, `updated_by`, `updated_at` (epoch float), `voice` (stem or null; absent/invalid in old files -> null), `guilds{gid: {channel_id, enabled}}`. Pretty-printed JSON. | `ConfigStore.set_channel` / `set_enabled` / `set_voice` / `seed_channel` |
| Watch list | `state.json` (`STATE_FILE`; default next to `bot.py`; `/var/lib/tts-bot/state.json` in production) | `{"watched": {"<gid>": [user ids]}}` | `WatchStore.mutate` |
| Installed voices | `models/` next to `PIPER_MODEL_PATH` (`PIPER_MODELS_DIR`; `/opt/tts-bot/models` in production) | `<stem>.onnx` + `<stem>.onnx.json`; scanned on demand, never cached | repo + `deploy.sh`, or `install_voice.sh` |
| Loaded Piper voice, `loading` flag, voice lock | memory | `TTSEngine`, `Controller._voice_lock` | |
| Queues, workers, voice connections, playing clip | memory | `GuildAudio` per guild | |
| EventHub subscribers, SSE tasks, bind-retry task | memory | | |
| Temp `.wav` clips | `<tempdir>/tts_bot` (`/tmp/tts_bot`; under `PrivateTmp` in the service) | deleted by `after`; `purge_tmp()` at start and in `engine.close()` | |

**`rev` semantics.** Starts at 0 for a fresh store; +1 for every effective change (channel set to a different value, enabled flipped, or first seed of a guild); persisted, so it survives restarts. `set_voice` is a global change: it bumps the same `rev`, a no-op (same value) does not. No-op writes (same value on an existing guild entry) return the current snapshot without bumping rev or publishing. Quirk: the first `set_*` on a guild with no entry always counts as a change, even when the value equals the default (`enabled=True`, `channel_id=None`), so it bumps rev. `expected_rev=None` bypasses conflict detection.

**Atomic writes.** `atomic_write_json` creates a temp file in the same directory (`.cfg-*.tmp`), writes, flushes, `fsync`s, then `os.replace`s over the target; on any failure the temp file is unlinked and the error re-raised. A crash leaves either the old or the new file, never a torn one. The same function backs `state.json` (the older private `save_state` was removed).

**Load behavior.** Both stores tolerate a missing file (start empty) and a corrupt file (log the exception, start empty). `ConfigStore` first renames an unreadable `config.json` to `config.json.corrupt` (best-effort, replaces an older `.corrupt`, never raises) so the next write cannot destroy it; `WatchStore` still overwrites a corrupt `state.json` with no backup. `ConfigStore` never raises from its constructor.

**Persist failure.** `_mutate` restores in-memory state and rev and re-raises; nothing is published. `WatchStore.mutate` instead logs "state save failed" and keeps the in-memory change (memory ahead of disk until the next successful write).

**Seed.** `TTS_CHANNEL_ID` (comma-separated ids allowed) is only a seed: on `on_ready`, for each guild with no entry in config, the first listed id that is a `TextChannel` in that guild is stored with editor `env-seed`. Once a guild has an entry, the env value is ignored. Clearing the channel in the GUI sets `channel_id: null` but keeps the entry, so the seed does NOT re-apply.

**Lost on restart:** queued-but-unspoken messages, the voice connection (the bot does not rejoin; someone must run `join` again), which also means no `GuildAudio` exists until `join`. **Kept:** channel selection, pause flag, chosen voice, rev, watch list.

Other state-changing behaviors: `leave` and auto-leave (no humans left in the bot's voice channel) both flush that guild's watch list.

---

## 6. Concurrency model

- **One asyncio event loop** runs discord.py, all per-guild workers, `config_watcher`, the aiohttp server, and all `ConfigStore`/`WatchStore` logic. `Controller` view/action methods are synchronous and non-blocking and must be called on the loop (aiohttp handlers are); the voice methods (`list_voices`, `apply_voice`) are coroutines that hand disk scans and model loads to threads.
- **Locks.**
  - `ConfigStore._lock` (`asyncio.Lock`): serializes all config mutations including the thread-offloaded disk write, so rev check + mutate + persist + publish is atomic with respect to other writers. Reads (`channel_for`, `is_*`, `snapshot`) take no lock; they read in-memory dicts on the loop and so never see a half-applied mutation except for the brief window in which a not-yet-persisted change is visible (and may be rolled back if persist fails).
  - `WatchStore.lock`: serializes watch-list mutations plus their persist.
  - `Controller._voice_lock`: serializes whole voice swaps (resolve, rev check, reload, persist, revert). `TTSEngine._lock` additionally serializes `reload` with synthesis, so a swap waits for the in-flight clip and later clips wait for the swap.
  - `TTSEngine._lock` (created lazily on first use so it binds to the running loop): ensures only one synthesis is submitted at a time.
- **Single GPU worker thread.** `TTSEngine._pool = ThreadPoolExecutor(max_workers=1)`. Why: the GTX 1060 has 6 GB and one CUDA context; ONNX Runtime sessions are not worth sharing across threads; parallel synthesis would only add VRAM pressure and contention without latency gain for short clips. The thread keeps inference off the event loop so the Discord gateway heartbeat is never blocked. The `asyncio.Lock` in front of the pool means cancellation of a waiting caller does not queue stale work behind a long job. Do not add a second thread/executor for GPU work. The same thread builds and warms the candidate voice in `reload`, so a hot swap can never overlap an inference. GPU memory: the candidate is built while the old session is still loaded (peak = two sessions), then the old one is freed right after the swap (`del` + `gc.collect()` on the worker thread). ORT's arena may keep some memory, so repeated swaps can grow usage on the 6 GB card; a service restart clears it.
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
| Admin (real sudo) | run `setup_system.sh`, `deploy.sh`, `install_token.sh --system`; read the Discord token | |
| Dev (group `devteam`) | edit the repo; `sudo systemctl start\|stop\|restart tts_bot` (exact commands, no wildcards); `systemctl status`, `journalctl -u tts_bot` (via `systemd-journal` group); use the GUI if Tailscale ACLs let them reach it | read `/etc/tts-bot/*`; modify `/opt/tts-bot`; use any other sudo command |
| Service (`ttsbot` user) | read `/opt/tts-bot` (root-owned), write `/var/lib/tts-bot`, `/tmp` (private) | write code, see home dirs, gain privileges |
| GUI user (anyone who can reach the port: loopback, or tailnet per ACLs) | change the configured channel, pause/resume, skip and clear the queue, swap the voice, make the bot speak test phrases, and post as the bot in the configured channel (mentions limited to <=10 distinct members who can see the channel; 10 sends per 60 s plus a 2 s gap per guild) | read the Discord token; read/write anything on the host |

### Token locations and flow (names only)
- **Discord token**: production `/etc/tts-bot/token` (root:root 600, dir `/etc/tts-bot` 700) -> systemd `LoadCredential=discord_token:/etc/tts-bot/token` -> process sees `$CREDENTIALS_DIRECTORY/discord_token`. Dev: `~/.config/tts-bot/token` (mode 600), override `DISCORD_TOKEN_FILE`. Lookup order in `load_token()`: credential, file, then `DISCORD_TOKEN` env (last resort). Shape-validated against `TOKEN_RE` at import; on mismatch the process exits with a message containing only the length.
- There is **no GUI token**: the GUI is protected by the network layer (below). The browser keeps only the editor name, in `localStorage`.
- `.env`, `~/.config/tts-bot/`, and `/etc/tts-bot/` are off-limits for agents and must never be committed (`.gitignore` covers `.env`).

### Web UI trust boundary (network layer + session authentication)
**The boundary is who can reach the port, backed by application-layer session authentication.** The GUI listens only on loopback and this host's Tailscale address(es); access control is Tailscale ACLs (and, later, a zero-trust proxy in front of a domain). Additionally, an application-layer authentication system controls who can view and interact with the app via `users.json` (or `USERS_FILE`).

- **Session authentication & users credentials**: When a credentials file (`users.json`, default next to `config.json` or specified via `USERS_FILE`) is present and configured, unauthenticated requests cannot view the app or access protected API routes (`401 unauthorized`). Visitors must authenticate through the Sign in form (`POST /api/login`), which issues an in-memory session token stored in an `HttpOnly`, `SameSite=Lax` cookie (`tts_session`, 7-day lifetime) or supported via `Authorization: Bearer <token>`. Sign out (`POST /api/logout`) destroys the session and clears the cookie.
- **Dynamic credential reloading**: Credentials in `users.json` are re-read dynamically whenever the file's modification time changes, allowing administrators to add or update user credentials without restarting the bot. Formats supported include list of user objects (`[{"username": "...", "password": "..."}]`), user dictionary mappings, or `"users"` wrappers. Passwords may be plaintext or hashed using `sha256:<hex>`.
- **Brute-force protection**: Login attempts are throttled per IP address (max 5 failed attempts per 60 seconds); excess attempts return `429 rate_limited`.
- **Fail-closed bind policy** (`validate_bind_hosts`): every bind address must be an IP literal in `127.0.0.0/8`, `::1`, Tailscale CGNAT `100.64.0.0/10` or Tailscale ULA `fd7a:115c:a1e0::/48` (`localhost` maps to `127.0.0.1`). Anything else (`0.0.0.0`, `::`, LAN addresses, hostnames) raises `ValueError` and `maybe_start_webui` disables the GUI (logged), so a typo cannot make the app listen on a LAN address or the wildcard. `start_webui` re-validates. This alone does NOT keep LAN hosts out (see the peer check). Default (auto mode): `127.0.0.1` plus auto-detected Tailscale addresses (`tailscale ip`, fallback `ip -o addr show dev tailscale0`, 3 s timeout, no shell). Each address is bound independently; one that cannot bind yet is retried every 15 s by a background task that `runner.cleanup()` cancels (cleanup closes the sites even if that task died). In auto mode the same task also re-runs detection every 15 s until a Tailscale address is found (Tailscale without an IP at boot), adds it to the Host allowlist and binds it once; detection failures are logged at INFO on first occurrence and on state change only. In explicit `WEBUI_HOSTS` mode only the listed addresses are retried and nothing is re-detected. Documented sharp edge: explicit `WEBUI_HOSTS` without a loopback entry and Tailscale down at boot => nothing binds, `start_webui` raises `OSError`, the GUI stays disabled until the bot restarts; always list `127.0.0.1`. The range check also accepts a non-Tailscale address in `100.64.0.0/10` (ISP CGNAT).
- **Peer-address check** (`peer_guard`, the first middleware, every route including static, SSE and 404): the TCP peer (`transport.get_extra_info('peername')`, IPv4-mapped IPv6 unwrapped) must be in loopback (`127.0.0.0/8`, `::1`) or Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), using the same ranges as the bind policy; otherwise `403 {"error":"bad_peer"}` (a missing peername is refused too). It is unconditional. Rationale: Linux uses the weak-host model, so a device on the same Wi-Fi/LAN can add a route to the server's Tailscale IP via the server's LAN address and reach `100.x:8765`, bypassing Tailscale ACLs; the Host allowlist does not help because `Host: 100.x` is allowed. A legitimate tailnet connection always has a tailnet source address; that LAN host arrives with its LAN source address, and spoofing a tailnet source cannot complete the TCP handshake. A reverse proxy or cloudflared on the same machine connects from loopback and works; a proxy on another host must be on the tailnet. Refusals are logged at WARNING at most once per peer per minute, peer IP only. **Recommended belt and braces** (host firewall, e.g. in the runbook): `iptables -I INPUT -d 100.64.0.0/10 ! -i tailscale0 -j DROP` and `ip6tables -I INPUT -d fd7a:115c:a1e0::/48 ! -i tailscale0 -j DROP` (or the nft equivalent). The `bad_peer` check cannot protect against a process on this host itself (accepted limitation below).
- **Host allowlist** (DNS-rebinding defense), on every route including static and `/api/events`: a malicious web page can rebind its own hostname to `127.0.0.1` or the tailnet IP and then talk to the GUI same-origin from the victim's browser, which no CORS or Origin rule would stop; but the browser still sends `Host: evil.example`. Allowed: `localhost`, `127.0.0.1`, `::1`, every configured bind address, plus `WEBUI_ALLOWED_HOSTS` (hostnames, no wildcards, e.g. the MagicDNS name or later the domain). Case-insensitive, port ignored, `[v6]:port` handled. Otherwise `403 {"error":"bad_host"}`; a browser opening a disallowed hostname sees that raw JSON (index.html itself is blocked) - fix by adding the hostname to `WEBUI_ALLOWED_HOSTS`. `WEBUI_ALLOWED_HOSTS` entries must be plain `host[:port]` or `[v6][:port]`; entries containing `://`, `/`, `@`, wildcards, whitespace, unbalanced brackets or stray colons are dropped with a warning (a URL never silently becomes `https`).
- **Origin check on writes**: for non-GET/HEAD, if `Origin` is present its hostname must be allowed, and `Sec-Fetch-Site: cross-site` is refused (`403 bad_origin`) - on writes and also on every `/api/*` GET, so a cross-site no-cors fetch cannot open an SSE subscription. A missing Origin is allowed on purpose: browsers always send Origin on cross-origin writes (and on same-origin non-GET fetches), so absence means a non-browser client such as curl, which is not the CSRF threat and is already inside the network boundary. The JSON-only `Content-Type` plus the required `X-Editor` header (a non-simple header that forces a CORS preflight, which is never granted) are additional CSRF barriers.
- **Accepted limitation**: any local process or account on the server can reach `127.0.0.1:8765`, but without valid credentials in `users.json` cannot view app data or send actions. Run no untrusted local users on this host. Traffic is plain HTTP (Tailscale encrypts the tailnet hop; loopback needs none); a domain deployment must terminate TLS and authenticate at the proxy.

### Speaking as the bot (Messages tab)
Anyone who can reach the GUI can post arbitrary text as the bot in each guild's configured channel. That is the same trust boundary as everything else here (Tailscale ACLs / the zero-trust proxy decide who can reach the port; editor names are self-declared). Mitigations in code: only the configured channel (never a caller-supplied id), user mentions only (no `@everyone`/`@here`/roles, enforced by text checks AND `allowed_mentions`), at most 10 distinct tagged members, every tagged user must be able to see that channel, per-guild rate limit (10 / 60 s, 2 s gap), bounded body. The audit log records editor, guild, channel, character count and mention count, never the message content.

### Web UI hardening
- **CSP** on every response (including 403s): `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` for `/api/*` and `no-cache` for static. The UI uses `textContent`/DOM APIs only (channel names are attacker-influenced); `tests/test_static.py` forbids `innerHTML`, `eval`, inline handlers/styles and external URLs. No cookies.
- **Body limit** 16384 bytes for the whole app (D41). Guild ids and channel ids must be ASCII digits; `is_text_channel` confirms the channel exists in that guild and the bot can view it.
- **Editor attribution** (`X-Editor`) is self-declared: it names who changed what in the UI and logs; it is not authentication. The client percent-encodes it (`encodeURIComponent`) because a raw non-Latin-1 name would make `fetch` throw; the server `unquote`s then sanitizes (strip control chars, collapse whitespace, truncate to 40, reject empty).
- **Never crash the bot**: `maybe_start_webui` wraps address resolution, `make_app` and the binds; any failure (invalid `WEBUI_HOSTS`, bad port, port in use on every address) logs and the bot continues without the GUI. `parse_port` falls back to 8765 on an invalid or out-of-range `WEBUI_PORT`. A guild without a configured channel reads nothing.
- **Client**: a 403 `bad_host` / `bad_origin` / `bad_peer` shows a distinct toast (for `bad_host`: "This hostname is not allowed - add it to WEBUI_ALLOWED_HOSTS on the server") and stops the SSE reconnect loop (no retry storm).

### systemd sandbox (`tts_bot.service`)
`User=ttsbot`, `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict` (everything read-only except `StateDirectory=tts-bot` -> `/var/lib/tts-bot`), `ProtectHome=true`, `ProtectKernelModules`, `ProtectControlGroups`, `RestrictSUIDSGID`.

### Deploy trust boundary (important)
- `scripts/deploy.sh` runs as **root** from a **group-writable checkout**. That is a privilege-escalation surface, so it is defended in depth by `scripts/lib/deploy_validate.sh`: every source file (root `*.py`, `static/*`, `models/*.onnx` and `*.onnx.json`, `requirements.txt`) must be a regular file, not a symlink; its `realpath` must lie inside the physically-resolved repo; `static/` and `models/` must be real directories; no subdirectories. Files are copied into a root-owned `mktemp -d` snapshot (mode 700), the snapshot is re-verified (no symlinks or special files), and everything after that (py_compile, install, stale pruning of `*.py`/`static/`) reads only from the snapshot. `models/` is never pruned (voices from `install_voice.sh` live only on the host). `install_voice.sh` applies the same philosophy: it copies the (dev-writable) source into a root-owned staging dir and validates the copy, never the original. The compiled output goes to the snapshot's own `.pyc` dir so nothing is written into the repo.
- **Accepted residual risk**: a microsecond race between the `realpath` check and the `cp` into the snapshot (the later re-verification catches a swapped-in symlink but not a swapped-in regular file with different content, which is the same as "dev edited the file", i.e. legitimately deployable code).
- `deploy.sh`, `setup_system.sh` and `install_voice.sh` also `source`/execute files from the dev-writable repo as root (`scripts/lib/deploy_validate.sh` is sourced by `deploy.sh` and `install_voice.sh`; `setup_system.sh` installs `tts_bot.service` from the repo and runs `pip install -r requirements.txt` from it; `deploy.sh` copies `*.py`, `static/` and `requirements.txt` into `/opt/tts-bot`). **The admin must review `scripts/` (and the diff in general) before running them.**
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
- **Client detail:** `app.js` uses `fetch` streaming plus a hand-written parser (`createSSEParser`), not `EventSource`, because `EventSource` cannot set custom headers such as `X-Editor`, and `fetch` streaming gives control over reconnect and backoff.
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

### D6. Loopback + Tailscale bind, no bearer token (supersedes the original loopback + bearer token design)
- See D31. The original design (loopback only, bearer token via `LoadCredential`, SSH tunnel) was replaced.

### D7. Self-declared editor name
- **Why:** small, known team; the network boundary is the real gate; names give "who changed this" without building accounts.
- **Cost:** anyone who can reach the GUI can impersonate a name. Attribution only.

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

### D12. (removed) `make_app` token length check
- Superseded by D31: there is no token. `make_app` now requires an explicit `allowed_hosts` argument instead.

### D13. Config writes shielded from handler cancellation
- See section 6. **Cost:** a disconnecting client may leave a completed write the user did not see confirmed (harmless; the SSE event/refetch shows it).

### D14. (removed) Per-IP auth throttle
- Superseded by D31: without credentials there is nothing to brute-force.

### D15. Unconfigured guild reads nothing
- **Why:** fail closed; a bot invited to a new server must not start reading channels. `is_listen_channel`/`is_command_channel` return false without an entry. Slash replies say "No channel configured - set one in the web UI."

### D16. `maybe_start_webui` never crashes the bot
- **Why:** voice TTS is the product; the GUI is optional. Bad port, invalid `WEBUI_HOSTS`, bind failure on every address -> log and run without GUI. Tests: `tests/test_wiring.py`.

### D17. Deploy: validate, snapshot, install from snapshot
- See section 7. **Alternatives rejected:** copying straight from the repo as root (symlink dereference by root could publish any secret file, e.g. a symlinked token, world-readable in `/opt`). **Cost:** more script complexity; the residual microsecond race is accepted.

### D18. (removed) `LoadCredential` for the GUI token
- Superseded by D31. The unit no longer loads `gui_token`; `deploy.sh`/`setup_system.sh` no longer refuse on it.

### D31. Remove the GUI token; secure the GUI at the network layer (loopback + Tailscale), Host allowlist, Origin check
- **Decision:** no application-layer auth. Bind only loopback and Tailscale addresses (fail-closed policy), answer only allowlisted Host names, check Origin on writes.
- **Why:** the user secures access with Tailscale ACLs (and later zero trust in front of a domain); a second shared secret in a password manager added friction and an extra credential file/unit dependency without a stronger boundary than the ACLs already give. Removing the token makes DNS-rebinding and cross-site writes the main browser-borne risks, hence the mandatory peer, Host and Origin checks and a bind policy that cannot be mis-set to `0.0.0.0` by accident. The peer check was added after review because binding a Tailscale IP does not stop LAN hosts on Linux (weak-host model).
- **Alternatives rejected:** keeping the token optional (an optional control silently off is the failure mode; two code paths to test); binding `0.0.0.0` and relying on a firewall (one typo exposes it on the LAN; the bind policy refuses it); keeping the bearer token behind a reverse proxy (proxy auth is the same boundary as ACLs, with extra credential handling).
- **Why Origin is checked only when present:** browsers always send `Origin` on cross-origin writes, so a forged cross-site write is caught; scripts and curl send none and are not the CSRF threat. Requiring it would break legitimate tooling for no gain.
- **Cost:** any local process or account on the server can reach `127.0.0.1:8765` and can change the configured channel, pause/resume, skip and clear the queue, swap the voice, make the bot speak test phrases, and post as the bot (accepted; run no untrusted local users; mentions limited to <=10 distinct members; 10 sends per 60 s plus a 2 s gap). Anyone on the tailnet that ACLs allow can use the GUI. If a domain is added later it must authenticate at the proxy.

### D32. The voice is global, not per guild
- **Decision:** one `voice` in `config.json`, one loaded Piper session for all guilds.
- **Why:** the engine is a single GPU context on a 6 GB card (see section 6). A per-guild voice would mean either several resident sessions (VRAM) or a swap before almost every clip (seconds of latency and constant allocator churn).
- **Alternatives rejected:** per-guild voices with an LRU of sessions (VRAM and complexity for a small team); per-user voices (same, plus a UI).
- **Cost:** every server hears the same voice; changing it affects everyone, including text queued but not yet synthesized.

### D33. Hot swap on the single worker thread: build and warm up the candidate, then swap, then free
- **Decision:** `TTSEngine.reload` runs under the engine lock on `_pool` (the only inference thread). It builds a `PiperVoice`, warm-up synthesizes, and only then swaps the whole `VoiceState` (`voice/sample_rate/model_path/config_path/cuda_active`) in one assignment, refusing a GPU->CPU downgrade, and frees the old session. Failure raises `VoiceLoadError` and leaves the old voice serving.
- **Why:** serializing with synthesis on the one thread means a swap can never corrupt or interleave with an inference, with no extra locking, and the bot keeps its voice if the new model is broken (bad file, CUDA OOM, wrong format).
- **Alternatives rejected:** unload-then-load (a failure leaves the bot mute, and every failed attempt is an outage); a second executor/thread for loading (violates the one-GPU-thread rule, allows concurrent CUDA use); restarting the service (drops voice connections, needs sudo).
- **Cost:** peak GPU memory is two sessions during a swap; the old session is freed immediately after (`del` + `gc.collect()`), but ORT may retain arena memory, so many swaps can ratchet usage up on the 6 GB GTX 1060. A service restart resets it. Synthesis is paused for the seconds a load takes (queued text waits).

### D34. Apply order: validate name, check rev, swap the engine, then save with `expected_rev=None`
- **Decision:** `Controller.apply_voice` (under `_voice_lock`): resolve the name, compare `expected_rev` with `config.rev` (409 before touching the engine), reload the engine, then `config.set_voice(..., expected_rev=None)`. If the save fails with `OSError` the previous model is reloaded (best effort) and the caller gets 500.
- **Why:** the engine swap is slow (seconds) and has side effects, while the config write is fast and can be rolled back. Doing the cheap, side-effect-free checks first means a stale client never costs a model load, and persisting last means a failed load saves nothing.
- **Why `expected_rev=None` on the save:** another editor's channel/pause edit during the load bumps `rev`; passing the original rev would make the save conflict *after* the engine already swapped, leaving engine and config disagreeing with no clean way to tell the client. Stale-client detection already happened in the pre-check.
- **Alternatives rejected:** save first, then swap (a failed load would leave a saved voice that cannot load, retried at every restart); save with the original rev and roll the engine back on conflict (a second multi-second load, more failure modes); holding the config lock across the load (blocks channel edits for seconds).
- **Cost:** a narrow window in which two editors both believe they chose the voice: the later apply wins, and everyone is told via the `config` event. If the revert after a persist failure itself fails, the engine keeps the new voice while the saved config names the old one (visible as configured != current).

### D35. Multi-speaker models are excluded
- **Decision:** `discover_voices` skips models with `num_speakers > 1`.
- **Why:** the engine calls `synthesize_stream_raw(text)` with no speaker id, so a multi-speaker model would always speak as speaker 0 with no way to choose; listing it would be misleading. Supporting it needs a speaker picker, a persisted speaker id and a wire format, which was out of scope.
- **Alternatives rejected:** listing them with a default speaker (silently wrong); a per-voice speaker field now (YAGNI).
- **Cost:** some good Piper voices (e.g. libritts, vctk) are unavailable until speaker selection is built.

### D36. Voices are addressed only by file stem; discovery is a rescan, not a watcher
- **Decision:** the API carries the stem (`[A-Za-z0-9_.+-]{1,100}`, no leading dot). `discover_voices` and `resolve_voice` share ONE validation (`voices._checked_files`, via `_entry`): the `.onnx` and the `.onnx.json` must each be a regular file and not a symlink (`lstat`), and each realpath must equal `<realpath of models dir>/<its name>` (so an entry pointing outside the models dir, or reached through a symlinked directory, is rejected; a symlinked models dir itself is fine). Filesystem work is done once per voice. `resolve_voice` runs that same check for the requested stem and raises `BadVoiceError("unknown voice")` without any path. The folder is rescanned on every `GET /api/voices` (the Rescan button) and after a swap.
- **Why:** paths are rebuilt server-side from a discovered entry, so there is no path traversal surface and no filesystem path ever appears in a response. A rescan on demand is stateless; there is nothing to go stale or to leak between tabs.
- **Alternatives rejected:** passing a path or relative filename (traversal); inotify/polling (extra moving part, half-copied files); caching the list (stale, needs invalidation).
- **Cost:** a scan reads every `*.onnx.json` (a few KB each) on each call; fine for tens of voices. A voice added by `install_voice.sh` is invisible until someone presses Rescan.
- **Loading-state ordering (frontend):** SSE `voice` events own `loading/current/configured/cuda`. `loadVoices` records the SSE event counter before its GET; if the counter moved by the time the reply arrives and the stream is live, the pure `mergeVoiceSummary` keeps those four fields from the current state and only takes the voices list from the GET (otherwise it takes everything from the reply). The `hello` summary is applied as an SSE-owned update.

### D37. Deploy no longer prunes `models/`; voices are installed by `install_voice.sh`
- **Decision:** `deploy.sh` still copies/updates repo models but never deletes from `/opt/tts-bot/models`. `scripts/install_voice.sh` adds a voice without a deploy or restart.
- **Why:** voice files are large, gitignored, and chosen at runtime from the GUI; a deploy that deleted everything not in the repo would silently remove the voice the GUI had saved as active.
- **Alternatives rejected:** keeping the prune (data loss on every deploy); making `/opt/tts-bot/models` writable by the service or devs (breaks the root-owned boundary: ONNX files are parsed by native code); uploading through the GUI (an upload surface on an unauthenticated UI).
- **Cost:** removed or renamed repo models linger in `/opt/tts-bot/models` until an admin deletes them by hand; a repo model with the same name overwrites an installed voice on deploy; the bot cannot delete voices (no write access, by design).

### D38. Messages go only to the configured channel
- **Decision:** `POST /api/guilds/{gid}/send` takes no channel id; the controller reads `config.channel_for(gid)` and requires it to be a `TextChannel` of that guild.
- **Why:** the GUI is unauthenticated; a caller-chosen channel would let anyone on the tailnet speak as the bot anywhere it has access. The configured channel is already the one channel the team agreed the bot uses.
- **Alternatives rejected:** a channel picker in the Messages tab (wider blast radius, needs per-channel permission modeling); DMs.
- **Cost:** to post elsewhere an editor must first switch the listen channel (which also moves the TTS listener).

### D39. User mentions only, validated against channel visibility
- **Decision:** `@everyone`, `@here` and role pings are impossible: role tokens are rejected (`bad_mention`) and `allowed_mentions` is `everyone=False, roles=False, replied_user=False, users=[exactly the validated ids]`. Every tagged id must be a cached guild member for whom `channel.permissions_for(member).view_channel` is true.
- **Why:** mass pings are the main abuse and spam risk of a tool that speaks as the bot; pinging someone who cannot see the channel is useless and surprising. Two layers (text check + `allowed_mentions`) so a bug in either does not ping.
- **Alternatives rejected:** silently stripping role tokens (hides the problem from the editor); trusting Discord to drop pings for people who cannot see the channel (it does, but the GUI should say so instead of pretending).
- **Cost:** pinging a role or everyone needs Discord itself; members missing from the cache (no Members intent data yet) cannot be tagged until chunked.

### D40. Tags are substituted client-side from explicit chips
- **Decision:** the textarea holds readable `@Display Name`; the client records `{id, label}` chips when a member is picked and rewrites exactly those labels (longest first, single pass, not glued to word characters) to `<@id>` at send time. The server only sees `<@id>` tokens.
- **Why:** the server stays a dumb validator of one syntax; typing a name that was never picked can never ping anyone by accident; the preview shows exactly what will be sent.
- **Alternatives rejected:** server-side name resolution (ambiguous names, extra attack surface); writing raw `<@id>` into the textarea (unreadable).
- **Cost:** editing a label by hand un-tags it (the chip greys out as "not in message"); the character counter counts the substituted text, which is longer than the visible names.

### D41. Body limit raised to 16 KB
- **Decision:** `MAX_BODY = 16384` for the whole app (was 4096).
- **Why:** browsers send raw UTF-8 (worst case ~8 KB for 2000 emoji, ~12 KB for escaped control characters), not `\uXXXX` for everything. 2000 characters thus require up to ~12 KB; a per-route limit would need a custom reader for one endpoint.
- **Alternatives rejected:** per-route limit (more code on a security-relevant path); limiting content to fewer chars (Discord allows 2000).
- **Cost:** every write route now accepts bodies up to 16 KB before returning 400/413.

### D42. Auto-deploy file watcher NOT adopted; deploys remain manual
- **Decision:** Reject the auto-deploy watcher (scripts removed). Deploys stay manual: dev edits the repo, reviewer checks it, admin runs `sudo scripts/deploy.sh` after review.
- **Why:** an auto-deploy watcher would watch the group-writable repo and run whatever lands there as root within seconds, with no human review of that specific change. Even if the watcher code itself were reviewed, the deployed payload (`.py` files, scripts) changes without a new review; a developer could push malicious code that a code reviewer missed, and a watcher would deploy it as root automatically. `deploy.sh` sources `scripts/lib/deploy_validate.sh` from the repo and installs the repo's `*.py` and `static/` files (the systemd unit is installed by `scripts/setup_system.sh`), so repo write access gives code execution as the bot with its Discord token. For fast local iteration: devs run `python bot.py` as their own user against a personal dev bot token. For production: review the diff first, then admin runs the deploy.
- **Alternatives rejected:** keeping an optional file watcher (optional-but-unused controls silently fail; two code paths to test); making the watcher go through review (extra complexity, does not solve the "deployed code changes without per-deploy review" problem).
- **Cost:** manual deploys are slower than watched auto-deploy, but the review gate is a feature, not a bug.

### D43. Help text is generated dynamically from the command registry
- **Decision:** `/help` iterates `bot.commands` (sorted by name) and builds the command list from their `.name` and `.description` fields at runtime.
- **Why:** help text is always in sync with the actual commands; there is no separate documentation to drift. A command's description is assigned once in its `@bot.hybrid_command` decorator.
- **Alternatives rejected:** hardcoded command list (must be kept in sync, easy to drift); reading from a static file (same sync problem); a separate `help_command` handler left to discord.py default (harder to customize, usually less informative).
- **Cost:** a small runtime cost (iterating 8 commands) on every `!help` call (negligible); each command must have a useful `.description` or `help` will say "No description" for that command. The "Right Now" block (channel, voice, watched users, status) is generated fresh each call so it always reflects the live state.

### D44. Voices command is read-only; voice switching stays GUI-only
- **Decision:** `/voices` lists installed voices and marks the active one, but never calls `config.set_voice` or `engine.reload`. The GUI (Voice tab) is the only way to switch voices.
- **Why:** voice switching is a system-wide change that affects all guilds; a public command that anyone could invoke is a risk (spam, accidents). The voice swap involves a multi-second GPU load and is better initiated from a UI where the operator can see the loading state and any errors. The GUI is reachable only over loopback/Tailscale (see section 7, trust boundaries), which keeps voice changes behind that network boundary.
- **Alternatives rejected:** a public `/setvoice` command (spam risk, system-wide impact); rate-limiting on the voices command (still allows spam); requiring a manager role (still allows accidents from legitimate managers). Voice is global per system, not per-guild, so a per-guild permission model would not help.
- **Cost:** users cannot swap voices from Discord chat; they must reach the web UI. This is intentional to keep voice changes deliberate and auditable.

### D45. Web UI session authentication with users.json
- **Decision:** Application-layer authentication is provided via a login form backed by `users.json` (or `USERS_FILE`). Successful authentication issues an in-memory session token stored in an `HttpOnly`, `SameSite=Lax` cookie (`tts_session`, 7-day lifetime) or supported via `Authorization: Bearer <token>`.
- **Why:** Network-layer boundaries (loopback / Tailscale ACLs) restrict which network routes reach the port, but do not differentiate between individual testers or authorized human operators. A credentials file allows operators to control exactly who views and interacts with the app, with dynamic reloads on file modification so credentials can be updated without restarting the bot. A GUI user-management interface can later build directly on this credential store.
- **Alternatives rejected:** Static HTTP Basic Auth (prompts natively without custom styled UI, credentials sent on every request, no logout support); bearer token in URL query parameter (leaked in access logs and referrer headers); database or heavy ORM (violates stdlib dependency rule).
- **Cost:** Browsers must complete login before viewing app state; failed attempts are rate-limited per IP (5 failures per 60 seconds).

---

## 9. Nuances and gotchas

- **Dual onnxruntime install.** `piper-tts` pulls CPU `onnxruntime`; it must be uninstalled and `onnxruntime-gpu` reinstalled. Symptom of getting it wrong: log line "CUDAExecutionProvider unavailable ... using CPU" and slow synthesis.
- **conda ToS under sudo.** Without `--override-channels -c conda-forge`, root conda prompts for/needs a Terms-of-Service acceptance and ignores the invoking user's config.
- **`sudo` resets the environment.** `CONDA`, `TEAM_GROUP` must be passed as `sudo CONDA=... scripts/setup_system.sh`; `~` and `$HOME` under sudo may not be the dev's. The default `CONDA` in `setup_system.sh` is a host-specific path.
- **Message Content intent.** Must be enabled in the Developer Portal as well as in code, or `message.content` is empty and nothing is ever spoken.
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
- **Reverse proxy and the Host allowlist.** The proxy must pass `Host` unchanged, or `WEBUI_ALLOWED_HOSTS` must contain what it sends; otherwise every request gets `403 bad_host`. Run the proxy on this machine (loopback) or put its host on the tailnet (`bad_peer` otherwise) and turn response buffering off for `/api/events`.
- **`EnvironmentFile=-/etc/tts-bot/bot.env` is optional** (leading `-`): a fresh install starts without it. It only carries a first-boot `TTS_CHANNEL_ID` seed. No script creates it; the unit comment shows how.
- **MagicDNS name gives `bad_host`.** Opening `http://box.tailnet.ts.net:8765` needs that name in `WEBUI_ALLOWED_HOSTS`; the raw Tailscale IP works without it. Tailscale not up at boot is fine in auto mode: detection and the bind are retried every 15 s (look for `web UI now listening`); explicit `WEBUI_HOSTS` retries only the listed addresses.
- **Upgrading: `deploy.sh` alone ships new code with the OLD unit.** The old unit still requires the `gui_token` credential file (and sets the removed `WEBUI_HOST`); re-run `setup_system.sh` (or copy the unit and `daemon-reload`) first, then deploy. Delete the unused `/etc/tts-bot/gui_token` with `sudo shred -u`.
- **Config rollback vs disk.** `_mutate` catches `BaseException`: if a caller is cancelled while the thread-offloaded write is in flight, memory rolls back while the write may still land (disk ahead of memory). The web layer's `asyncio.shield` closes this for HTTP callers; `seed_channel` in `on_ready` is not shielded (acceptable; runs once).
- **Persist errors surface differently.** Config: the HTTP call fails (500) and state is rolled back. Watch list: logged, memory keeps the change.
- **Editor-name comparison.** The "X switched to ..." toast is suppressed when the event's `updated_by` equals the local `normalizeEditor(editor)`; two people using the same name will not see each other's toasts.
- **`known_guild` calls `controller.guilds_view()`** (all guilds' channels) on every write request; fine for a handful of guilds.
- **Static files are read at app creation.** Editing `static/*` requires a bot restart (deploy restarts it anyway).
- **Tests import `bot.py`**, which runs side effects (token lookup, opus load, `Bot` construction). Tests import it through `tests/botimport.py`, which sets the env vars first and patches `dotenv.load_dotenv` out for the import, so the repo `.env` (and a developer's real token) never reaches the test process. Import `botimport` before `bot`.
- **Voice tab quirks.** Selecting a row only highlights it; nothing changes until Apply. A swap takes a few seconds (model load + warm-up) and disables Apply in every open tab through the SSE `voice` events. If `configured` (saved) differs from `current` (loaded) the tab warns: "Saved voice missing" (file gone) or "could not be loaded". "Reset to default" saves `voice: null` and loads `PIPER_MODEL_PATH`. The test phrase needs the bot in voice in the selected guild (it is queued like any message, so it waits behind queued text).
- **Sample rate differs between voices.** Each WAV carries its own header rate and ffmpeg resamples to Discord's 48 kHz, so mixing 16/22.05 kHz voices is fine.
- **Persisted voice vs env default.** `PIPER_MODEL_PATH` is only the fallback. After the first GUI change the saved voice wins at startup; editing the unit's `PIPER_MODEL_PATH` alone will not change the voice until "Reset to default".
- **Stale state in `__pycache__`.** `deploy.sh` removes `/opt/tts-bot/__pycache__`; the repo is compiled to a throwaway dir during validation.

---

## 10. Operations runbook

### Fresh install (order matters)
```
sudo scripts/setup_system.sh             # 1) user, /opt/tts-bot, conda env, sudoers, unit
sudo scripts/install_token.sh --system   # 2) prompts for the Discord token (hidden)
sudo scripts/deploy.sh                   # 3) validate, snapshot, install, restart
sudo systemctl enable tts_bot            # 4)
```
Optionally create `/etc/tts-bot/bot.env` (can hold `TTS_CHANNEL_ID=<id>` for first-boot seeding; the unit starts fine without it). Put the Piper voice (`.onnx` + `.onnx.json`) in `models/` first; `deploy.sh` refuses when no model files exist. Review `scripts/` before running any of this as root (section 7).

### Upgrade an existing deployment
The GUI token is gone. Re-run `setup_system.sh` (installs the new unit; or copy `tts_bot.service` to `/etc/systemd/system/` and `sudo systemctl daemon-reload`), then `deploy.sh`, then delete the now-unused file: `sudo shred -u /etc/tts-bot/gui_token` (deleting it earlier would stop the OLD unit from restarting).

### Deploy a code change
`git pull`/edit in the repo, review, run tests, then `sudo scripts/deploy.sh` (restarts the service; the bot leaves voice, so re-run `!join`).

### Rotate the Discord token
Developer Portal -> Bot -> Reset Token, then `sudo scripts/install_token.sh --system`, then `sudo systemctl restart tts_bot`. A leaked token: reset it first.

### Reach the GUI
- Local / on the server: `http://127.0.0.1:8765`.
- Over Tailscale: `http://<tailscale-ip>:8765`, or `http://<machine>.<tailnet>.ts.net:8765` after setting `WEBUI_ALLOWED_HOSTS=<machine>.<tailnet>.ts.net` in the unit (uncomment the line, `systemctl daemon-reload`, restart). Restrict who may connect with Tailscale ACLs.
- Later via a domain behind zero trust: add the domain to `WEBUI_ALLOWED_HOSTS`; keep the reverse proxy on loopback/tailnet; it must pass `Host` unchanged (or the entry must match what it sends) and must not buffer `/api/events`.

### Add or change a voice (no restart)
1. Download a voice (both files) from https://huggingface.co/rhasspy/piper-voices (samples: https://rhasspy.github.io/piper-samples; check the licence on each model card). Single-speaker only.
2. On the server: `sudo scripts/install_voice.sh /path/to/en_US-amy-medium.onnx` (JSON next to it, or pass it as the second argument; `--force` replaces an installed voice). Alternatively put both files in the repo's `models/` and run `deploy.sh` (restarts the service).
3. In the GUI: Voice tab -> Rescan, select the voice, Apply (a few seconds), try "Say it". "Reset to default" returns to `PIPER_MODEL_PATH`.
If it will not load (422 / toast), the previous voice is still active; the journal has the reason (`voice reload failed`). After many swaps, `sudo systemctl restart tts_bot` clears accumulated GPU memory.

### Change the listen channel (no restart)
Open the GUI (see above), enter your name, search, click/Enter. Takes effect immediately; the guild's pending queue is cleared. Keyboard: `/` search, Up/Down, Enter, Esc. Use Pause/Resume to mute reading while testing.

### Firewall belt and braces (recommended)
The app refuses non-loopback/non-Tailscale peers itself (`bad_peer`), but also drop such traffic at the host so the port is not even reachable from the LAN:
```
sudo iptables  -I INPUT -d 100.64.0.0/10        ! -i tailscale0 -j DROP
sudo ip6tables -I INPUT -d fd7a:115c:a1e0::/48  ! -i tailscale0 -j DROP
```
(or the nftables equivalent; persist them with your distro's mechanism). These rules do not affect loopback traffic to `127.0.0.1`.

### Logs
`journalctl -u tts_bot -f` (devs are in `systemd-journal` after re-login). Useful lines: `Logged in as ...`, `Piper ready (cuda=True, ...)`, `web UI listening on ...`, `web UI now listening ...` (retried address came up), `Web UI disabled: ...`, `queue full`, `worker error (continuing)`, `config watcher error`.

### Common failures
| Symptom | Likely cause | Fix |
|---|---|---|
| Unit fails to start immediately | stale old unit still loading `/etc/tts-bot/gui_token` (file deleted), or `/etc/tts-bot/token` missing (`bot.env` is optional) | re-run `setup_system.sh` (new unit); create the Discord token file |
| "Web UI disabled: bind address ... is not loopback or a Tailscale address" | invalid `WEBUI_HOSTS` | fix or unset `WEBUI_HOSTS` (policy: loopback / Tailscale only) |
| GUI works locally, not over Tailscale | Tailscale address not detected/up, or ACL blocks port 8765 | check `tailscale ip`, logs (`no Tailscale address detected`), ACLs; set `WEBUI_HOSTS` explicitly |
| GUI keeps "Reconnecting" behind a proxy | response buffering | `proxy_buffering off` for `/api/events` |
| `403 bad_peer` in logs/browser | connection source is not loopback/Tailscale (LAN host, or a reverse proxy on another host not on the tailnet) | put the proxy on this machine or the tailnet; LAN access is intentionally refused |
| `403 bad_host` / toast "hostname is not allowed" | Host name not on the allowlist (MagicDNS name, domain, proxy rewriting Host) | add it to `WEBUI_ALLOWED_HOSTS`; make the proxy pass Host unchanged |
| Bot silent, no errors | guild has no channel / paused / user not watched / not joined / Message Content intent off | check GUI, `!status`, Portal intents |
| "CUDAExecutionProvider unavailable" | CPU `onnxruntime` installed over GPU build, or CUDA libs not on `LD_LIBRARY_PATH` | section 9 "Dual onnxruntime install" and "GPU notes" (README) |
| "Token malformed" at start | quotes/prefix/newline in token file | re-run `install_token.sh` |
| GUI shows new code missing / no GUI after upgrade | old unit installed | upgrade order above |
| `deploy.sh` "Refusing to deploy" | symlink, subdir, or file escaping the repo | remove it; it is intentional |
| Clips not played, `libopus not loaded` warning | opus missing from env | conda-forge `libopus` in the env |

---

## 11. Testing

Run from the repo root:
```
~/miniconda3/envs/tts-bot/bin/python -m unittest discover -s tests -v   # 379 tests
bash tests/test_deploy_validation.sh                                    # deploy validation, no root needed
```
`unittest` only; no pytest.

| File | Covers |
|---|---|
| `tests/test_config_store.py` | atomic write (no `.tmp` leftovers on success/failure), EventHub (fan-out, idempotent unsubscribe, drop-oldest), ConfigStore (also `set_voice`: rev/no-op/conflict/rollback/round-trip/old-file compatibility; rev/no-op, conflict leaves state untouched, `expected_rev=None`, round-trip, corrupt file quarantined to `.corrupt`, persist-failure rollback, truth table of `is_*`, event publishing, seed, 20 concurrent writes). |
| `tests/test_webui.py` | no auth needed on any route and a stray `Authorization` header ignored, persist failure -> JSON 500 `persist_failed`, static headers/CSP, PUT channel/enabled incl. persistence and hub events, conflict, bad inputs, editor required/sanitized/percent-encoded, skip/clear, handler cancellation mid-write stays consistent, SSE (hello, config, status only on change, keepalive, subscriber cleanup), `validate_bind_hosts` table, Tailscale output parsers and detection fallbacks, Host allowlist (ports, IPv6, case, missing Host, static/state/events, `allowed_hosts` extras), Origin / `Sec-Fetch-Site` rules, `start_webui` (policy, partial bind + retry, all-fail `OSError`, cleanup cancels retry quickly with an open SSE). |
| `tests/test_auth.py` | `AuthManager` with JSON credential reloading, session creation, expiration, revocation, cookie flags (`HttpOnly`, `SameSite=Lax`), IP brute-force rate limit (5 attempts per minute), public vs protected routes (`/`, `/app.js`, `/app.css`, `/api/session`, `/api/login`, `/api/logout`). |
| `tests/test_admin.py` | Admin unit tests and endpoints: `is_admin`, group checks, list users, add/update user with groups, delete user (cannot delete self, cannot delete admin), permissions enforcement (`403 Forbidden` for non-admins). |
| `tests/test_features.py` | `FeatureStore` persistence to `features.json`, atomic writes, input validation (name and description lengths), `GET /api/features`, `POST /api/features` (auto-records authenticated author), `DELETE /api/features/{fid}` permission enforcement (admin only, 403 for non-admins). |
| `tests/test_voices.py` | `discover_voices` (fields, sorting, quality/language fallbacks, missing/bad/oversized JSON, bad keys, multi-speaker, bad stems, non-files) and `resolve_voice` (traversal, separators, absolute paths, unknown names, non-strings), plus real-symlink tests (symlinked `.onnx` alone, symlinked `.onnx.json` alone, entries pointing outside the models dir, in-dir aliases, symlinked models dir) checked through both `discover_voices` and `resolve_voice`. |
| `tests/test_engine.py` | `TTSEngine` with a fake `PiperVoice`: load (CUDA, CPU fallback, missing file), `VoiceState` atomicity (all fields change together via `engine.state` and the legacy properties; failed `load`/`reload` and a refused GPU->CPU swap leave the snapshot untouched), `reload` (swap + old voice freed via weakref, failure/warm-up failure keep the old voice and give a path-free message, never-loaded engine, CPU fallback, `loading` flag, synth vs reload ordering both ways, cancelled caller, single worker thread), `clean_plain_text`. |
| `tests/test_voice_apply.py` | `Controller.list_voices/apply_voice/say` and `load_engine` with a fake engine: event order, conflict before engine, bad voice, load failure saves nothing, persist failure reverts the engine, failed revert, lock serialization, saved voice at startup (single load, missing -> one WARNING, unloadable -> fallback, traversal in config). |
| `tests/test_cuda_exposure.py` | the real `Controller.voice_summary`/`list_voices` backed by the real `TTSEngine` with a fake Piper: `cuda` true, false after startup CPU fallback, null before ready, preserved (and in the `voice` event) after a refused swap. |
| `tests/test_voice_routes.py` | `GET /api/voices`, `PUT /api/voice` (200/400/409/422/500 bodies carry no paths, shield on disconnect), `/say` validation and refusals, `hello` voices summary, SSE `voice` events, `wire_event` null guild, all guards (bad host/origin/peer, missing `X-Editor`) on the new routes. |
| `tests/test_messages.py` | `parse_mentions` (dedupe, role rejection, literal everyone) and `SendLimiter` (2 s gap, 10 per rolling minute, injectable clock, int `retry_after`, per-guild, rejected attempts not recorded). |
| `tests/test_message_controller.py` | `Controller.send_message` (exact `allowed_mentions`, content limits, role/hidden/unknown user, no channel, Forbidden, HTTPException, rate limit, reservation before the await, audit line without content) and `members_for` (filter, sort, 25 cap + truncated, visibility, chunk once). |
| `tests/test_message_routes.py` | `GET .../members` and `POST .../send` with a fake controller: bodies for every error code, string ids, X-Editor required, 16 KB body limit (2000 non-ASCII chars accepted, oversize 413), disconnect does not cancel or double-send, bad peer/host/origin/cross-site. |
| `tests/test_controller.py` | `Controller` views and actions with fake guilds, `config_watcher` drain behavior (channel change drains; `enabled` change does not; other guilds and malformed events are survived). |
| `tests/test_bot_events.py` | hot-swap path with fake objects: `on_message` (configured channel queues; paused, old channel after a swap, unconfigured guild, unwatched, bots, DMs do not; prefix goes to commands only), `only_tts_channel`, `on_command_error` slash reply, `on_ready` seeding (lowest matching id, skips existing, idempotent) and its `channels` event, channel-relay listeners, no `.env` loaded. |
| `tests/test_help_command.py` | `help_cmd`: every registered command and description listed, listen channel set/unset, voice connected/stale/not connected, engine loaded (with CPU marker)/not loaded, idle/reading/paused, watched-user list capped at 15 with "+N more" (and the 2000-char backstop not what made it fit), 2000-char backstop, hostile display names (`@everyone`, markdown, unicode), `allowed_mentions` is `AllowedMentions.none()`. |
| `tests/test_watch_autojoin.py` | `watch` auto-join (joins when not connected, stale disconnected client counts as not connected, no join when connected, join failure reports the helper's message and still watches, author not in voice gets the `!join` hint, hostile name, `allowed_mentions` none) and `join_author_voice` (connect, `ensure_audio`, engine-not-loaded note, `move_to`, not-in-voice, Forbidden, ClientException). |
| `tests/test_voices_command.py` | `voices`: defer before the rescan, listing format, active/CPU/saved-not-loaded markers, 25-voice slice with "+N more", 2000-char backstop, engine not loaded (with and without voices), loading, `list_voices` failure gives a friendly message without exception text, read-only (no `set_voice`/`reload`/`apply_voice`), `allowed_mentions` none. |
| `tests/test_wiring.py` | `maybe_start_webui` never raises (invalid `WEBUI_HOSTS`, bad `make_app`, out-of-range port, port in use, unexpected error), default hosts via Tailscale detection, `WEBUI_ALLOWED_HOSTS` reaches the app, `parse_port`, good config serves and cleans up. |
| `tests/test_static.py` | static payload < 60 KB (61440 bytes), CSP-safe content (no `innerHTML`/`eval`/inline handlers/external URLs), served bytes equal files, and runs `tests/static_logic.js` under node if available. |
| `tests/static_logic.js` | pure JS helpers: `substituteTags`/`segmentText` (longest-first, overlaps, word boundaries), `activeTagIds`, `uniqueLabel`, `insertAtCursor`, `counterText`/`counterState`, `pingTargets`, `pingSummary`, `chipText`, `messageBlocker`, `voiceTitle`, `voiceWarning`, `sayBlocker`, `voiceSwitchText`, `mergeVoiceSummary` (stale GET vs newer SSE event, fresh, not live, equal seq), `tokenizeMentions` (mention token regex), `channelChanged` (prev/next config, same gid?), `resetMemberCache`, `shouldApplyMembers`, SSE parser, `matchChannel`, `relTime`, `backoffDelay`, `groupChannels`, `normalizeEditor`, `shouldRollback`, `shouldApplySnapshot` (stale snapshots ignored). |
| `tests/test_deploy_validation.sh` | deploy validator: clean tree accepted; symlinked dirs/files, in-repo symlinks, subdirectories, missing `.py` rejected; `validate_voice_files` (stem rules incl. non-ASCII, symlinks, size, text-as-model, JSON/sample_rate/multi-speaker); static check that `deploy.sh` does not prune `models/`. |

**Not covered:** anything needing live Discord, a GPU, ffmpeg/voice, or a browser. `engine.synthesize`/playback/`audio_worker`/`on_message` end to end, the real systemd unit, `setup_system.sh`/`deploy.sh` as root, and DOM behavior of `app.js` (rendering, optimistic rollback wiring, keyboard handling) have no automated tests; the same holds for the Voice tab, real Piper/CUDA voice loading, GPU memory growth over repeated swaps, and `install_voice.sh` as root (only its validation function and a sandboxed copy were run). The frontend was verified by node logic tests, static content checks and code review only. Run the manual smoke checklist in a real browser (and a real Discord test guild) before relying on it: name prompt, `bad_host` toast, channel list/search/keyboard, select + toast + undo, pause/resume, skip/clear, two browsers (conflict toast, live toast), kill/restart the bot and watch reconnect, 360 px width, dark mode.

---

## 12. Known limitations

- No live Discord, GPU or browser tests. UI is verified by node pure-helper tests (`tests/static_logic.js`), static checks, and code review. Run the manual smoke checklist in a browser before relying on it for production.
- Editor names are self-declared attribution, not authentication; there is no application-layer auth at all. Any local process/account on the server can reach the loopback GUI and can change the configured channel, pause/resume, skip and clear the queue, swap the voice, make the bot speak test phrases, and post as the bot (accepted; mentions limited to <=10 distinct members; 10 sends per 60 s plus 2 s gap); the tailnet side is governed by Tailscale ACLs.
- Messages tab: text only (no embeds, attachments, replies, edits or deletes); sends go to the configured listen channel only. The 10/60 s and 2 s limits are in-memory per process (reset on restart) and per guild, not per editor. Members only appear if they are in the member cache (Server Members intent; first use of a big guild triggers one `chunk()`, up to 10 s). Tags are plain `@Name` text until send; a name edited by hand loses its tag. DOM behavior of the tab is untested (no browser).
- One channel per guild (plus pause). One global voice (D32); multi-speaker voices are not offered (D35).
- Voice swaps: peak GPU memory is two sessions while loading, and ORT may keep memory after freeing the old one, so use a service restart if memory creeps up on the 6 GB card. Speech is delayed for the seconds a load takes. Two editors applying different voices at once: last apply wins (D34). Installed voices cannot be deleted from the GUI.
- Deploys are manual (D42): dev reviews code, admin runs `sudo scripts/deploy.sh`. No auto-deploy watcher (rejected: it would watch the filesystem and run unreviewed code as root).
- Deploy copies every root `*.py` (stray files ship) plus `static/*`, `models/*` (copy only, never pruned), `requirements.txt`; `deploy.sh` and `setup_system.sh` run repo-supplied scripts as root (admin review required); a microsecond check/copy race is accepted; `setup_system.sh` resolves `SRC` with `pwd`, not `pwd -P`.
- The GUI is down whenever the bot is down; all plain HTTP (Tailscale encrypts the tailnet hop; a domain needs TLS at the proxy).
- The bot does not rejoin voice after a restart.
- `config.json` and `state.json` corruption: starts empty; only `config.json` is kept aside as `.corrupt` (one generation).
- Minor, deferred review items: first-set-on-unknown-guild bumps rev; `RecursionError` on pathologically nested JSON gives 500; 404 bodies are not JSON; `on_guild_channel_update` is chatty; shutdown cancels `_watcher_task` without awaiting it; DM `NoPrivateMessage` is unreachable on the prefix path; UI polish deferred (render skip button smoothly, `scrollIntoView`, `aria-live` only-on-change); members scan sorts by the whole guild (not paginated by role or online status).

---

## 13. Asking an LLM about this repo

Give it first, in this order: `CLAUDE.md`, this file, then as needed `bot.py`, `config_store.py`, `webui.py`, `tts_engine.py`, `tts_bot.service`, `scripts/deploy.sh` + `scripts/lib/deploy_validate.sh`, `static/app.js`. For rationale history: `docs/superpowers/plans/2026-10-13-channel-gui.md` and `git log --oneline`.

Example questions it should be able to answer from these files:
- Why is the GUI inside the bot process and what happens to it when the bot stops?
- What happens to queued messages when someone changes the channel? To the clip currently playing?
- Two people click different channels at the same time: what does each see?
- Why does the browser not use `EventSource`?
- Why is there no GUI login, and what stops a web page from driving the GUI (Host allowlist, Origin check)?
- Why do deploys run from a snapshot, and who can read the Discord token?
- Why is there only one Piper worker thread?
- Why must `setup_system.sh` be re-run before `deploy.sh` when upgrading?
- What does pause do and not do?
- How do I reach the GUI over Tailscale or a domain? How do I rotate the Discord token?
- Why does `TTS_CHANNEL_ID` stop having any effect after first boot?
- What happens if Tailscale is not up when the bot starts?
- What is not tested?
