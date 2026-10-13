import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import botimport  # noqa: F401,E402  (hermetic env + import of bot; must come first)

import config_store  # noqa: E402
from config_store import ConfigStore, ConflictError  # noqa: E402
from voices import BadVoiceError, SayError, VoiceLoadError  # noqa: E402

bot = botimport.bot


def write_voice(d, stem, rate=22050):
    with open(os.path.join(d, stem + ".onnx"), "wb") as f:
        f.write(b"\0" * 64)
    with open(os.path.join(d, stem + ".onnx.json"), "w") as f:
        json.dump({"audio": {"sample_rate": rate}, "num_speakers": 1}, f)


class FakeEngine:
    def __init__(self, ready=True, model="models/default.onnx"):
        self.model_path = Path(model)
        self.config_path = Path(model + ".json")
        self.ready, self.loading = ready, False
        self.cuda_active = False  # Mock GPU status
        self.calls, self.fail_next, self.gate, self.started = [], [], None, None
        self.load_results = []

    @property
    def model_name(self):
        return self.model_path.stem

    async def reload(self, model_path, config_path):
        self.calls.append((str(model_path), str(config_path)))
        self.loading = True
        try:
            if self.started:
                self.started.set()
            if self.gate:
                await self.gate.wait()
            if self.fail_next and self.fail_next.pop(0):
                raise VoiceLoadError()
            self.model_path, self.config_path, self.ready = Path(model_path), Path(config_path), True
        finally:
            self.loading = False

    def load(self, model_path=None, config_path=None):
        self.calls.append(("load", str(model_path), str(config_path)))
        ok = self.load_results.pop(0) if self.load_results else True
        if ok:
            self.model_path, self.config_path, self.ready = Path(model_path), Path(config_path), True
        return ok


class ApplyBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.models = os.path.join(self.tmp.name, "models")
        os.mkdir(self.models)
        for stem in ("alpha", "beta"):
            write_voice(self.models, stem)
        self.cfg = ConfigStore(os.path.join(self.tmp.name, "c.json"))
        self.engine = FakeEngine()
        for target, value in (("config", self.cfg), ("engine", self.engine), ("MODELS_DIR", self.models),
                              ("MODEL_PATH", "models/default.onnx"), ("CONFIG_PATH", None)):
            p = mock.patch.object(bot, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.ctl = bot.Controller()
        self.q = self.cfg.hub.subscribe()

    def events(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait())
        return out

    def voice_events(self):
        return [e for e in self.events() if e["type"] == "voice"]

    def path(self, stem):
        return os.path.join(self.models, stem + ".onnx")


class ListVoicesTests(ApplyBase):
    async def test_shape_and_rescan(self):
        r = await self.ctl.list_voices()
        self.assertEqual([v["name"] for v in r["voices"]], ["alpha", "beta"])
        self.assertEqual((r["current"], r["configured"], r["loading"]), ("default", None, False))
        write_voice(self.models, "gamma")
        r = await self.ctl.list_voices()
        self.assertEqual([v["name"] for v in r["voices"]], ["alpha", "beta", "gamma"])

    async def test_current_none_when_engine_not_ready(self):
        self.engine.ready = False
        self.assertIsNone((await self.ctl.list_voices())["current"])
        self.assertIsNone(self.ctl.voice_summary()["current"])

    async def test_configured_and_loading_reflect_state(self):
        await self.cfg.set_voice("alpha", expected_rev=None, editor="x")
        self.engine.loading = True
        r = await self.ctl.list_voices()
        self.assertEqual((r["configured"], r["loading"]), ("alpha", True))


class ApplyVoiceTests(ApplyBase):
    async def test_success_order_events_and_save(self):
        snap = await self.ctl.apply_voice("alpha", 0, "ed")
        self.assertEqual((snap["voice"], snap["rev"], snap["updated_by"]), ("alpha", 1, "ed"))
        self.assertEqual(self.engine.calls, [(self.path("alpha"), self.path("alpha") + ".json")])
        evs = self.events()
        self.assertEqual([e["type"] for e in evs], ["voice", "voice", "config"])
        self.assertEqual(evs[0], {"type": "voice", "loading": True})
        self.assertEqual(evs[1], {"type": "voice", "loading": False, "current": "alpha", "error": None, "cuda": False})
        self.assertEqual(evs[2]["change"], {"guild_id": None, "field": "voice", "old": None, "new": "alpha"})

    async def test_conflict_before_engine_is_touched(self):
        await self.cfg.set_channel(1, 10, expected_rev=None, editor="x")
        self.events()
        with self.assertRaises(ConflictError) as cm:
            await self.ctl.apply_voice("alpha", 0, "ed")
        self.assertEqual(cm.exception.snapshot["rev"], 1)
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.events(), [])
        self.assertIsNone(self.cfg.voice)

    async def test_bad_voice_rejected_before_anything(self):
        for bad in ("../x", "nope", "a/b", "alpha.onnx", ""):
            with self.assertRaises(BadVoiceError):
                await self.ctl.apply_voice(bad, None, "ed")
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.events(), [])

    async def test_bad_voice_beats_conflict(self):
        await self.cfg.set_channel(1, 10, expected_rev=None, editor="x")
        with self.assertRaises(BadVoiceError):
            await self.ctl.apply_voice("nope", 0, "ed")

    async def test_load_failure_saves_nothing_and_reports(self):
        self.engine.fail_next = [True]
        with self.assertRaises(VoiceLoadError):
            await self.ctl.apply_voice("alpha", 0, "ed")
        self.assertIsNone(self.cfg.voice)
        self.assertEqual(self.cfg.rev, 0)
        self.assertEqual(self.engine.model_name, "default")
        evs = self.events()
        self.assertEqual([e["type"] for e in evs], ["voice", "voice"])
        self.assertEqual(evs[1], {"type": "voice", "loading": False, "current": "default", "error": "load_failed", "cuda": False})

    async def test_none_reverts_to_env_default(self):
        await self.ctl.apply_voice("alpha", None, "ed")
        self.events()
        snap = await self.ctl.apply_voice(None, None, "ed")
        self.assertIsNone(snap["voice"])
        self.assertEqual(self.engine.calls[-1], ("models/default.onnx", "models/default.onnx.json"))
        self.assertEqual(self.engine.model_name, "default")

    async def test_default_uses_piper_config_path_override(self):
        with mock.patch.object(bot, "CONFIG_PATH", "elsewhere/cfg.json"):
            await self.ctl.apply_voice(None, None, "ed")
        self.assertEqual(self.engine.calls[-1], ("models/default.onnx", "elsewhere/cfg.json"))

    async def test_persist_failure_reverts_engine_and_raises_oserror(self):
        with mock.patch.object(config_store, "atomic_write_json", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                await self.ctl.apply_voice("alpha", 0, "ed")
        self.assertEqual(self.engine.model_name, "default")  # restored
        self.assertEqual([c[0] for c in self.engine.calls], [self.path("alpha"), "models/default.onnx"])
        self.assertIsNone(self.cfg.voice)
        types = [(e["type"], e.get("loading")) for e in self.events()]
        self.assertEqual(types, [("voice", True), ("voice", False), ("voice", True), ("voice", False)])

    async def test_persist_failure_with_failed_restore_still_raises_oserror(self):
        self.engine.fail_next = [False, True]
        with mock.patch.object(config_store, "atomic_write_json", side_effect=OSError("disk")):
            with self.assertLogs("bot", "ERROR"):
                with self.assertRaises(OSError):
                    await self.ctl.apply_voice("alpha", None, "ed")
        self.assertEqual(self.engine.model_name, "alpha")  # could not restore; engine keeps the new voice
        last = self.voice_events()[-1]
        self.assertEqual(last["error"], "load_failed")

    async def test_concurrent_applies_are_serialized(self):
        self.engine.gate = asyncio.Event()
        self.engine.started = asyncio.Event()
        t1 = asyncio.create_task(self.ctl.apply_voice("alpha", None, "one"))
        await self.engine.started.wait()
        t2 = asyncio.create_task(self.ctl.apply_voice("beta", None, "two"))
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.engine.calls), 1)  # second apply waits for the first
        self.engine.gate.set()
        await asyncio.gather(t1, t2)
        self.assertEqual([os.path.basename(c[0]) for c in self.engine.calls], ["alpha.onnx", "beta.onnx"])
        self.assertEqual(self.cfg.voice, "beta")
        self.assertEqual(self.cfg.rev, 2)

    async def test_second_apply_sees_rev_bumped_by_first(self):
        self.engine.gate = asyncio.Event()
        self.engine.started = asyncio.Event()
        t1 = asyncio.create_task(self.ctl.apply_voice("alpha", 0, "one"))
        await self.engine.started.wait()
        t2 = asyncio.create_task(self.ctl.apply_voice("beta", 0, "two"))
        await asyncio.sleep(0.01)
        self.engine.gate.set()
        await t1
        with self.assertRaises(ConflictError):
            await t2
        self.assertEqual(self.cfg.voice, "alpha")
        self.assertEqual(len(self.engine.calls), 1)

    async def test_channel_edit_during_load_does_not_discard_swap(self):
        self.engine.gate = asyncio.Event()
        self.engine.started = asyncio.Event()
        t = asyncio.create_task(self.ctl.apply_voice("alpha", 0, "one"))
        await self.engine.started.wait()
        await self.cfg.set_channel(1, 10, expected_rev=None, editor="other")
        self.engine.gate.set()
        snap = await t  # saved with expected_rev=None: documented trade-off
        self.assertEqual((snap["voice"], snap["rev"]), ("alpha", 2))


class StartupTests(ApplyBase):
    async def load(self):
        return await bot.load_engine()

    async def test_saved_voice_loaded_directly_single_load(self):
        await self.cfg.set_voice("alpha", expected_rev=None, editor="x")
        self.assertTrue(await self.load())
        self.assertEqual(self.engine.calls, [("load", self.path("alpha"), self.path("alpha") + ".json")])

    async def test_default_when_nothing_saved(self):
        self.assertTrue(await self.load())
        self.assertEqual(self.engine.calls, [("load", "models/default.onnx", "models/default.onnx.json")])

    async def test_missing_saved_voice_warns_once_and_uses_default(self):
        await self.cfg.set_voice("gone", expected_rev=None, editor="x")
        with self.assertLogs("bot", "WARNING") as cm:
            self.assertTrue(await self.load())
        self.assertEqual(len([m for m in cm.output if "WARNING" in m]), 1)
        self.assertEqual(self.engine.calls, [("load", "models/default.onnx", "models/default.onnx.json")])
        self.assertEqual(self.cfg.voice, "gone")  # config untouched: GUI shows configured != current

    async def test_unloadable_saved_voice_falls_back(self):
        await self.cfg.set_voice("alpha", expected_rev=None, editor="x")
        self.engine.load_results = [False, True]
        with self.assertLogs("bot", "ERROR"):
            self.assertTrue(await self.load())
        self.assertEqual([c[1] for c in self.engine.calls], [self.path("alpha"), "models/default.onnx"])

    async def test_traversal_in_saved_config_is_not_followed(self):
        await self.cfg.set_voice("../../etc/passwd", expected_rev=None, editor="x")
        with self.assertLogs("bot", "WARNING"):
            self.assertTrue(await self.load())
        self.assertEqual(self.engine.calls[0][1], "models/default.onnx")

    async def test_both_fail_returns_false(self):
        self.engine.load_results = [False]
        self.assertFalse(await self.load())


class SayTests(ApplyBase):
    def setUp(self):
        super().setUp()
        self._saved = dict(bot.audio)
        bot.audio.clear()
        self.addCleanup(lambda: (bot.audio.clear(), bot.audio.update(self._saved)))
        vc = mock.MagicMock()
        vc.is_connected.return_value = True
        self.guild = mock.MagicMock(id=7, voice_client=vc)
        for p in (mock.patch.object(type(bot.bot), "guilds", new_callable=mock.PropertyMock, return_value=[self.guild]),
                  mock.patch.object(bot.bot, "get_guild", side_effect=lambda g: self.guild if g == 7 else None)):
            p.start()
            self.addCleanup(p.stop)
        self.ga = bot.GuildAudio(7)
        self.ga.worker = mock.MagicMock()
        self.ga.worker.done.return_value = False
        bot.audio[7] = self.ga

    def code(self, text, gid=7):
        with self.assertRaises(SayError) as cm:
            self.ctl.say(gid, text)
        return cm.exception.code

    async def test_queues_cleaned_text(self):
        self.assertEqual(self.ctl.say(7, "Hello!!!! **there** https://x.y"), {"queued": True})
        self.assertEqual(self.ga.queue.get_nowait(), "Hello! there link")

    async def test_unspeakable_text(self):
        self.assertEqual(self.code("!!! ???"), "bad_text")
        self.assertEqual(self.ga.queue.qsize(), 0)

    async def test_not_connected_variants(self):
        self.guild.voice_client = None
        self.assertEqual(self.code("hi"), "not_connected")
        self.guild.voice_client = mock.MagicMock(is_connected=mock.Mock(return_value=False))
        self.assertEqual(self.code("hi"), "not_connected")
        self.guild.voice_client = mock.MagicMock(is_connected=mock.Mock(return_value=True))
        del bot.audio[7]
        self.assertEqual(self.code("hi"), "not_connected")
        bot.audio[7] = self.ga
        self.ga.worker.done.return_value = True
        self.assertEqual(self.code("hi"), "not_connected")
        self.assertEqual(self.code("hi", gid=99), "not_connected")

    async def test_queue_full(self):
        for _ in range(bot.MAX_QUEUE):
            self.ga.queue.put_nowait("x")
        self.assertEqual(self.code("hi"), "queue_full")

    async def test_engine_unavailable(self):
        self.engine.ready = False
        self.assertEqual(self.code("hi"), "tts_unavailable")


if __name__ == "__main__":
    unittest.main()
