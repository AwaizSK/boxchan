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
2. Production, no restart: `sudo scripts/install_voice.sh path/to/en_US-amy-medium.onnx` (the JSON is picked up next to it, or pass it as the second argument). A new voice is created as a .json/.onnx pair (rolled back together if the second link fails); `--force` removes the old voice then moves the new one in, which is NOT atomic. No service restart; rescan in the GUI after install.
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
- `watch @user` — add watched user and auto-join your voice channel if the bot is not connected (persisted in `state.json`, atomic write via `os.replace`, guarded by `asyncio.Lock`); if you are not in voice, suggests `!join`
- `stop @user` — remove watched user (persisted in `state.json`)
- `skip` — stop the clip now playing; worker moves to next
- `status` — watched users, voice channel, queue depth, CUDA state
- `help` — list all available commands (dynamically from bot registry) and show current state (channel, voice connection, active voice, watched users)
- `voices` — list installed Piper voices with quality/language/size, mark the active one (read-only; voice switching is GUI-only)

## Channel lock
The bot reads messages and accepts commands only in the text channel chosen per guild. `config.json` (`CONFIG_FILE`, default next to `state.json`; `/var/lib/tts-bot/config.json` in production) is the source of truth and is edited from the Web UI. `TTS_CHANNEL_ID` is only a first-boot seed: it populates a guild with no entry yet and is ignored afterwards. `join` follows whoever runs it into their voice channel.

## Web UI
A small page with five tabs: **Channel** (pick which text channel the bot listens to, pause/resume it, skip or clear the queue, per guild), **Voice** (see above), **Messages** (post as the bot, see below), **Features** (request features, log author, admin-only deletion), and **Admin** (admin-only user & group management). Changes apply live; no restart.

Start: run the bot. **Login is required and fails closed**: the GUI always shows a **Sign in** page, and if the users file (`users.json` next to `config.json`, or `USERS_FILE`; `/var/lib/tts-bot/users.json` in production) is missing, unreadable, invalid or has no users, nobody can sign in (every API call answers `401`) and the problem is logged once at ERROR. There are no default credentials: create the first user with the CLI below.
```bash
python bot.py                          # dev: GUI on http://127.0.0.1:8765
```
Open it, sign in, and optionally set a display name (shown to other editors). You can sign out anytime with **Sign out**.

### Managing web UI users
Passwords are stored only as scrypt hashes (`scrypt$16384$8$1$<salt>$<hash>`, stdlib `hashlib.scrypt`); the Admin tab and the CLI enforce 10 to 200 characters and refuse well-known defaults (`changeme`, `testerpassword`, `password`, `admin`). The CLI prompts twice for the password (never on the command line or in the environment) and writes the file with mode 0600:
```bash
# production (the file is owned by the service user)
sudo -u ttsbot env USERS_FILE=/var/lib/tts-bot/users.json /opt/tts-bot/env/bin/python /opt/tts-bot/webui.py add-user alice --admin
# dev
python webui.py --users-file users.json add-user alice --admin
python webui.py --users-file users.json list-users     # names and groups only
python webui.py --users-file users.json remove-user alice
```
`add-user` on an existing name changes that user's password (and adds `admin` with `--admin`). Credentials reload automatically on file change without restarting the bot; a user removed from the file loses their session at the next request. File layout (see `users.example.json`, which contains only invalid placeholder hashes):
```json
[
  { "username": "alice", "password": "scrypt$16384$8$1$...$...", "groups": ["admin"] },
  { "username": "tester1", "password": "scrypt$16384$8$1$...$...", "groups": ["user"] }
]
```
Admin privileges (deleting feature requests, the Admin tab) come only from membership of the `admin` group in this file; the user name alone grants nothing. Migration: files from older versions with plaintext or `sha256:<hex>` passwords still work, and each such user's entry is rewritten to a scrypt hash automatically on their next successful login (other users and fields are untouched); a legacy user named `admin` with no groups is treated as an admin. To run the GUI without login (dev only, never on a shared host) set `WEBUI_AUTH=off`; the bot logs a warning at start.

### Features tab (feature requests)
Submit and track feature requests directly in the GUI:
- **Request feature:** Any signed-in user can submit a feature with a name (1–100 chars) and description (1–2000 chars).
- **Author logging:** The server automatically records the logged-in username as the author (who requested it) along with the creation date.
- **Admin deletion:** Only administrators can delete feature requests. Non-admins cannot see the delete button and the backend rejects unauthorized deletions with `403 Forbidden`.
- **Storage:** Persisted in `features.json` (`FEATURES_FILE` env override; `/var/lib/tts-bot/features.json` in production).

### Admin tab (user & group management)
Available exclusively to admin users:
- **Manage users:** Create or update users, reset passwords, and assign groups (e.g. `admin`, `tester`).
- **Group capabilities:** Group membership is checked for privileged actions (`in_group(user, "admin")`).
- **Admin protection:** A user named `admin` cannot be deleted from the tab, and signed-in admins cannot delete their own active user. Passwords set here must be 10 to 200 characters (`400 weak_password` otherwise) and a new user needs one (`400 password_required`); a request that only changes groups keeps the stored hash. Non-admins cannot see the tab and all backend endpoints enforce `403 Forbidden`.

Settings (all optional):
- `WEBUI_PORT` (default `8765`).
- `WEBUI_HOSTS`: comma-separated bind addresses. Only loopback (`127.0.0.0/8`, `::1`, or `localhost`) and Tailscale addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) are accepted; anything else (`0.0.0.0`, `::`, LAN addresses, hostnames) disables the GUI with an error log. Unset (auto mode) = `127.0.0.1` plus this machine's Tailscale addresses (`tailscale ip`, else `ip addr show dev tailscale0`). If Tailscale has no address yet at boot, detection is repeated every 15 s until one is found and bound. In explicit mode only the listed addresses are retried (every 15 s) and nothing is re-detected; always include `127.0.0.1` in the list, otherwise Tailscale being down at boot leaves nothing bound, startup fails and the GUI stays off until the bot restarts. Note a non-Tailscale address inside `100.64.0.0/10` (ISP CGNAT) also passes the range policy.
- `WEBUI_ALLOWED_HOSTS`: extra `Host` names the GUI answers to (comma-separated hostnames, no wildcards), e.g. your MagicDNS name. Always allowed: `localhost`, `127.0.0.1`, `::1` and the configured bind addresses. Any other Host gets `403 {"error":"bad_host"}`: in a browser you see that raw JSON instead of the page (the page itself is blocked), so add the hostname you typed to `WEBUI_ALLOWED_HOSTS`. Entries must be plain hostnames (optionally `:port`); URLs, paths and wildcards are rejected with a warning.

Over Tailscale: open `http://<tailscale-ip>:8765` (works out of the box) or `http://your-machine.tailnet-name.ts.net:8765` after adding that name to `WEBUI_ALLOWED_HOSTS`. Restrict who can reach port 8765 with Tailscale ACLs.

Later, behind a domain with zero trust (Cloudflare Access, etc.): add the domain to `WEBUI_ALLOWED_HOSTS` and run the reverse proxy on the same machine (it connects from loopback) or, if it is on another host, put that host on the tailnet: the app refuses any connection whose source address is not loopback or Tailscale (`403 bad_peer`). The proxy must pass `Host` unchanged (or the allowlist entry must match what it sends), and must not buffer `/api/events` (SSE).

If the port cannot be bound on any address the error is logged and the bot runs without the GUI.

### Messages tab (speak as the bot)
Write a message and the bot posts it in the server's **configured listen channel** ("Posting to #name as the bot"; with no channel set the tab is disabled until you pick one in the Channel tab). The bot needs **Send Messages** (and View Channel) in that channel, otherwise you get "Bot lacks Send Messages in that channel".
- **Tag someone:** type in the "Tag someone" box, pick a member (click or Enter); `@Name` is inserted at the cursor and a chip appears (the chip greys out if you delete the name from the text). Only members who can see the channel are listed and accepted. The preview shows the message as it will go out, tagged names highlighted; "Will ping: ..." lists who gets notified.
- **Presets:** "Ready to use" fills `The voice bot is ready to use! Join a voice channel, type !join here, and I'll read watched users' messages aloud.` (edit it freely) and puts you in the tag box so you can tag the person it is for; "Clear" empties everything.
- Send with the button or Ctrl/Cmd+Enter. 1 to 2000 characters. Your text is kept if sending fails.
- Limits: only individual users can be pinged, never `@everyone`, `@here` or roles (typing those words sends plain text with no ping; pasting a raw role mention is refused). At most 10 messages per minute and one every 2 seconds per server (you see "try again in N s").
- Audit: each send logs one line, `gui message sent guild=... channel=... editor=... chars=N mentions=N`. The message content is never logged.

Keyboard: `/` focus search, Up/Down move, Enter select channel, Esc clear search / leave it.

Multiple editors: every change carries the config revision it was based on. If someone else changed it first the server answers 409 and your view is refreshed; edits by others arrive live over SSE with a toast naming the editor.

Pause: the bot stops reading messages for that guild (nothing new is queued). Commands (`!join`, `!skip`, ...) still work in the chosen channel, and clips already queued finish unless you press Clear/Skip.

Security notes:
- Two layers. Application layer: session login against `users.json` with scrypt hashes, fail closed (see above), 5 failed logins per minute per IP. Network layer: who can reach the port: loopback plus Tailscale only, narrowed by your Tailscale ACLs / zero-trust proxy. Two mechanisms enforce it: the bind policy (only loopback/Tailscale addresses are listened on) and a peer-address check on every request (source must be loopback or Tailscale, else `403 bad_peer`), because Linux's weak-host model lets a LAN device route to the Tailscale IP via the LAN address. Recommended extra: host firewall rules (`iptables -I INPUT -d 100.64.0.0/10 ! -i tailscale0 -j DROP`, `ip6tables -I INPUT -d fd7a:115c:a1e0::/48 ! -i tailscale0 -j DROP`). Anyone who can reach the GUI AND sign in can change the configured channel, pause/resume, skip and clear the queue, swap the voice, make the bot speak test phrases, and post as the bot in the configured channel (mentions limited to <=10 distinct members who can see the channel; 10 sends per 60 s plus a 2 s gap per guild).
- Host allowlist on every request (DNS-rebinding defense) and an Origin check on writes (a cross-site `Origin` gets `403 bad_origin`, and `Sec-Fetch-Site: cross-site` is refused on writes and on all `/api/*` requests; a missing Origin, e.g. curl, is allowed). Strong CSP; the only cookie is the `HttpOnly` session cookie. Plain HTTP: it is safe only because of the network boundary.
- Accepted limitation: any local process or account on the server can reach `127.0.0.1:8765`; without a valid login it cannot use the API, but a local user who can read the users file can run the CLI against it, and one who can read a session cookie can act as that user. With `WEBUI_AUTH=off` it can change the configured channel, pause/resume, skip and clear the queue, swap the voice, make the bot speak test phrases, and post as the bot in the configured channel (mentions limited to <=10 distinct members who can see the channel; 10 sends per 60 s plus a 2 s gap per guild). Do not run untrusted users on this host.
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
sudo scripts/deploy.sh                  # byte-compile, copy *.py + static/ + models to /opt/tts-bot, prune stale *.py/static files (never prune models/), restart
sudo systemctl enable tts_bot
```
Devs (no password) can: `sudo systemctl start|stop|restart tts_bot`, `systemctl status tts_bot`, `journalctl -u tts_bot` (after re-login for the `systemd-journal` group). They cannot read the token or change deployed code.
Local testing: each dev should create their own dev bot application and run `scripts/install_token.sh` (personal `~/.config/tts-bot/token`). Never share the production token.

### Developer Collaboration & PR Workflow
1. **Clone & Environment Setup:**
   ```bash
   git clone <repo-url>
   cd boxchan
   conda create -n tts-bot python=3.11 -y && conda activate tts-bot
   pip install -r requirements.txt
   cp .env.example .env
   python webui.py --users-file users.json add-user yourname --admin   # creates users.json (the example file holds only placeholders)
   ```
2. **Local Verification:**
   Before pushing changes and opening a pull request, run the full validation suite:
   ```bash
   python -m unittest discover -s tests -v
   bash tests/test_deploy_validation.sh
   node tests/static_logic.js
   ```
3. **Pull Request Process:**
   - Create a feature branch (`git checkout -b feature/my-enhancement`).
   - Keep commits granular and adhere to documentation guidelines.
   - Open a PR against `main` for review.

### CI/CD & Self-Hosted Runner Deployment
To automate deployments from PR merges:
- Setup a self-hosted GitHub Actions / GitLab CI runner on this server.
- The runner executes tests on incoming PRs.
- On merge to `main`, an authorized pipeline triggers `sudo scripts/deploy.sh` to update `/opt/tts-bot` and restart `tts_bot.service`.

### Upgrading an existing deployment
The GUI token is gone and the unit changed (Tailscale ordering, no `gui_token` credential, `WEBUI_HOST` replaced by `WEBUI_HOSTS`):
```bash
# If this host had the old auto-deploy watcher, remove it:
systemctl list-unit-files | grep tts_bot_watcher   # check if it exists
# If present, run:
sudo systemctl disable --now tts_bot_watcher.service
sudo rm -f /etc/systemd/system/tts_bot_watcher.service
sudo systemctl daemon-reload
# Then proceed with the upgrade:
sudo scripts/setup_system.sh             # 1) installs the new unit (or: copy tts_bot.service to /etc/systemd/system/ && sudo systemctl daemon-reload)
sudo scripts/deploy.sh                   # 2) ship the code, restart
sudo shred -u /etc/tts-bot/gui_token     # 3) an existing gui_token is no longer used; delete it (after the new unit is live)
```
The watcher would auto-deploy any code that landed in the repo as root with no review gate (decision D42). `deploy.sh` alone ships new code with the OLD unit (which still requires the `gui_token` credential file), so re-run `setup_system.sh` first. `deploy.sh` refuses symlinks or non-regular files in the repo's `*.py`, `static/`, `models/`.

Known boundary: anyone who can deploy code can read the token (the code runs with it). Review changes before `deploy.sh`.
