import asyncio
import json
import logging
import os
import re
import signal
import sys
import tempfile

import discord
from discord.ext import commands
from dotenv import load_dotenv

from tts_engine import TTSEngine, clean_discord_text

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")



TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{23,30}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,38}$")


def load_token() -> str | None:
    """Token sources, safest first: systemd credential, token file, plain env var."""
    cred_dir = os.getenv("CREDENTIALS_DIRECTORY")
    candidates = []
    if cred_dir:
        candidates.append(os.path.join(cred_dir, "discord_token"))
    candidates.append(os.path.expanduser(os.getenv("DISCORD_TOKEN_FILE", "~/.config/tts-bot/token")))
    for path in candidates:
        try:
            with open(path) as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            continue
    return os.getenv("DISCORD_TOKEN")


TOKEN = load_token()
if TOKEN and not TOKEN_RE.match(TOKEN):
    # Never log the value itself.
    raise SystemExit(f"Token malformed (len={len(TOKEN)}): expected '<id>.<ts>.<hmac>'. "
                     "No quotes, 'Bot ' prefix, or newline. Re-run scripts/install_token.sh")
PREFIX = os.getenv("COMMAND_PREFIX", "!")
MODEL_PATH = os.getenv("PIPER_MODEL_PATH", "models/en_US-lessac-medium.onnx")
CONFIG_PATH = os.getenv("PIPER_CONFIG_PATH") or None
TTS_CHANNEL_IDS = {int(x) for x in os.getenv("TTS_CHANNEL_ID", "").replace(" ", "").split(",") if x.isdigit()}
MAX_CHARS = int(os.getenv("MAX_CHARS", "300"))
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "20"))
STATE_FILE = os.getenv("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
RECONNECT_WAIT = 10.0   # s to wait for voice reconnect before dropping a job
PLAY_TIMEOUT = 180.0    # safety: max s a single clip may block the worker

# Conda env bin/lib: ffmpeg + libopus live there; make them findable under systemd too.
_env_prefix = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
os.environ["PATH"] = os.path.join(_env_prefix, "bin") + os.pathsep + os.environ.get("PATH", "")
if not discord.opus.is_loaded():
    for _lib in (os.path.join(_env_prefix, "lib", "libopus.so.0"), "libopus.so.0"):
        try:
            discord.opus.load_opus(_lib)
            break
        except Exception:
            continue
    if not discord.opus.is_loaded():
        log.warning("libopus not loaded; voice playback will fail")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True  # needed to resolve mentions/display names

engine = TTSEngine(MODEL_PATH, CONFIG_PATH)


# ---------- persistent state ----------
def save_state(data: dict, path: str = STATE_FILE) -> None:
    """Atomic: write temp file in same dir, fsync, os.replace."""
    dir_name = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile("w", dir=dir_name, suffix=".tmp", delete=False) as tf:
        temp_name = tf.name
        try:
            json.dump(data, tf, indent=2)
            tf.flush()
            os.fsync(tf.fileno())
        except BaseException:
            tf.close()
            os.unlink(temp_name)
            raise
    os.replace(temp_name, path)


class WatchStore:
    """guild_id -> set(user_id); mutations serialized by asyncio.Lock."""

    def __init__(self, path: str):
        self.path = path
        self.data: dict[int, set[int]] = {}
        self.lock = asyncio.Lock()
        try:
            with open(path) as f:
                raw = json.load(f)
            self.data = {int(g): {int(u) for u in us} for g, us in raw.get("watched", {}).items()}
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("state file unreadable; starting empty")

    def is_watched(self, guild_id: int, user_id: int) -> bool:
        return user_id in self.data.get(guild_id, ())

    def snapshot(self) -> dict:
        return {"watched": {str(g): sorted(u) for g, u in self.data.items() if u}}

    async def mutate(self, guild_id: int, op: str, user_id: int | None = None) -> None:
        async with self.lock:
            users = self.data.setdefault(guild_id, set())
            if op == "add":
                users.add(user_id)
            elif op == "remove":
                users.discard(user_id)
            elif op == "clear":
                users.clear()
            snap = self.snapshot()
            try:
                await asyncio.to_thread(save_state, snap, self.path)
            except Exception:
                log.exception("state save failed")


store = WatchStore(STATE_FILE)


# ---------- per-guild audio ----------
class GuildAudio:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_QUEUE)
        self.play_done = asyncio.Event()
        self.worker: asyncio.Task | None = None


class TTSBot(commands.Bot):
    async def setup_hook(self):
        try:
            await self.tree.sync()
        except Exception:
            log.exception("slash command sync failed")


bot = TTSBot(command_prefix=PREFIX, intents=intents, help_command=None)
audio: dict[int, GuildAudio] = {}
_shutting_down = False


def _rm(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def drain(q: asyncio.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            return


def ensure_audio(guild_id: int) -> GuildAudio:
    ga = audio.get(guild_id)
    if ga is None:
        ga = audio[guild_id] = GuildAudio(guild_id)
    if ga.worker is None or ga.worker.done():
        ga.worker = asyncio.create_task(audio_worker(ga), name=f"tts-worker-{guild_id}")
    return ga


async def wait_connected(guild: discord.Guild) -> discord.VoiceClient | None:
    """Tolerate transient voice reconnects (UDP drop / region move)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + RECONNECT_WAIT
    while loop.time() < end:
        vc = guild.voice_client
        if vc is None:
            return None
        if vc.is_connected():
            return vc
        await asyncio.sleep(0.5)
    return None


async def audio_worker(ga: GuildAudio) -> None:
    """One per guild. Survives errors/reconnects; only cancellation ends it."""
    loop = asyncio.get_running_loop()
    while True:
        text = await ga.queue.get()
        path = None
        try:
            guild = bot.get_guild(ga.guild_id)
            if guild is None or await wait_connected(guild) is None:
                continue
            path = await engine.synthesize(text)  # off-loop, GPU-serialized
            if not path:
                continue
            vc = await wait_connected(guild)
            if vc is None:
                continue
            ga.play_done.clear()

            def after(err, p=path):
                _rm(p)
                if err:
                    log.error("playback error: %s", err)
                loop.call_soon_threadsafe(ga.play_done.set)

            try:
                vc.play(discord.FFmpegPCMAudio(path, options="-loglevel error"), after=after)
            except Exception:
                log.exception("vc.play failed")
                continue  # path cleaned in finally
            path = None  # now owned by after()
            try:
                await asyncio.wait_for(ga.play_done.wait(), timeout=PLAY_TIMEOUT)
            except asyncio.TimeoutError:
                log.error("playback timeout; stopping")
                vc.stop()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker error (continuing)")
        finally:
            if path:
                _rm(path)


async def teardown(guild: discord.Guild) -> None:
    """Clear queue, cancel worker, stop playback, disconnect."""
    ga = audio.pop(guild.id, None)
    if ga:
        drain(ga.queue)
        if ga.worker:
            ga.worker.cancel()
    vc = guild.voice_client
    if vc:
        try:
            vc.stop()
            await vc.disconnect(force=True)
        except Exception:
            log.exception("disconnect failed for guild %s", guild.id)


# ---------- events ----------
@bot.check
async def only_tts_channel(ctx: commands.Context) -> bool:
    """Commands work only in the configured text channel."""
    if ctx.channel.id not in TTS_CHANNEL_IDS:
        raise commands.CheckFailure("wrong channel")
    return True


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s) prefix=%r channels=%s tts_ready=%s cuda=%s",
             bot.user, bot.user.id, PREFIX, sorted(TTS_CHANNEL_IDS), engine.ready, engine.cuda_active)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if message.channel.id not in TTS_CHANNEL_IDS:  # watch ONLY the configured channel
        return
    if message.content.startswith(PREFIX):
        await bot.process_commands(message)
        return
    if not store.is_watched(message.guild.id, message.author.id):
        return
    vc = message.guild.voice_client
    ga = audio.get(message.guild.id)
    if not vc or not ga:
        return
    text = clean_discord_text(message, MAX_CHARS)
    if not text:  # empty-result guard: never reaches queue/GPU
        return
    try:
        ga.queue.put_nowait(text)
    except asyncio.QueueFull:
        log.warning("queue full (guild %s); dropped message %s", message.guild.id, message.id)
        try:
            await message.add_reaction("⏳")
        except discord.HTTPException:
            pass


@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    vc = guild.voice_client
    if member.id == bot.user.id:
        if after.channel is None and guild.id in audio:  # kicked / dropped for good
            await teardown(guild)
        return
    if vc and vc.channel and before.channel == vc.channel and not any(not m.bot for m in vc.channel.members):
        log.info("auto-leave guild %s (empty)", guild.id)
        await store.mutate(guild.id, "clear")
        await teardown(guild)


# ---------- commands (work as !cmd and /cmd) ----------
@bot.hybrid_command(name="join", description="Join your voice channel")
@commands.guild_only()
async def join(ctx: commands.Context):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("Join a voice channel first.")
    channel = ctx.author.voice.channel
    perms = channel.permissions_for(ctx.guild.me)
    if not (perms.connect and perms.speak):
        return await ctx.send(f"Missing Connect/Speak permission in {channel.name}.")
    await ctx.defer()
    try:
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect()
    except discord.Forbidden:
        return await ctx.send("Forbidden: cannot join that channel.")
    except (discord.ClientException, asyncio.TimeoutError) as e:
        log.exception("join failed")
        return await ctx.send(f"Join failed: {e}")
    ensure_audio(ctx.guild.id)
    await ctx.send(f"Joined **{channel.name}**." + ("" if engine.ready else " TTS engine NOT loaded; check logs."))


@bot.hybrid_command(name="leave", description="Leave voice, clear queue and watch list")
@commands.guild_only()
async def leave(ctx: commands.Context):
    await store.mutate(ctx.guild.id, "clear")
    had_vc = ctx.voice_client is not None
    await teardown(ctx.guild)
    await ctx.send("Left. Queue cleared, watch list flushed." if had_vc else "Not in voice. Watch list flushed.")


@bot.hybrid_command(name="watch", description="Read this user's messages aloud")
@commands.guild_only()
async def watch(ctx: commands.Context, member: discord.Member):
    await store.mutate(ctx.guild.id, "add", member.id)
    await ctx.send(f"Watching **{member.display_name}**.")


@bot.hybrid_command(name="stop", description="Stop reading this user's messages")
@commands.guild_only()
async def stop(ctx: commands.Context, member: discord.Member):
    await store.mutate(ctx.guild.id, "remove", member.id)
    await ctx.send(f"Stopped watching **{member.display_name}**.")


@bot.hybrid_command(name="skip", description="Skip the clip currently playing")
@commands.guild_only()
async def skip(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.stop()  # fires after() -> worker event -> next clip
        await ctx.send("Skipped.")
    else:
        await ctx.send("Nothing playing.")


@bot.hybrid_command(name="status", description="Show watched users and voice state")
@commands.guild_only()
async def status(ctx: commands.Context):
    names = []
    for uid in sorted(store.data.get(ctx.guild.id, ())):
        m = ctx.guild.get_member(uid)
        names.append(m.display_name if m else str(uid))
    vc = ctx.voice_client
    voice = f"connected to **{vc.channel.name}**" if vc and vc.is_connected() else "not connected"
    ga = audio.get(ctx.guild.id)
    await ctx.send(
        f"Voice: {voice}\nWatched: {', '.join(names) or 'none'}\n"
        f"Queue: {ga.queue.qsize() if ga else 0}/{MAX_QUEUE} | TTS: {'ready' if engine.ready else 'unavailable'}"
        f" | CUDA: {'yes' if engine.cuda_active else 'no'}"
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        if ctx.interaction:  # slash: must answer or Discord shows "did not respond"
            mentions = " or ".join(f"<#{c}>" for c in sorted(TTS_CHANNEL_IDS))
            await ctx.send(f"Use TTS commands in {mentions}.", ephemeral=True)
        return
    if isinstance(error, (commands.MemberNotFound, commands.BadArgument, commands.MissingRequiredArgument)):
        return await ctx.send("Mention a valid member.")
    if isinstance(error, commands.NoPrivateMessage):
        return await ctx.send("Server only.")
    if isinstance(error, commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
        return await ctx.send("Missing permissions.")
    log.error("command %s failed: %r", ctx.command, error)


# ---------- lifecycle ----------
async def shutdown(b: commands.Bot) -> None:
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    log.info("Shutting down")
    for guild in list(b.guilds):
        if guild.voice_client or guild.id in audio:
            await teardown(guild)
    engine.close()
    await b.close()


async def main() -> None:
    if not TOKEN:
        raise SystemExit("No token: set ~/.config/tts-bot/token (see README)")
    if not TTS_CHANNEL_IDS:
        raise SystemExit("TTS_CHANNEL_ID not set (numeric text channel ID; comma-separate for several)")
    engine.purge_tmp()
    if not await asyncio.to_thread(engine.load):
        log.error("TTS engine failed to load; bot runs but cannot speak")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(bot)))
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
