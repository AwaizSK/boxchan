import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import botimport  # noqa: F401,E402  (hermetic env + import of bot; must come first)
import discord  # noqa: E402
from messages import MessageError, SendLimiter  # noqa: E402

bot = botimport.bot
GID, CID = 10**18 + 1, 777


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_member(uid, display, name=None, bot_=False, global_name=None, can_view=True):
    m = mock.MagicMock()
    m.id, m.display_name, m.name, m.bot, m.global_name = uid, display, name or display.lower(), bot_, global_name
    m.can_view = can_view
    return m


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.ctl = bot.Controller(limiter=SendLimiter(clock=self.clock))
        self.members = {}
        self.me = mock.MagicMock()
        self.me.can_send = True
        self.ch = mock.MagicMock(spec=discord.TextChannel)
        self.ch.id = CID
        self.ch.permissions_for.side_effect = self.perms
        self.ch.send = mock.AsyncMock(return_value=mock.MagicMock(id=555))
        self.guild = mock.MagicMock()
        self.guild.id = GID
        self.guild.me = self.me
        self.guild.chunked = True
        self.guild.chunk = mock.AsyncMock(side_effect=self._chunked)
        self.guild.get_channel.side_effect = lambda cid: self.ch if cid == CID else None
        self.guild.get_member.side_effect = lambda uid: self.members.get(uid)
        type(self.guild).members = mock.PropertyMock(side_effect=lambda: list(self.members.values()))
        self.channel_id = CID
        for p in (mock.patch.object(bot.bot, "get_guild", side_effect=lambda g: self.guild if g == GID else None),
                  mock.patch.object(bot.config, "channel_for", side_effect=lambda g: self.channel_id)):
            p.start()
            self.addCleanup(p.stop)

    def _chunked(self):
        self.guild.chunked = True

    def perms(self, who):
        if who is self.me:
            return mock.MagicMock(view_channel=True, send_messages=self.me.can_send)
        return mock.MagicMock(view_channel=who.can_view, send_messages=True)

    def add(self, *members):
        for m in members:
            self.members[m.id] = m


class SendTests(Base):
    async def send(self, content, editor="alice"):
        with self.assertLogs("bot", "INFO") as cm:
            res = await self.ctl.send_message(GID, content, editor)
        return res, cm.output

    async def test_success_users_only_allowed_mentions(self):
        self.add(make_member(5, "Bob"), make_member(7, "Cy"))
        res, logs = await self.send("  hi <@5> and <@!7> and <@5>  ")
        self.assertEqual(res, {"ok": True, "message_id": "555"})
        args, kw = self.ch.send.call_args
        self.assertEqual(args, ("hi <@5> and <@!7> and <@5>",))
        am = kw["allowed_mentions"]
        self.assertIs(am.everyone, False)
        self.assertIs(am.roles, False)
        self.assertIs(am.replied_user, False)
        self.assertEqual(sorted(u.id for u in am.users), [5, 7])
        self.assertEqual(len(logs), 1)

    async def test_no_mentions_means_no_user_pings(self):
        res, _ = await self.send("plain")
        am = self.ch.send.call_args.kwargs["allowed_mentions"]
        self.assertEqual((am.everyone, am.roles, am.users), (False, False, []))
        self.assertEqual(res["message_id"], "555")

    async def test_literal_everyone_is_sent_but_cannot_ping(self):
        await self.send("hello @everyone and @here")
        args, kw = self.ch.send.call_args
        self.assertEqual(args[0], "hello @everyone and @here")
        self.assertIs(kw["allowed_mentions"].everyone, False)
        self.assertIs(kw["allowed_mentions"].roles, False)

    async def test_content_limits(self):
        for bad in ("", "   \n", "x" * 2001, None, 5):
            with self.assertRaises(MessageError) as cm:
                await self.ctl.send_message(GID, bad, "a")
            self.assertEqual(cm.exception.code, "bad_request")
        await self.send("y" * 2000)
        self.ch.send.assert_awaited_once()

    async def test_role_mention_rejected(self):
        with self.assertRaises(MessageError) as cm:
            await self.ctl.send_message(GID, "hi <@&99>", "a")
        self.assertEqual(cm.exception.code, "bad_mention")
        self.ch.send.assert_not_awaited()

    async def test_unknown_or_hidden_user_rejected(self):
        self.add(make_member(5, "Bob", can_view=False))
        for text in ("<@5>", "<@404>", "ok <@!404>"):
            with self.assertRaises(MessageError) as cm:
                await self.ctl.send_message(GID, text, "a")
            self.assertEqual(cm.exception.code, "bad_mention")
        self.ch.send.assert_not_awaited()

    async def test_bad_mention_does_not_consume_rate_limit(self):
        with self.assertRaises(MessageError):
            await self.ctl.send_message(GID, "<@404>", "a")
        await self.send("fine")

    async def test_no_channel_cases(self):
        self.channel_id = None
        with self.assertRaises(MessageError) as cm:
            await self.ctl.send_message(GID, "hi", "a")
        self.assertEqual(cm.exception.code, "no_channel")
        self.channel_id = 12345  # configured but gone
        with self.assertRaises(MessageError) as cm:
            await self.ctl.send_message(GID, "hi", "a")
        self.assertEqual(cm.exception.code, "no_channel")
        self.channel_id = CID
        self.guild.get_channel.side_effect = lambda cid: mock.MagicMock(spec=discord.VoiceChannel)
        with self.assertRaises(MessageError) as cm:
            await self.ctl.send_message(GID, "hi", "a")
        self.assertEqual(cm.exception.code, "no_channel")
        with self.assertRaises(MessageError) as cm:
            await self.ctl.send_message(GID + 1, "hi", "a")
        self.assertEqual(cm.exception.code, "no_channel")
        self.ch.send.assert_not_awaited()

    async def test_forbidden_and_missing_send_permission(self):
        self.ch.send.side_effect = discord.Forbidden(mock.MagicMock(status=403, reason="x"), "secret detail")
        with self.assertRaises(MessageError) as cm:
            await self.ctl.send_message(GID, "hi", "a")
        self.assertEqual((cm.exception.code, str(cm.exception)), ("missing_permission", "missing_permission"))
        self.me.can_send = False
        self.ch.send.reset_mock()
        self.clock.t += 10
        with self.assertRaises(MessageError) as cm:
            await self.ctl.send_message(GID, "hi", "a")
        self.assertEqual(cm.exception.code, "missing_permission")
        self.ch.send.assert_not_awaited()

    async def test_http_exception_is_discord_error(self):
        self.ch.send.side_effect = discord.HTTPException(mock.MagicMock(status=500, reason="x"), "boom at /srv")
        with self.assertLogs("bot", "WARNING") as cm:
            with self.assertRaises(MessageError) as ex:
                await self.ctl.send_message(GID, "secret text", "a")
        self.assertEqual(ex.exception.code, "discord_error")
        self.assertNotIn("secret text", "\n".join(cm.output))
        self.assertNotIn("/srv", "\n".join(cm.output))

    async def test_rate_limit_gap_and_window(self):
        await self.send("one")
        with self.assertRaises(MessageError) as cm:
            await self.ctl.send_message(GID, "two", "a")
        self.assertEqual((cm.exception.code, cm.exception.retry_after), ("rate_limited", 2))
        self.assertEqual(self.ch.send.await_count, 1)
        for _ in range(9):
            self.clock.t += 2.0
            await self.send("more")
        self.clock.t += 2.0
        with self.assertRaises(MessageError) as cm:
            await self.ctl.send_message(GID, "eleven", "a")
        self.assertEqual(cm.exception.retry_after, 60 - 20)
        self.assertEqual(self.ch.send.await_count, 10)

    async def test_reserved_before_the_await(self):
        gate = asyncio.Event()

        async def slow(*a, **k):
            await gate.wait()
            return mock.MagicMock(id=1)

        self.ch.send.side_effect = slow
        t = asyncio.ensure_future(self.ctl.send_message(GID, "first", "a"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with self.assertRaises(MessageError) as cm:  # in flight: a second send is already limited
            await self.ctl.send_message(GID, "second", "a")
        self.assertEqual(cm.exception.code, "rate_limited")
        gate.set()
        with self.assertLogs("bot", "INFO"):
            await t

    async def test_audit_log_has_counts_not_content(self):
        self.add(make_member(5, "Bob"))
        _, logs = await self.send("top secret words <@5>", editor="alice")
        self.assertEqual(logs, [f"INFO:bot:gui message sent guild={GID} channel={CID} editor=alice chars=21 mentions=1"])
        self.assertNotIn("secret", logs[0])


class MembersTests(Base):
    async def test_no_channel(self):
        self.channel_id = None
        with self.assertRaises(MessageError) as cm:
            await self.ctl.members_for(GID, "")
        self.assertEqual(cm.exception.code, "no_channel")

    async def test_sorted_non_bots_first_and_wire_shape(self):
        self.add(make_member(3, "zed"), make_member(1, "Amy", bot_=True), make_member(2, "bob"),
                 make_member(10**18 + 5, "Carl"))
        res = await self.ctl.members_for(GID, "")
        self.assertEqual([m["name"] for m in res["members"]], ["bob", "Carl", "zed", "Amy"])
        self.assertEqual(res["members"][1], {"id": str(10**18 + 5), "name": "Carl", "username": "carl", "bot": False})
        self.assertIs(res["members"][3]["bot"], True)
        self.assertIs(res["truncated"], False)

    async def test_filter_case_insensitive_on_all_names(self):
        self.add(make_member(1, "Alice", name="al"), make_member(2, "Bob", name="robert", global_name="The Builder"),
                 make_member(3, "Cy", name="cy"))
        for q, want in (("ALI", ["1"]), ("robe", ["2"]), ("builder", ["2"]), ("  cy ", ["3"]), ("zzz", [])):
            res = await self.ctl.members_for(GID, q)
            self.assertEqual([m["id"] for m in res["members"]], want, q)

    async def test_only_members_who_can_view(self):
        self.add(make_member(1, "A"), make_member(2, "B", can_view=False))
        res = await self.ctl.members_for(GID, "")
        self.assertEqual([m["id"] for m in res["members"]], ["1"])

    async def test_cap_and_truncated(self):
        self.add(*(make_member(i, f"m{i:03d}") for i in range(1, 31)))
        res = await self.ctl.members_for(GID, "")
        self.assertEqual(len(res["members"]), 25)
        self.assertIs(res["truncated"], True)
        self.add(*(make_member(i, f"m{i:03d}", can_view=i <= 25) for i in range(1, 31)))
        res = await self.ctl.members_for(GID, "")  # exactly 25 viewers: not truncated
        self.assertEqual((len(res["members"]), res["truncated"]), (25, False))

    async def test_chunk_once_when_not_chunked(self):
        self.guild.chunked = False
        self.add(make_member(1, "A"))
        await self.ctl.members_for(GID, "")
        await self.ctl.members_for(GID, "")
        self.guild.chunk.assert_awaited_once()

    async def test_chunk_not_called_when_chunked(self):
        await self.ctl.members_for(GID, "")
        self.guild.chunk.assert_not_awaited()

    async def test_chunk_failure_falls_back_to_cache(self):
        self.guild.chunked = False
        self.guild.chunk.side_effect = RuntimeError("gateway")
        self.add(make_member(1, "A"))
        with self.assertLogs("bot", "WARNING"):
            res = await self.ctl.members_for(GID, "")
        self.assertEqual(len(res["members"]), 1)


if __name__ == "__main__":
    unittest.main()
