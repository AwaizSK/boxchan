"""Handler cancelled (client disconnect) while a shielded inner task runs; the task then fails.

Each of the three `asyncio.shield` sites in webui.py (config write, apply_voice, send_message) must
attach `_make_shielded_callback`: unexpected failures are logged server-side with exc_info, expected
ones are silent, and in neither case does asyncio report "Task exception was never retrieved".
The handler is driven directly with a mocked request and cancelled, which is what aiohttp does to a
handler task on disconnect."""
import asyncio
import gc
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_webui as tw  # noqa: E402

from aiohttp import streams  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402
from config_store import ConflictError  # noqa: E402
from messages import MessageError  # noqa: E402
from voices import VoiceLoadError  # noqa: E402

SECRET = "boom-secret-path /etc/x"
ED = {"X-Editor": "bob"}


class ShieldDisconnectBase(tw.Base):
    async def asyncSetUp(self):
        tw.quiet_loggers(self)
        await super().asyncSetUp()
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda lp, ctx: self.loop_errors.append(ctx))
        self.addCleanup(loop.set_exception_handler, None)
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    def handler(self, method, path):
        template = path.replace("/guilds/1/", "/guilds/{gid}/")
        route = next(r for r in self.app.router.routes()
                     if r.method == method and r.resource.canonical == template)
        return route.handler

    def request(self, method, path, body):
        payload = streams.StreamReader(mock.Mock(_reading_paused=False), 2 ** 16, loop=asyncio.get_running_loop())
        payload.feed_data(json.dumps(body).encode())
        payload.feed_eof()
        info = {"gid": "1"} if "/guilds/" in path else {}
        return make_mocked_request(method, path, headers=ED, app=self.app, payload=payload, match_info=info)

    async def stub(self, exc):
        self.entered.set()
        await self.gate.wait()
        raise exc

    async def cancel_then_fail(self, method, path, body):
        """Run the handler, cancel it while the stub blocks, then release the stub (which raises)."""
        task = asyncio.ensure_future(self.handler(method, path)(self.request(method, path, body)))
        await asyncio.wait_for(self.entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.gate.set()

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        gc.collect()
        for _ in range(3):
            await asyncio.sleep(0)

    async def assert_no_leak_in_follow_up(self):
        r = await self.client.get("/api/state")
        self.assertEqual(r.status, 200)
        self.assertNotIn("boom-secret", await r.text())

    # one place that knows how to wire each site
    def arm(self, site, exc):
        if site == "config_write":
            async def set_channel(*a, **k):
                await self.stub(exc)
            p = mock.patch.object(self.config, "set_channel", set_channel)
            req = ("PUT", "/api/guilds/1/channel", {"channel_id": "10", "rev": None})
        elif site == "apply_voice":
            p = mock.patch.object(self.ctl, "apply_voice", lambda *a: self.stub(exc))
            req = ("PUT", "/api/voice", {"name": "amy", "rev": None})
        else:
            p = mock.patch.object(self.ctl, "send_message", lambda *a: self.stub(exc))
            req = ("POST", "/api/guilds/1/send", {"content": "hello"})
        p.start()
        self.addCleanup(p.stop)
        return req


class UnexpectedFailureAfterDisconnect(ShieldDisconnectBase):
    async def run_site(self, site):
        req = self.arm(site, RuntimeError(SECRET))
        with self.assertLogs("webui", "ERROR") as cm:
            await self.cancel_then_fail(*req)
            await self.settle()
        rec = [r for r in cm.records if f"{site} failed after client disconnect" in r.getMessage()]
        self.assertEqual(len(rec), 1, cm.output)
        self.assertIsNotNone(rec[0].exc_info)
        self.assertIsInstance(rec[0].exc_info[1], RuntimeError)
        self.assertEqual(self.loop_errors, [], "Task exception was never retrieved")
        await self.assert_no_leak_in_follow_up()

    async def test_config_write(self):
        await self.run_site("config_write")

    async def test_apply_voice(self):
        await self.run_site("apply_voice")

    async def test_send_message(self):
        await self.run_site("send_message")


class ExpectedFailureAfterDisconnect(ShieldDisconnectBase):
    async def run_site(self, site, exc):
        req = self.arm(site, exc)
        with self.assertNoLogs("webui", "ERROR"):
            await self.cancel_then_fail(*req)
            await self.settle()
        self.assertEqual(self.loop_errors, [], "Task exception was never retrieved")

    async def test_config_write_conflict(self):
        await self.run_site("config_write", ConflictError(self.config.snapshot()))

    async def test_apply_voice_load_error(self):
        await self.run_site("apply_voice", VoiceLoadError("x"))

    async def test_send_message_error(self):
        await self.run_site("send_message", MessageError("no_channel"))


if __name__ == "__main__":
    unittest.main()
