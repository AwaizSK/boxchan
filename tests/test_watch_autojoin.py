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


class WatchBase(CommandTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        self.vch = make_voice_channel(20, "Lounge")
        self.vch.connect = mock.AsyncMock()
        self.vch.permissions_for = mock.MagicMock(return_value=mock.MagicMock(connect=True, speak=True))
        self.ensure = mock.patch.object(bot, "ensure_audio")
        self.ensure_mock = self.ensure.start()
        self.addCleanup(self.ensure.stop)
        self.ctx = make_context(self.guild, make_member(1, "author"))
        self.ctx.author.voice.channel = self.vch
        self.watched = make_member(2, "watched")

    def live_vc(self):
        vc = mock.MagicMock()
        vc.is_connected.return_value = True
        vc.move_to = mock.AsyncMock()
        return vc


class WatchAutoJoinTest(WatchBase):
    async def test_watch_adds_to_watch_list(self):
        await bot.watch(self.ctx, self.watched)
        self.assertIn(2, bot.store.data.get(1, set()))

    async def test_watch_joins_when_not_connected_and_author_in_voice(self):
        with mock.patch.object(bot.engine, "ready", False):
            await bot.watch(self.ctx, self.watched)

        self.vch.connect.assert_awaited_once()
        self.ensure_mock.assert_called_once_with(1)
        self.ctx.defer.assert_awaited_once()
        self.ctx.send.assert_awaited_once()
        self.assertEqual(self.ctx.send.await_args[0][0], "Watching **watched**. Joined **Lounge**. TTS engine NOT loaded; check logs.")
        self.assertIn(2, bot.store.data[1])
        assert_no_mentions(self, self.ctx.send)

    async def test_watch_joins_when_voice_client_is_stale_disconnected(self):
        """A voice client whose is_connected() is False is NOT connected: watch must attempt the join."""
        vc = self.live_vc()
        vc.is_connected.return_value = False
        self.ctx.voice_client = vc

        await bot.watch(self.ctx, self.watched)

        vc.move_to.assert_awaited_once_with(self.vch)
        self.ensure_mock.assert_called_once_with(1)
        self.assertIn("Joined", self.ctx.send.await_args[0][0])

    async def test_watch_when_already_connected_does_not_join(self):
        vc = self.live_vc()
        self.ctx.voice_client = vc
        with mock.patch.object(bot, "join_author_voice", new_callable=mock.AsyncMock) as helper:
            await bot.watch(self.ctx, self.watched)

        helper.assert_not_awaited()
        self.vch.connect.assert_not_awaited()
        vc.move_to.assert_not_awaited()
        self.ensure_mock.assert_not_called()
        self.assertEqual(self.ctx.send.await_args[0][0], "Watching **watched**.")
        self.assertIn(2, bot.store.data[1])
        assert_no_mentions(self, self.ctx.send)

    async def test_watch_join_failure_reports_same_message_and_keeps_watch(self):
        self.vch.permissions_for = mock.MagicMock(return_value=mock.MagicMock(connect=False, speak=False))
        # What the helper says for exactly this failure:
        probe = make_context(self.guild, make_member(3, "probe"))
        probe.author.voice.channel = self.vch
        ok, join_msg = await bot.join_author_voice(probe)
        self.assertFalse(ok)
        self.assertIn("Missing Connect/Speak permission in Lounge", join_msg)

        await bot.watch(self.ctx, self.watched)

        self.assertEqual(self.ctx.send.await_args[0][0], f"Watching **watched**. {join_msg}")
        self.assertIn(2, bot.store.data[1])
        self.vch.connect.assert_not_awaited()
        self.ensure_mock.assert_not_called()
        assert_no_mentions(self, self.ctx.send)

    async def test_watch_when_author_not_in_voice_suggests_join(self):
        self.ctx.author.voice.channel = None
        await bot.watch(self.ctx, self.watched)

        self.vch.connect.assert_not_awaited()
        self.ctx.defer.assert_not_awaited()
        self.ensure_mock.assert_not_called()
        self.assertEqual(self.ctx.send.await_args[0][0],
                         f"Watching **watched**. Run `{bot.PREFIX}join` from a voice channel to start reading.")
        self.assertIn("Run `!join`", self.ctx.send.await_args[0][0])
        self.assertIn(2, bot.store.data[1])
        assert_no_mentions(self, self.ctx.send)

    async def test_watch_when_author_has_no_voice_state(self):
        self.ctx.author.voice = None
        await bot.watch(self.ctx, self.watched)
        self.assertIn("Run `!join`", self.ctx.send.await_args[0][0])
        self.assertIn(2, bot.store.data[1])

    async def test_watch_hostile_display_name_sent_with_no_mentions(self):
        await bot.watch(self.ctx, make_member(2, "@everyone"))
        self.assertIn("**@everyone**", self.ctx.send.await_args[0][0])
        assert_no_mentions(self, self.ctx.send)


class JoinHelperTest(WatchBase):
    async def test_join_connects_and_ensures_audio(self):
        with mock.patch.object(bot.engine, "ready", True):
            ok, msg = await bot.join_author_voice(self.ctx)

        self.assertTrue(ok)
        self.assertEqual(msg, "Joined **Lounge**.")
        self.vch.connect.assert_awaited_once()
        self.ensure_mock.assert_called_once_with(1)
        self.ctx.defer.assert_awaited_once()

    async def test_join_notes_engine_not_loaded(self):
        with mock.patch.object(bot.engine, "ready", False):
            ok, msg = await bot.join_author_voice(self.ctx)
        self.assertTrue(ok)
        self.assertEqual(msg, "Joined **Lounge**. TTS engine NOT loaded; check logs.")

    async def test_join_moves_existing_voice_client(self):
        vc = self.live_vc()
        self.ctx.voice_client = vc
        ok, _ = await bot.join_author_voice(self.ctx)
        self.assertTrue(ok)
        vc.move_to.assert_awaited_once_with(self.vch)
        self.vch.connect.assert_not_awaited()
        self.ensure_mock.assert_called_once_with(1)

    async def test_join_fails_without_voice(self):
        self.ctx.author.voice.channel = None
        ok, msg = await bot.join_author_voice(self.ctx)
        self.assertFalse(ok)
        self.assertEqual(msg, "Join a voice channel first.")
        self.ctx.defer.assert_not_awaited()
        self.ensure_mock.assert_not_called()

    async def test_join_forbidden(self):
        self.vch.connect.side_effect = discord.Forbidden(mock.MagicMock(status=403, reason="x"), "no")
        ok, msg = await bot.join_author_voice(self.ctx)
        self.assertFalse(ok)
        self.assertIn("Forbidden", msg)
        self.ensure_mock.assert_not_called()

    async def test_join_client_exception_is_reported(self):
        self.vch.connect.side_effect = discord.ClientException("Already connected")
        ok, msg = await bot.join_author_voice(self.ctx)
        self.assertFalse(ok)
        self.assertEqual(msg, "Join failed: Already connected")
        self.ensure_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
