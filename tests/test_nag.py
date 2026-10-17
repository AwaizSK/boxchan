import os
import random
import tempfile
import unittest

import nag

DAY = 86400.0


class NagTests(unittest.TestCase):
    def test_due_only_after_a_week_and_once_per_stale_week(self):
        now = 100 * DAY
        self.assertFalse(nag.due(now - 6 * DAY, 0, now))
        self.assertTrue(nag.due(now - 8 * DAY, 0, now))
        self.assertFalse(nag.due(now - 8 * DAY, now - DAY, now))   # nagged yesterday
        self.assertTrue(nag.due(now - 20 * DAY, now - 8 * DAY, now))
        self.assertFalse(nag.due(0, 0, now))                       # no files found

    def test_parse_ids(self):
        self.assertEqual(nag.parse_ids("123, 456"), [123, 456])
        self.assertEqual(nag.parse_ids("1,2,3"), [1, 2])
        self.assertEqual(nag.parse_ids("x,12;34"), [12, 34])
        self.assertEqual(nag.parse_ids(None), [])

    def test_message_mentions_both(self):
        msg = nag.build_message([11, 22], 9, random.Random(1))
        self.assertIn("<@11>", msg)
        self.assertIn("<@22>", msg)
        self.assertIn("9", msg)

    def test_newest_mtime_and_load_last(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(nag.newest_mtime(d), 0.0)
            open(os.path.join(d, "a.py"), "w").close()
            self.assertGreater(nag.newest_mtime(d), 0)
            self.assertEqual(nag.load_last(os.path.join(d, "none.json")), 0.0)


if __name__ == "__main__":
    unittest.main()
