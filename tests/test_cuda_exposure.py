"""`cuda` exposure through the REAL Controller, backed by the REAL TTSEngine with a fake Piper."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import botimport  # noqa: F401,E402  (hermetic env + import of bot; must come first)

from config_store import ConfigStore  # noqa: E402
from test_engine import FakePiper  # noqa: E402
from test_voice_apply import write_voice  # noqa: E402
from tts_engine import TTSEngine  # noqa: E402

bot = botimport.bot


class CudaExposureTests(unittest.IsolatedAsyncioTestCase):
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.models = os.path.join(self.tmp.name, "models")
        os.mkdir(self.models)
        for stem in ("alpha", "beta"):
            write_voice(self.models, stem)
        self.piper = FakePiper()
        self.engine = TTSEngine(os.path.join(self.models, "alpha.onnx"))
        self.engine._backend = lambda: (self.providers, self.piper)
        self.addCleanup(self.engine.close)
        self.cfg = ConfigStore(os.path.join(self.tmp.name, "c.json"))
        for target, value in (("config", self.cfg), ("engine", self.engine), ("MODELS_DIR", self.models)):
            p = mock.patch.object(bot, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.ctl = bot.Controller()

    def load(self):
        with self.assertLogs("tts_engine", None):
            self.assertTrue(self.engine.load())

    async def test_cuda_true_in_summary_and_list(self):
        self.load()
        self.assertIs(self.ctl.voice_summary()["cuda"], True)
        r = await self.ctl.list_voices()
        self.assertIs(r["cuda"], True)
        self.assertEqual(r["current"], "alpha")

    async def test_cuda_false_after_startup_cpu_fallback(self):
        self.piper.plan["alpha"] = {"cuda_fails": True}
        self.load()
        self.assertIn(("alpha", False), self.piper.calls)
        self.assertIs(self.ctl.voice_summary()["cuda"], False)
        self.assertIs((await self.ctl.list_voices())["cuda"], False)

    async def test_cuda_none_before_engine_ready(self):
        self.assertIsNone(self.ctl.voice_summary()["cuda"])
        r = await self.ctl.list_voices()
        self.assertIn("cuda", r)
        self.assertIsNone(r["cuda"])

    async def test_cuda_survives_refused_swap_and_event_carries_it(self):
        self.load()
        q = self.cfg.hub.subscribe()
        self.piper.plan["beta"] = {"cuda_fails": True}
        from voices import VoiceLoadError
        with self.assertLogs("tts_engine", None), self.assertRaises(VoiceLoadError):
            await self.ctl.apply_voice("beta", None, "t")
        self.assertIs(self.ctl.voice_summary()["cuda"], True)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        done = [e for e in events if e["type"] == "voice" and not e["loading"]][-1]
        self.assertIs(done["cuda"], True)
        self.assertEqual(done["error"], "load_failed")


if __name__ == "__main__":
    unittest.main()
