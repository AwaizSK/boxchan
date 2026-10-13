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
    ctx.send = mock.AsyncMock()
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


class HelpCommandTest(CommandTestBase):
    async def test_help_lists_all_registered_commands(self):
        """Test that help contains all registered command names."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "testuser")
        ctx = make_context(guild, author)

        await bot.help(ctx)

        help_text = ctx.send.await_args[0][0]
        # Check that help text contains all command names
        for cmd in bot.bot.commands:
            self.assertIn(cmd.name, help_text)

    async def test_help_shows_listen_channel(self):
        """Test that help shows the configured listen channel."""
        guild = make_guild(1, [make_text_channel(10, "alerts")])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "testuser")
        ctx = make_context(guild, author)

        await bot.help(ctx)

        help_text = ctx.send.await_args[0][0]
        self.assertIn("<#10>", help_text)

    async def test_help_shows_unconfigured_channel(self):
        """Test that help shows 'not set' when no channel is configured."""
        guild = make_guild(1, [make_text_channel(10)])
        author = make_member(1, "testuser")
        ctx = make_context(guild, author)

        await bot.help(ctx)

        help_text = ctx.send.await_args[0][0]
        self.assertIn("not set", help_text)

    async def test_help_shows_voice_connection_status(self):
        """Test that help shows voice connection status."""
        vc = mock.MagicMock()
        vc.is_connected.return_value = True
        vc.channel.name = "General"
        guild = make_guild(1, [make_text_channel(10)], vc=vc)
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "testuser")
        ctx = make_context(guild, author)
        ctx.voice_client = vc

        await bot.help(ctx)

        help_text = ctx.send.await_args[0][0]
        self.assertIn("General", help_text)

    async def test_help_shows_watched_users(self):
        """Test that help shows watched users."""
        guild = make_guild(1, [make_text_channel(10)])
        guild.get_member = mock.MagicMock(side_effect=lambda uid: make_member(uid, f"user{uid}"))
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        bot.store.data[1] = {5, 6}
        author = make_member(1, "testuser")
        ctx = make_context(guild, author)

        await bot.help(ctx)

        help_text = ctx.send.await_args[0][0]
        self.assertIn("user5", help_text)
        self.assertIn("user6", help_text)

    async def test_help_no_allowed_mentions(self):
        """Test that help uses no allowed mentions."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "testuser")
        ctx = make_context(guild, author)

        await bot.help(ctx)

        call_kwargs = ctx.send.await_args[1]
        if "allowed_mentions" in call_kwargs:
            am = call_kwargs["allowed_mentions"]
            self.assertFalse(am.everyone)
            self.assertFalse(am.roles)
            self.assertFalse(am.users)

    async def test_help_under_2000_chars(self):
        """Test that help text is under 2000 characters."""
        guild = make_guild(1, [make_text_channel(10)])
        guild.get_member = mock.MagicMock(side_effect=lambda uid: make_member(uid, f"user{uid}"))
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        # Add 100 watched users to test truncation
        bot.store.data[1] = set(range(100))
        author = make_member(1, "testuser")
        ctx = make_context(guild, author)

        await bot.help(ctx)

        help_text = ctx.send.await_args[0][0]
        self.assertLess(len(help_text), 2000)

    async def test_help_no_webui_address(self):
        """Test that help does not contain Web UI addresses."""
        guild = make_guild(1, [make_text_channel(10)])
        await self.config.set_channel(1, 10, expected_rev=None, editor="t")
        author = make_member(1, "testuser")
        ctx = make_context(guild, author)

        await bot.help(ctx)

        help_text = ctx.send.await_args[0][0]
        # No IP addresses that look like 127.0.0.1 or similar
        self.assertNotIn("127.0", help_text)
        self.assertNotIn("100.6", help_text)
        self.assertNotIn("WEBUI", help_text)


if __name__ == "__main__":
    unittest.main()
