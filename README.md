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
Bot reads messages and accepts commands only in the text channel(s) from `TTS_CHANNEL_ID`. `join` follows whoever runs it into their voice channel. Production: `echo 'TTS_CHANNEL_ID=<id>' | sudo tee /etc/tts-bot/bot.env`, then `sudo systemctl restart tts_bot`.

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
sudo scripts/setup_system.sh            # service user, /opt/tts-bot (root-owned), sudoers, unit
sudo scripts/install_token.sh --system  # token -> /etc/tts-bot/token (root:root 600)
sudo scripts/deploy.sh                  # copy reviewed code to /opt/tts-bot + restart
sudo systemctl enable tts_bot
```
Devs (no password) can: `sudo systemctl start|stop|restart tts_bot`, `systemctl status tts_bot`, `journalctl -u tts_bot` (after re-login for the `systemd-journal` group). They cannot read the token or change deployed code.
Local testing: each dev should create their own dev bot application and run `scripts/install_token.sh` (personal `~/.config/tts-bot/token`). Never share the production token.

Known boundary: anyone who can deploy code can read the token (the code runs with it). Review changes before `deploy.sh`.
