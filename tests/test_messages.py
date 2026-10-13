import unittest

from messages import MessageError, SendLimiter, parse_mentions


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ParseMentionsTests(unittest.TestCase):
    def test_user_tokens_deduped_in_order(self):
        self.assertEqual(parse_mentions("a <@5> b <@!7> c <@5>"), [5, 7])

    def test_everyone_text_is_not_a_mention(self):
        self.assertEqual(parse_mentions("@everyone @here hi"), [])

    def test_role_token_rejected(self):
        for text in ("<@&123>", "hi <@5> <@&9>"):
            with self.assertRaises(MessageError) as cm:
                parse_mentions(text)
            self.assertEqual(cm.exception.code, "bad_mention")

    def test_malformed_tokens_ignored(self):
        self.assertEqual(parse_mentions("<@abc> <@> <@ 5> <#5> <@٥٥>"), [])

    def test_exactly_10_mentions_ok(self):
        content = " ".join(f"<@{i}>" for i in range(10))
        result = parse_mentions(content)
        self.assertEqual(result, list(range(10)))

    def test_11_distinct_mentions_rejected(self):
        content = " ".join(f"<@{i}>" for i in range(11))
        with self.assertRaises(MessageError) as cm:
            parse_mentions(content)
        self.assertEqual(cm.exception.code, "bad_mention")

    def test_duplicates_count_as_one(self):
        # 10 distinct ids + duplicates = still valid if <= 10 distinct
        content = "<@1> <@2> <@3> <@4> <@5> <@6> <@7> <@8> <@9> <@10> <@1> <@2>"
        result = parse_mentions(content)
        self.assertEqual(len(result), 10)
        self.assertEqual(result, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

    def test_11_distinct_with_duplicates_rejected(self):
        # 11 distinct ids + duplicates = rejected
        content = "<@1> <@2> <@3> <@4> <@5> <@6> <@7> <@8> <@9> <@10> <@11> <@1> <@2>"
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
