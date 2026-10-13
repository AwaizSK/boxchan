"""Piper voice discovery and shared voice errors. Pure stdlib; imported by tts_engine, bot and webui.

A "voice" is `<stem>.onnx` plus `<stem>.onnx.json` in the models directory. The stem is the only
identifier that ever crosses the API; paths are rebuilt server-side from a discovered entry."""
import json
import logging
import os
import re
import stat
from pathlib import Path

log = logging.getLogger("voices")

VOICE_NAME_RE = re.compile(r"[A-Za-z0-9_.+-]{1,100}")
QUALITIES = ("x_low", "low", "medium", "high")
MAX_JSON_BYTES = 1 << 20  # a Piper config is a few KB; refuse to parse anything absurd


class VoiceLoadError(Exception):
    """A voice could not be loaded. The message is safe to show to clients (no paths, no traces)."""

    def __init__(self, message: str = "voice failed to load"):
        super().__init__(message)


class BadVoiceError(ValueError):
    """The requested voice name does not match any installed voice."""


class SayError(Exception):
    """Test-phrase request refused. `code` is a stable machine-readable reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def valid_name(name) -> bool:
    return isinstance(name, str) and VOICE_NAME_RE.fullmatch(name) is not None and not name.startswith(".")


def _read_config(path: str) -> dict | None:
    try:
        if os.path.getsize(path) > MAX_JSON_BYTES:
            log.debug("voice config too large: %s", os.path.basename(path))
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):  # ValueError covers JSONDecodeError and UnicodeDecodeError
        log.debug("unreadable voice config: %s", os.path.basename(path), exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _checked_files(models_dir: str, models_real: str, stem: str) -> tuple[str, str, int] | None:
    """THE voice-file validation, shared by discover_voices and resolve_voice. Returns
    (onnx_path, config_path, onnx_size) or None. Rejects a symlinked .onnx, a symlinked .onnx.json,
    anything that is not a regular file, and anything whose realpath is not directly inside the
    realpath of the models dir (which also catches a symlinked parent component). One lstat and one
    realpath per file."""
    onnx = os.path.join(models_dir, stem + ".onnx")
    cfg = onnx + ".json"
    size = 0
    for path in (onnx, cfg):
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            log.debug("voice %s uses a symlink; skipped", stem)
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        if os.path.realpath(path) != os.path.join(models_real, os.path.basename(path)):
            log.debug("voice %s resolves outside the models dir; skipped", stem)
            return None
        if path is onnx:
            size = st.st_size
    return onnx, cfg, size


def _entry(models_dir: str, models_real: str, stem: str) -> dict | None:
    checked = _checked_files(models_dir, models_real, stem)
    if checked is None:
        return None
    onnx, cfg_path, size = checked
    cfg = _read_config(cfg_path)
    if cfg is None:
        return None
    audio = cfg.get("audio")
    rate = audio.get("sample_rate") if isinstance(audio, dict) else None
    if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
        log.debug("voice %s has no usable audio.sample_rate; skipped", stem)
        return None
    speakers = cfg.get("num_speakers", 1)
    if isinstance(speakers, bool) or not isinstance(speakers, int):
        log.debug("voice %s has a malformed num_speakers; skipped", stem)
        return None
    if speakers > 1:
        log.debug("voice %s is multi-speaker; skipped (not supported)", stem)
        return None
    quality = audio.get("quality")
    if not isinstance(quality, str) or not quality:
        quality = next((q for q in QUALITIES if stem.endswith("-" + q)), None)
    espeak = cfg.get("espeak")
    lang = espeak.get("voice") if isinstance(espeak, dict) else None
    if not isinstance(lang, str) or not lang:
        lang = stem.split("-", 1)[0] if "-" in stem else None
    return {"name": stem, "sample_rate": rate, "quality": quality, "language": lang,
            "size_mb": round(size / (1024 * 1024), 1)}


def discover_voices(models_dir) -> list[dict]:
    """Blocking. Every installed single-speaker voice, sorted by name. Never raises.
    Each voice goes through `_checked_files` (symlinks and escapes rejected)."""
    models_dir = os.fspath(models_dir)
    out = []
    try:
        names = sorted(os.listdir(models_dir))
    except OSError:
        log.debug("models dir not readable", exc_info=True)
        return []

    models_real = os.path.realpath(models_dir)
    for fname in names:
        if not fname.endswith(".onnx"):
            continue
        stem = fname[:-len(".onnx")]
        if not valid_name(stem):
            log.debug("skipping voice with unsafe file name")
            continue
        try:
            entry = _entry(models_dir, models_real, stem)
        except Exception:  # defensive: one odd file must not hide the others
            log.debug("could not inspect voice %s", stem, exc_info=True)
            continue
        if entry:
            out.append(entry)
    out.sort(key=lambda e: e["name"])
    return out


def resolve_voice(models_dir, name) -> tuple[Path, Path]:
    """(onnx_path, json_path) for an installed voice. ValueError unless `name` exactly equals a
    discovered entry: the path is built from the discovered stem, never from raw input."""
    if not valid_name(name):
        raise BadVoiceError("unknown voice")
    models_dir = os.fspath(models_dir)
    try:
        entry = _entry(models_dir, os.path.realpath(models_dir), name)
    except Exception:
        log.debug("could not inspect voice %s", name, exc_info=True)
        entry = None
    if entry is None:
        raise BadVoiceError("unknown voice")
    onnx = Path(models_dir) / (name + ".onnx")
    return onnx, Path(str(onnx) + ".json")
