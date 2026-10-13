"""Piper TTS wrapper (ONNX Runtime, CUDA when available, FP32) + Discord text cleaner."""
import asyncio
import gc
import logging
import re
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import discord

from voices import VoiceLoadError

log = logging.getLogger("tts_engine")

RE_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
RE_CUSTOM_EMOJI = re.compile(r"<a?:([a-zA-Z0-9_]+):\d+>")
RE_USER_MENTION = re.compile(r"<@!?(\d+)>")
RE_ROLE_MENTION = re.compile(r"<@&(\d+)>")
RE_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
RE_TIMESTAMP = re.compile(r"<t:-?\d+(?::[a-zA-Z])?>")
RE_CODE_BLOCK = re.compile(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```", re.DOTALL)
RE_INLINE_CODE = re.compile(r"`([^`]+)`")
RE_SPOILER = re.compile(r"\|\|(.*?)\|\|", re.DOTALL)
RE_MD_CHARS = re.compile(r"[*_~>#`|]")
RE_EXCESSIVE_PUNCT = re.compile(r"([!?,.:;~])\1{2,}")
RE_REPEAT_LETTER = re.compile(r"([^\W\d_])\1{3,}")
RE_SPACES = re.compile(r"\s+")


def _normalize(text: str, max_chars: int, user, role, channel) -> str | None:
    """Shared speech-cleaning pipeline; `user`/`role`/`channel` resolve mention regex matches."""
    text = RE_URL.sub("link", text)
    text = RE_CODE_BLOCK.sub("code block", text)
    text = RE_INLINE_CODE.sub(r"\1", text)
    text = RE_SPOILER.sub(r"\1", text)  # preference: read spoilers, drop the pipes
    text = RE_USER_MENTION.sub(user, text)
    text = RE_ROLE_MENTION.sub(role, text)
    text = RE_CHANNEL_MENTION.sub(channel, text)
    text = RE_CUSTOM_EMOJI.sub(r"\1", text)
    text = RE_TIMESTAMP.sub("timestamp", text)
    text = RE_MD_CHARS.sub(" ", text)
    text = RE_EXCESSIVE_PUNCT.sub(r"\1", text)
    text = RE_REPEAT_LETTER.sub(r"\1\1\1", text)
    text = RE_SPACES.sub(" ", text).strip()

    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "..."
    if not any(c.isalnum() for c in text):
        return None
    return text


def clean_discord_text(message: discord.Message, max_chars: int = 300) -> str | None:
    """Normalize a Discord message for speech. None if nothing speakable remains."""
    text = message.content
    guild = message.guild
    if not text or not text.strip():
        return None

    def user(m):
        member = guild.get_member(int(m.group(1))) if guild else None
        return f"at {member.display_name}" if member else "someone"

    def role(m):
        r = guild.get_role(int(m.group(1))) if guild else None
        return f"at {r.name}" if r else "role"

    def channel(m):
        c = guild.get_channel(int(m.group(1))) if guild else None
        return f"channel {c.name}" if c else "channel"

    return _normalize(text, max_chars, user, role, channel)


def clean_plain_text(text: str, max_chars: int = 300) -> str | None:
    """Same pipeline for plain text (no Discord objects): mentions become generic words."""
    if not isinstance(text, str) or not text.strip():
        return None
    return _normalize(text, max_chars, lambda m: "someone", lambda m: "role", lambda m: "channel")


class TTSEngine:
    """Single GPU context: one worker thread + asyncio.Lock serialize all inference."""

    def __init__(self, model_path: str, config_path: str | None = None):
        self.model_path = Path(model_path)
        self.config_path = Path(config_path) if config_path else Path(str(model_path) + ".json")
        self.voice = None
        self.sample_rate = 22050
        self.cuda_active = False
        self.ready = False
        self._pending_reloads = 0
        self.tmp_dir = Path(tempfile.gettempdir()) / "tts_bot"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="piper")
        self._lock: asyncio.Lock | None = None

    @property
    def loading(self) -> bool:
        """True while a reload() is queued or running."""
        return self._pending_reloads > 0

    @property
    def model_name(self) -> str:
        """Stem of the loaded (or configured) model file."""
        return self.model_path.stem

    def purge_tmp(self) -> None:
        for f in self.tmp_dir.glob("*.wav"):
            try:
                f.unlink()
            except OSError:
                pass

    @staticmethod
    def _backend():
        """(available onnxruntime providers, PiperVoice). Isolated so tests can fake it."""
        import onnxruntime as ort
        from piper import PiperVoice
        return ort.get_available_providers(), PiperVoice

    def _build(self, model_path: Path, config_path: Path):
        """Blocking; worker thread (or load()'s caller). Build and warm up a PiperVoice: CUDA when
        available, CPU fallback. Returns (voice, cuda_used); raises if no provider works."""
        providers, PiperVoice = self._backend()
        voice = None
        cuda = False
        if "CUDAExecutionProvider" in providers:
            try:
                voice = PiperVoice.load(str(model_path), config_path=str(config_path), use_cuda=True)
                self._synth_with(voice, "warm up")
                cuda = True
            except Exception:
                log.exception("CUDA init failed; falling back to CPU")
                voice = None
        else:
            log.error("CUDAExecutionProvider unavailable (%s); using CPU", providers)
        if voice is None:
            voice = PiperVoice.load(str(model_path), config_path=str(config_path), use_cuda=False)
            self._synth_with(voice, "warm up")
        return voice, cuda

    def load(self, model_path=None, config_path=None) -> bool:
        """Never raises. False (logged) if model cannot load. Optional paths replace the configured ones."""
        if model_path is not None:
            self.model_path = Path(model_path)
            self.config_path = Path(config_path) if config_path else Path(str(model_path) + ".json")
        for p in (self.model_path, self.config_path):
            if not p.is_file():
                log.error("Model file missing: %s", p)
                return False
        try:
            voice, cuda = self._build(self.model_path, self.config_path)
        except Exception:
            log.exception("model load failed")
            return False
        self.voice, self.cuda_active = voice, cuda
        self.sample_rate = voice.config.sample_rate
        self.ready = True
        log.info("Piper ready (model=%s, cuda=%s, rate=%d)", self.model_name, self.cuda_active, self.sample_rate)
        return True

    @staticmethod
    def _synth_with(voice, text: str) -> bytes:
        return b"".join(voice.synthesize_stream_raw(text))

    def _synth(self, text: str) -> bytes:
        return self._synth_with(self.voice, text)

    def _reload_blocking(self, model_path: Path, config_path: Path) -> None:
        """Worker thread. Build + warm up the candidate; swap only on success."""
        for p in (model_path, config_path):
            if not p.is_file():
                log.error("Model file missing: %s", p)
                raise VoiceLoadError("voice files not found")
        try:
            voice, cuda = self._build(model_path, config_path)
            rate = voice.config.sample_rate
        except VoiceLoadError:
            raise
        except Exception as e:
            log.exception("voice reload failed; keeping the current voice")
            raise VoiceLoadError() from e
        old = self.voice
        # Single step on the only inference thread: no synthesis can observe a half-swapped engine.
        self.voice, self.sample_rate, self.cuda_active = voice, rate, cuda
        self.model_path, self.config_path, self.ready = model_path, config_path, True
        del old
        gc.collect()  # drop the old ORT session now so its (GPU) memory is released
        log.info("Piper voice swapped (model=%s, cuda=%s, rate=%d)", self.model_name, cuda, rate)

    async def reload(self, model_path, config_path) -> None:
        """Hot-swap the voice. Serialized with synthesis (same lock, same single worker thread).
        On any failure raises VoiceLoadError and the previous voice keeps serving."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        self._pending_reloads += 1
        fut = None
        try:
            async with self._lock:
                fut = asyncio.get_running_loop().run_in_executor(
                    self._pool, self._reload_blocking, Path(model_path), Path(config_path))
                fut.add_done_callback(self._reload_done)
                # shield: if our caller is cancelled the job still finishes (and is counted until then)
                await asyncio.shield(fut)
        except (VoiceLoadError, asyncio.CancelledError):
            raise
        except Exception as e:  # e.g. pool already shut down
            log.exception("voice reload could not run")
            raise VoiceLoadError() from e
        finally:
            if fut is None:
                self._pending_reloads -= 1

    def _reload_done(self, fut) -> None:
        self._pending_reloads -= 1
        if not fut.cancelled():
            fut.exception()  # mark retrieved when nobody awaits it any more

    def _synth_to_wav(self, text: str) -> str | None:
        try:
            pcm = self._synth(text)
            if not pcm:
                return None
            with tempfile.NamedTemporaryFile(dir=self.tmp_dir, suffix=".wav", delete=False) as tf:
                path = tf.name
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.sample_rate)
                w.writeframes(pcm)
            return path
        except Exception:
            log.exception("synthesis failed")
            return None

    async def synthesize(self, text: str) -> str | None:
        """Off-loop, strictly serialized. Returns temp .wav path (caller deletes) or None."""
        if not self.ready or not text.strip():
            return None
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(self._pool, self._synth_to_wav, text)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.purge_tmp()
