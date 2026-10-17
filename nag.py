"""Easter egg: if the deployed code has not changed for a week, tease two friends into coding.
Pure stdlib. Off unless NAG_USER_IDS and NAG_CHANNEL_ID are set (see bot.py nag_loop)."""
import glob
import json
import os
import random

WEEK = 7 * 86400.0

LINES = [
    "{who} the code has not moved in {days} days. The bot is writing more commits than you two. Open an editor. Any editor. Even Notepad counts.",
    "{who} {days} days with zero commits. A rubber duck has better debugging stats than both of you. Go learn a for loop.",
    "{who} this repo has been staring at the ceiling for {days} days waiting for you two. `print('hello world')` is a fine place to start.",
    "{who} {days} days. No commits. The tutorial you bookmarked is gathering dust. Chapter one is free and the semicolons are gentle.",
    "{who} bold of you to enjoy the bot while it has gone {days} days without a single line from either of you. Ship something. Even a typo fix.",
    "{who} breaking news: nothing has been deployed for {days} days. Sources say two friends are 'about to start' coding. Sources are skeptical.",
]


def newest_mtime(app_dir: str) -> float:
    """Newest modification time of the bot's top-level .py files (deploy re-installs them, so this is the last deploy)."""
    times = []
    for p in glob.glob(os.path.join(app_dir, "*.py")):
        try:
            times.append(os.path.getmtime(p))
        except OSError:
            pass
    return max(times) if times else 0.0


def parse_ids(raw: str | None) -> list[int]:
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isascii() and part.isdigit() and len(part) <= 25:
            out.append(int(part))
    return out[:2]


def due(newest: float, last_nag: float, now: float, threshold: float = WEEK) -> bool:
    """Code is a week old, and we have not already nagged since it went stale (so: once per stale week)."""
    if newest <= 0 or now - newest < threshold:
        return False
    return now - last_nag >= threshold


def build_message(user_ids: list[int], days: int, rng=random) -> str:
    who = " ".join(f"<@{i}>" for i in user_ids)
    return rng.choice(LINES).format(who=who, days=days)


def load_last(path: str) -> float:
    try:
        with open(path) as f:
            v = json.load(f).get("last_nag", 0)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
    except (OSError, ValueError, AttributeError):
        return 0.0
