import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

# Must be set before importing bot: no real token, state/config in a temp dir.
_TMP = tempfile.mkdtemp(prefix="tts-bot-test-")
os.environ.pop("DISCORD_TOKEN", None)
os.environ.pop("CREDENTIALS_DIRECTORY", None)
os.environ["DISCORD_TOKEN_FILE"] = os.path.join(_TMP, "no-such-token")
os.environ["STATE_FILE"] = os.path.join(_TMP, "state.json")
os.environ["CONFIG_FILE"] = os.path.join(_TMP, "config.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

import bot  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
