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
| `bot.py` | Entry point. Discord events and commands, per-guild queue/worker, WatchStore, Controller, config watcher, lifecycle (startup, signals, shutdown). | Run `python bot.py`. Module-level: `config` (ConfigStore), `store` (WatchStore), `engine` (TTSEngine), `audio` (dict guild_id -> GuildAudio), `controller` (Controller), `load_token()`, `parse_port()`, `maybe_start_webui(cfg, ctl, port)`, `shutdown()`, `main()`. |
| `tts_engine.py` | Piper wrapper (CUDA with CPU fallback) and Discord-text normalizer. | `TTSEngine(model_path, config_path)`: `load() -> bool` (never raises), `async synthesize(text) -> path\|None`, `purge_tmp()`, `close()`, attrs `ready`, `cuda_active`. `clean_discord_text(message, max_chars=300) -> str\|None`. |
| `config_store.py` | Runtime config with revision counter, atomic persistence, pub/sub. | `atomic_write_json(data, path)`, `ConflictError(snapshot)`, `EventHub` (`subscribe/unsubscribe/publish/subscriber_count`), `ConfigStore` (`snapshot`, `channel_for`, `is_enabled`, `is_listen_channel`, `is_command_channel`, `async set_channel`, `async set_enabled`, `async seed_channel`, `.rev`, `.hub`). |
| `webui.py` | aiohttp backend: bind-address policy (`validate_bind_hosts`, Tailscale detection), Host allowlist + Origin check, REST, SSE, static serving. No token auth. Duck-typed on the controller; never imports `bot.py`. | `validate_bind_hosts(hosts)`, `detect_tailscale_addrs()`, `parse_tailscale_ip()`, `parse_ip_addr()`, `parse_allowed_hosts()`, `host_of()`, `make_app(config, controller, *, allowed_hosts, status_interval=3.0, keepalive_interval=15.0, static_dir=None)`, `async start_webui(app, hosts, port, *, retry_interval=15.0) -> WebUIRunner` (binds each address independently, retries failures every 15 s, raises `OSError` if none bind), `sanitize_editor()`, `wire_snapshot()`, `wire_event()`. |
| `static/index.html`, `app.js`, `app.css` | Vanilla-JS single-page UI. No framework, no CDN, no inline script/style (CSP). `app.js` also exports pure helpers under node for tests. | Served at `/`, `/app.js`, `/app.css`. |
| `tts_bot.service` | systemd unit: user `ttsbot`, Discord-token credential, env, sandboxing, ordered after `tailscaled`. | Installed to `/etc/systemd/system/` by `setup_system.sh`. |
| `scripts/setup_system.sh` | One-time root setup: service user, `/opt/tts-bot`, private conda env, sudoers rule, journal group, unit install. | `sudo scripts/setup_system.sh` (env: `CONDA`, `TEAM_GROUP`). |
| `scripts/deploy.sh` | Admin deploy: validate, snapshot, compile, install, prune, restart. | `sudo scripts/deploy.sh`. |
| `scripts/lib/deploy_validate.sh` | Sourced function `snapshot_sources SRC SNAP` (validation + snapshot). | Tested by `tests/test_deploy_validation.sh`. |
| `scripts/install_token.sh` | Hidden prompt, shape-checks and stores the Discord token (mode 600). | `[--system]`; `TOKEN_DIR` env override (undocumented in README). |
| `tests/` | See section 11. | `python -m unittest discover -s tests` |
| `requirements.txt` | `discord.py[voice]>=2.4`, `PyNaCl>=1.5`, `piper-tts==1.2.0`, `onnxruntime-gpu>=1.18,<1.21`, `python-dotenv>=1.0`. | |
| `.env.example` | Non-secret dev settings template. | |

### Web API (all under `webui.py`)

| Route | Guard | Purpose |
|---|---|---|
| `GET /`, `/app.js`, `/app.css` | Host allowlist (no secrets in them) | Three explicit routes; files read once at app creation. No directory serving, so no path traversal. |
| `GET /api/state` | Host | `{"config": snapshot, "guilds": guilds_view}` |
| `PUT /api/guilds/{gid}/channel` | Host/Origin + `X-Editor` | Body `{"channel_id": "<digits>"\|null, "rev": int\|null}`. 400 `bad_channel`, 409 `conflict` (with snapshot), 200 `{"snapshot"}`. |
| `PUT /api/guilds/{gid}/enabled` | Host/Origin + `X-Editor` | Body `{"enabled": bool, "rev": int\|null}`. |
| `POST /api/guilds/{gid}/skip` | Host/Origin + `X-Editor` | `{"skipped": bool}` |
| `POST /api/guilds/{gid}/clear` | Host/Origin + `X-Editor` | `{"cleared": int}` |
| `GET /api/events` | Host | SSE: `hello` (snapshot+guilds) on connect, then `config`, `channels`, `status` (only when changed), `: keepalive` comment every 15 s. |

Error codes: 403 `bad_peer` (TCP peer not loopback/Tailscale, any route), 403 `bad_host` (Host not on the allowlist, any route) / `bad_origin` (write with a foreign `Origin` or `Sec-Fetch-Site: cross-site`), 500 `persist_failed` (config write raised `OSError`; no detail leaked), 400 `editor_required` / `bad_request` / `bad_channel`, 404 (unknown or non-digit guild id; body is not JSON), 409 `conflict`, 413 for bodies > 4096 bytes (aiohttp `client_max_size`). Bad JSON, non-object body, or a `rev` that is not an int (bools rejected) -> 400 `bad_request`.

All IDs on the wire are strings (JS numbers lose precision above 2^53). `wire_snapshot`/`wire_event` convert `channel_id` and `change.guild_id/old/new`; internally the store uses ints.

### Event shapes
- Hub `config` event (internal, ints): `{"type":"config","snapshot":{...},"change":{"guild_id":int,"field":"channel_id"|"enabled","old":..,"new":..}}`.
- Hub `channels` event: `{"type":"channels","guild_id":"<str>"|null}` published by `bot.py` on channel create/delete/update, guild join/remove/available, and (with `guild_id: null`) at the end of every `on_ready`, so a tab that connected during the restart window (empty guild cache) refetches. The browser reacts by refetching `/api/state`, coalesced (~250 ms trailing, one request in flight).
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

---

## 5. State and persistence

| Item | Where | Format / semantics | Written by |
|---|---|---|---|
| Listen channel + pause flag | `config.json` (`CONFIG_FILE`; default: next to `STATE_FILE`; `/var/lib/tts-bot/config.json` in production) | The snapshot dict: `rev`, `updated_by`, `updated_at` (epoch float), `guilds{gid: {channel_id, enabled}}`. Pretty-printed JSON. | `ConfigStore._mutate` |
| Watch list | `state.json` (`STATE_FILE`; default next to `bot.py`; `/var/lib/tts-bot/state.json` in production) | `{"watched": {"<gid>": [user ids]}}` | `WatchStore.mutate` |
| Queues, workers, voice connections, playing clip | memory | `GuildAudio` per guild | |
| EventHub subscribers, SSE tasks, bind-retry task | memory | | |
| Temp `.wav` clips | `<tempdir>/tts_bot` (`/tmp/tts_bot`; under `PrivateTmp` in the service) | deleted by `after`; `purge_tmp()` at start and in `engine.close()` | |

**`rev` semantics.** Starts at 0 for a fresh store; +1 for every effective change (channel set to a different value, enabled flipped, or first seed of a guild); persisted, so it survives restarts. No-op writes (same value on an existing guild entry) return the current snapshot without bumping rev or publishing. Quirk: the first `set_*` on a guild with no entry always counts as a change, even when the value equals the default (`enabled=True`, `channel_id=None`), so it bumps rev. `expected_rev=None` bypasses conflict detection.

**Atomic writes.** `atomic_write_json` creates a temp file in the same directory (`.cfg-*.tmp`), writes, flushes, `fsync`s, then `os.replace`s over the target; on any failure the temp file is unlinked and the error re-raised. A crash leaves either the old or the new file, never a torn one. The same function backs `state.json` (the older private `save_state` was removed).

**Load behavior.** Both stores tolerate a missing file (start empty) and a corrupt file (log the exception, start empty). `ConfigStore` first renames an unreadable `config.json` to `config.json.corrupt` (best-effort, replaces an older `.corrupt`, never raises) so the next write cannot destroy it; `WatchStore` still overwrites a corrupt `state.json` with no backup. `ConfigStore` never raises from its constructor.

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
| Admin (real sudo) | run `setup_system.sh`, `deploy.sh`, `install_token.sh --system`; read the Discord token | |
| Dev (group `devteam`) | edit the repo; `sudo systemctl start\|stop\|restart tts_bot` (exact commands, no wildcards); `systemctl status`, `journalctl -u tts_bot` (via `systemd-journal` group); use the GUI if Tailscale ACLs let them reach it | read `/etc/tts-bot/*`; modify `/opt/tts-bot`; use any other sudo command |
| Service (`ttsbot` user) | read `/opt/tts-bot` (root-owned), write `/var/lib/tts-bot`, `/tmp` (private) | write code, see home dirs, gain privileges |
| GUI user (anyone who can reach the port: loopback, or tailnet per ACLs) | change channel, pause, skip, clear for any guild the bot is in | read the Discord token; read/write anything on the host |

### Token locations and flow (names only)
- **Discord token**: production `/etc/tts-bot/token` (root:root 600, dir `/etc/tts-bot` 700) -> systemd `LoadCredential=discord_token:/etc/tts-bot/token` -> process sees `$CREDENTIALS_DIRECTORY/discord_token`. Dev: `~/.config/tts-bot/token` (mode 600), override `DISCORD_TOKEN_FILE`. Lookup order in `load_token()`: credential, file, then `DISCORD_TOKEN` env (last resort). Shape-validated against `TOKEN_RE` at import; on mismatch the process exits with a message containing only the length.
- There is **no GUI token**: the GUI is protected by the network layer (below). The browser keeps only the editor name, in `localStorage`.
- `.env`, `~/.config/tts-bot/`, and `/etc/tts-bot/` are off-limits for agents and must never be committed (`.gitignore` covers `.env`).

### Web UI trust boundary (network layer, no application auth)
**The trust boundary is who can reach the port.** The GUI listens only on loopback and this host's Tailscale address(es); access control is Tailscale ACLs (and, later, a zero-trust proxy in front of a domain). Anyone who can reach it can change the bot's channel, pause, skip and clear. Because there is no token any more, four cheap application-layer defenses are mandatory:
- **Fail-closed bind policy** (`validate_bind_hosts`): every bind address must be an IP literal in `127.0.0.0/8`, `::1`, Tailscale CGNAT `100.64.0.0/10` or Tailscale ULA `fd7a:115c:a1e0::/48` (`localhost` maps to `127.0.0.1`). Anything else (`0.0.0.0`, `::`, LAN addresses, hostnames) raises `ValueError` and `maybe_start_webui` disables the GUI (logged), so a typo cannot make the app listen on a LAN address or the wildcard. `start_webui` re-validates. This alone does NOT keep LAN hosts out (see the peer check). Default (auto mode): `127.0.0.1` plus auto-detected Tailscale addresses (`tailscale ip`, fallback `ip -o addr show dev tailscale0`, 3 s timeout, no shell). Each address is bound independently; one that cannot bind yet is retried every 15 s by a background task that `runner.cleanup()` cancels (cleanup closes the sites even if that task died). In auto mode the same task also re-runs detection every 15 s until a Tailscale address is found (Tailscale without an IP at boot), adds it to the Host allowlist and binds it once; detection failures are logged at INFO on first occurrence and on state change only. In explicit `WEBUI_HOSTS` mode only the listed addresses are retried and nothing is re-detected. Documented sharp edge: explicit `WEBUI_HOSTS` without a loopback entry and Tailscale down at boot => nothing binds, `start_webui` raises `OSError`, the GUI stays disabled until the bot restarts; always list `127.0.0.1`. The range check also accepts a non-Tailscale address in `100.64.0.0/10` (ISP CGNAT).
- **Peer-address check** (`peer_guard`, the first middleware, every route including static, SSE and 404): the TCP peer (`transport.get_extra_info('peername')`, IPv4-mapped IPv6 unwrapped) must be in loopback (`127.0.0.0/8`, `::1`) or Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), using the same ranges as the bind policy; otherwise `403 {"error":"bad_peer"}` (a missing peername is refused too). It is unconditional. Rationale: Linux uses the weak-host model, so a device on the same Wi-Fi/LAN can add a route to the server's Tailscale IP via the server's LAN address and reach `100.x:8765`, bypassing Tailscale ACLs; the Host allowlist does not help because `Host: 100.x` is allowed. A legitimate tailnet connection always has a tailnet source address; that LAN host arrives with its LAN source address, and spoofing a tailnet source cannot complete the TCP handshake. A reverse proxy or cloudflared on the same machine connects from loopback and works; a proxy on another host must be on the tailnet. Refusals are logged at WARNING at most once per peer per minute, peer IP only. **Recommended belt and braces** (host firewall, e.g. in the runbook): `iptables -I INPUT -d 100.64.0.0/10 ! -i tailscale0 -j DROP` and `ip6tables -I INPUT -d fd7a:115c:a1e0::/48 ! -i tailscale0 -j DROP` (or the nft equivalent). The `bad_peer` check cannot protect against a process on this host itself (accepted limitation below).
- **Host allowlist** (DNS-rebinding defense), on every route including static and `/api/events`: a malicious web page can rebind its own hostname to `127.0.0.1` or the tailnet IP and then talk to the GUI same-origin from the victim's browser, which no CORS or Origin rule would stop; but the browser still sends `Host: evil.example`. Allowed: `localhost`, `127.0.0.1`, `::1`, every configured bind address, plus `WEBUI_ALLOWED_HOSTS` (hostnames, no wildcards, e.g. the MagicDNS name or later the domain). Case-insensitive, port ignored, `[v6]:port` handled. Otherwise `403 {"error":"bad_host"}`; a browser opening a disallowed hostname sees that raw JSON (index.html itself is blocked) - fix by adding the hostname to `WEBUI_ALLOWED_HOSTS`. `WEBUI_ALLOWED_HOSTS` entries must be plain `host[:port]` or `[v6][:port]`; entries containing `://`, `/`, `@`, wildcards, whitespace, unbalanced brackets or stray colons are dropped with a warning (a URL never silently becomes `https`).
- **Origin check on writes**: for non-GET/HEAD, if `Origin` is present its hostname must be allowed, and `Sec-Fetch-Site: cross-site` is refused (`403 bad_origin`) - on writes and also on every `/api/*` GET, so a cross-site no-cors fetch cannot open an SSE subscription. A missing Origin is allowed on purpose: browsers always send Origin on cross-origin writes (and on same-origin non-GET fetches), so absence means a non-browser client such as curl, which is not the CSRF threat and is already inside the network boundary. The JSON-only `Content-Type` plus the required `X-Editor` header (a non-simple header that forces a CORS preflight, which is never granted) are additional CSRF barriers.
- **Accepted limitation**: any local process or account on the server can reach `127.0.0.1:8765` and change the channel (loopback is shared by all local users). Run no untrusted local users on this host. Traffic is plain HTTP (Tailscale encrypts the tailnet hop; loopback needs none); a domain deployment must terminate TLS and authenticate at the proxy.

### Web UI hardening
- **CSP** on every response (including 403s): `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` for `/api/*` and `no-cache` for static. The UI uses `textContent`/DOM APIs only (channel names are attacker-influenced); `tests/test_static.py` forbids `innerHTML`, `eval`, inline handlers/styles and external URLs. No cookies.
- **Body limit** 4096 bytes. Guild ids and channel ids must be ASCII digits; `is_text_channel` confirms the channel exists in that guild and the bot can view it.
- **Editor attribution** (`X-Editor`) is self-declared: it names who changed what in the UI and logs; it is not authentication. The client percent-encodes it (`encodeURIComponent`) because a raw non-Latin-1 name would make `fetch` throw; the server `unquote`s then sanitizes (strip control chars, collapse whitespace, truncate to 40, reject empty).
- **Never crash the bot**: `maybe_start_webui` wraps address resolution, `make_app` and the binds; any failure (invalid `WEBUI_HOSTS`, bad port, port in use on every address) logs and the bot continues without the GUI. `parse_port` falls back to 8765 on an invalid or out-of-range `WEBUI_PORT`. A guild without a configured channel reads nothing.
- **Client**: a 403 `bad_host` / `bad_origin` / `bad_peer` shows a distinct toast (for `bad_host`: "This hostname is not allowed - add it to WEBUI_ALLOWED_HOSTS on the server") and stops the SSE reconnect loop (no retry storm).

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
- **Cost:** any local process or account on the server can reach `127.0.0.1:8765` and change the channel (accepted; run no untrusted local users). Anyone on the tailnet that ACLs allow can use the GUI. If a domain is added later it must authenticate at the proxy.

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
| "CUDAExecutionProvider unavailable" | CPU `onnxruntime` installed over GPU build, or CUDA libs not on `LD_LIBRARY_PATH` | section 8 D20, D28 |
| "Token malformed" at start | quotes/prefix/newline in token file | re-run `install_token.sh` |
| GUI shows new code missing / no GUI after upgrade | old unit installed | upgrade order above |
| `deploy.sh` "Refusing to deploy" | symlink, subdir, or file escaping the repo | remove it; it is intentional |
| Clips not played, `libopus not loaded` warning | opus missing from env | conda-forge `libopus` in the env |

---

## 11. Testing

Run from the repo root:
```
~/miniconda3/envs/tts-bot/bin/python -m unittest discover -s tests -v   # 114 tests at time of writing
bash tests/test_deploy_validation.sh                                    # deploy validation, no root needed
```
`unittest` only; no pytest.

| File | Covers |
|---|---|
| `tests/test_config_store.py` | atomic write (no `.tmp` leftovers on success/failure), EventHub (fan-out, idempotent unsubscribe, drop-oldest), ConfigStore (rev/no-op, conflict leaves state untouched, `expected_rev=None`, round-trip, corrupt file quarantined to `.corrupt`, persist-failure rollback, truth table of `is_*`, event publishing, seed, 20 concurrent writes). |
| `tests/test_webui.py` | no auth needed on any route and a stray `Authorization` header ignored, persist failure -> JSON 500 `persist_failed`, static headers/CSP, PUT channel/enabled incl. persistence and hub events, conflict, bad inputs, editor required/sanitized/percent-encoded, skip/clear, handler cancellation mid-write stays consistent, SSE (hello, config, status only on change, keepalive, subscriber cleanup), `validate_bind_hosts` table, Tailscale output parsers and detection fallbacks, Host allowlist (ports, IPv6, case, missing Host, static/state/events, `allowed_hosts` extras), Origin / `Sec-Fetch-Site` rules, `start_webui` (policy, partial bind + retry, all-fail `OSError`, cleanup cancels retry quickly with an open SSE). |
| `tests/test_controller.py` | `Controller` views and actions with fake guilds, `config_watcher` drain behavior (channel change drains; `enabled` change does not; other guilds and malformed events are survived). |
| `tests/test_bot_events.py` | hot-swap path with fake objects: `on_message` (configured channel queues; paused, old channel after a swap, unconfigured guild, unwatched, bots, DMs do not; prefix goes to commands only), `only_tts_channel`, `on_command_error` slash reply, `on_ready` seeding (lowest matching id, skips existing, idempotent) and its `channels` event, channel-relay listeners, no `.env` loaded. |
| `tests/test_wiring.py` | `maybe_start_webui` never raises (invalid `WEBUI_HOSTS`, bad `make_app`, out-of-range port, port in use, unexpected error), default hosts via Tailscale detection, `WEBUI_ALLOWED_HOSTS` reaches the app, `parse_port`, good config serves and cleans up. |
| `tests/test_static.py` | static payload < 40 KB, CSP-safe content (no `innerHTML`/`eval`/inline handlers/external URLs), served bytes equal files, and runs `tests/static_logic.js` under node if available. |
| `tests/static_logic.js` | pure JS helpers: SSE parser, `matchChannel`, `relTime`, `backoffDelay`, `groupChannels`, `normalizeEditor`, `shouldRollback`, `shouldApplySnapshot` (stale snapshots ignored). |
| `tests/test_deploy_validation.sh` | deploy validator: clean tree accepted; symlinked dirs/files, in-repo symlinks, subdirectories, missing `.py` rejected. |

**Not covered:** anything needing live Discord, a GPU, ffmpeg/voice, or a browser. `engine.synthesize`/playback/`audio_worker`/`on_message` end to end, the real systemd unit, `setup_system.sh`/`deploy.sh` as root, and DOM behavior of `app.js` (rendering, optimistic rollback wiring, keyboard handling) have no automated tests. The frontend was verified by node logic tests, static content checks and code review only. Run the manual smoke checklist in a real browser (and a real Discord test guild) before relying on it: name prompt, `bad_host` toast, channel list/search/keyboard, select + toast + undo, pause/resume, skip/clear, two browsers (conflict toast, live toast), kill/restart the bot and watch reconnect, 360 px width, dark mode.

---

## 12. Known limitations

- No live Discord, GPU or browser tests (section 11).
- Editor names are self-declared attribution, not authentication; there is no application-layer auth at all. Any local process/account on the server can reach the loopback GUI and change the channel (accepted); the tailnet side is governed by Tailscale ACLs.
- One channel per guild (plus pause).
- Deploy copies every root `*.py` (stray files ship) plus `static/*`, `models/*`, `requirements.txt`; `deploy.sh` and `setup_system.sh` run repo-supplied scripts as root (admin review required); a microsecond check/copy race is accepted; `setup_system.sh` resolves `SRC` with `pwd`, not `pwd -P`.
- The GUI is down whenever the bot is down; all plain HTTP (Tailscale encrypts the tailnet hop; a domain needs TLS at the proxy).
- The bot does not rejoin voice after a restart.
- `config.json` and `state.json` corruption: starts empty; only `config.json` is kept aside as `.corrupt` (one generation).
- Minor, deferred review items: first-set-on-unknown-guild bumps rev; `RecursionError` on pathologically nested JSON gives 500; 404 bodies are not JSON; `on_guild_channel_update` is chatty; shutdown cancels `_watcher_task` without awaiting it; DM `NoPrivateMessage` is unreachable on the prefix path; clicks while a request is in flight are silently ignored by the UI.

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
