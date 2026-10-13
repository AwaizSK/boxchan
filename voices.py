"""Piper voice discovery and shared voice errors. Pure stdlib; imported by tts_engine, bot and webui.

A "voice" is `<stem>.onnx` plus `<stem>.onnx.json` in the models directory. The stem is the only
identifier that ever crosses the API; paths are rebuilt server-side from a discovered entry."""
import json
import logging
import os
import re
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


def _entry(models_dir: str, stem: str) -> dict | None:
    onnx = os.path.join(models_dir, stem + ".onnx")
    cfg_path = onnx + ".json"

    # Check for symlinks: skip if either .onnx or .onnx.json is a symlink
    if os.path.islink(onnx) or os.path.islink(cfg_path):
        log.debug("voice %s is a symlink; skipped", stem)
        return None

    # Check realpath is inside models dir: both files must resolve within the models directory
    models_real = os.path.realpath(models_dir)
    if not os.path.isfile(onnx) or not os.path.isfile(cfg_path):
        log.debug("voice %s has missing files; skipped", stem)
        return None

    onnx_real = os.path.realpath(onnx)
    cfg_real = os.path.realpath(cfg_path)
    if not (onnx_real.startswith(models_real + os.sep) or onnx_real == os.path.join(models_real, stem + ".onnx")):
        log.debug("voice %s .onnx outside models dir; skipped", stem)
        return None
    if not (cfg_real.startswith(models_real + os.sep) or cfg_real == os.path.join(models_real, stem + ".onnx.json")):
        log.debug("voice %s .onnx.json outside models dir; skipped", stem)
        return None
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
    try:
        size = os.path.getsize(onnx)
    except OSError:
        return None
    return {"name": stem, "sample_rate": rate, "quality": quality, "language": lang,
            "size_mb": round(size / (1024 * 1024), 1)}


def discover_voices(models_dir) -> list[dict]:
    """Blocking. Every installed single-speaker voice, sorted by name. Never raises.
    Skips symlinked .onnx files and any voice whose .onnx or .onnx.json resolve outside
    the models directory."""
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
        target_path = os.path.join(models_dir, fname)

        # Skip symlinked .onnx files
        if os.path.islink(target_path):
            log.debug("voice %s is a symlink; skipped", stem)
            continue

        if not os.path.isfile(target_path):
            continue

        # Additional realpath check: ensure .onnx resolves inside the models directory
        target_real = os.path.realpath(target_path)
        if not (target_real.startswith(models_real + os.sep) or target_real == os.path.join(models_real, fname)):
            log.debug("voice %s resolves outside models dir; skipped", stem)
            continue

        try:
            entry = _entry(models_dir, stem)
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
    for e in discover_voices(models_dir):
        if e["name"] == name:
            base = Path(os.fspath(models_dir)) / (e["name"] + ".onnx")
            return base, Path(str(base) + ".json")
    raise BadVoiceError("unknown voice")
