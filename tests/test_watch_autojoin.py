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


def make_guild(gid, channels=(), vc=None):
    g = mock.MagicMock()
    g.id = gid
    g.voice_client = vc

    def get_channel(cid):
        for c in channels:
            if c.id == cid:
                return c
        return None

    g.get_channel = get_channel
    g.get_member = mock.MagicMock(return_value=None)
    return g


def make_text_channel(cid, name="test-channel"):
    ch = mock.MagicMock(spec=discord.TextChannel)
    ch.id = cid
    ch.name = name
    return ch


def make_voice_channel(cid, name="voice-channel"):
    ch = mock.MagicMock(spec=discord.VoiceChannel)
    ch.id = cid
    ch.name = name
    return ch


def make_member(uid, name="testuser"):
    m = mock.MagicMock()
    m.id = uid
    m.display_name = name
    m.mention = f"<@{uid}>"
    return m


def make_context(guild, author):
    ctx = mock.MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author = author
    ctx.author.voice = mock.MagicMock()
    ctx.author.voice.channel = None
    ctx.send = mock.AsyncMock()
    ctx.defer = mock.AsyncMock()
    ctx.voice_client = None
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


class WatchAutoJoinTest(CommandTestBase):
    async def test_watch_adds_to_watch_list(self):
        """Test that watch adds the member to the watch list."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        watched = make_member(2, "watched")
        ctx = make_context(guild, author)

        await bot.watch(ctx, watched)

        self.assertIn(2, bot.store.data.get(1, set()))

    async def test_watch_joins_when_not_connected_and_author_in_voice(self):
        """Test that watch joins voice when bot not connected and author in voice."""
        guild = make_guild(1, [make_text_channel(10), make_voice_channel(20)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        watched = make_member(2, "watched")
        ctx = make_context(guild, author)
        ctx.author.voice.channel = guild.get_channel(20)

        # Mock the channel.connect
        async def mock_connect():
            return mock.MagicMock()
        ctx.author.voice.channel.connect = mock_connect

        await bot.watch(ctx, watched)

        # Should have called send (with join result in message)
        ctx.send.assert_awaited_once()
        message = ctx.send.await_args[0][0]
        self.assertIn("Watching", message)
        self.assertIn("watched", message)

    async def test_watch_when_author_not_in_voice(self):
        """Test that watch handles author not in voice."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        watched = make_member(2, "watched")
        ctx = make_context(guild, author)
        # Author not in voice
        ctx.author.voice.channel = None

        await bot.watch(ctx, watched)

        ctx.send.assert_awaited_once()
        message = ctx.send.await_args[0][0]
        self.assertIn("Watching", message)
        self.assertIn("!join", message)

    async def test_watch_when_already_connected(self):
        """Test that watch doesn't try to join when already connected."""
        vc = mock.MagicMock()
        vc.is_connected.return_value = True
        guild = make_guild(1, [make_text_channel(10), make_voice_channel(20)], vc=vc)
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        watched = make_member(2, "watched")
        ctx = make_context(guild, author)
        ctx.voice_client = vc
        ctx.author.voice.channel = guild.get_channel(20)

        await bot.watch(ctx, watched)

        ctx.send.assert_awaited_once()
        message = ctx.send.await_args[0][0]
        self.assertIn("Watching", message)
        # Should not try to join since already connected
        self.assertNotIn("Joined", message)

    async def test_watch_joins_when_voice_client_is_stale_disconnected(self):
        """A voice client whose is_connected() is False is NOT connected: watch must attempt the join."""
        vc = mock.MagicMock()
        vc.is_connected.return_value = False
        vc.move_to = mock.AsyncMock()
        guild = make_guild(1, [make_text_channel(10), make_voice_channel(20)], vc=vc)
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        ctx = make_context(guild, make_member(1, "author"))
        ctx.voice_client = vc
        vch = guild.get_channel(20)
        vch.permissions_for = mock.MagicMock(return_value=mock.MagicMock(connect=True, speak=True))
        ctx.author.voice.channel = vch

        await bot.watch(ctx, make_member(2, "watched"))

        vc.move_to.assert_awaited_once_with(vch)
        self.assertIn("Joined", ctx.send.await_args[0][0])

    async def test_watch_persists_even_on_join_failure(self):
        """Test that watch list is updated even if join fails."""
        guild = make_guild(1, [make_text_channel(10), make_voice_channel(20)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        watched = make_member(2, "watched")
        ctx = make_context(guild, author)
        ctx.author.voice.channel = guild.get_channel(20)
        ctx.author.voice.channel.permissions_for = mock.MagicMock(return_value=mock.MagicMock(connect=False, speak=False))

        await bot.watch(ctx, watched)

        # Watch list should be updated despite join failure
        self.assertIn(2, bot.store.data.get(1, set()))

    async def test_join_helper_returns_tuple(self):
        """Test that join_author_voice returns (ok, msg) tuple."""
        guild = make_guild(1, [make_text_channel(10), make_voice_channel(20)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        ctx = make_context(guild, author)
        ctx.author.voice.channel = guild.get_channel(20)

        # Mock connect
        async def mock_connect():
            return mock.MagicMock()
        ctx.author.voice.channel.connect = mock_connect

        ok, msg = await bot.join_author_voice(ctx)

        self.assertTrue(ok)
        self.assertIn("Joined", msg)

    async def test_join_helper_fails_without_voice(self):
        """Test that join_author_voice fails if author not in voice."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        ctx = make_context(guild, author)
        ctx.author.voice.channel = None

        ok, msg = await bot.join_author_voice(ctx)

        self.assertFalse(ok)
        self.assertIn("Join a voice channel first", msg)

    async def test_watch_sends_no_mentions(self):
        """Test that watch command uses AllowedMentions.none()."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "author")
        watched = make_member(2, "watched")
        ctx = make_context(guild, author)

        await bot.watch(ctx, watched)

        call_kwargs = ctx.send.await_args[1]
        if "allowed_mentions" in call_kwargs:
            am = call_kwargs["allowed_mentions"]
            self.assertFalse(am.everyone)
            self.assertFalse(am.roles)
            self.assertFalse(am.users)


if __name__ == "__main__":
    unittest.main()
