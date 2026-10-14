import asyncio
import os
import tempfile
import unittest
from unittest import mock

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import botimport  # noqa: F401  (hermetic env + import of bot; must come first)
import discord
from discord.ext import commands

from config_store import ConfigStore

bot = botimport.bot


def text_channel(cid):
    ch = mock.MagicMock(spec=discord.TextChannel)
    ch.id = cid
    return ch


def make_guild(gid, channels=(), vc="vc"):
    g = mock.MagicMock()
    g.id = gid
    g.voice_client = mock.MagicMock() if vc == "vc" else vc
    g.get_channel.side_effect = lambda cid: next((c for c in channels if c.id == cid), None)
    return g


def make_message(guild, channel_id, author_id=5, content="hello", is_bot=False):
    m = mock.MagicMock()
    m.guild, m.content, m.id = guild, content, 999
    m.channel.id = channel_id
    m.author.id, m.author.bot = author_id, is_bot
    m.add_reaction = mock.AsyncMock()
    return m


class BotTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = ConfigStore(os.path.join(tmp.name, "c.json"))
        for p in (mock.patch.object(bot, "config", self.config),
                  mock.patch.object(bot.store, "data", {}),
                  mock.patch.dict(bot.audio, {}, clear=True)):
            p.start()
            self.addCleanup(p.stop)


class EnvIsolationTest(unittest.TestCase):
    def test_import_did_not_load_dotenv(self):
        self.assertNotIn("DISCORD_TOKEN", os.environ)
        self.assertEqual(botimport.LOAD_DOTENV_CALLS, 1)  # real loader was patched out
        self.assertIsNone(bot.TOKEN)


class OnMessageTest(BotTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.guild = make_guild(1)
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        bot.store.data[1] = {5}
        self.ga = bot.GuildAudio(1)
        bot.audio[1] = self.ga
        for p in (mock.patch.object(bot, "clean_discord_text", return_value="spoken"),
                  mock.patch.object(bot.bot, "process_commands", new=mock.AsyncMock())):
            self.process = p.start()
            self.addCleanup(p.stop)
        self.process = bot.bot.process_commands

    async def test_queues_in_configured_channel(self):
        await bot.on_message(make_message(self.guild, 10))
        self.assertEqual(self.ga.queue.get_nowait(), "spoken")

    async def test_paused_does_not_queue(self):
        await self.config.set_enabled(1, False, expected_rev=None, editor="t")
        await bot.on_message(make_message(self.guild, 10))
        self.assertEqual(self.ga.queue.qsize(), 0)

    async def test_old_channel_ignored_after_swap(self):
        await self.config.set_channel(1, 20, expected_rev=None, editor="t")
        await bot.on_message(make_message(self.guild, 10))
        self.assertEqual(self.ga.queue.qsize(), 0)
        await bot.on_message(make_message(self.guild, 20))
        self.assertEqual(self.ga.queue.qsize(), 1)

    async def test_unconfigured_guild_reads_nothing(self):
        await bot.on_message(make_message(make_guild(2), 10))
        self.assertEqual(self.ga.queue.qsize(), 0)

    async def test_unwatched_bot_and_dm_ignored(self):
        await bot.on_message(make_message(self.guild, 10, author_id=6))
        await bot.on_message(make_message(self.guild, 10, is_bot=True))
        await bot.on_message(make_message(None, 10))
        self.assertEqual(self.ga.queue.qsize(), 0)
        self.process.assert_not_called()

    async def test_prefix_goes_to_commands_only(self):
        msg = make_message(self.guild, 10, content=bot.PREFIX + "status")
        await bot.on_message(msg)
        self.process.assert_awaited_once_with(msg)
        self.assertEqual(self.ga.queue.qsize(), 0)

    async def test_no_voice_or_no_audio_does_not_queue(self):
        await bot.on_message(make_message(make_guild(1, vc=None), 10))
        bot.audio.clear()
        await bot.on_message(make_message(self.guild, 10))
        self.assertEqual(self.ga.queue.qsize(), 0)

    async def test_queue_full_reacts(self):
        for _ in range(bot.MAX_QUEUE):
            self.ga.queue.put_nowait("x")
        msg = make_message(self.guild, 10)
        with self.assertLogs("bot", level="WARNING"):
            await bot.on_message(msg)
        msg.add_reaction.assert_awaited_once()


class ChannelCheckTest(BotTestBase):
    def ctx(self, gid, cid):
        c = mock.MagicMock()
        c.guild = make_guild(gid) if gid else None
        c.channel.id = cid
        return c

    async def test_passes_in_configured_channel_even_when_paused(self):
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        await self.config.set_enabled(1, False, expected_rev=None, editor="t")
        self.assertTrue(await bot.only_tts_channel(self.ctx(1, 10)))

    async def test_fails_elsewhere_unconfigured_and_dm(self):
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        for gid, cid in ((1, 11), (2, 10), (None, 10)):
            with self.assertRaises(commands.CheckFailure):
                await bot.only_tts_channel(self.ctx(gid, cid))

    async def test_fails_in_old_channel_after_swap(self):
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        await self.config.set_channel(1, 20, expected_rev=None, editor="t")
        with self.assertRaises(commands.CheckFailure):
            await bot.only_tts_channel(self.ctx(1, 10))
        self.assertTrue(await bot.only_tts_channel(self.ctx(1, 20)))

    async def test_big_bots_role_allows_command_in_any_channel(self):
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        role = mock.MagicMock()
        role.name = "Big bots"
        ctx = self.ctx(1, 999)  # unconfigured channel
        ctx.author.roles = [role]
        self.assertTrue(await bot.only_tts_channel(ctx))

    async def test_big_bots_role_case_insensitive(self):
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        role = mock.MagicMock()
        role.name = "  big BOTS  "
        ctx = self.ctx(1, 999)  # unconfigured channel
        ctx.author.roles = [role]
        self.assertTrue(await bot.only_tts_channel(ctx))

    async def test_big_bots_role_still_fails_in_dm(self):
        role = mock.MagicMock()
        role.name = "Big bots"
        ctx = self.ctx(None, 999)  # DM
        ctx.author.roles = [role]
        with self.assertRaises(commands.CheckFailure):
            await bot.only_tts_channel(ctx)

    async def test_other_role_fails_in_unconfigured_channel(self):
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        role = mock.MagicMock()
        role.name = "Normal role"
        ctx = self.ctx(1, 999)
        ctx.author.roles = [role]
        with self.assertRaises(commands.CheckFailure):
            await bot.only_tts_channel(ctx)


class CommandErrorTest(BotTestBase):
    def ctx(self, gid, interaction=True):
        c = mock.MagicMock()
        c.guild = make_guild(gid)
        c.interaction = object() if interaction else None
        c.send = mock.AsyncMock()
        return c

    async def test_slash_reply_names_current_channel(self):
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        c = self.ctx(1)
        await bot.on_command_error(c, commands.CheckFailure("x"))
        self.assertIn("<#10>", c.send.await_args.args[0])
        self.assertTrue(c.send.await_args.kwargs["ephemeral"])
        await self.config.set_channel(1, 20, expected_rev=None, editor="t")
        c = self.ctx(1)
        await bot.on_command_error(c, commands.CheckFailure("x"))
        self.assertIn("<#20>", c.send.await_args.args[0])

    async def test_slash_reply_when_unconfigured(self):
        c = self.ctx(3)
        await bot.on_command_error(c, commands.CheckFailure("x"))
        self.assertIn("No channel configured", c.send.await_args.args[0])

    async def test_prefix_check_failure_is_silent(self):
        c = self.ctx(1, interaction=False)
        await bot.on_command_error(c, commands.CheckFailure("x"))
        c.send.assert_not_awaited()


class OnReadyTest(BotTestBase):
    def patch_world(self, guilds, seeds):
        for p in (mock.patch.object(type(bot.bot), "guilds", new_callable=mock.PropertyMock,
                                    return_value=guilds),
                  mock.patch.object(type(bot.bot), "user", new_callable=mock.PropertyMock,
                                    return_value=mock.MagicMock(id=42)),
                  mock.patch.object(bot, "SEED_CHANNEL_IDS", seeds)):
            p.start()
            self.addCleanup(p.stop)

    async def test_seeds_lowest_matching_text_channel(self):
        g = make_guild(1, [text_channel(30), text_channel(20)])
        self.patch_world([g], {99, 30, 20})  # 99 does not exist in the guild
        await bot.on_ready()
        self.assertEqual(self.config.channel_for(1), 20)
        self.assertEqual(self.config.snapshot()["updated_by"], "env-seed")

    async def test_non_text_channel_is_not_seeded(self):
        voice = mock.MagicMock(spec=discord.VoiceChannel)
        voice.id = 20
        self.patch_world([make_guild(1, [voice])], {20})
        await bot.on_ready()
        self.assertIsNone(self.config.channel_for(1))

    async def test_skips_existing_entries_and_is_idempotent(self):
        await self.config.set_channel(1, 77, expected_rev=None, editor="gui")
        g1 = make_guild(1, [text_channel(20)])
        g2 = make_guild(2, [text_channel(20)])
        self.patch_world([g1, g2], {20})
        await bot.on_ready()
        self.assertEqual(self.config.channel_for(1), 77)  # GUI choice wins
        self.assertEqual(self.config.channel_for(2), 20)
        rev = self.config.rev
        await bot.on_ready()  # reconnect
        self.assertEqual(self.config.rev, rev)

    async def test_seed_failure_is_logged_and_does_not_abort(self):
        g = make_guild(1, [text_channel(20)])
        self.patch_world([g], {20})
        with mock.patch.object(self.config, "seed_channel", side_effect=OSError("disk")):
            with self.assertLogs("bot", level="ERROR"):
                await bot.on_ready()

    async def test_publishes_channels_event(self):
        self.patch_world([], set())
        q = self.config.hub.subscribe()
        await bot.on_ready()
        self.assertEqual(q.get_nowait(), {"type": "channels", "guild_id": None})


class ChannelRelayTest(BotTestBase):
    async def test_listeners_publish_channels_event(self):
        g = make_guild(7)
        ch = mock.MagicMock()
        ch.guild = g
        cases = (
            (bot.on_guild_channel_create, (ch,)),
            (bot.on_guild_channel_delete, (ch,)),
            (bot.on_guild_channel_update, (ch, ch)),
            (bot.on_guild_join, (g,)),
            (bot.on_guild_remove, (g,)),
            (bot.on_guild_available, (g,)),
        )
        for fn, args in cases:
            q = self.config.hub.subscribe()
            await fn(*args)
            self.assertEqual(q.get_nowait(), {"type": "channels", "guild_id": "7"}, fn.__name__)
            self.config.hub.unsubscribe(q)


class ReconnectVoiceTest(BotTestBase):
    def patch_world(self, guilds):
        for p in (mock.patch.object(type(bot.bot), "guilds", new_callable=mock.PropertyMock,
                                    return_value=guilds),
                  mock.patch.object(type(bot.bot), "user", new_callable=mock.PropertyMock,
                                    return_value=mock.MagicMock(id=42)),
                  mock.patch.object(bot, "SEED_CHANNEL_IDS", set()),
                  mock.patch.object(bot, "_initial_ready", False)):
            p.start()
            self.addCleanup(p.stop)

    async def test_reconnects_and_announces_on_ready(self):
        vch = mock.MagicMock(spec=discord.VoiceChannel)
        vch.id, vch.name = 50, "Voice Lounge"
        vch.connect = mock.AsyncMock()
        vch.permissions_for.return_value = mock.MagicMock(connect=True, speak=True)

        tch = text_channel(10)
        tch.permissions_for.return_value = mock.MagicMock(send_messages=True)
        tch.send = mock.AsyncMock()

        g = make_guild(1, [tch, vch], vc=None)
        g.me = mock.MagicMock()
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        bot.store.voice_channels[1] = 50

        self.patch_world([g])
        await bot.on_ready()

        vch.connect.assert_awaited_once()
        self.assertIn(1, bot.audio)
        self.assertEqual(bot.audio[1].queue.get_nowait(), "Updated and reconnected.")
        tch.send.assert_awaited_once_with("Updated and reconnected to **Voice Lounge**.")

    async def test_reconnect_clears_channel_if_missing(self):
        g = make_guild(1, [], vc=None)
        g.me = mock.MagicMock()
        bot.store.voice_channels[1] = 999
        self.patch_world([g])

        await bot.on_ready()
        self.assertIsNone(bot.store.get_voice_channel(1))

    async def test_reconnect_clears_channel_if_unpermitted(self):
        vch = mock.MagicMock(spec=discord.VoiceChannel)
        vch.id, vch.name = 50, "Voice Lounge"
        vch.connect = mock.AsyncMock()
        vch.permissions_for.return_value = mock.MagicMock(connect=False, speak=False)

        g = make_guild(1, [vch], vc=None)
        g.me = mock.MagicMock()
        bot.store.voice_channels[1] = 50
        self.patch_world([g])

        await bot.on_ready()
        vch.connect.assert_not_awaited()
        self.assertIsNone(bot.store.get_voice_channel(1))

    async def test_voice_state_update_tracks_channel(self):
        g = make_guild(1)
        vch = mock.MagicMock(spec=discord.VoiceChannel)
        vch.id = 60
        bot_member = mock.MagicMock()
        bot_member.id = 42
        bot_member.guild = g

        with mock.patch.object(type(bot.bot), "user", new_callable=mock.PropertyMock,
                               return_value=mock.MagicMock(id=42)):
            # Bot joins/moves to channel 60
            await bot.on_voice_state_update(bot_member, mock.MagicMock(), mock.MagicMock(channel=vch))
            self.assertEqual(bot.store.get_voice_channel(1), 60)

            # Bot disconnects
            await bot.on_voice_state_update(bot_member, mock.MagicMock(), mock.MagicMock(channel=None))
            self.assertIsNone(bot.store.get_voice_channel(1))


if __name__ == "__main__":
    unittest.main()
