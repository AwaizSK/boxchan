"""Piper TTS wrapper (ONNX Runtime, CUDA when available, FP32) + Discord text cleaner."""
import asyncio
import logging
import re
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import discord

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


def clean_discord_text(message: discord.Message, max_chars: int = 300) -> str | None:
    """Normalize a Discord message for speech. None if nothing speakable remains."""
    text = message.content
    guild = message.guild
    if not text or not text.strip():
        return None

    text = RE_URL.sub("link", text)
    text = RE_CODE_BLOCK.sub("code block", text)
    text = RE_INLINE_CODE.sub(r"\1", text)
    text = RE_SPOILER.sub(r"\1", text)  # preference: read spoilers, drop the pipes

    def user(m):
        member = guild.get_member(int(m.group(1))) if guild else None
        return f"at {member.display_name}" if member else "someone"

    def role(m):
        r = guild.get_role(int(m.group(1))) if guild else None
        return f"at {r.name}" if r else "role"

    def channel(m):
        c = guild.get_channel(int(m.group(1))) if guild else None
        return f"channel {c.name}" if c else "channel"

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


class TTSEngine:
    """Single GPU context: one worker thread + asyncio.Lock serialize all inference."""

    def __init__(self, model_path: str, config_path: str | None = None):
        self.model_path = Path(model_path)
        self.config_path = Path(config_path) if config_path else Path(str(model_path) + ".json")
        self.voice = None
        self.sample_rate = 22050
        self.cuda_active = False
        self.ready = False
        self.tmp_dir = Path(tempfile.gettempdir()) / "tts_bot"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="piper")
        self._lock: asyncio.Lock | None = None

    def purge_tmp(self) -> None:
        for f in self.tmp_dir.glob("*.wav"):
            try:
                f.unlink()
            except OSError:
                pass

    def load(self) -> bool:
        """Never raises. False (logged) if model cannot load."""
        try:
            import onnxruntime as ort
            from piper import PiperVoice
        except Exception:
            log.exception("piper/onnxruntime import failed")
            return False
        for p in (self.model_path, self.config_path):
            if not p.is_file():
                log.error("Model file missing: %s", p)
                return False

        if "CUDAExecutionProvider" in ort.get_available_providers():
            try:
                self.voice = PiperVoice.load(str(self.model_path), config_path=str(self.config_path), use_cuda=True)
                self._synth("warm up")
                self.cuda_active = True
            except Exception:
                log.exception("CUDA init failed; falling back to CPU")
                self.voice = None
        else:
            log.error("CUDAExecutionProvider unavailable (%s); using CPU", ort.get_available_providers())

        if self.voice is None:
            try:
                self.voice = PiperVoice.load(str(self.model_path), config_path=str(self.config_path), use_cuda=False)
            except Exception:
                log.exception("CPU model load failed")
                return False

        self.sample_rate = self.voice.config.sample_rate
        self.ready = True
        log.info("Piper ready (cuda=%s, rate=%d)", self.cuda_active, self.sample_rate)
        return True

    def _synth(self, text: str) -> bytes:
        return b"".join(self.voice.synthesize_stream_raw(text))

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
