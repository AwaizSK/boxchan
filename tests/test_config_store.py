import asyncio
import glob
import json
import os
import tempfile
import unittest
from unittest import mock

import config_store
from config_store import ConfigStore, ConflictError, EventHub, atomic_write_json


class AtomicWriteTests(unittest.TestCase):
    def test_success_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            atomic_write_json({"a": 1}, p)
            with open(p) as f:
                self.assertEqual(json.load(f), {"a": 1})
            self.assertEqual(os.listdir(d), ["c.json"])

    def test_failure_leaves_no_tmp_and_reraises(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with self.assertRaises(TypeError):
                atomic_write_json({"a": object()}, p)
            self.assertEqual(os.listdir(d), [])
            self.assertEqual(glob.glob(os.path.join(d, "*.tmp")), [])


class EventHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_subscriber_gets_event(self):
        hub = EventHub()
        q1, q2 = hub.subscribe(), hub.subscribe()
        self.assertEqual(hub.subscriber_count, 2)
        hub.publish({"n": 1})
        self.assertEqual(q1.get_nowait(), {"n": 1})
        self.assertEqual(q2.get_nowait(), {"n": 1})

    async def test_unsubscribe_idempotent(self):
        hub = EventHub()
        q = hub.subscribe()
        hub.unsubscribe(q)
        hub.unsubscribe(q)
        self.assertEqual(hub.subscriber_count, 0)
        hub.publish({"n": 1})
        self.assertTrue(q.empty())

    async def test_slow_subscriber_drops_oldest(self):
        hub = EventHub()
        q = hub.subscribe()
        for i in range(64):
            hub.publish({"n": i})
        self.assertTrue(q.full())
        hub.publish({"n": 64})
        self.assertEqual(q.qsize(), 64)
        self.assertEqual(q.get_nowait(), {"n": 1})
        items = []
        while not q.empty():
            items.append(q.get_nowait()["n"])
        self.assertEqual(items[-1], 64)


class ConfigStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = os.path.join(self._td.name, "config.json")

    async def test_rev_increments_and_noop(self):
        s = ConfigStore(self.path)
        self.assertEqual(s.rev, 0)
        snap = await s.set_channel(1, 10, expected_rev=0, editor="a")
        self.assertEqual(snap["rev"], 1)
        self.assertEqual(snap["updated_by"], "a")
        self.assertIsNotNone(snap["updated_at"])
        self.assertEqual(snap["guilds"]["1"], {"channel_id": 10, "enabled": True})
        snap = await s.set_channel(1, 10, expected_rev=None, editor="b")
        self.assertEqual(snap["rev"], 1)
        self.assertEqual(snap["updated_by"], "a")
        snap = await s.set_enabled(1, False, expected_rev=1, editor="c")
        self.assertEqual(snap["rev"], 2)
        snap = await s.set_enabled(1, False, expected_rev=None, editor="c")
        self.assertEqual(snap["rev"], 2)

    async def test_conflict_carries_snapshot_and_no_mutation(self):
        s = ConfigStore(self.path)
        await s.set_channel(1, 10, expected_rev=0, editor="a")
        q = s.hub.subscribe()
        with self.assertRaises(ConflictError) as cm:
            await s.set_channel(1, 20, expected_rev=0, editor="b")
        self.assertEqual(cm.exception.snapshot, s.snapshot())
        self.assertEqual(cm.exception.snapshot["rev"], 1)
        self.assertEqual(s.channel_for(1), 10)
        self.assertEqual(s.rev, 1)
        self.assertTrue(q.empty())
        with self.assertRaises(ConflictError):
            await s.set_enabled(1, False, expected_rev=0, editor="b")
        self.assertTrue(s.is_enabled(1))

    async def test_none_expected_rev_always_applies(self):
        s = ConfigStore(self.path)
        await s.set_channel(1, 10, expected_rev=None, editor="a")
        await s.set_channel(1, 20, expected_rev=None, editor="a")
        self.assertEqual(s.channel_for(1), 20)
        self.assertEqual(s.rev, 2)

    async def test_persistence_round_trip(self):
        s = ConfigStore(self.path)
        await s.set_channel(1, 10, expected_rev=None, editor="a")
        await s.set_enabled(1, False, expected_rev=None, editor="a")
        await s.set_channel(2, 30, expected_rev=None, editor="a")
        s2 = ConfigStore(self.path)
        self.assertEqual(s2.snapshot(), s.snapshot())
        self.assertEqual(s2.rev, 3)
        self.assertEqual(s2.channel_for(2), 30)
        self.assertFalse(s2.is_enabled(1))

    async def test_corrupt_file_starts_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        s = ConfigStore(self.path)
        self.assertEqual(s.rev, 0)
        self.assertEqual(s.snapshot()["guilds"], {})

    async def test_persist_failure_rolls_back(self):
        s = ConfigStore(self.path)
        await s.set_channel(1, 10, expected_rev=None, editor="a")
        before = s.snapshot()
        q = s.hub.subscribe()
        with mock.patch.object(config_store, "atomic_write_json", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                await s.set_channel(1, 20, expected_rev=None, editor="b")
            with self.assertRaises(OSError):
                await s.set_enabled(1, False, expected_rev=None, editor="b")
            with self.assertRaises(OSError):
                await s.set_channel(5, 50, expected_rev=None, editor="b")
        self.assertEqual(s.snapshot(), before)
        self.assertNotIn("5", s.snapshot()["guilds"])
        self.assertEqual(s.rev, 1)
        self.assertTrue(q.empty())

    async def test_truth_table(self):
        s = ConfigStore(self.path)
        self.assertFalse(s.is_listen_channel(1, 10))
        self.assertFalse(s.is_command_channel(1, 10))
        self.assertTrue(s.is_enabled(1))
        self.assertIsNone(s.channel_for(1))
        await s.set_channel(1, 10, expected_rev=None, editor="a")
        self.assertTrue(s.is_listen_channel(1, 10))
        self.assertTrue(s.is_command_channel(1, 10))
        self.assertFalse(s.is_listen_channel(1, 11))
        self.assertFalse(s.is_command_channel(1, 11))
        await s.set_enabled(1, False, expected_rev=None, editor="a")
        self.assertFalse(s.is_listen_channel(1, 10))
        self.assertTrue(s.is_command_channel(1, 10))
        await s.set_channel(1, None, expected_rev=None, editor="a")
        self.assertFalse(s.is_command_channel(1, 10))
        self.assertFalse(s.is_listen_channel(1, 10))

    async def test_publishes_event_after_change(self):
        s = ConfigStore(self.path)
        q = s.hub.subscribe()
        await s.set_channel(1, 10, expected_rev=None, editor="a")
        ev = q.get_nowait()
        self.assertEqual(ev["type"], "config")
        self.assertEqual(ev["snapshot"]["rev"], 1)
        self.assertEqual(ev["change"], {"guild_id": 1, "field": "channel_id", "old": None, "new": 10})
        await s.set_enabled(1, False, expected_rev=None, editor="a")
        ev = q.get_nowait()
        self.assertEqual(ev["change"], {"guild_id": 1, "field": "enabled", "old": True, "new": False})
        await s.set_enabled(1, False, expected_rev=None, editor="a")
        self.assertTrue(q.empty())

    async def test_seed_channel(self):
        s = ConfigStore(self.path)
        self.assertTrue(await s.seed_channel(1, 10))
        self.assertEqual(s.snapshot()["updated_by"], "env-seed")
        self.assertFalse(await s.seed_channel(1, 20))
        self.assertEqual(s.channel_for(1), 10)
        self.assertEqual(s.rev, 1)

    async def test_concurrent_set_channel(self):
        s = ConfigStore(self.path)
        ids = [100 + (i % 2) for i in range(20)]
        calls = [s.set_channel(1, cid, expected_rev=None, editor="t") for cid in ids]
        await asyncio.gather(*calls)
        # lock is FIFO and ids alternate, so every call changes the value
        changes, cur = 0, None
        for cid in ids:
            if cid != cur:
                changes += 1
                cur = cid
        self.assertEqual(changes, 20)
        self.assertEqual(s.rev, changes)
        with open(self.path) as f:
            self.assertEqual(json.load(f), s.snapshot())
        self.assertEqual(glob.glob(os.path.join(self._td.name, "*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
