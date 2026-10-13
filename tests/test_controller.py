import asyncio
import unittest
from unittest import mock

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import botimport  # noqa: F401,E402  (hermetic env + import of bot; must come first)
import discord  # noqa: E402

from messages import MessageError, SendLimiter  # noqa: E402
from test_messages import Clock  # noqa: E402

bot = botimport.bot


def make_channel(cid, name, position, category=None, can_view=True):
    ch = mock.MagicMock(spec=discord.TextChannel)
    ch.id, ch.name, ch.position, ch.category = cid, name, position, category
    ch.permissions_for.return_value = mock.MagicMock(view_channel=can_view)
    return ch


def make_category(name, position):
    cat = mock.MagicMock()
    cat.name, cat.position = name, position
    return cat


def make_guild(gid, name, channels, vc=None):
    g = mock.MagicMock()
    g.id, g.name, g.text_channels, g.voice_client = gid, name, channels, vc
    g.get_channel.side_effect = lambda cid: next((c for c in channels if c.id == cid), None)
    return g


def make_vc(playing=False, connected=True, channel_name="Lounge"):
    vc = mock.MagicMock()
    vc.is_playing.return_value = playing
    vc.is_connected.return_value = connected
    vc.channel.name = channel_name
    return vc


class ControllerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_audio = dict(bot.audio)
        bot.audio.clear()
        self._saved_store = bot.store.data
        bot.store.data = {}
        self.addCleanup(self._restore)

    def _restore(self):
        bot.audio.clear()
        bot.audio.update(self._saved_audio)
        bot.store.data = self._saved_store

    def patch_guilds(self, guilds):
        p1 = mock.patch.object(type(bot.bot), "guilds", new_callable=mock.PropertyMock, return_value=guilds)
        p2 = mock.patch.object(bot.bot, "get_guild", side_effect=lambda gid: next((g for g in guilds if g.id == gid), None))
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    async def test_guilds_view_shape_order_and_string_ids(self):
        cat_a, cat_b = make_category("A", 0), make_category("B", 1)
        chans = [
            make_channel(5, "b-two", 1, cat_b),
            make_channel(4, "a-one", 0, cat_a),
            make_channel(3, "loose", 7, None),
            make_channel(6, "hidden", 0, cat_a, can_view=False),
            make_channel(2, "b-one", 0, cat_b),
        ]
        g = make_guild(10**18 + 1, "Guild", chans, vc=make_vc(playing=True))
        self.patch_guilds([g])
        bot.store.data[g.id] = {1, 2, 3}
        ga = bot.GuildAudio(g.id)
        ga.queue.put_nowait("x"); ga.queue.put_nowait("y")
        bot.audio[g.id] = ga

        view = bot.controller.guilds_view()
        self.assertEqual(len(view), 1)
        v = view[0]
        self.assertEqual(v["id"], str(10**18 + 1))
        self.assertEqual(v["name"], "Guild")
        self.assertEqual([c["id"] for c in v["channels"]], ["3", "4", "2", "5"])
        self.assertEqual(v["channels"][0], {"id": "3", "name": "loose", "category": None, "position": 7})
        self.assertEqual(v["channels"][1]["category"], "A")
        self.assertEqual((v["voice"], v["queue"], v["playing"], v["watched"]), ("Lounge", 2, True, 3))

    async def test_status_view_dynamic_only(self):
        g1 = make_guild(1, "One", [], vc=None)
        g2 = make_guild(2, "Two", [], vc=make_vc(playing=False))
        self.patch_guilds([g1, g2])
        self.assertEqual(bot.controller.status_view(), {
            "1": {"voice": None, "queue": 0, "playing": False, "watched": 0},
            "2": {"voice": "Lounge", "queue": 0, "playing": False, "watched": 0},
        })

    async def test_is_text_channel(self):
        visible = make_channel(1, "ok", 0)
        hidden = make_channel(2, "no", 1, can_view=False)
        g = make_guild(10, "G", [visible, hidden])
        voice = mock.MagicMock(spec=discord.VoiceChannel)
        voice.id = 3
        g.get_channel.side_effect = lambda cid: {1: visible, 2: hidden, 3: voice}.get(cid)
        self.patch_guilds([g])
        c = bot.controller
        self.assertTrue(c.is_text_channel(10, 1))
        self.assertFalse(c.is_text_channel(10, 2))
        self.assertFalse(c.is_text_channel(10, 3))
        self.assertFalse(c.is_text_channel(10, 99))
        self.assertFalse(c.is_text_channel(11, 1))

    async def test_skip(self):
        vc = make_vc(playing=True)
        g = make_guild(1, "G", [], vc=vc)
        self.patch_guilds([g])
        ga = bot.GuildAudio(1)
        ga.queue.put_nowait("keep")
        bot.audio[1] = ga
        self.assertTrue(bot.controller.skip(1))
        vc.stop.assert_called_once()
        self.assertEqual(ga.queue.qsize(), 1)  # queue untouched
        vc.is_playing.return_value = False
        vc.stop.reset_mock()
        self.assertFalse(bot.controller.skip(1))
        vc.stop.assert_not_called()
        self.assertFalse(bot.controller.skip(404))

    async def test_clear_queue(self):
        ga = bot.GuildAudio(1)
        for t in "abc":
            ga.queue.put_nowait(t)
        bot.audio[1] = ga
        self.assertEqual(bot.controller.clear_queue(1), 3)
        self.assertEqual(ga.queue.qsize(), 0)
        self.assertEqual(bot.controller.clear_queue(1), 0)
        self.assertEqual(bot.controller.clear_queue(999), 0)


class ConfigWatcherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.audio.clear()
        self.addCleanup(bot.audio.clear)
        self.ga = bot.GuildAudio(7)
        for t in "ab":
            self.ga.queue.put_nowait(t)
        bot.audio[7] = self.ga
        self.task = asyncio.create_task(bot.config_watcher())
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(bot.config.hub.subscriber_count, 1)

    async def asyncTearDown(self):
        self.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.task
        self.assertEqual(bot.config.hub.subscriber_count, 0)

    def _event(self, field, gid=7):
        return {"type": "config", "snapshot": {},
                "change": {"guild_id": gid, "field": field, "old": 1, "new": 2}}

    async def _settle(self):
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_drains_on_channel_change(self):
        bot.config.hub.publish(self._event("channel_id"))
        await self._settle()
        self.assertEqual(self.ga.queue.qsize(), 0)

    async def test_keeps_queue_on_enabled_change(self):
        bot.config.hub.publish(self._event("enabled"))
        await self._settle()
        self.assertEqual(self.ga.queue.qsize(), 2)

    async def test_ignores_other_guild_and_survives_bad_event(self):
        bot.config.hub.publish({"type": "config"})  # malformed: no change
        bot.config.hub.publish(self._event("channel_id", gid=8))
        await self._settle()
        self.assertEqual(self.ga.queue.qsize(), 2)
        bot.config.hub.publish(self._event("channel_id"))
        await self._settle()
        self.assertEqual(self.ga.queue.qsize(), 0)
        self.assertFalse(self.task.done())


class SendMessageTests(unittest.IsolatedAsyncioTestCase):
    """Every test builds its own Controller with a fresh limiter on an injected clock, so
    nothing depends on the shared global bot.controller or on test order."""

    def fresh_controller(self):
        return bot.Controller(limiter=SendLimiter(clock=Clock()))

    async def test_audit_forbidden_logged_with_reason_format(self):
        """Forbidden errors log with 'reason=forbidden' format, never message content."""
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100
        ch.send = mock.AsyncMock(side_effect=discord.Forbidden(mock.MagicMock(status=403), "Forbidden"))

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.me = mock.MagicMock()
        guild.get_member.return_value = mock.MagicMock()
        guild.get_channel.return_value = ch
        ch.permissions_for.return_value = mock.MagicMock(view_channel=True)

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                with self.assertLogs("bot", "WARNING") as log_ctx:
                    with self.assertRaises(MessageError) as cm:
                        await self.fresh_controller().send_message(1, "ZZ-DISTINCT-CONTENT <@42>", "alice")
                self.assertEqual(cm.exception.code, "missing_permission")

        # Verify log contains exact format
        warning_msgs = [msg for msg in log_ctx.output if "WARNING" in msg]
        self.assertTrue(len(warning_msgs) > 0, "Expected at least one WARNING")
        warning_line = warning_msgs[0]
        self.assertIn("gui message failed guild=1 channel=100 editor=alice reason=forbidden", warning_line)

        # Verify message content NEVER appears in any log record
        for record in log_ctx.records:
            full_msg = record.getMessage()
            self.assertNotIn("ZZ-DISTINCT-CONTENT", full_msg,
                           f"Message content leaked in log: {full_msg}")
            if record.exc_text:
                self.assertNotIn("ZZ-DISTINCT-CONTENT", record.exc_text,
                               f"Message content leaked in exception: {record.exc_text}")

    async def test_send_message_11_distinct_mentions_rejected(self):
        """Controller.send_message rejects 11 distinct mentions without calling send() or reserving rate limit."""
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.me = mock.MagicMock()
        # Mock get_member to return valid members
        guild.get_member.return_value = mock.MagicMock()
        guild.get_channel.return_value = ch
        ch.permissions_for.return_value = mock.MagicMock(view_channel=True)

        # 11 distinct mentions
        content = " ".join(f"<@{i}>" for i in range(11))

        # Create a spy on the limiter to verify reserve was not called
        limiter_mock = mock.MagicMock()
        controller = bot.Controller(limiter=limiter_mock)

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                with self.assertRaises(MessageError) as cm:
                    await controller.send_message(1, content, "alice")

        self.assertEqual(cm.exception.code, "bad_mention")
        # Verify send() was never awaited
        ch.send.assert_not_awaited()
        # Verify rate limiter was never called (no slot consumed)
        limiter_mock.reserve.assert_not_called()

    async def test_send_message_exactly_10_distinct_mentions_succeeds(self):
        """Controller.send_message succeeds with exactly 10 distinct mentions."""
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100
        ch.send = mock.AsyncMock(return_value=mock.MagicMock(id=999))

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.me = mock.MagicMock()
        guild.get_member.return_value = mock.MagicMock()
        guild.get_channel.return_value = ch
        ch.permissions_for.return_value = mock.MagicMock(view_channel=True)

        # Exactly 10 distinct mentions
        content = " ".join(f"<@{i}>" for i in range(10))

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                result = await self.fresh_controller().send_message(1, content, "alice")

        self.assertEqual(result["ok"], True)
        ch.send.assert_awaited_once()

    async def test_guild_me_is_none_returns_no_channel_for_send(self):
        """When guild.me is None during outage, send_message raises no_channel."""
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.me = None  # During outage
        guild.get_member.return_value = mock.MagicMock()  # Mentioned user exists
        guild.get_channel.return_value = ch
        ch.permissions_for.return_value = mock.MagicMock(view_channel=True)

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                with self.assertRaises(MessageError) as cm:
                    await self.fresh_controller().send_message(1, "hi", "alice")

        self.assertEqual(cm.exception.code, "no_channel")


class MembersForTests(unittest.IsolatedAsyncioTestCase):

    async def test_permission_check_cap_at_2000(self):
        """Scan stops at 2000 permission checks and returns truncated=true."""
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100

        # Create 5000 members: members 2000+ can view, first 2000 cannot
        members = []
        for i in range(5000):
            m = mock.MagicMock(spec=discord.Member)
            m.id = i
            m.bot = False
            m.display_name = f"member_{i:04d}"
            m.name = f"user{i}"
            m.global_name = None
            members.append(m)

        # Mock ch.permissions_for to return view_channel based on member id
        def perm_for(member):
            perm = mock.MagicMock()
            perm.view_channel = member.id >= 2000  # First 2000 cannot view
            return perm

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.members = members
        guild.me = mock.MagicMock()
        guild.chunked = True
        guild.get_channel.return_value = ch
        ch.permissions_for.side_effect = perm_for

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                result = await bot.Controller().members_for(1, "")

        # Should have capped at 2000 permission checks (all without view), so 0 results
        self.assertEqual(len(result["members"]), 0)
        self.assertTrue(result["truncated"])
        # Verify that we stopped after checking exactly 2000 permissions
        perm_check_count = ch.permissions_for.call_count
        self.assertEqual(perm_check_count, 2000)

    async def test_yields_to_event_loop(self):
        """Scan yields to event loop every ~250 members via asyncio.sleep(0)."""
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100

        # Create 5000 members where NONE can view the channel
        # This forces the scan to run through the permission check cap (2000)
        members = []
        for i in range(5000):
            m = mock.MagicMock(spec=discord.Member)
            m.id = i
            m.bot = False
            m.display_name = f"member_{i:04d}"
            m.name = f"user{i}"
            m.global_name = None
            members.append(m)

        # Nobody can view
        def perm_for(member):
            perm = mock.MagicMock()
            perm.view_channel = False
            return perm

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.members = members
        guild.me = mock.MagicMock()
        guild.chunked = True
        guild.get_channel.return_value = ch
        ch.permissions_for.side_effect = perm_for

        # Ticker task that runs every time the event loop yields
        ticker_count = []
        ticker_cancelled = asyncio.Event()
        async def ticker():
            try:
                while True:
                    await asyncio.sleep(0)
                    ticker_count.append(1)
            except asyncio.CancelledError:
                ticker_cancelled.set()

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                ticker_task = asyncio.create_task(ticker())
                result = await bot.Controller().members_for(1, "")
                ticker_task.cancel()
                try:
                    await ticker_task
                except asyncio.CancelledError:
                    pass

        # Scan should stop at 2000 permission checks with truncated=true
        self.assertEqual(len(result["members"]), 0)
        self.assertTrue(result["truncated"])
        # Ticker should have run ~2000/250 = ~8 times (within reasonable bounds)
        self.assertGreaterEqual(len(ticker_count), 6,
                               f"ticker should have run ~8 times, got {len(ticker_count)}")
        self.assertLessEqual(len(ticker_count), 15,
                            f"ticker ran too many times: {len(ticker_count)}")

    async def test_guild_me_is_none_returns_empty(self):
        """When guild.me is None (outage), return empty member list."""
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.me = None  # During outage
        guild.get_channel.return_value = ch

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                result = await bot.Controller().members_for(1, "")

        self.assertEqual(result["members"], [])
        self.assertFalse(result["truncated"])

    async def test_chunk_timeout_cache_concurrent_and_memo(self):
        """Two concurrent searches call chunk() once; within 60s no retry; after 60s retry."""
        from tests.test_messages import Clock

        clk = Clock()
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100

        # Create members that all can view
        members = []
        for i in range(10):
            m = mock.MagicMock(spec=discord.Member)
            m.id = i
            m.bot = False
            m.display_name = f"member_{i:04d}"
            m.name = f"user{i}"
            m.global_name = None
            members.append(m)

        def perm_for(member):
            perm = mock.MagicMock()
            perm.view_channel = True
            return perm

        # chunk_events to control when chunk() completes
        chunk_events = [asyncio.Event() for _ in range(3)]
        chunk_count = [0]
        async def hanging_chunk():
            call_num = chunk_count[0]
            chunk_count[0] += 1
            if call_num < len(chunk_events):
                await chunk_events[call_num].wait()

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.members = members
        guild.me = mock.MagicMock()
        guild.chunked = False
        guild.chunk = mock.AsyncMock(side_effect=hanging_chunk)
        guild.get_channel.return_value = ch
        ch.permissions_for.side_effect = perm_for

        # Use controller with injected clock
        controller = bot.Controller(clock=clk)

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                # Scenario 1: Two concurrent searches
                search1 = asyncio.create_task(controller.members_for(1, ""))
                search2 = asyncio.create_task(controller.members_for(1, ""))
                await asyncio.sleep(0.01)  # Let both start waiting in chunk_lock

                # Release chunk() - both searches return cached members
                guild.chunked = True
                chunk_events[0].set()
                result1 = await search1
                result2 = await search2

                # chunk() should have been called exactly once
                self.assertEqual(chunk_count[0], 1)
                self.assertEqual(len(result1["members"]), 10)
                self.assertEqual(len(result2["members"]), 10)

                # Scenario 2: Search within 60s should not retry chunk()
                clk.t += 30.0
                guild.chunked = False
                search3 = asyncio.create_task(controller.members_for(1, ""))
                await asyncio.sleep(0.01)
                # chunk() should not have been called again
                self.assertEqual(chunk_count[0], 1)
                # Search still returns results from cache
                result3 = await search3
                self.assertEqual(len(result3["members"]), 10)

                # Scenario 3: Search after 60s should retry chunk()
                clk.t += 31.0  # Now at 61s, past the 60s window
                guild.chunked = False
                search4 = asyncio.create_task(controller.members_for(1, ""))
                await asyncio.sleep(0.01)
                # chunk() should have been called again
                self.assertEqual(chunk_count[0], 2)
                # Release it
                guild.chunked = True
                chunk_events[1].set()
                result4 = await search4
                self.assertEqual(len(result4["members"]), 10)

    async def test_chunk_timeout_cache_prevents_retry_within_window(self):
        """After timeout, second search within 60s doesn't call chunk()."""
        from tests.test_messages import Clock

        clk = Clock()
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100

        members = []
        for i in range(10):
            m = mock.MagicMock(spec=discord.Member)
            m.id = i
            m.bot = False
            m.display_name = f"member_{i:04d}"
            m.name = f"user{i}"
            m.global_name = None
            members.append(m)

        def perm_for(member):
            perm = mock.MagicMock()
            perm.view_channel = True
            return perm

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.members = members
        guild.me = mock.MagicMock()
        guild.chunked = False
        guild.chunk = mock.AsyncMock(side_effect=asyncio.TimeoutError("chunk timeout"))
        guild.get_channel.return_value = ch
        ch.permissions_for.side_effect = perm_for

        # Use injected clock
        controller = bot.Controller(clock=clk)

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                # First search: chunk fails
                result1 = await controller.members_for(1, "")
                self.assertEqual(guild.chunk.call_count, 1)

                # Advance clock by 30s (still within 60s window)
                clk.t += 30.0

                # Second search: chunk should not be called again
                result2 = await controller.members_for(1, "")
                self.assertEqual(guild.chunk.call_count, 1)  # Still 1, not called again

                # Advance clock by 31s more (now >60s from failure)
                clk.t += 31.0

                # Third search: chunk should be called again
                guild.chunk.side_effect = asyncio.TimeoutError("another timeout")
                result3 = await controller.members_for(1, "")
                self.assertEqual(guild.chunk.call_count, 2)  # Now called again


if __name__ == "__main__":
    unittest.main()
