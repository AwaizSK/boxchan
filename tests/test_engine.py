import asyncio
import gc
import logging
import os
import tempfile
import threading
import unittest
import wave
import weakref
from types import SimpleNamespace

import tts_engine
from tts_engine import TTSEngine, clean_plain_text
from voices import VoiceLoadError


class FakeVoice:
    def __init__(self, tag, rate, gate=None, fail_synth=False, log=None):
        self.tag, self.config = tag, SimpleNamespace(sample_rate=rate)
        self.gate, self.fail_synth, self.log = gate, fail_synth, log if log is not None else []

    def synthesize_stream_raw(self, text):
        if self.fail_synth:
            raise RuntimeError("cuda exploded at /secret/path")
        self.log.append(("synth", self.tag, text))
        if self.gate is not None and text != "warm up":
            self.gate.wait(5)
        yield b"\x01\x00" * 50


class FakePiper:
    """Stands in for piper.PiperVoice; `plan` maps model file stem -> behaviour."""

    def __init__(self):
        self.plan, self.calls, self.log = {}, [], []

    def load(self, model_path, config_path=None, use_cuda=False):
        stem = os.path.basename(model_path).split(".")[0]
        self.calls.append((stem, use_cuda))
        p = self.plan.get(stem, {})
        if p.get("gate_load"):
            p["gate_load"].wait(5)
        if use_cuda and p.get("cuda_fails"):
            raise RuntimeError("no cuda")
        if p.get("load_fails") or (not use_cuda and p.get("cpu_fails")):
            raise RuntimeError(f"cannot load {model_path}")
        return FakeVoice(stem, p.get("rate", 22050), p.get("gate"), p.get("fail_synth", False), self.log)


class EngineBase(unittest.IsolatedAsyncioTestCase):
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.piper = FakePiper()
        for stem in ("a", "b", "c"):
            for suffix in (".onnx", ".onnx.json"):
                open(os.path.join(self.tmp.name, stem + suffix), "w").close()
        self.engine = TTSEngine(self.p("a"), self.p("a") + ".json")
        self.engine._backend = lambda: (self.providers, self.piper)
        self.addCleanup(self.engine.close)

    def p(self, stem):
        return os.path.join(self.tmp.name, stem + ".onnx")

    def load_a(self):
        with self.assertLogs("tts_engine", "INFO"):
            self.assertTrue(self.engine.load())

    @staticmethod
    def rate_of(path):
        with wave.open(path) as w:
            r = w.getframerate()
        os.unlink(path)
        return r


class LoadTests(EngineBase):
    def test_load_cuda_then_state(self):
        self.load_a()
        self.assertTrue(self.engine.ready and self.engine.cuda_active)
        self.assertEqual(self.engine.model_name, "a")
        self.assertEqual(self.piper.calls, [("a", True)])

    def test_load_cpu_fallback_when_cuda_init_fails(self):
        self.piper.plan["a"] = {"cuda_fails": True}
        with self.assertLogs("tts_engine", "INFO") as cm:
            self.assertTrue(self.engine.load())
        self.assertFalse(self.engine.cuda_active)
        self.assertEqual(self.piper.calls, [("a", True), ("a", False)])
        self.assertTrue(any("falling back to CPU" in m for m in cm.output))

    def test_load_cpu_when_provider_missing(self):
        self.providers = ["CPUExecutionProvider"]
        with self.assertLogs("tts_engine", "INFO"):
            self.assertTrue(self.engine.load())
        self.assertEqual(self.piper.calls, [("a", False)])
        self.assertFalse(self.engine.cuda_active)

    def test_load_failure_returns_false(self):
        self.piper.plan["a"] = {"load_fails": True}
        with self.assertLogs("tts_engine", "ERROR"):
            self.assertFalse(self.engine.load())
        self.assertFalse(self.engine.ready)

    def test_load_missing_file_false(self):
        with self.assertLogs("tts_engine", "ERROR"):
            self.assertFalse(self.engine.load(os.path.join(self.tmp.name, "zz.onnx")))

    def test_load_with_explicit_paths(self):
        with self.assertLogs("tts_engine", "INFO"):
            self.assertTrue(self.engine.load(self.p("b"), self.p("b") + ".json"))
        self.assertEqual(self.engine.model_name, "b")


class ReloadTests(EngineBase):
    async def test_success_swaps_everything_and_frees_old(self):
        self.load_a()
        self.assertEqual(self.engine.sample_rate, 22050)
        old_ref = weakref.ref(self.engine.voice)
        self.piper.plan["b"] = {"rate": 16000}
        with self.assertLogs("tts_engine", "INFO"):
            await self.engine.reload(self.p("b"), self.p("b") + ".json")
        e = self.engine
        self.assertEqual((e.voice.tag, e.sample_rate, e.model_name, e.ready), ("b", 16000, "b", True))
        self.assertEqual(str(e.config_path), self.p("b") + ".json")
        self.assertFalse(e.loading)
        gc.collect()
        self.assertIsNone(old_ref(), "old voice must be released after the swap")
        path = await e.synthesize("hi")
        self.assertEqual(self.rate_of(path), 16000)

    async def test_failure_keeps_old_voice_serving(self):
        self.load_a()
        old = self.engine.voice
        self.piper.plan["b"] = {"load_fails": True}
        with self.assertLogs("tts_engine", "ERROR"):
            with self.assertRaises(VoiceLoadError) as cm:
                await self.engine.reload(self.p("b"), self.p("b") + ".json")
        self.assertNotIn(self.tmp.name, str(cm.exception))
        self.assertNotIn("cannot load", str(cm.exception))
        e = self.engine
        self.assertIs(e.voice, old)
        self.assertEqual((e.model_name, e.sample_rate, e.ready, e.loading), ("a", 22050, True, False))
        self.assertTrue(e.cuda_active)
        self.assertEqual(self.rate_of(await e.synthesize("still me")), 22050)

    async def test_warmup_failure_keeps_old_voice(self):
        self.load_a()
        old = self.engine.voice
        self.piper.plan["b"] = {"fail_synth": True}
        with self.assertLogs("tts_engine", "ERROR"):
            with self.assertRaises(VoiceLoadError) as cm:
                await self.engine.reload(self.p("b"), self.p("b") + ".json")
        self.assertNotIn("secret", str(cm.exception))
        self.assertIs(self.engine.voice, old)
        self.assertEqual(self.engine.model_name, "a")

    async def test_missing_files_fail_safely(self):
        self.load_a()
        with self.assertLogs("tts_engine", "ERROR"):
            with self.assertRaises(VoiceLoadError) as cm:
                await self.engine.reload(os.path.join(self.tmp.name, "gone.onnx"),
                                         os.path.join(self.tmp.name, "gone.onnx.json"))
        self.assertNotIn(self.tmp.name, str(cm.exception))
        self.assertEqual(self.engine.model_name, "a")

    async def test_reload_recovers_an_engine_that_never_loaded(self):
        self.piper.plan["a"] = {"load_fails": True}
        with self.assertLogs("tts_engine", "ERROR"):
            self.assertFalse(self.engine.load())
        with self.assertLogs("tts_engine", "INFO"):
            await self.engine.reload(self.p("b"), self.p("b") + ".json")
        self.assertTrue(self.engine.ready)
        self.assertEqual(self.engine.model_name, "b")

    async def test_refuses_gpu_to_cpu_downgrade(self):
        """When current voice runs on CUDA and new voice can only run on CPU, refuse the swap."""
        self.load_a()
        self.assertTrue(self.engine.cuda_active)
        self.piper.plan["b"] = {"cuda_fails": True}
        with self.assertLogs("tts_engine", "WARNING"), self.assertRaises(VoiceLoadError):
            await self.engine.reload(self.p("b"), self.p("b") + ".json")
        # Old voice and cuda_active unchanged
        self.assertTrue(self.engine.cuda_active)
        self.assertEqual(self.engine.voice.tag, "a")
        self.assertFalse(self.engine.loading)

    async def test_cpu_to_cpu_swap_allowed(self):
        """CPU-to-CPU swaps are allowed (initial load may be CPU if CUDA unavailable)."""
        # Set up so CUDA is unavailable (force CPU-only)
        self.engine._backend = lambda: (["CPUExecutionProvider"], self.piper)
        self.load_a()
        self.assertFalse(self.engine.cuda_active)
        self.assertEqual(self.engine.voice.tag, "a")
        # Now swap to another CPU-only voice
        with self.assertLogs("tts_engine", "INFO"):
            await self.engine.reload(self.p("b"), self.p("b") + ".json")
        self.assertFalse(self.engine.cuda_active)
        self.assertEqual(self.engine.voice.tag, "b")

    async def test_loading_flag_toggles(self):
        self.load_a()
        gate = threading.Event()
        self.piper.plan["b"] = {"gate_load": gate}
        self.assertFalse(self.engine.loading)
        with self.assertLogs("tts_engine", "INFO"):
            task = asyncio.create_task(self.engine.reload(self.p("b"), self.p("b") + ".json"))
            await asyncio.sleep(0.05)
            self.assertTrue(self.engine.loading)
            self.assertEqual(self.engine.model_name, "a")  # nothing swapped yet
            gate.set()
            await task
        self.assertFalse(self.engine.loading)

    async def test_loading_flag_cleared_after_failure(self):
        self.load_a()
        self.piper.plan["b"] = {"load_fails": True}
        with self.assertLogs("tts_engine", "ERROR"), self.assertRaises(VoiceLoadError):
            await self.engine.reload(self.p("b"), self.p("b") + ".json")
        self.assertFalse(self.engine.loading)

    async def test_synth_in_flight_finishes_on_old_voice_then_reload(self):
        self.load_a()
        gate = threading.Event()
        self.engine.voice.gate = gate  # the in-flight clip blocks inside the old voice
        self.piper.plan["b"] = {"rate": 16000}
        synth = asyncio.create_task(self.engine.synthesize("first"))
        await asyncio.sleep(0.05)
        with self.assertLogs("tts_engine", "INFO"):
            reload_task = asyncio.create_task(self.engine.reload(self.p("b"), self.p("b") + ".json"))
            await asyncio.sleep(0.05)
            self.assertTrue(self.engine.loading)
            self.assertEqual(self.engine.voice.tag, "a")  # reload waits for the clip
            gate.set()
            first = await synth
            await reload_task
        self.assertEqual(self.rate_of(first), 22050)  # clip keeps its own header rate
        self.assertEqual(self.engine.voice.tag, "b")

    async def test_synth_during_reload_waits_and_uses_new_voice(self):
        self.load_a()
        gate = threading.Event()
        self.piper.plan["b"] = {"rate": 16000, "gate_load": gate}
        with self.assertLogs("tts_engine", "INFO"):
            reload_task = asyncio.create_task(self.engine.reload(self.p("b"), self.p("b") + ".json"))
            await asyncio.sleep(0.05)
            synth = asyncio.create_task(self.engine.synthesize("queued text"))
            await asyncio.sleep(0.05)
            self.assertFalse(synth.done())  # serialized behind the reload
            gate.set()
            await reload_task
            path = await synth
        self.assertEqual(self.rate_of(path), 16000)
        self.assertIn(("synth", "b", "queued text"), self.piper.log)

    async def test_cancelled_caller_does_not_corrupt_state(self):
        self.load_a()
        gate = threading.Event()
        self.piper.plan["b"] = {"gate_load": gate}
        with self.assertLogs("tts_engine", "INFO"):
            task = asyncio.create_task(self.engine.reload(self.p("b"), self.p("b") + ".json"))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.engine.loading)  # the job is still running on the worker
            gate.set()
            await asyncio.wait_for(self.engine.synthesize("after"), 5)  # queues behind the swap
        self.assertEqual(self.engine.voice.tag, "b")
        self.assertFalse(self.engine.loading)

    async def test_uses_single_worker_thread(self):
        self.load_a()
        names = set()
        orig = self.piper.load

        def load(*a, **k):
            names.add(threading.current_thread().name)
            return orig(*a, **k)

        self.piper.load = load
        with self.assertLogs("tts_engine", "INFO"):
            await self.engine.reload(self.p("b"), self.p("b") + ".json")
        self.assertEqual(len(names), 1)
        self.assertTrue(next(iter(names)).startswith("piper"))
        self.assertEqual(self.engine._pool._max_workers, 1)


class StateAtomicityTests(EngineBase):
    FIELDS = ("voice", "sample_rate", "model_path", "config_path", "cuda_active")

    async def test_successful_reload_changes_all_fields_together(self):
        self.load_a()
        before = self.engine.state
        self.assertEqual(self.engine.model_name, "a")
        self.piper.plan["b"] = {"rate": 16000}
        with self.assertLogs("tts_engine", "INFO"):
            await self.engine.reload(self.p("b"), self.p("b") + ".json")
        after = self.engine.state
        self.assertIsNot(after, before)
        self.assertEqual((after.voice.tag, after.sample_rate, after.cuda_active), ("b", 16000, True))
        self.assertEqual((str(after.model_path), str(after.config_path)), (self.p("b"), self.p("b") + ".json"))
        # Legacy properties read the very same snapshot.
        for f in self.FIELDS:
            self.assertIs(getattr(self.engine, f), getattr(after, f), f)
        self.assertEqual(self.engine.model_name, "b")
        # The old snapshot object is immutable and still self-consistent.
        self.assertEqual((before.voice.tag, before.sample_rate, before.model_path.stem), ("a", 22050, "a"))
        with self.assertRaises(Exception):
            after.sample_rate = 1

    async def test_snapshot_never_mixes_old_and_new_during_swap(self):
        self.load_a()
        self.piper.plan["b"] = {"rate": 16000}
        seen = []
        orig = self.piper.load

        def load(model_path, config_path=None, use_cuda=False):
            st = self.engine.state  # read while the candidate is being built
            seen.append((st.voice.tag, st.sample_rate, st.model_path.stem))
            return orig(model_path, config_path=config_path, use_cuda=use_cuda)

        self.piper.load = load
        with self.assertLogs("tts_engine", "INFO"):
            await self.engine.reload(self.p("b"), self.p("b") + ".json")
        self.assertTrue(seen)
        self.assertTrue(all(s == ("a", 22050, "a") for s in seen), seen)

    async def test_failed_reload_leaves_state_untouched(self):
        self.load_a()
        before = self.engine.state
        self.piper.plan["b"] = {"load_fails": True}
        with self.assertLogs("tts_engine", "ERROR"), self.assertRaises(VoiceLoadError):
            await self.engine.reload(self.p("b"), self.p("b") + ".json")
        self.assertIs(self.engine.state, before)
        self.assertEqual(self.engine.model_name, "a")

    def test_failed_load_leaves_previous_paths_and_state_untouched(self):
        self.load_a()
        before = self.engine.state
        self.piper.plan["b"] = {"load_fails": True}
        with self.assertLogs("tts_engine", "ERROR"):
            self.assertFalse(self.engine.load(self.p("b"), self.p("b") + ".json"))
        self.assertIs(self.engine.state, before)
        self.assertEqual((self.engine.model_path, self.engine.config_path), (before.model_path, before.config_path))
        self.assertEqual(self.engine.model_name, "a")
        self.assertTrue(self.engine.ready)

    def test_failed_load_missing_file_leaves_configured_paths(self):
        before = self.engine.state
        with self.assertLogs("tts_engine", "ERROR"):
            self.assertFalse(self.engine.load(os.path.join(self.tmp.name, "zz.onnx")))
        self.assertIs(self.engine.state, before)
        self.assertEqual(self.engine.model_name, "a")

    async def test_refused_gpu_to_cpu_swap_leaves_every_field_untouched(self):
        self.load_a()
        before = self.engine.state
        legacy_before = tuple(getattr(self.engine, f) for f in self.FIELDS) + (self.engine.model_name,)
        self.piper.plan["b"] = {"cuda_fails": True, "rate": 16000}
        with self.assertLogs("tts_engine", "WARNING"), self.assertRaises(VoiceLoadError):
            await self.engine.reload(self.p("b"), self.p("b") + ".json")
        self.assertIs(self.engine.state, before)
        legacy_after = tuple(getattr(self.engine, f) for f in self.FIELDS) + (self.engine.model_name,)
        self.assertEqual(legacy_after, legacy_before)
        self.assertEqual((self.engine.sample_rate, self.engine.cuda_active), (22050, True))
        self.assertIn(("b", False), self.piper.calls)  # the CPU candidate really was built, then refused


class CleanPlainTextTests(unittest.TestCase):
    def test_basic_pipeline(self):
        self.assertEqual(clean_plain_text("Hello!  See https://x.io  **now**"), "Hello! See link now")
        self.assertEqual(clean_plain_text("hi <@123> in <#456> <@&9>"), "hi someone in channel role")
        self.assertEqual(clean_plain_text("```code``` and `x` ||secret||"), "code block and x secret")

    def test_unspeakable_is_none(self):
        for t in ("", "   ", "!!! ???", "***", None, 5):
            self.assertIsNone(clean_plain_text(t), t)

    def test_truncates_at_word(self):
        out = clean_plain_text("alpha beta gamma delta", 12)
        self.assertEqual(out, "alpha beta...")

    def test_matches_discord_cleaner_on_plain_text(self):
        msg = SimpleNamespace(content="Wow!!!! look at https://a.b `x`", guild=None)
        self.assertEqual(tts_engine.clean_discord_text(msg), clean_plain_text(msg.content))


if __name__ == "__main__":
    unittest.main()
