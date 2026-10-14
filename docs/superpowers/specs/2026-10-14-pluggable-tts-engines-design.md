# Pluggable TTS engines (Piper + Kokoro) — design

## Intent
Piper voices sound robotic to the owner. Kokoro-82M (ONNX) sounds far more natural and has British
female voices (`bf_emma`, `bf_isabella`). Add it as a second engine, selectable at runtime from the
web UI, and keep Piper fully working so the owner can revert by flipping a toggle.

## Goals
- Engine backends are separate modules behind one small interface; adding or removing one touches
  no other module.
- Global `engine` setting (`piper` | `kokoro`) in `config.json`, changed only via `ConfigStore.set_engine`.
- Voice tab: engine toggle above the voice list; list shows only the active (or selected) engine's voices.
- Apply loads engine + voice together with the existing hot-swap guarantees (old one keeps serving on failure).
- Piper behaviour, files and tests keep working unchanged.

## Non-goals / anti-goals
- No per-guild engine. No multi-speaker Piper. No streaming synthesis. No new executor or thread.
- A missing Kokoro dependency or model must never break Piper or the bot (fail closed: Kokoro shows
  "unavailable", Piper stays active).
- Installing the Kokoro dependency in prod is a separate, explicitly approved step (CLAUDE.md rule).

## Constraints (from CLAUDE.md)
- All inference and (re)loading on the single `TTSEngine` worker thread (`max_workers=1` + lock).
- State changes via `ConfigStore` only; `webui.py` talks to `Controller` only; frontend vanilla rules
  are superseded by the existing React build (`frontend/` -> `static/`), keep it as is.
- Strict wire format: IDs strings; `rev` optimistic concurrency (409) preserved.
- Importing `bot.py` has side effects; tests set env first.

## Design
### `engines/` package
- `engines/__init__.py`: registry `ENGINES = {"piper": ..., "kokoro": ...}`, `get(name)`; kokoro import is lazy
  so a missing package only marks it unavailable.
- Interface (module-level functions or small class per backend):
  - `available() -> (bool, reason)`: dependency + model files present.
  - `list_voices(models_dir) -> list[{name, ...}]`.
  - `resolve(models_dir, name) -> VoiceRef` (raises `BadVoiceError`).
  - `default_ref() -> VoiceRef`.
  - `build(ref) -> Loaded(handle, sample_rate, cuda_active, label)`: blocking; CUDA first, CPU fallback; warm-up.
  - `synth(handle, text) -> bytes` (16-bit mono PCM).
- `engines/piper_engine.py`: current `_backend/_build/_synth_with` logic moved here, plus existing `discover_voices` use.
- `engines/kokoro_engine.py`: `kokoro-onnx` (`Kokoro(model, voices)`, `create(text, voice, speed, lang)`); float32
  samples converted to int16 PCM at 24000 Hz. Files under `PIPER_MODELS_DIR/kokoro/` (`kokoro.onnx`, `voices.bin`).
  Voice list from the voices file keys; names like `bf_emma`. Voice names validated against that list. GB voices
  use lang `en-gb`.

### `TTSEngine`
- Holds `engine_name` plus `VoiceState` (adds `engine`). `reload(engine, ref)` replaces `reload(model_path, config_path)`;
  a thin compatibility wrapper keeps the old signature for Piper so existing tests/callers work.
- Hot swap order unchanged: build candidate on worker thread, refuse silent GPU->CPU downgrade, swap in one step, gc old.

### Config / Controller / API
- `ConfigStore`: new global field `engine` (default `"piper"`), `set_engine`, included in snapshot; same
  rev/no-op/rollback semantics as `set_voice`. Old config files without the field read as `piper`.
- `Controller.apply_voice(name, engine, expected_rev, editor)`: resolve in target engine -> rev check (409) ->
  `engine.reload` (422 on failure, old keeps serving) -> persist engine and voice together (new
  `ConfigStore.set_voice_and_engine`, one rev bump, one event). OSError -> revert engine (500).
- `GET /api/voices?engine=` lists that engine's voices (default: active); response includes `engines`:
  `[{name, available, reason}]`. `PUT /api/voice` body gains optional `engine` (default: current).
- Boot: load engine/voice from config; if Kokoro was configured but is unavailable, log and fall back to Piper
  default without rewriting config.

### Frontend (`frontend/src`)
- `VoiceTab`: segmented toggle (Piper / Kokoro; unavailable one disabled with reason). Selecting an engine
  fetches its list; Apply sends `{engine, name, rev}`. Status text shows engine + voice + CUDA/CPU.
  Fix: "Active" badge follows the loaded voice, not the selection.
- Failure toast shows server error code. Rebuild `static/` via `npm run build` and commit.

## Tests
unittest, fakes for both backends (no GPU/Discord): registry/availability, engine reload success/failure/
downgrade refusal, ConfigStore engine persistence + old-file default, Controller ordering and rollback, route
validation, existing suites untouched. State plainly that no live GPU/Kokoro/browser test ran.

## Left to the builder
Exact file/function names inside `engines/`; Kokoro model download URL and `scripts/install_voice.sh`
extension for Kokoro files (calls: install script accepts `--engine kokoro <model> <voices>`).

## Builder-check calls (mine)
- Kokoro files in `models/kokoro/`; engine global; voice names for Kokoro unprefixed (`bf_emma`).
- Prod dependency install (`kokoro-onnx`) only after the owner approves, via a documented one-time step.
