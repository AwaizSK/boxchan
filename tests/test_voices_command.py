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


class VoicesCommandTest(CommandTestBase):
    async def test_voices_lists_voices(self):
        """Test that voices command lists available voices."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        ctx = make_context(guild, author)

        # Mock controller.list_voices
        fake_voices = {
            "voices": [
                {"name": "voice1", "quality": "medium", "language": "en", "size_mb": 100},
                {"name": "voice2", "quality": "high", "language": "en", "size_mb": 200},
            ],
            "current": "voice1",
            "configured": "voice1",
            "loading": False,
            "cuda": True,
        }
        with mock.patch.object(bot.controller, "list_voices", return_value=fake_voices):
            await bot.voices(ctx)

        ctx.send.assert_awaited_once()
        message = ctx.send.await_args[0][0]
        self.assertIn("voice1", message)
        self.assertIn("voice2", message)

    async def test_voices_marks_active_voice(self):
        """Test that voices command marks the active voice."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        ctx = make_context(guild, author)

        fake_voices = {
            "voices": [
                {"name": "voice1", "quality": "medium", "language": "en", "size_mb": 100},
            ],
            "current": "voice1",
            "configured": "voice1",
            "loading": False,
            "cuda": True,
        }
        with mock.patch.object(bot.controller, "list_voices", return_value=fake_voices):
            await bot.voices(ctx)

        message = ctx.send.await_args[0][0]
        self.assertIn("active", message)

    async def test_voices_shows_engine_not_loaded(self):
        """Test that voices command shows when engine is not loaded."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        ctx = make_context(guild, author)

        fake_voices = {
            "voices": [],
            "current": None,
            "configured": None,
            "loading": False,
            "cuda": None,
        }
        with mock.patch.object(bot.controller, "list_voices", return_value=fake_voices):
            await bot.voices(ctx)

        message = ctx.send.await_args[0][0]
        self.assertIn("not loaded", message)

    async def test_voices_truncates_at_25(self):
        """Test that voices truncates the list at 25 voices."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        ctx = make_context(guild, author)

        # Create 30 voices
        voices_list = [
            {"name": f"voice{i}", "quality": "medium", "language": "en", "size_mb": 100}
            for i in range(30)
        ]
        fake_voices = {
            "voices": voices_list,
            "current": "voice0",
            "configured": "voice0",
            "loading": False,
            "cuda": True,
        }
        with mock.patch.object(bot.controller, "list_voices", return_value=fake_voices):
            await bot.voices(ctx)

        message = ctx.send.await_args[0][0]
        # Should have truncation indicator
        self.assertIn("+", message)

    async def test_voices_under_2000_chars(self):
        """Test that voices message is under 2000 characters."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        ctx = make_context(guild, author)

        voices_list = [
            {"name": f"voice{i}", "quality": "medium", "language": "en", "size_mb": 100}
            for i in range(50)
        ]
        fake_voices = {
            "voices": voices_list,
            "current": None,
            "configured": None,
            "loading": False,
            "cuda": False,
        }
        with mock.patch.object(bot.controller, "list_voices", return_value=fake_voices):
            await bot.voices(ctx)

        message = ctx.send.await_args[0][0]
        self.assertLess(len(message), 2000)

    async def test_voices_no_allowed_mentions(self):
        """Test that voices uses no allowed mentions."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        ctx = make_context(guild, author)

        fake_voices = {
            "voices": [],
            "current": None,
            "configured": None,
            "loading": False,
            "cuda": None,
        }
        with mock.patch.object(bot.controller, "list_voices", return_value=fake_voices):
            await bot.voices(ctx)

        call_kwargs = ctx.send.await_args[1]
        if "allowed_mentions" in call_kwargs:
            am = call_kwargs["allowed_mentions"]
            self.assertFalse(am.everyone)
            self.assertFalse(am.roles)
            self.assertFalse(am.users)

    async def test_voices_shows_loading(self):
        """Test that voices shows 'loading' message when engine is loading."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        ctx = make_context(guild, author)

        fake_voices = {
            "voices": [],
            "current": None,
            "configured": None,
            "loading": True,
            "cuda": None,
        }
        with mock.patch.object(bot.controller, "list_voices", return_value=fake_voices):
            await bot.voices(ctx)

        message = ctx.send.await_args[0][0]
        self.assertIn("loading", message.lower())

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
        message = ctx.send.await_args[0][0]
        self.assertIn("Could not list voices", message)
        self.assertNotIn("secret-path-boom", message)
        self.assertNotIn("OSError", message)
        self.assertLess(len(message), 100)

    async def test_voices_read_only(self):
        """Test that voices command does not call config.set_voice or engine.reload."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        ctx = make_context(guild, author)

        fake_voices = {
            "voices": [
                {"name": "voice1", "quality": "medium", "language": "en", "size_mb": 100},
            ],
            "current": "voice1",
            "configured": "voice1",
            "loading": False,
            "cuda": True,
        }

        # Spy on config.set_voice and engine.reload
        config_spy = mock.AsyncMock(side_effect=Exception("Should not be called"))
        engine_spy = mock.AsyncMock(side_effect=Exception("Should not be called"))

        with mock.patch.object(bot.controller, "list_voices", return_value=fake_voices), \
             mock.patch.object(self.config, "set_voice", side_effect=lambda *a, **kw: config_spy(*a, **kw)), \
             mock.patch.object(bot.engine, "reload", side_effect=lambda *a, **kw: engine_spy(*a, **kw)):
            await bot.voices(ctx)

        # These should not have been called
        config_spy.assert_not_called()
        engine_spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
