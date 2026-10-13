import asyncio
import os
import tempfile
import unittest
from unittest import mock

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import botimport  # noqa: F401  (hermetic env + import of bot; must come first)
import discord
from discord.ext import commands

from config_store import ConfigStore

bot = botimport.bot


def assert_no_mentions(tc, send_mock):
    """The reply MUST carry allowed_mentions == AllowedMentions.none() (unconditionally)."""
    kwargs = send_mock.await_args[1]
    tc.assertIn("allowed_mentions", kwargs)
    tc.assertIsInstance(kwargs["allowed_mentions"], discord.AllowedMentions)
    tc.assertEqual(kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())


def make_guild(gid, channels=()):
    g = mock.MagicMock()
    g.id = gid

    def get_channel(cid):
        for c in channels:
            if c.id == cid:
                return c
        return None

    g.get_channel = get_channel
    return g


def make_text_channel(cid, name="test-channel"):
    ch = mock.MagicMock(spec=discord.TextChannel)
    ch.id = cid
    ch.name = name
    return ch


def make_member(uid, name="testuser"):
    m = mock.MagicMock()
    m.id = uid
    m.display_name = name
    return m


def make_context(guild, author):
    ctx = mock.MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author = author
    ctx.send = mock.AsyncMock()
    ctx.defer = mock.AsyncMock()
    return ctx


class CommandTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = ConfigStore(os.path.join(tmp.name, "c.json"))
        for p in (mock.patch.object(bot, "config", self.config),
                  mock.patch.object(bot.store, "data", {}),
                  mock.patch.dict(bot.audio, {}, clear=True)):
            p.start()
            self.addCleanup(p.stop)


def voice(i, name=None):
    return {"name": name or f"voice{i}", "quality": "medium", "language": "en", "size_mb": 100}


def summary(voices, current=None, configured=None, loading=False, cuda=True):
    return {"voices": voices, "current": current, "configured": configured, "loading": loading, "cuda": cuda}


class VoicesCommandTest(CommandTestBase):
    async def run_voices(self, fake):
        ctx = make_context(make_guild(1, [make_text_channel(10)]), make_member(1, "author"))
        with mock.patch.object(bot.controller, "list_voices", return_value=fake):
            await bot.voices(ctx)
        ctx.send.assert_awaited_once()
        return ctx, ctx.send.await_args[0][0]

    async def test_voices_lists_voices(self):
        ctx, msg = await self.run_voices(summary([voice(1), voice(2)], "voice1", "voice1"))
        self.assertIn("• voice1 — medium, en, 100 MB", msg)
        self.assertIn("• voice2 — medium, en, 100 MB", msg)
        assert_no_mentions(self, ctx.send)

    async def test_voices_marks_only_the_active_voice(self):
        _, msg = await self.run_voices(summary([voice(1), voice(2)], "voice1", "voice1"))
        lines = {l.split(" — ")[0]: l for l in msg.split("\n") if l.startswith("•")}
        self.assertIn("◀ active", lines["• voice1"])
        self.assertNotIn("active", lines["• voice2"])
        self.assertNotIn("(CPU)", lines["• voice1"])
        self.assertNotIn("saved", msg)

    async def test_voices_marks_cpu_when_cuda_inactive(self):
        _, msg = await self.run_voices(summary([voice(1)], "voice1", "voice1", cuda=False))
        self.assertIn("• voice1 — medium, en, 100 MB ◀ active (CPU)", msg)

    async def test_voices_marks_saved_not_loaded(self):
        _, msg = await self.run_voices(summary([voice(1), voice(2)], "voice1", "voice2"))
        lines = {l.split(" — ")[0]: l for l in msg.split("\n") if l.startswith("•")}
        self.assertTrue(lines["• voice2"].endswith(" (saved, not loaded)"))
        self.assertNotIn("saved", lines["• voice1"])
        self.assertIn("◀ active", lines["• voice1"])

    async def test_voices_engine_not_loaded_no_voices(self):
        ctx, msg = await self.run_voices(summary([], None, None, cuda=None))
        self.assertIn("not loaded", msg)
        assert_no_mentions(self, ctx.send)

    async def test_voices_no_voices_but_engine_loaded(self):
        _, msg = await self.run_voices(summary([], "x", "x"))
        self.assertEqual(msg, "No voices available.")

    async def test_voices_truncates_at_25(self):
        _, msg = await self.run_voices(summary([voice(i) for i in range(30)], "voice0", "voice0"))
        self.assertIn("• voice24 ", msg)
        self.assertNotIn("voice25", msg)
        self.assertEqual(sum(1 for l in msg.split("\n") if l.startswith("•")), 25)
        self.assertIn("\u2026 +5 more", msg)

    async def test_voices_exactly_25_has_no_more_marker(self):
        _, msg = await self.run_voices(summary([voice(i) for i in range(25)], "voice0", "voice0"))
        self.assertIn("• voice24 ", msg)
        self.assertNotIn("more", msg)

    async def test_voices_under_2000_chars(self):
        long = [voice(i, name="n" * 200 + str(i)) for i in range(25)]
        _, msg = await self.run_voices(summary(long))
        self.assertLessEqual(len(msg), 2000)
        self.assertTrue(msg.endswith("\u2026"))

    async def test_voices_sends_no_mentions(self):
        ctx, _ = await self.run_voices(summary([voice(1, "@everyone")], "@everyone", "@everyone"))
        assert_no_mentions(self, ctx.send)

    async def test_voices_shows_loading(self):
        ctx, msg = await self.run_voices(summary([], loading=True, cuda=None))
        self.assertEqual(msg, "Voice is loading; please wait...")
        assert_no_mentions(self, ctx.send)

    async def test_voices_defers_before_listing_then_replies(self):
        """The interaction is acked (defer) BEFORE the slow rescan; the reply still goes out."""
        guild = make_guild(1, [make_text_channel(10)])
        ctx = make_context(guild, make_member(1, "author"))
        order = []
        ctx.defer.side_effect = lambda *a, **k: order.append("defer")

        async def fake_list():
            order.append("list_voices")
            return {"voices": [{"name": "v1", "quality": "medium", "language": "en", "size_mb": 1}],
                    "current": "v1", "configured": "v1", "loading": False, "cuda": True}

        with mock.patch.object(bot.controller, "list_voices", side_effect=fake_list):
            await bot.voices(ctx)

        self.assertEqual(order, ["defer", "list_voices"])
        ctx.defer.assert_awaited_once()
        ctx.send.assert_awaited_once()
        self.assertIn("v1", ctx.send.await_args[0][0])

    async def test_voices_engine_not_loaded_still_lists_voices(self):
        """current None but voices on disk: list them AND say the engine is not loaded."""
        guild = make_guild(1, [make_text_channel(10)])
        ctx = make_context(guild, make_member(1, "author"))
        fake = {"voices": [{"name": "voiceA", "quality": "low", "language": "en", "size_mb": 5}],
                "current": None, "configured": None, "loading": False, "cuda": None}
        with mock.patch.object(bot.controller, "list_voices", return_value=fake):
            await bot.voices(ctx)

        message = ctx.send.await_args[0][0]
        self.assertIn("voiceA", message)
        self.assertIn("engine not loaded", message)

    async def test_voices_list_failure_is_friendly(self):
        """list_voices raising yields a short message without exception text, not a crash."""
        guild = make_guild(1, [make_text_channel(10)])
        ctx = make_context(guild, make_member(1, "author"))
        with mock.patch.object(bot.controller, "list_voices", side_effect=OSError("secret-path-boom")):
            await bot.voices(ctx)

        ctx.send.assert_awaited_once()
        assert_no_mentions(self, ctx.send)
        message = ctx.send.await_args[0][0]
        self.assertIn("Could not list voices", message)
        self.assertNotIn("secret-path-boom", message)
        self.assertNotIn("OSError", message)
        self.assertLess(len(message), 100)

    async def test_voices_read_only(self):
        """voices must not change config, reload the engine or apply a voice."""
        guild = make_guild(1, [make_text_channel(10)])
        ctx = make_context(guild, make_member(1, "author"))
        fake = summary([voice(1)], "voice1", "voice1")
        with mock.patch.object(bot.controller, "list_voices", return_value=fake), \
             mock.patch.object(self.config, "set_voice", side_effect=AssertionError("set_voice called")), \
             mock.patch.object(bot.engine, "reload", side_effect=AssertionError("reload called")), \
             mock.patch.object(bot.controller, "apply_voice", side_effect=AssertionError("apply_voice called")):
            await bot.voices(ctx)
        ctx.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
