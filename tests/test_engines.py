import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import engines
from config_store import ConfigStore, ConflictError
import kokoro_engine
from tts_engine import TTSEngine
from voices import BadVoiceError, VoiceLoadError


class FakeKokoro:
    """Stands in for engines.kokoro_engine; `cuda` and `fail` steer build()."""

    def __init__(self, cuda=True, fail=False):
        self.cuda, self.fail = cuda, fail

    def build(self, models_dir, name):
        if self.fail:
            raise VoiceLoadError()
        return SimpleNamespace(model_path="/m/kokoro.onnx", voice=name), 24000, self.cuda

    def synth(self, handle, text):
        return b"\x01\x00" * 10


class EngineRegistryTests(unittest.TestCase):
    def test_valid_engine(self):
        self.assertTrue(engines.valid_engine("piper"))
        self.assertTrue(engines.valid_engine("kokoro"))
        self.assertFalse(engines.valid_engine("evil"))
        self.assertFalse(engines.valid_engine(None))

    def test_kokoro_unavailable_without_files(self):
        with tempfile.TemporaryDirectory() as d:
            ok, why = kokoro_engine.available(d)
            self.assertFalse(ok)
            self.assertTrue(why)
            self.assertEqual(kokoro_engine.list_voices(d), [])
            with self.assertRaises(BadVoiceError):
                kokoro_engine.resolve(d, "bf_emma")

    def test_resolve_rejects_bad_names(self):
        for bad in (None, "", "../x", "BF_EMMA", "bf_emma/../x"):
            with self.assertRaises(BadVoiceError):
                kokoro_engine.resolve("/nonexistent", bad)


class ConfigEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.d.name, "config.json")

    def tearDown(self):
        self.d.cleanup()

    async def test_default_and_persist(self):
        c = ConfigStore(self.path)
        self.assertEqual(c.engine, "piper")
        snap = await c.set_voice("bf_emma", expected_rev=None, editor="a", engine="kokoro")
        self.assertEqual((snap["engine"], snap["voice"]), ("kokoro", "bf_emma"))
        self.assertEqual(snap["rev"], 1)
        c2 = ConfigStore(self.path)
        self.assertEqual((c2.engine, c2.voice), ("kokoro", "bf_emma"))

    async def test_old_file_without_engine_reads_piper(self):
        with open(self.path, "w") as f:
            json.dump({"rev": 3, "voice": "amy", "guilds": {}}, f)
        self.assertEqual(ConfigStore(self.path).engine, "piper")

    async def test_unknown_engine_in_file_falls_back(self):
        with open(self.path, "w") as f:
            json.dump({"rev": 1, "engine": "evil", "guilds": {}}, f)
        self.assertEqual(ConfigStore(self.path).engine, "piper")

    async def test_noop_and_conflict_and_bad_engine(self):
        c = ConfigStore(self.path)
        await c.set_voice("bf_emma", expected_rev=None, editor="a", engine="kokoro")
        again = await c.set_voice("bf_emma", expected_rev=None, editor="b", engine="kokoro")
        self.assertEqual(again["rev"], 1)
        with self.assertRaises(ConflictError):
            await c.set_voice(None, expected_rev=0, editor="a", engine="piper")
        with self.assertRaises(ValueError):
            await c.set_voice(None, expected_rev=None, editor="a", engine="evil")
        self.assertEqual(c.engine, "kokoro")

    async def test_persist_failure_rolls_back_engine(self):
        c = ConfigStore(self.path)
        with mock.patch("config_store.atomic_write_json", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                await c.set_voice("bf_emma", expected_rev=None, editor="a", engine="kokoro")
        self.assertEqual((c.engine, c.voice, c.rev), ("piper", None, 0))


class EngineSwapTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        e = TTSEngine("/m/a.onnx")
        e.ready = True  # pretend a Piper voice is loaded
        return e

    async def test_swap_to_kokoro_then_synth_dispatch(self):
        e = self.make()
        fake = FakeKokoro(cuda=False)
        with mock.patch.object(engines, "get", return_value=fake):
            await e.reload_engine("kokoro", "/m", "bf_emma")
            self.assertEqual(e.engine_name, "kokoro")
            self.assertEqual(e.model_name, "bf_emma")
            self.assertEqual(e.sample_rate, 24000)
            self.assertEqual(e._synth("hi"), b"\x01\x00" * 10)
        e.close()

    async def test_failure_keeps_previous(self):
        e = self.make()
        before = e.state
        with mock.patch.object(engines, "get", return_value=FakeKokoro(fail=True)):
            with self.assertRaises(VoiceLoadError):
                await e.reload_engine("kokoro", "/m", "bf_emma")
        self.assertIs(e.state, before)
        self.assertFalse(e.loading)
        e.close()

    async def test_refuses_gpu_to_cpu_downgrade(self):
        e = self.make()
        object.__setattr__(e._state, "cuda_active", True)
        with mock.patch.object(engines, "get", return_value=FakeKokoro(cuda=False)):
            with self.assertRaises(VoiceLoadError):
                await e.reload_engine("kokoro", "/m", "bf_emma")
        self.assertEqual(e.engine_name, "piper")
        e.close()


if __name__ == "__main__":
    unittest.main()


class IdleUnloadTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        e = TTSEngine("/m/a.onnx")
        e.ready = True
        e._state = type(e._state)(voice=object(), sample_rate=22050, model_path=Path("/m/a.onnx"),
                                  config_path=Path("/m/a.onnx.json"), cuda_active=True)
        return e

    async def test_unload_then_resume_on_synth(self):
        e = self.make()
        built = []
        e._build = lambda m, c: (built.append(m) or object(), True)
        e._synth_with = staticmethod(lambda v, t: b"\x01\x00" * 4)
        e._unload_blocking()
        self.assertTrue(e.idle)
        self.assertIsNone(e.state.voice)
        self.assertEqual(e.model_name, "a")  # state survives for the GUI
        self.assertEqual(e._synth("hi"), b"\x01\x00" * 4)
        self.assertFalse(e.idle)
        self.assertEqual(built, [Path("/m/a.onnx")])
        e.close()

    async def test_loop_unloads_only_after_timeout(self):
        e = self.make()
        task = asyncio.create_task(e.idle_unload_loop(timeout=0.05))
        await asyncio.sleep(0.01)
        self.assertFalse(e.idle)
        await asyncio.sleep(0.2)
        self.assertTrue(e.idle)
        task.cancel()
        e.close()

    async def test_zero_disables(self):
        e = self.make()
        await asyncio.wait_for(e.idle_unload_loop(timeout=0), 1)
        self.assertFalse(e.idle)
        e.close()
