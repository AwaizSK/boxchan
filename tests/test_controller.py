import asyncio
import unittest
from unittest import mock

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import botimport  # noqa: F401,E402  (hermetic env + import of bot; must come first)
import discord  # noqa: E402

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
    def setUp(self):
        self.addCleanup(lambda: mock.patch.object(bot.bot, "get_guild").stop())
        self.addCleanup(lambda: mock.patch.object(bot.config, "channel_for").stop())

    async def test_audit_forbidden_logged_with_reason_format(self):
        """Forbidden errors log in 'reason=forbidden' format."""
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100
        ch.send = mock.AsyncMock(side_effect=discord.Forbidden(mock.MagicMock(status=403), "Forbidden"))

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.me = mock.MagicMock()
        guild.get_member.return_value = mock.MagicMock()  # All mentioned users exist and can view
        guild.get_channel.return_value = ch
        ch.permissions_for.return_value = mock.MagicMock(view_channel=True)

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                with self.assertLogs("bot", "WARNING") as log_ctx:
                    try:
                        await bot.controller.send_message(1, "hi <@42>", "alice")
                    except Exception:
                        pass  # Expected to raise MessageError

        # Verify log contains the reason format
        self.assertTrue(any("reason=forbidden" in msg for msg in log_ctx.output),
                       f"Expected 'reason=forbidden' in logs, got: {log_ctx.output}")
        # Verify message content is NOT in logs
        self.assertFalse(any("secret" in msg for msg in log_ctx.output))

    async def test_guild_me_is_none_returns_no_channel_for_send(self):
        """When guild.me is None during outage, send_message raises no_channel."""
        from messages import MessageError

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
                    await bot.controller.send_message(1, "hi", "alice")

        self.assertEqual(cm.exception.code, "no_channel")


class MembersForTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(lambda: mock.patch.object(bot.bot, "get_guild").stop())

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
                result = await bot.controller.members_for(1, "")

        # Should have capped at 2000 permission checks (all without view), so 0 results
        self.assertEqual(len(result["members"]), 0)
        self.assertTrue(result["truncated"])
        # Verify that we stopped after checking 2000 permissions
        perm_check_count = ch.permissions_for.call_count
        self.assertLessEqual(perm_check_count, 2001)

    async def test_yields_to_event_loop(self):
        """Scan yields to event loop every ~250 members (other tasks can run)."""
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.id = 100

        # Create many members that all can view
        members = []
        for i in range(1000):
            m = mock.MagicMock(spec=discord.Member)
            m.id = i
            m.bot = False
            m.display_name = f"member_{i:04d}"
            m.name = f"user{i}"
            m.global_name = None
            members.append(m)

        # All members can view
        def perm_for(member):
            perm = mock.MagicMock()
            perm.view_channel = True
            return perm

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.members = members
        guild.me = mock.MagicMock()
        guild.chunked = True
        guild.get_channel.return_value = ch
        ch.permissions_for.side_effect = perm_for

        task_ran = []
        async def other_task():
            await asyncio.sleep(0.01)
            task_ran.append(True)

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                search_task = asyncio.create_task(bot.controller.members_for(1, ""))
                other = asyncio.create_task(other_task())
                result = await search_task
                await other

        # If the search yields, the other task should have run
        self.assertTrue(task_ran, "other task should have run during scan")
        # Scan should have returned truncated=true (capped at 25 results)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["members"]), 25)

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
                result = await bot.controller.members_for(1, "")

        self.assertEqual(result["members"], [])
        self.assertFalse(result["truncated"])

    async def test_chunk_timeout_cache_concurrent_calls(self):
        """Two simultaneous searches on unchunked guild call chunk() exactly once."""
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

        chunk_event = asyncio.Event()
        async def slow_chunk():
            await chunk_event.wait()

        guild = mock.MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.members = members
        guild.me = mock.MagicMock()
        guild.chunked = False
        guild.chunk = mock.AsyncMock(side_effect=slow_chunk)
        guild.get_channel.return_value = ch
        ch.permissions_for.side_effect = perm_for

        with mock.patch.object(bot.bot, "get_guild", return_value=guild):
            with mock.patch.object(bot.config, "channel_for", return_value=100):
                # Start two searches concurrently
                search1 = asyncio.create_task(bot.controller.members_for(1, ""))
                search2 = asyncio.create_task(bot.controller.members_for(1, ""))
                await asyncio.sleep(0.01)  # Let both start waiting

                # Release the chunk call
                guild.chunked = True  # Simulate chunking completed
                chunk_event.set()

                # Wait for both to complete
                result1 = await search1
                result2 = await search2

        # chunk() should have been called exactly once due to the lock
        self.assertEqual(guild.chunk.call_count, 1)

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
