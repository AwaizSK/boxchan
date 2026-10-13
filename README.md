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

## Voices (swap without a restart)
The Web UI has a **Voice** tab: pick any installed Piper voice, press **Apply** and the bot hot-swaps it in a few seconds (no restart, voice connections stay up). The voice is global (one GPU engine for all servers) and is remembered in `config.json`; **Reset to default** returns to `PIPER_MODEL_PATH`. **Say it** speaks a test phrase through the normal queue (the bot must be in voice in that server). If a voice fails to load the previous one keeps working and you get an error toast.

Add voices (single-speaker only; multi-speaker models are not offered):
1. Download both files (`<name>.onnx` and `<name>.onnx.json`) from https://huggingface.co/rhasspy/piper-voices (listen first on https://rhasspy.github.io/piper-samples). Each voice has its own licence on its model card; check it before use.
2. Production, no restart: `sudo scripts/install_voice.sh path/to/en_US-amy-medium.onnx` (the JSON is picked up next to it, or pass it as the second argument; `--force` replaces an installed voice). It validates the files and installs them atomically into `/opt/tts-bot/models`.
   Or: put both files in the repo's `models/` (gitignored) and run `sudo scripts/deploy.sh` (restarts the bot). `deploy.sh` never deletes from `/opt/tts-bot/models`, so installed voices survive deploys.
   Dev: just drop the files into `models/` (or `PIPER_MODELS_DIR`).
3. Voice tab -> **Rescan**, select the voice, **Apply**.

`PIPER_MODELS_DIR` (default: the folder of `PIPER_MODEL_PATH`) is where voices are scanned. `PIPER_MODEL_PATH` stays the fallback default voice. Swapping loads the new model before dropping the old one (briefly two models in GPU memory); if usage creeps up on the 6 GB card after many swaps, restart the service.

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
A small page with two tabs: **Channel** (pick which text channel the bot listens to, pause/resume it, skip or clear the queue, per guild) and **Voice** (see above). Changes apply live; no restart.

Start: just run the bot. There is **no login**: the network is the security boundary (see Security notes).
```bash
python bot.py                          # dev: GUI on http://127.0.0.1:8765
```
Open it, enter a display name (shown to other editors). Settings (all optional):
- `WEBUI_PORT` (default `8765`).
- `WEBUI_HOSTS`: comma-separated bind addresses. Only loopback (`127.0.0.0/8`, `::1`, or `localhost`) and Tailscale addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) are accepted; anything else (`0.0.0.0`, `::`, LAN addresses, hostnames) disables the GUI with an error log. Unset (auto mode) = `127.0.0.1` plus this machine's Tailscale addresses (`tailscale ip`, else `ip addr show dev tailscale0`). If Tailscale has no address yet at boot, detection is repeated every 15 s until one is found and bound. In explicit mode only the listed addresses are retried (every 15 s) and nothing is re-detected; always include `127.0.0.1` in the list, otherwise Tailscale being down at boot leaves nothing bound, startup fails and the GUI stays off until the bot restarts. Note a non-Tailscale address inside `100.64.0.0/10` (ISP CGNAT) also passes the range policy.
- `WEBUI_ALLOWED_HOSTS`: extra `Host` names the GUI answers to (comma-separated hostnames, no wildcards), e.g. your MagicDNS name. Always allowed: `localhost`, `127.0.0.1`, `::1` and the configured bind addresses. Any other Host gets `403 {"error":"bad_host"}`: in a browser you see that raw JSON instead of the page (the page itself is blocked), so add the hostname you typed to `WEBUI_ALLOWED_HOSTS`. Entries must be plain hostnames (optionally `:port`); URLs, paths and wildcards are rejected with a warning.

Over Tailscale: open `http://<tailscale-ip>:8765` (works out of the box) or `http://your-machine.tailnet-name.ts.net:8765` after adding that name to `WEBUI_ALLOWED_HOSTS`. Restrict who can reach port 8765 with Tailscale ACLs.

Later, behind a domain with zero trust (Cloudflare Access, etc.): add the domain to `WEBUI_ALLOWED_HOSTS` and run the reverse proxy on the same machine (it connects from loopback) or, if it is on another host, put that host on the tailnet: the app refuses any connection whose source address is not loopback or Tailscale (`403 bad_peer`). The proxy must pass `Host` unchanged (or the allowlist entry must match what it sends), and must not buffer `/api/events` (SSE).

If the port cannot be bound on any address the error is logged and the bot runs without the GUI.

Keyboard: `/` focus search, Up/Down move, Enter select channel, Esc clear search / leave it.

Multiple editors: every change carries the config revision it was based on. If someone else changed it first the server answers 409 and your view is refreshed; edits by others arrive live over SSE with a toast naming the editor.

Pause: the bot stops reading messages for that guild (nothing new is queued). Commands (`!join`, `!skip`, ...) still work in the chosen channel, and clips already queued finish unless you press Clear/Skip.

Security notes:
- No application-layer auth. Trust boundary = who can reach the port: loopback plus Tailscale only, narrowed by your Tailscale ACLs / zero-trust proxy. Two mechanisms enforce it: the bind policy (only loopback/Tailscale addresses are listened on) and a peer-address check on every request (source must be loopback or Tailscale, else `403 bad_peer`), because Linux's weak-host model lets a LAN device route to the Tailscale IP via the LAN address. Recommended extra: host firewall rules (`iptables -I INPUT -d 100.64.0.0/10 ! -i tailscale0 -j DROP`, `ip6tables -I INPUT -d fd7a:115c:a1e0::/48 ! -i tailscale0 -j DROP`). Anyone who can reach it can change the bot's channel.
- Host allowlist on every request (DNS-rebinding defense) and an Origin check on writes (a cross-site `Origin` gets `403 bad_origin`, and `Sec-Fetch-Site: cross-site` is refused on writes and on all `/api/*` requests; a missing Origin, e.g. curl, is allowed). Strong CSP, no cookies. Plain HTTP: it is safe only because of the network boundary.
- Accepted limitation: any local process or account on the server can reach `127.0.0.1:8765` and change the channel. Do not run untrusted users on this host.
- If you put a proxy in front, disable response buffering for `/api/events` (SSE); the server sends `X-Accel-Buffering: no` but the proxy must honor it.

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
sudo scripts/deploy.sh                  # byte-compile, copy *.py + static/ + models to /opt/tts-bot, prune stale files, restart
sudo systemctl enable tts_bot
```
Devs (no password) can: `sudo systemctl start|stop|restart tts_bot`, `systemctl status tts_bot`, `journalctl -u tts_bot` (after re-login for the `systemd-journal` group). They cannot read the token or change deployed code.
Local testing: each dev should create their own dev bot application and run `scripts/install_token.sh` (personal `~/.config/tts-bot/token`). Never share the production token.

### Upgrading an existing deployment
The GUI token is gone and the unit changed (Tailscale ordering, no `gui_token` credential, `WEBUI_HOST` replaced by `WEBUI_HOSTS`):
```bash
sudo scripts/setup_system.sh             # 1) installs the new unit (or: copy tts_bot.service to /etc/systemd/system/ && sudo systemctl daemon-reload)
sudo scripts/deploy.sh                   # 2) ship the code, restart
sudo shred -u /etc/tts-bot/gui_token     # 3) an existing gui_token is no longer used; delete it (after the new unit is live)
```
`deploy.sh` alone ships new code with the OLD unit (which still requires the `gui_token` credential file), so re-run `setup_system.sh` first. `deploy.sh` refuses symlinks or non-regular files in the repo's `*.py`, `static/`, `models/`.

Known boundary: anyone who can deploy code can read the token (the code runs with it). Review changes before `deploy.sh`.
