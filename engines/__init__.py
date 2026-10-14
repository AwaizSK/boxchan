"""Pluggable TTS backends. Piper lives in tts_engine.py (the default); optional engines live here.
An engine module exposes available(models_dir), list_voices(models_dir), resolve(models_dir, name),
build(models_dir, name) and synth(handle, text). Importing this package never imports a heavy dependency."""
import importlib

ENGINE_NAMES = ("piper", "kokoro")
DEFAULT_ENGINE = "piper"


def valid_engine(name) -> bool:
    return isinstance(name, str) and name in ENGINE_NAMES


def get(name: str):
    """Optional-engine module (None for piper, which tts_engine.py owns)."""
    if name == "kokoro":
        return importlib.import_module("engines.kokoro_engine")
    return None
