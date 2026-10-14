import unittest

from messages import MessageError, SendLimiter, parse_mentions


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ParseMentionsTests(unittest.TestCase):
    def test_user_tokens_deduped_in_order(self):
        self.assertEqual(parse_mentions("a <@5> b <@!7> c <@5>"), ([5, 7], []))

    def test_role_tokens_deduped_in_order(self):
        self.assertEqual(parse_mentions("hi <@&123> there <@&99> again <@&123>"), ([], [123, 99]))

    def test_mixed_user_and_role_tokens(self):
        self.assertEqual(parse_mentions("<@5> and <@&99> with <@!7>"), ([5, 7], [99]))

    def test_everyone_text_is_not_a_mention(self):
        self.assertEqual(parse_mentions("@everyone @here hi"), ([], []))

    def test_malformed_tokens_ignored(self):
        self.assertEqual(parse_mentions("<@abc> <@> <@ 5> <#5> <@٥٥> <@&> <@&abc>"), ([], []))

    def test_exactly_10_mentions_ok(self):
        content = " ".join(f"<@{i}>" for i in range(7)) + " " + " ".join(f"<@&{i+100}>" for i in range(3))
        users, roles = parse_mentions(content)
        self.assertEqual(users, list(range(7)))
        self.assertEqual(roles, list(range(100, 103)))
        self.assertEqual(len(users) + len(roles), 10)

    def test_11_distinct_mentions_rejected(self):
        content = " ".join(f"<@{i}>" for i in range(8)) + " " + " ".join(f"<@&{i+100}>" for i in range(3))
        with self.assertRaises(MessageError) as cm:
            parse_mentions(content)
        self.assertEqual(cm.exception.code, "bad_mention")

    def test_duplicates_count_as_one(self):
        # 10 distinct ids + duplicates = still valid if <= 10 distinct
        content = "<@1> <@2> <@&100> <@3> <@4> <@5> <@&200> <@6> <@7> <@8> <@1> <@&100>"
        users, roles = parse_mentions(content)
        self.assertEqual(len(users) + len(roles), 10)
        self.assertEqual(users, [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(roles, [100, 200])

    def test_11_distinct_with_duplicates_rejected(self):
        # 11 distinct ids + duplicates = rejected
        content = "<@1> <@2> <@3> <@4> <@5> <@6> <@7> <@8> <@9> <@10> <@&11> <@1> <@&11>"
        with self.assertRaises(MessageError) as cm:
            parse_mentions(content)
        self.assertEqual(cm.exception.code, "bad_mention")


class SendLimiterTests(unittest.TestCase):
    def setUp(self):
        self.clk = Clock()
        self.lim = SendLimiter(clock=self.clk)

    def test_two_second_gap(self):
        self.lim.reserve(1)
        self.clk.t += 1.2
        with self.assertRaises(MessageError) as cm:
            self.lim.reserve(1)
        self.assertEqual((cm.exception.code, cm.exception.retry_after), ("rate_limited", 1))
        self.clk.t += 0.8
        self.lim.reserve(1)

    def test_retry_after_is_int_ceiling(self):
        self.lim.reserve(1)
        with self.assertRaises(MessageError) as cm:
            self.lim.reserve(1)
        self.assertEqual(cm.exception.retry_after, 2)
        self.assertIsInstance(cm.exception.retry_after, int)

    def test_ten_per_rolling_minute(self):
        for _ in range(10):
            self.lim.reserve(1)
            self.clk.t += 2.0   # 20 s elapsed after ten sends
        with self.assertRaises(MessageError) as cm:
            self.lim.reserve(1)
        self.assertEqual(cm.exception.retry_after, 40)
        self.clk.t += 39.0
        with self.assertRaises(MessageError):
            self.lim.reserve(1)
        self.clk.t += 1.0
        self.lim.reserve(1)  # the oldest send rolled out

    def test_guilds_are_independent(self):
        self.lim.reserve(1)
        self.lim.reserve(2)

    def test_rejected_attempt_is_not_recorded(self):
        self.lim.reserve(1)
        for _ in range(5):
            with self.assertRaises(MessageError):
                self.lim.reserve(1)
        self.clk.t += 2.0
        self.lim.reserve(1)


if __name__ == "__main__":
    unittest.main()
