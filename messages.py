"""Pure stdlib helpers for "send a message as the bot" (shared by bot.py and webui.py):
the error type, mention parsing and the per-guild rate limiter. No Discord objects here."""
import math
import re
import time
from collections import defaultdict, deque

MAX_MESSAGE_CHARS = 2000
RATE_PER_WINDOW = 10    # sends per rolling window, per guild
RATE_WINDOW = 60.0      # seconds
RATE_MIN_GAP = 2.0      # minimum seconds between two sends, per guild

USER_MENTION_RE = re.compile(r"<@!?(\d{1,20})>")
ROLE_MENTION_RE = re.compile(r"<@&\d+>")


class MessageError(Exception):
    """Controller -> web layer. `code` is the wire error code; `retry_after` only for rate_limited."""

    def __init__(self, code: str, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def parse_mentions(content: str) -> list[int]:
    """User ids from `<@id>` / `<@!id>` tokens, first-seen order, de-duplicated.
    Role tokens `<@&id>` raise MessageError('bad_mention') (rejected, never silently stripped).
    The literal text @everyone / @here is allowed but can never ping (allowed_mentions)."""
    if ROLE_MENTION_RE.search(content):
        raise MessageError("bad_mention")
    seen: dict[int, None] = {}
    for m in USER_MENTION_RE.finditer(content):
        seen.setdefault(int(m.group(1)), None)
    return list(seen)


class SendLimiter:
    """At most `per_window` sends per rolling `window` seconds and `min_gap` seconds between
    sends, tracked per key (guild id). `reserve` records the send immediately (call it BEFORE the
    Discord request so a cancelled/duplicated request cannot bypass the limit)."""

    def __init__(self, clock=time.monotonic, per_window: int = RATE_PER_WINDOW,
                 window: float = RATE_WINDOW, min_gap: float = RATE_MIN_GAP):
        self._clock, self._per, self._window, self._gap = clock, per_window, window, min_gap
        self._sent: dict[object, deque] = defaultdict(deque)

    def reserve(self, key) -> None:
        """Record a send now, or raise MessageError('rate_limited', retry_after=<int seconds>)."""
        now = self._clock()
        q = self._sent[key]
        while q and now - q[0] >= self._window:
            q.popleft()
        wait = 0.0
        if q:
            wait = max(wait, q[-1] + self._gap - now)
        if len(q) >= self._per:
            wait = max(wait, q[0] + self._window - now)
        if wait > 0:
            raise MessageError("rate_limited", retry_after=max(1, math.ceil(wait)))
        q.append(now)
        if len(self._sent) > 1024:  # bounded: drop idle guilds
            for k in [k for k, v in self._sent.items() if not v or now - v[-1] >= self._window]:
                del self._sent[k]
