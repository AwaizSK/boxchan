# Discord TTS Bot (Piper + CUDA)

Reads messages from targeted users aloud in a voice channel. Piper TTS runs on ONNX Runtime GPU (CUDA, FP32) — suited to GTX 1060 (Pascal).

## Setup
```bash
sudo apt install ffmpeg libopus0
conda create -n tts-bot python=3.11 -y && conda activate tts-bot
pip install -r requirements.txt
cp .env.example .env   # non-secret settings only
./scripts/install_token.sh   # hidden prompt; stores token at ~/.config/tts-bot/token (600)
```
Download a voice into `models/` (both files):
```bash
cd models
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json
```
Discord Developer Portal: enable **Message Content Intent**. Invite with scopes `bot applications.commands`, permissions View Channel, Connect, Speak.

## Token safety
- Token is never in the repo or `.env`. Order of lookup: systemd credential (`$CREDENTIALS_DIRECTORY/discord_token`), `~/.config/tts-bot/token`, then `DISCORD_TOKEN` env.
- systemd: `LoadCredential=` in the unit passes the file to the process only.
- Agent tools: `.claude/settings.json` denies Read/Edit on the token dir, `.env` and editor swap files. Permission rules do not stop a process that runs arbitrary code, so also keep the token file out of reach (mode 600, and consider a separate OS user for the agent).
- Leaked token? Developer Portal -> Bot -> Reset Token.

## GPU notes
- `onnxruntime-gpu` needs matching CUDA + cuDNN runtime. Pascal works with CUDA 12.x (driver ≤ 580 branch). Pin per the ORT compatibility table; `pip install nvidia-cudnn-cu12 nvidia-cublas-cu12` if system libs missing, and add them to `LD_LIBRARY_PATH`.
- Do not install `onnxruntime` (CPU) next to `onnxruntime-gpu`.
- Check CUDA provider: `python -c "import onnxruntime as o; print(o.get_available_providers())"`.
- Synthesis runs on one worker thread; no FP16 used.

## Commands
Every command works as prefix (`COMMAND_PREFIX`, default `!`) and as slash command. Slash needs the `applications.commands` invite scope.
- `join` — join your voice channel, start this guild's queue worker
- `leave` — stop playback, clear queue, cancel worker, disconnect, flush guild watch list
- `watch @user` / `stop @user` — add / remove watched user (persisted in `state.json`, atomic write via `os.replace`, guarded by `asyncio.Lock`)
- `skip` — stop the clip now playing; worker moves to next
- `status` — watched users, voice channel, queue depth, CUDA state

## Channel lock
The bot reads messages and accepts commands only in the text channel chosen per guild. `config.json` (`CONFIG_FILE`, default next to `state.json`; `/var/lib/tts-bot/config.json` in production) is the source of truth and is edited from the Web UI. `TTS_CHANNEL_ID` is only a first-boot seed: it populates a guild with no entry yet and is ignored afterwards. `join` follows whoever runs it into their voice channel.

## Web UI
A small page to pick which text channel the bot listens to, pause/resume it, and skip or clear the queue, per guild. Changes apply live; no restart.

Start:
```bash
scripts/gen_gui_token.sh              # dev: ~/.config/tts-bot/gui_token; prints the token ONCE
sudo scripts/gen_gui_token.sh --system  # prod: /etc/tts-bot/gui_token (root:root 600); --rotate to replace
ssh -L 8765:127.0.0.1:8765 <host>     # tunnel (not needed for a local dev run)
```
Open `http://127.0.0.1:8765`, enter the token and a display name (shown to other editors). Settings: `WEBUI_HOST` (default `127.0.0.1`), `WEBUI_PORT` (default `8765`), `WEBUI_TOKEN_FILE`. Token lookup: `$CREDENTIALS_DIRECTORY/gui_token`, the token file, then `WEBUI_TOKEN`. A token shorter than 24 chars (or none) disables the GUI with a log warning; the bot still runs. If the port cannot be bound the error is logged and the bot runs without the GUI. In production the unit requires `/etc/tts-bot/gui_token` to exist (`LoadCredential`), so generate it before the first `deploy.sh`.

Keyboard: `/` focus search, Up/Down move, Enter select channel, Esc clear search / leave it.

Multiple editors: every change carries the config revision it was based on. If someone else changed it first the server answers 409 and your view is refreshed; edits by others arrive live over SSE with a toast naming the editor.

Pause: the bot stops reading messages for that guild (nothing new is queued). Commands (`!join`, `!skip`, ...) still work in the chosen channel, and clips already queued finish unless you press Clear/Skip.

Security notes:
- Bearer token compared in constant time; strong CSP, no cookies. Plain HTTP: keep it on loopback and reach it through the SSH tunnel. Do not expose it directly.
- Failed-auth throttle is per client IP (`request.remote`): 5 invalid or missing credentials / 60 s, then further invalid attempts get 429 for 60 s. A valid token is always accepted (even while the IP is blocked) and never counted, so a mistyped token or stale tabs cannot lock out working sessions. A local process (or everyone behind a shared proxy IP) can still make invalid attempts return 429, but cannot lock out valid tokens.
- If you do put a proxy in front, disable response buffering for `/api/events` (SSE); the server sends `X-Accel-Buffering: no` but the proxy must honor it.
- Anyone with the token can change the bot's channel. Rotate with `--rotate` and restart the service.

## Behavior
- Per-guild `asyncio.Queue` (`MAX_QUEUE`, default 20) and worker. Full queue: message gets a ⏳ reaction and a log line.
- Text normalization (`clean_discord_text`): URLs -> "link"; code blocks -> "code block"; inline code/spoilers keep content, drop markup; user/role/channel mentions -> display names; timestamps -> "timestamp"; custom emoji -> name; repeated punctuation/letters collapsed; truncated at word boundary. Empty/symbol-only results never reach the queue or GPU.
- Inference: one worker thread plus `asyncio.Lock` — one CUDA context, FP32. Never blocks the gateway.
- Playback waits on an `asyncio.Event` set by the `after` callback (no polling). Temp `.wav` deleted in `after`; stale files purged at start/stop.
- Voice reconnects: worker waits up to 10 s for `is_connected()` and never exits except on cancel.
- Auto-leave when no humans remain; also cleans up if the bot is kicked from voice.
- SIGINT/SIGTERM: every guild is torn down (stop, disconnect — no orphan ffmpeg), then the bot closes.
- CUDA failure is logged; engine falls back to CPU.

Enable **Message Content** and **Server Members** intents in the Developer Portal.

## Run
```bash
python bot.py
```
## Team deployment (production)
Roles: devs edit the repo (group `devteam`); an admin with real sudo deploys and holds the token.
```bash
sudo scripts/gen_gui_token.sh --system  # web UI token -> /etc/tts-bot/gui_token (required by the unit; setup refuses without it)
sudo scripts/setup_system.sh            # service user, /opt/tts-bot (root-owned), sudoers, unit
sudo scripts/install_token.sh --system  # token -> /etc/tts-bot/token (root:root 600)
sudo scripts/deploy.sh                  # byte-compile, copy *.py + static/ + models to /opt/tts-bot, prune stale files, restart
sudo systemctl enable tts_bot
```
Devs (no password) can: `sudo systemctl start|stop|restart tts_bot`, `systemctl status tts_bot`, `journalctl -u tts_bot` (after re-login for the `systemd-journal` group). They cannot read the token or change deployed code.
Local testing: each dev should create their own dev bot application and run `scripts/install_token.sh` (personal `~/.config/tts-bot/token`). Never share the production token.

### Upgrading an existing deployment
The unit changed (it now loads `/etc/tts-bot/gui_token`), so order matters:
```bash
sudo scripts/gen_gui_token.sh --system   # 1) token first; the unit will not start without it
sudo scripts/setup_system.sh             # 2) installs the new unit (or: copy tts_bot.service to /etc/systemd/system/ && sudo systemctl daemon-reload)
sudo scripts/deploy.sh                   # 3) ship the code
```
`deploy.sh` alone ships new code with the OLD unit (no `gui_token` credential, no `CONFIG_FILE`/`WEBUI_*`), so the GUI would silently stay disabled. `deploy.sh` and `setup_system.sh` refuse to run if the token file is missing, and `deploy.sh` refuses symlinks or non-regular files in the repo's `*.py`, `static/`, `models/`.

Known boundary: anyone who can deploy code can read the token (the code runs with it). Review changes before `deploy.sh`.
