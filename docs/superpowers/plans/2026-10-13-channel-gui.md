# Channel-Select GUI + Hot-Swappable Config — Implementation Plan

**Spec (user request, verbatim intent):** A lightweight GUI that lists the server's text channels, has a search bar to filter and select one, and makes the listened-to channel hot-swappable at runtime (no bot restart, even when deployed). Smooth/fast for live testing, with the usual niceties. Several people may change settings at once, so configuration state must sync across them. A subagent must document the architecture and nuances, with the *why*, so other devs (and LLMs) can understand it.

No separate spec file exists; rulings below are provisional against the request above.

## Global Constraints

- Python 3.11, conda env interpreter `~/miniconda3/envs/tts-bot/bin/python`. **No new third-party dependencies**: use `aiohttp` (already installed via discord.py) and the standard library. Frontend: vanilla JS/CSS/HTML, **no CDN, no framework, no external requests**, total static payload < 40 KB.
- Tests use `unittest` (`IsolatedAsyncioTestCase`) and `aiohttp.test_utils`; run with `~/miniconda3/envs/tts-bot/bin/python -m unittest discover -s tests -v` from the repo root. No pytest.
- Never log, return, or write the Discord bot token. The GUI token is a separate secret, never logged, never returned by any endpoint, compared with `hmac.compare_digest`.
- Fail closed: no GUI token configured → web server does not start (log a warning). A guild with no configured channel → the bot reads nothing and ignores commands there.
- GUI binds `127.0.0.1:8765` by default (`WEBUI_HOST`, `WEBUI_PORT` env). Auth is `Authorization: Bearer <gui token>` header (never cookies → no CSRF surface).
- Writes require `X-Editor` header (self-declared display name, 1-40 chars after stripping control chars) — attribution only, not authentication.
- Optimistic concurrency: every config write carries `rev`; mismatch → HTTP 409 with the current snapshot. Server is the single source of truth; clients never merge.
- Config persisted atomically (temp file in same dir + fsync + `os.replace`) under an `asyncio.Lock`. Config file path from env `CONFIG_FILE` (default: `config.json` next to `STATE_FILE`).
- Existing behavior must keep working: prefix + slash hybrid commands, per-guild queue/worker, single-thread GPU inference, atomic `state.json` watch list, systemd credential token loading.
- Code style: match `bot.py` (type hints, `log = logging.getLogger(...)`, small functions, comments only for non-obvious *why*).
- Commit after each task with a conventional-commit message ending with the line `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Rationale for key decisions (input for the documentation task)

1. **GUI is served by the bot process itself (aiohttp).** Hot-swap must change in-memory state the bot is using *right now*; a separate process would need IPC or file watching and could race. aiohttp already ships with discord.py → zero new deps, one process, one event loop, nothing extra to deploy or restart.
2. **Server-Sent Events, not WebSockets/polling.** One-directional push (config changed, channel list changed, status) is all that's needed; SSE is plain HTTP, auto-reconnect friendly, trivial to proxy, lighter than a WS stack. The client uses `fetch` streaming (not `EventSource`) because `EventSource` cannot set the `Authorization` header.
3. **Optimistic concurrency with a monotonically increasing `rev`.** Several devs edit at once. Last-writer-wins would silently clobber; locks would need unlock semantics. A rev check + 409 + pushed snapshot is simple, stateless per client, and makes conflicts visible.
4. **One active channel per guild, plus a pause flag.** "Hot-swap" means replacing, not accumulating. Pause (`enabled=false`) lets a dev stop speech during live tests without losing the selection. Env `TTS_CHANNEL_ID` becomes only a first-boot **seed**.
5. **On channel swap the pending queue is cleared; the clip currently playing is not interrupted.** Queued items belong to the old channel and would be spoken out of context; killing the in-flight clip mid-word is jarring and a `skip` button exists.
6. **Loopback bind + bearer token + systemd credential.** The GUI controls a production bot, so it is not exposed on the network by default; remote use is via SSH tunnel or a TLS reverse proxy. Token delivered the same way as the Discord token (`LoadCredential`), keeping the "agents/devs can't read secrets" boundary.
7. **Self-declared editor name.** The team is small and known; the token is the real gate. Names give "who changed this" attribution in the UI without building an account system.
8. **Static files are three explicit routes with a strict CSP** (`default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'`; no inline) and the UI only uses `textContent` — channel names are attacker-influenced strings.
9. **`unittest`, not pytest** — avoids adding a dev dependency to the production conda env.

## File Structure

```
config_store.py      NEW  EventHub (pub/sub) + ConfigStore (rev, persistence, locks) + atomic_write_json
webui.py             NEW  aiohttp app factory, auth middleware, REST + SSE, token loader, start/stop helpers
static/index.html    NEW  markup
static/app.js        NEW  UI logic
static/app.css       NEW  styles (light/dark)
bot.py               MOD  use ConfigStore for channel decisions, Controller class, start web UI, channel-change relay
tests/test_config_store.py   NEW
tests/test_webui.py          NEW
tests/test_controller.py     NEW
scripts/gen_gui_token.sh     NEW
scripts/deploy.sh, tts_bot.service, .env.example, README.md   MOD
docs/ARCHITECTURE.md, CLAUDE.md   NEW (Task 6)
```

---

### Task 1: ConfigStore, EventHub, atomic writes

**Files:** Create `config_store.py`, `tests/test_config_store.py`.

Interfaces (exact names/signatures; other tasks depend on them):

```python
def atomic_write_json(data: dict, path: str) -> None
    # tempfile.NamedTemporaryFile in same dir, json.dump(indent=2), flush, os.fsync, os.replace.
    # On any exception: close and unlink the temp file, re-raise.

class ConflictError(Exception):
    snapshot: dict          # current snapshot at time of conflict

class EventHub:
    def subscribe(self) -> asyncio.Queue          # Queue(maxsize=64)
    def unsubscribe(self, q: asyncio.Queue) -> None   # idempotent
    def publish(self, event: dict) -> None        # put_nowait to every subscriber; if a queue is full
                                                  # drop its OLDEST item then enqueue (slow client never blocks)
    @property subscriber_count -> int

class ConfigStore:
    def __init__(self, path: str)                 # loads file if present; corrupt/unreadable file -> log exception, start empty (rev 0); never raises
    hub: EventHub
    rev: int
    def snapshot(self) -> dict
        # {"rev": int, "updated_by": str|None, "updated_at": float|None,
        #  "guilds": {"<guild_id as str>": {"channel_id": int|None, "enabled": bool}}}
    def channel_for(self, guild_id: int) -> int | None
    def is_enabled(self, guild_id: int) -> bool          # unknown guild -> True (only matters once a channel is set)
    def is_listen_channel(self, guild_id: int, channel_id: int) -> bool
        # True iff configured channel == channel_id AND enabled. Fail closed: no entry -> False.
    def is_command_channel(self, guild_id: int, channel_id: int) -> bool
        # True iff configured channel == channel_id (ignores enabled, so a paused bot can still be un-paused/controlled by chat commands)
    async def set_channel(self, guild_id: int, channel_id: int | None, *, expected_rev: int | None, editor: str) -> dict
    async def set_enabled(self, guild_id: int, enabled: bool, *, expected_rev: int | None, editor: str) -> dict
        # under one asyncio.Lock: if expected_rev is not None and != self.rev -> raise ConflictError(snapshot)
        # no-op change (same value) -> return snapshot WITHOUT bumping rev or publishing
        # else mutate, rev += 1, set updated_by/updated_at (time.time()), persist via atomic_write_json in asyncio.to_thread,
        # THEN publish {"type": "config", "snapshot": snapshot, "change": {"guild_id": int, "field": "channel_id"|"enabled", "old": ..., "new": ...}}
        # If persisting raises: roll back in-memory state and rev, re-raise.
        # Returns the new snapshot.
    async def seed_channel(self, guild_id: int, channel_id: int) -> bool
        # Sets channel only if guild has no entry; editor "env-seed"; returns True if it changed anything. Bumps rev/publishes like set_channel.
```

Persisted file format = the `snapshot()` dict.

Tests (all must be written and pass): rev increments on change and not on no-op; ConflictError carries current snapshot and nothing is mutated; stale `expected_rev=None` always applies; persistence round-trip (new ConfigStore on same path sees same data); corrupt file starts empty without raising; persist failure (monkeypatch `atomic_write_json` to raise) rolls back state and rev and publishes nothing; `is_listen_channel` / `is_command_channel` truth table incl. paused and unknown guild; hub: each subscriber gets event, unsubscribe idempotent, slow subscriber (full queue) drops oldest and does not block publisher; atomic_write_json leaves no `.tmp` files after success or failure; concurrent `asyncio.gather` of 20 `set_channel` calls with `expected_rev=None` ends with rev == number of distinct effective changes and a consistent file.

Commit: `feat: add ConfigStore with optimistic concurrency and event hub`.

---

### Task 2: Bot integration — hot-swappable channel, Controller

**Files:** Modify `bot.py`. Create `tests/test_controller.py`. (Depends on Task 1 interfaces.)

Behavior:

1. Create module-level `config = ConfigStore(CONFIG_FILE)` where `CONFIG_FILE = os.getenv("CONFIG_FILE", os.path.join(os.path.dirname(STATE_FILE), "config.json"))`. Remove the `TTS_CHANNEL_IDS` constant and the "TTS_CHANNEL_ID not set → SystemExit" check in `main()`.
2. `TTS_CHANNEL_ID` env is now a **seed only**: keep parsing it into `SEED_CHANNEL_IDS`. In `on_ready`, for each `guild` in `bot.guilds`, for each seed id where `guild.get_channel(id)` is a `discord.TextChannel`, `await config.seed_channel(guild.id, id)` (first match wins).
3. `only_tts_channel` check → `config.is_command_channel(ctx.guild.id, ctx.channel.id)`; on failure raise `commands.CheckFailure`. The slash-command CheckFailure reply must name the *current* configured channel (`<#id>`), or say "No channel configured — set one in the web UI" when none.
4. `on_message`: replace the TTS_CHANNEL_IDS test with `config.is_listen_channel(...)` (so paused → nothing is queued). Prefix-command dispatch must use `is_command_channel` (via the global check, as today).
5. Hot-swap side effect: add `async def config_watcher()` that subscribes to `config.hub`, and on each `{"type":"config"}` event whose `change.field == "channel_id"` drains that guild's queue (`drain(audio[gid].queue)` if present). It must not stop the clip currently playing. Run it as a task started in `main()` before `bot.start`, cancelled in `shutdown`. It must survive exceptions (log + continue) and exit only on cancellation. Unsubscribe in `finally`.
6. Add `class Controller` (module level, one instance `controller = Controller()`), the only surface the web UI uses. Methods (all sync unless marked, no I/O blocking):
   - `guilds_view() -> list[dict]`: for every `bot.guilds`: `{"id": str, "name": str, "channels": [{"id": str, "name": str, "category": str|None, "position": int}], "voice": str|None, "queue": int, "playing": bool, "watched": int}`. Channels = `guild.text_channels` the bot can `view_channel`+`read_message_history`-not-required (use `permissions_for(guild.me).view_channel`); sorted by (category position, channel position). IDs are **strings** in JSON to avoid JS 53-bit loss.
   - `status_view() -> dict`: same dynamic parts only: `{ "<gid>": {"voice": ..., "queue": int, "playing": bool, "watched": int} }` (cheap, used for periodic push).
   - `is_text_channel(guild_id: int, channel_id: int) -> bool`: channel exists in that guild, is `discord.TextChannel`, bot can view it.
   - `skip(guild_id: int) -> bool`: `vc.stop()` if playing; return whether it did.
   - `clear_queue(guild_id: int) -> int`: drain and return the number of removed items.
7. Channel-list relay: add listeners `on_guild_channel_create/delete/update`, `on_guild_channel_update`-equivalent for renames, `on_guild_join/remove` that call `config.hub.publish({"type": "channels", "guild_id": str(guild.id)})`. If a deleted channel is the configured one, leave config as is (UI shows it as missing; do NOT auto-change).
8. Replace `bot.py`'s own `save_state` with `from config_store import atomic_write_json` and make `WatchStore.mutate` call it (delete the duplicate function; keep `os.fsync` behavior — it lives in `atomic_write_json`).
9. Do **not** start the web server here (Task 3/5 wire it); but leave a clearly marked `# WEBUI hook` spot in `main()`.

Tests in `tests/test_controller.py` (import `bot` with env `DISCORD_TOKEN` unset and `CONFIG_FILE`/`STATE_FILE` pointing into a temp dir — set env before import; if import side effects make this impossible, report BLOCKED rather than refactoring broadly): `Controller.guilds_view`/`is_text_channel` with fake guild/channel objects (use `unittest.mock.MagicMock(spec=discord.TextChannel)`), `skip`, `clear_queue`, and `config_watcher` draining the queue on a channel_id change but not on an `enabled` change.

Verification: `~/miniconda3/envs/tts-bot/bin/python -m py_compile bot.py` and the full unittest suite.

Commit: `feat: hot-swappable listen channel via ConfigStore and Controller`.

---

### Task 3: Web backend (REST + SSE + auth)

**Files:** Create `webui.py`, `tests/test_webui.py`. (Depends on Tasks 1-2 interfaces; tests use fakes for the controller.)

```python
def load_gui_token() -> str | None
    # sources in order: $CREDENTIALS_DIRECTORY/gui_token, file at $WEBUI_TOKEN_FILE (default ~/.config/tts-bot/gui_token), env WEBUI_TOKEN.
    # strip whitespace; must be >= 24 chars else log a warning (length only) and return None.
def make_app(config: ConfigStore, controller, token: str, *, status_interval: float = 3.0) -> aiohttp.web.Application
async def start_webui(app, host: str, port: int) -> aiohttp.web.AppRunner     # returns runner; caller awaits runner.cleanup() on shutdown
```

`controller` is duck-typed: `guilds_view()`, `status_view()`, `is_text_channel(gid, cid)`, `skip(gid)`, `clear_queue(gid)` (Task 2 signatures).

Routes:
- `GET /` → `static/index.html`; `GET /app.js`, `GET /app.css` — three **explicit** routes reading files from `static/` next to `webui.py` (read once at app creation; no directory serving → no traversal). All responses (incl. API) get headers: `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` (API) / `no-cache` (static). The three static routes need no auth (they contain no secrets).
- All `/api/*` require `Authorization: Bearer <token>` (constant-time compare) else 401 JSON `{"error":"unauthorized"}`. Add a small failed-auth throttle: after 5 failures from one remote IP within 60 s, respond 429 for that IP for 60 s.
- `GET /api/state` → `{"config": config.snapshot(), "guilds": controller.guilds_view()}`.
- `PUT /api/guilds/{gid}/channel` body `{"channel_id": "<str>"|null, "rev": int|null}`. Validate: gid/channel_id digits; `controller.is_text_channel` true (unless null) else 400 `{"error":"bad_channel"}`; `X-Editor` present else 400 `{"error":"editor_required"}`. Conflict → 409 `{"error":"conflict","snapshot":...}`. Success → 200 `{"snapshot":...}`.
- `PUT /api/guilds/{gid}/enabled` body `{"enabled": bool, "rev": int|null}` same rules.
- `POST /api/guilds/{gid}/skip` → `{"skipped": bool}`; `POST /api/guilds/{gid}/clear` → `{"cleared": int}`. Both need `X-Editor`.
- `GET /api/events` → `text/event-stream`. On connect: subscribe to `config.hub`; immediately send `event: hello` with `{"snapshot":..., "guilds":...}`. Forward hub events as `event: config` / `event: channels` (JSON `data:`). Every `status_interval` seconds send `event: status` with `controller.status_view()` **only if it differs from the last sent value**. Send `: keepalive` comment every 15 s. Always unsubscribe in `finally` (client disconnects must not leak subscribers). Disable proxy buffering header `X-Accel-Buffering: no`.
- Body size limit 4 KB. Invalid JSON → 400. Unknown guild id → 404.
- Editor sanitization: strip control chars, collapse whitespace, truncate to 40, reject empty.
- Never include the token in any response or log line.

Tests: auth missing/wrong/right; throttle; PUT channel success + hub event published + persisted; conflict 409 body; bad channel 400; editor required; enabled toggle; skip/clear call the controller; SSE: hello on connect, config event arrives after a PUT from another client, status only sent when changed, subscriber_count returns to 0 after disconnect; CSP headers present; token never appears in any response body; `load_gui_token` source order and short-token rejection.

Commit: `feat: add web API with SSE sync and bearer auth`.

---

### Task 4: Frontend (vanilla JS, light)

**Files:** Create `static/index.html`, `static/app.js`, `static/app.css`. No tests required beyond a manual smoke checklist in the report; a small `node --check static/app.js` syntax check is expected if `node` exists, otherwise skip it and say so.

Requirements:
- No external resources, no inline scripts/styles/handlers (CSP). Use `textContent` / DOM APIs only for any server-provided string — never `innerHTML` with data.
- **Auth:** on load, token from `sessionStorage`; if missing or a 401 occurs, show a token prompt (password input). Editor name from `localStorage` (prompt once; editable via a small control).
- **Layout (single column, max-width ~720px, responsive):** header with guild selector (only shown when >1 guild), live-connection dot (green/amber/red with text label for a11y), revision + "Last changed by {name} · {relative time}". Controls row: Pause/Resume toggle, Skip, Clear queue, with live voice/queue/watched status. Search input (autofocus). Channel list below.
- **Channel list:** grouped by category (collapsed groups not required), each row `# name`, active channel highlighted with a "Listening" badge; if the configured channel no longer exists show a warning row "Configured channel missing". Filtering is client-side, instant, case-insensitive, matches on channel name and category, highlights matched substring using DOM nodes (not innerHTML). Empty state text. List is a `role="listbox"` with `aria-activedescendant`.
- **Keyboard:** `/` focuses search; `↑/↓` move highlight; `Enter` selects; `Esc` clears search then blurs. Mouse click selects. Selection is **optimistic**: row updates immediately, request sent with current `rev`; on 409 → revert to the server snapshot in the response and show a toast "Changed by {updated_by} — your change was not applied"; on other errors revert and toast the error.
- **Live sync:** `fetch('/api/events')` streaming reader with an SSE parser; handle `hello`, `config`, `channels` (refetch `/api/state`), `status`. Reconnect with exponential backoff (1 s → 10 s, jittered); on reconnect the `hello` event resyncs everything. Show amber "Reconnecting…" state; disable write controls while disconnected. When another editor changes the channel, flash the row and show a toast "{editor} switched to #name".
- **Niceties:** dark/light via `prefers-color-scheme`; toasts with `aria-live="polite"`; confirm-free but undo-friendly (toast for channel switch includes the previous channel name); `prefers-reduced-motion` respected; document title shows `# current-channel` ; works at 360 px width; focus-visible outlines.
- Total of the three files < 40 KB uncompressed. 60 fps feel: no layout thrash — build rows once per state change, filtering toggles `hidden` on existing nodes rather than rebuilding.

Commit: `feat: add lightweight channel-select web UI`.

---

### Task 5: Wiring, deploy, credentials, README

**Files:** Modify `bot.py` (the `# WEBUI hook` spot), `tts_bot.service`, `scripts/deploy.sh`, `.env.example`, `README.md`; create `scripts/gen_gui_token.sh`.

- `bot.py` `main()`: after the engine loads, `token = load_gui_token()`; if token: `runner = await start_webui(make_app(config, controller, token), WEBUI_HOST, WEBUI_PORT)` with `WEBUI_HOST = os.getenv("WEBUI_HOST","127.0.0.1")`, `WEBUI_PORT = int(os.getenv("WEBUI_PORT","8765"))`; else log a warning that the GUI is disabled. A failure to bind must log the exception and continue running the bot without the GUI. `shutdown()` must `await runner.cleanup()` (store runner in a module global) before `bot.close()`. Log the bind address at INFO (never the token).
- `scripts/gen_gui_token.sh [--system]`: generates `openssl rand -base64 36 | tr '+/' '-_' | tr -d '='` (≥ 48 chars); without flag writes `~/.config/tts-bot/gui_token` (mode 600, dir 700) and **prints the token once**; with `--system` (requires root) writes `/etc/tts-bot/gui_token` root:root 600 and prints it once. Refuses to overwrite an existing file unless `--rotate` is given. `bash -n` clean.
- `tts_bot.service`: add `LoadCredential=gui_token:/etc/tts-bot/gui_token`, `Environment=CONFIG_FILE=/var/lib/tts-bot/config.json`, `Environment=WEBUI_HOST=127.0.0.1`, `Environment=WEBUI_PORT=8765`. Keep all existing hardening. Comment explaining SSH tunnel access.
- `scripts/deploy.sh`: copy **all** `*.py` at repo root except none excluded (bot.py, tts_engine.py, config_store.py, webui.py) plus `static/*` into `/opt/tts-bot` (root-owned, dirs 755, files 644), `py_compile` all before copying, remove stale `__pycache__`.
- `.env.example`: document `CONFIG_FILE`, `WEBUI_HOST`, `WEBUI_PORT`, `WEBUI_TOKEN_FILE`, and that `TTS_CHANNEL_ID` is now a first-boot seed only.
- `README.md`: new "Web UI" section: what it is, how to start (generate token, SSH tunnel `ssh -L 8765:127.0.0.1:8765 host`, open `http://127.0.0.1:8765`), keyboard shortcuts, multi-editor behavior (409 / live sync), pause semantics, security notes, and an updated "Channel lock" section (config.json is the truth; env is a seed). Keep it concise.

Verification: `bash -n` on scripts, `py_compile` all, run the unit suite, and an end-to-end smoke test that starts `make_app` with a fake controller on an ephemeral port and fetches `/` and `/api/state` with and without the token (this may live in `tests/test_webui.py`).

Commit: `feat: wire web UI into bot, deploy and docs`.

---

### Task 6: Architecture documentation

**Files:** Create `docs/ARCHITECTURE.md` and `CLAUDE.md` (repo root). Documentation only; do not modify code.

Audience: other developers **and LLMs** that will be asked questions about this repo. Read the final code (`bot.py`, `tts_engine.py`, `config_store.py`, `webui.py`, `static/*`, `scripts/*`, `tts_bot.service`, `tests/*`) and the "Rationale for key decisions" and "Global Constraints" sections of this plan (`docs/superpowers/plans/2026-10-13-channel-gui.md`). Everything stated must be verified against the code — if the code differs from this plan, document the code and note the divergence.

`docs/ARCHITECTURE.md` must contain, in this order:
1. **Purpose & one-paragraph summary.**
2. **System diagram** (ASCII): Discord gateway ↔ bot process (discord.py loop, per-guild queue/worker, ConfigStore, EventHub, aiohttp web UI) ↔ Piper (single worker thread, CUDA) ↔ ffmpeg ↔ Discord voice; browsers ↔ web UI (REST+SSE).
3. **Module map**: each file, its single responsibility, its public interface.
4. **Data flow walkthroughs**: (a) a watched user's message → speech; (b) a dev switching channels in the GUI → effect on the live bot; (c) two devs editing simultaneously (conflict path).
5. **State & persistence**: `config.json` (schema, rev semantics, atomic writes), `state.json` (watch list), what lives only in memory, what is lost on restart.
6. **Concurrency model**: event loop, locks (ConfigStore lock, WatchStore lock, engine lock), the single GPU worker thread and why (GTX 1060 6 GB, one CUDA context), playback via `asyncio.Event` set by `after`, worker cancellation/teardown, voice reconnect tolerance.
7. **Security model & trust boundaries**: token locations and flow (systemd credential, root-owned files, `ttsbot` user, `/opt/tts-bot` root-owned), GUI token, loopback bind, bearer auth, CSP, why deployers can read the token, what the sudoers rule allows. **No secret values.**
8. **Decision log** (ADR style, each: Decision / Why / Alternatives rejected / Cost if wrong) covering every item in the plan's rationale list **plus** the pre-existing decisions: piper-tts pinned to 1.2.0 (`synthesize_stream_raw` API), onnxruntime-gpu <1.21 and uninstalling CPU `onnxruntime`, FP32/CUDA only for Pascal, hybrid prefix+slash commands, `message_content` + `members` intents, per-guild queue with ⏳ reaction on overflow, temp `.wav` in `/tmp/tts_bot` purged at start/stop, conda-forge only (`--override-channels`) and explicit `pip` install, `PrivateTmp`/`ProtectHome` choices, `LD_LIBRARY_PATH` for pip-installed CUDA libs, text normalization rules and the spoiler preference.
9. **Nuances & gotchas** (bulleted, each with the reason): things that look wrong but are intentional, and traps (e.g. dual onnxruntime install, conda ToS under sudo, `sudo` env reset, Message Content intent, unconfigured guild reads nothing, paused ≠ disconnected, deleted configured channel is not auto-cleared, `rev` semantics, SSE through proxies needs buffering off).
10. **Operations runbook**: deploy, rotate Discord token, rotate GUI token, change channel (GUI, no restart), logs, common failures and fixes.
11. **Testing**: how to run, what is covered, what is not (no live Discord/GPU tests).
12. **"Asking an LLM about this repo"**: a short section listing the files to give it first and example questions it can answer.

`CLAUDE.md` (≤ 80 lines): terse orientation for coding agents — what the project is, run/test commands, the file map, the hard rules (never read/print/commit secrets; token locations are off-limits; don't add dependencies without asking; GPU work stays on the single worker thread; config changes go through `ConfigStore`), and a pointer to `docs/ARCHITECTURE.md`.

Commit: `docs: add architecture guide and agent orientation`.
