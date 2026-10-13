import asyncio
import json
import logging
import os
import tempfile
import time

log = logging.getLogger("config_store")


def atomic_write_json(data: dict, path: str) -> None:
    """Write JSON to a temp file in the same dir, fsync, then atomically replace."""
    directory = os.path.dirname(os.path.abspath(path))
    tmp = tempfile.NamedTemporaryFile("w", dir=directory, prefix=".cfg-", suffix=".tmp", delete=False)
    try:
        try:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


class ConflictError(Exception):
    def __init__(self, snapshot: dict):
        super().__init__("config revision conflict")
        self.snapshot = snapshot


class EventHub:
    def __init__(self):
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=64)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


class ConfigStore:
    def __init__(self, path: str):
        self.path = path
        self.hub = EventHub()
        self.rev = 0
        self.updated_by = None
        self.updated_at = None
        self._guilds: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
            guilds = {}
            for gid, entry in data.get("guilds", {}).items():
                ch = entry.get("channel_id")
                guilds[str(gid)] = {
                    "channel_id": int(ch) if ch is not None else None,
                    "enabled": bool(entry.get("enabled", True)),
                }
            self.rev = int(data.get("rev", 0))
            self.updated_by = data.get("updated_by")
            self.updated_at = data.get("updated_at")
            self._guilds = guilds
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("could not load %s; starting with empty config", self.path)
            self.rev, self.updated_by, self.updated_at, self._guilds = 0, None, None, {}

    def snapshot(self) -> dict:
        return {
            "rev": self.rev,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
            "guilds": {g: dict(e) for g, e in self._guilds.items()},
        }

    def channel_for(self, guild_id: int) -> int | None:
        entry = self._guilds.get(str(guild_id))
        return entry["channel_id"] if entry else None

    def is_enabled(self, guild_id: int) -> bool:
        entry = self._guilds.get(str(guild_id))
        return entry["enabled"] if entry else True

    def is_listen_channel(self, guild_id: int, channel_id: int) -> bool:
        entry = self._guilds.get(str(guild_id))
        return bool(entry) and entry["channel_id"] == channel_id and entry["enabled"]

    def is_command_channel(self, guild_id: int, channel_id: int) -> bool:
        entry = self._guilds.get(str(guild_id))
        return bool(entry) and entry["channel_id"] == channel_id

    async def set_channel(self, guild_id: int, channel_id: int | None, *,
                          expected_rev: int | None, editor: str) -> dict:
        return await self._apply(guild_id, "channel_id", channel_id, expected_rev, editor)

    async def set_enabled(self, guild_id: int, enabled: bool, *,
                          expected_rev: int | None, editor: str) -> dict:
        return await self._apply(guild_id, "enabled", bool(enabled), expected_rev, editor)

    async def seed_channel(self, guild_id: int, channel_id: int) -> bool:
        async with self._lock:
            if str(guild_id) in self._guilds:
                return False
            await self._mutate(guild_id, "channel_id", channel_id, "env-seed")
            return True

    async def _apply(self, guild_id, field, value, expected_rev, editor) -> dict:
        async with self._lock:
            if expected_rev is not None and expected_rev != self.rev:
                raise ConflictError(self.snapshot())
            entry = self._guilds.get(str(guild_id))
            current = entry[field] if entry else (None if field == "channel_id" else True)
            if entry is not None and current == value:
                return self.snapshot()
            return await self._mutate(guild_id, field, value, editor)

    async def _mutate(self, guild_id, field, value, editor) -> dict:
        """Apply, persist, publish. Caller holds the lock."""
        key = str(guild_id)
        had = key in self._guilds
        entry = self._guilds.get(key) or {"channel_id": None, "enabled": True}
        old = entry[field]
        prev = (self.rev, self.updated_by, self.updated_at, dict(self._guilds.get(key) or {}))
        self._guilds[key] = {**entry, field: value}
        self.rev += 1
        self.updated_by = editor
        self.updated_at = time.time()
        snap = self.snapshot()
        try:
            await asyncio.to_thread(atomic_write_json, snap, self.path)
        except BaseException:
            self.rev, self.updated_by, self.updated_at = prev[:3]
            if had:
                self._guilds[key] = prev[3]
            else:
                self._guilds.pop(key, None)
            raise
        self.hub.publish({
            "type": "config",
            "snapshot": snap,
            "change": {"guild_id": guild_id, "field": field, "old": old, "new": value},
        })
        return snap
