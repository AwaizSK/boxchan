import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from aiohttp import streams

import config_store
import webui
from config_store import ConfigStore

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ED = {**AUTH, "X-Editor": "alice"}


class FakeController:
    def __init__(self):
        self.status = {"1": {"playing": False, "watched": 0}}
        self.skips, self.clears = [], []
        self.text_channels = {(1, 10), (1, 11)}

    def guilds_view(self):
        return [{"id": "1", "name": "G", "channels": [
            {"id": "10", "name": "a", "category": None, "position": 0}]}]

    def status_view(self):
        return self.status

    def is_text_channel(self, gid, cid):
        return (gid, cid) in self.text_channels

    def skip(self, gid):
        self.skips.append(gid)
        return True

    def clear_queue(self, gid):
        self.clears.append(gid)
        return 3


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.static = os.path.join(self.tmp.name, "static")
        os.mkdir(self.static)
        for n, c in (("index.html", "<h1>hi</h1>"), ("app.js", "//js"), ("app.css", "/*css*/")):
            with open(os.path.join(self.static, n), "w") as f:
                f.write(c)
        self.cfgpath = os.path.join(self.tmp.name, "c.json")
        self.config = ConfigStore(self.cfgpath)
        self.ctl = FakeController()
        self.clock = Clock()
        self.app = webui.make_app(self.config, self.ctl, TOKEN, status_interval=0.05,
                                  static_dir=self.static, clock=self.clock)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def put_channel(self, body, headers=ED, gid="1"):
        return self.client.put(f"/api/guilds/{gid}/channel", json=body, headers=headers)


class AuthTests(Base):
    async def test_missing_wrong_right(self):
        r = await self.client.get("/api/state")
        self.assertEqual(r.status, 401)
        self.assertEqual(await r.json(), {"error": "unauthorized"})
        r = await self.client.get("/api/state", headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status, 401)
        r = await self.client.get("/api/state", headers={"Authorization": TOKEN})
        self.assertEqual(r.status, 401)
        r = await self.client.get("/api/state", headers=AUTH)
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["config"]["rev"], 0)
        self.assertEqual(body["guilds"][0]["id"], "1")

    async def test_throttle(self):
        for _ in range(5):
            r = await self.client.get("/api/state", headers={"Authorization": "Bearer x"})
            self.assertEqual(r.status, 401)
        r = await self.client.get("/api/state", headers=AUTH)
        self.assertEqual(r.status, 429)
        self.clock.t += 61
        r = await self.client.get("/api/state", headers=AUTH)
        self.assertEqual(r.status, 200)

    async def test_failures_outside_window_dont_count(self):
        for _ in range(4):
            await self.client.get("/api/state")
        self.clock.t += 61
        await self.client.get("/api/state")
        r = await self.client.get("/api/state", headers=AUTH)
        self.assertEqual(r.status, 200)

    async def test_static_no_auth_and_headers(self):
        for path, ct in (("/", "text/html"), ("/app.js", "javascript"), ("/app.css", "text/css")):
            r = await self.client.get(path)
            self.assertEqual(r.status, 200)
            self.assertIn(ct, r.headers["Content-Type"])
            self.assertEqual(r.headers["Cache-Control"], "no-cache")
            self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        r = await self.client.get("/api/state", headers=AUTH)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(r.headers["Content-Security-Policy"],
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        r = await self.client.get("/api/state")  # 401 also carries headers
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        r = await self.client.get("/nope")
        self.assertEqual(r.status, 404)
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    async def test_missing_static_does_not_crash(self):
        os.unlink(os.path.join(self.static, "app.js"))
        app = webui.make_app(self.config, self.ctl, TOKEN, static_dir=self.static)
        async with TestClient(TestServer(app)) as c:
            self.assertEqual((await c.get("/app.js")).status, 500)
            self.assertEqual((await c.get("/")).status, 200)

    async def test_token_never_in_body(self):
        await self.put_channel({"channel_id": "10", "rev": None})
        for method, path in (("get", "/api/state"), ("get", "/"), ("get", "/nope"),
                             ("post", "/api/guilds/1/skip")):
            r = await getattr(self.client, method)(path, headers=ED)
            self.assertNotIn(TOKEN, await r.text())
        r = await self.client.get("/api/state", headers={"Authorization": "Bearer wrong"})
        self.assertNotIn(TOKEN, await r.text())


class WriteTests(Base):
    async def test_put_channel_success_event_persisted(self):
        q = self.config.hub.subscribe()
        r = await self.put_channel({"channel_id": "10", "rev": 0})
        self.assertEqual(r.status, 200)
        snap = (await r.json())["snapshot"]
        self.assertEqual(snap["rev"], 1)
        self.assertEqual(snap["updated_by"], "alice")
        self.assertEqual(snap["guilds"]["1"]["channel_id"], "10")
        ev = q.get_nowait()
        self.assertEqual(ev["type"], "config")
        with open(self.cfgpath) as f:
            self.assertEqual(json.load(f)["guilds"]["1"]["channel_id"], 10)
        self.assertEqual(self.config.channel_for(1), 10)

    async def test_clear_channel_null(self):
        await self.put_channel({"channel_id": "10", "rev": None})
        r = await self.put_channel({"channel_id": None, "rev": None})
        self.assertEqual(r.status, 200)
        self.assertIsNone((await r.json())["snapshot"]["guilds"]["1"]["channel_id"])

    async def test_conflict(self):
        await self.put_channel({"channel_id": "10", "rev": None})
        r = await self.put_channel({"channel_id": "11", "rev": 0})
        self.assertEqual(r.status, 409)
        body = await r.json()
        self.assertEqual(body["error"], "conflict")
        self.assertEqual(body["snapshot"]["rev"], 1)
        self.assertEqual(body["snapshot"]["guilds"]["1"]["channel_id"], "10")

    async def test_bad_channel(self):
        for ch in ("99", "abc", 10, "", "-1"):
            r = await self.put_channel({"channel_id": ch, "rev": None})
            self.assertEqual(r.status, 400, ch)
            self.assertEqual((await r.json())["error"], "bad_channel")
        self.assertEqual(self.config.rev, 0)

    async def test_editor_required_and_sanitized(self):
        r = await self.put_channel({"channel_id": "10", "rev": None}, headers=AUTH)
        self.assertEqual(r.status, 400)
        self.assertEqual((await r.json())["error"], "editor_required")
        r = await self.put_channel({"channel_id": "10", "rev": None}, headers={**AUTH, "X-Editor": " \t "})
        self.assertEqual(r.status, 400)
        r = await self.put_channel({"channel_id": "10", "rev": None},
                                   headers={**AUTH, "X-Editor": "ab   c" + "z" * 100})
        self.assertEqual(r.status, 200)
        by = (await r.json())["snapshot"]["updated_by"]
        self.assertTrue(by.startswith("ab c"))
        self.assertEqual(len(by), 40)

    async def test_editor_percent_encoded(self):
        from urllib.parse import quote
        r = await self.put_channel({"channel_id": "10", "rev": None},
                                   headers={**AUTH, "X-Editor": quote("\u7530\u4e2d \U0001F600 Jos\u00e9")})
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["snapshot"]["updated_by"], "\u7530\u4e2d \U0001F600 Jos\u00e9")
        r = await self.put_channel({"channel_id": "11", "rev": None},
                                   headers={**AUTH, "X-Editor": "al%00ic%07e%1B"})
        self.assertEqual((await r.json())["snapshot"]["updated_by"], "alice")
        r = await self.put_channel({"channel_id": "10", "rev": None}, headers={**AUTH, "X-Editor": "bob"})
        self.assertEqual((await r.json())["snapshot"]["updated_by"], "bob")
        for bad in ("100%", "%zz", "%E4%B8", "%"):
            r = await self.put_channel({"channel_id": "11", "rev": None}, headers={**AUTH, "X-Editor": "x" + bad})
            self.assertIn(r.status, (200, 400), bad)

    def test_sanitize(self):
        self.assertEqual(webui.sanitize_editor("  x\n\ty\x00 "), "x y")
        self.assertIsNone(webui.sanitize_editor("\x01\x02"))
        self.assertIsNone(webui.sanitize_editor(None))

    async def test_enabled_toggle(self):
        r = await self.client.put("/api/guilds/1/enabled", json={"enabled": False, "rev": None}, headers=ED)
        self.assertEqual(r.status, 200)
        self.assertFalse((await r.json())["snapshot"]["guilds"]["1"]["enabled"])
        self.assertFalse(self.config.is_enabled(1))
        r = await self.client.put("/api/guilds/1/enabled", json={"enabled": "no", "rev": None}, headers=ED)
        self.assertEqual(r.status, 400)
        r = await self.client.put("/api/guilds/1/enabled", json={"enabled": True, "rev": 0}, headers=ED)
        self.assertEqual(r.status, 409)
        r = await self.client.put("/api/guilds/1/enabled", json={"enabled": True, "rev": 0}, headers=AUTH)
        self.assertEqual(r.status, 400)

    async def test_bad_bodies_and_ids(self):
        r = await self.client.put("/api/guilds/1/channel", data=b"{not json", headers=ED)
        self.assertEqual(r.status, 400)
        r = await self.client.put("/api/guilds/1/channel", json=[1], headers=ED)
        self.assertEqual(r.status, 400)
        r = await self.put_channel({"channel_id": "10", "rev": "0"})
        self.assertEqual(r.status, 400)
        r = await self.put_channel({"channel_id": "10", "rev": True})
        self.assertEqual(r.status, 400)
        r = await self.put_channel({"channel_id": "10", "rev": None}, gid="2")
        self.assertEqual(r.status, 404)
        r = await self.put_channel({"channel_id": "10", "rev": None}, gid="x")
        self.assertEqual(r.status, 404)
        r = await self.client.put("/api/guilds/1/channel", data=b"x" * 5000, headers=ED)
        self.assertEqual(r.status, 413)

    async def test_skip_clear(self):
        r = await self.client.post("/api/guilds/1/skip", headers=ED)
        self.assertEqual(await r.json(), {"skipped": True})
        r = await self.client.post("/api/guilds/1/clear", headers=ED)
        self.assertEqual(await r.json(), {"cleared": 3})
        self.assertEqual((self.ctl.skips, self.ctl.clears), ([1], [1]))
        r = await self.client.post("/api/guilds/1/skip", headers=AUTH)
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/guilds/2/skip", headers=ED)
        self.assertEqual(r.status, 404)
        self.assertEqual(self.ctl.skips, [1])

    async def test_handler_cancel_mid_write_stays_consistent(self):
        started, release = threading.Event(), threading.Event()
        real = config_store.atomic_write_json

        def slow(data, path):
            started.set()
            release.wait(5)
            real(data, path)

        route = next(r for r in self.app.router.routes()
                     if r.method == "PUT" and r.resource.canonical.endswith("/channel"))
        payload = streams.StreamReader(mock.Mock(_reading_paused=False), 2 ** 16,
                                       loop=asyncio.get_running_loop())
        payload.feed_data(json.dumps({"channel_id": "10", "rev": None}).encode())
        payload.feed_eof()
        req = make_mocked_request("PUT", "/api/guilds/1/channel", headers={"X-Editor": "bob"},
                                  match_info={"gid": "1"}, app=self.app, payload=payload)
        with mock.patch.object(config_store, "atomic_write_json", slow):
            task = asyncio.ensure_future(route.handler(req))
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            for _ in range(200):
                if self.config.rev == 1 and os.path.exists(self.cfgpath):
                    break
                await asyncio.sleep(0.01)
        with open(self.cfgpath) as f:
            disk = json.load(f)
        self.assertEqual(disk["rev"], self.config.rev)
        self.assertEqual(disk["guilds"]["1"]["channel_id"], self.config.channel_for(1))
        self.assertEqual(self.config.channel_for(1), 10)


async def read_event(resp, timeout=2):
    """Read one SSE block (skipping comments); returns (event, data) or None for a comment-only block."""
    async def go():
        buf = b""
        while not buf.endswith(b"\n\n"):
            buf += await resp.content.readline()
        return buf.decode()
    block = await asyncio.wait_for(go(), timeout)
    ev = data = None
    for line in block.splitlines():
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
    return ev, data


class SSETests(Base):
    async def test_hello_config_status_and_cleanup(self):
        r = await self.client.get("/api/events", headers=AUTH)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertEqual(r.headers["X-Accel-Buffering"], "no")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        ev, data = await read_event(r)
        self.assertEqual(ev, "hello")
        self.assertEqual(data["snapshot"]["rev"], 0)
        self.assertEqual(data["guilds"][0]["id"], "1")
        self.assertEqual(self.config.hub.subscriber_count, 1)

        await self.put_channel({"channel_id": "10", "rev": None})
        seen = []
        while "config" not in [e for e, _ in seen]:
            seen.append(await read_event(r))
        cfg = dict(seen)["config"]
        self.assertEqual(cfg["snapshot"]["guilds"]["1"]["channel_id"], "10")
        self.assertEqual(cfg["change"]["guild_id"], "1")
        self.assertEqual(cfg["change"]["new"], "10")

        # initial status, then silence while unchanged
        got = list(seen)
        while "status" not in [e for e, _ in got]:
            got.append(await read_event(r))
        with self.assertRaises(asyncio.TimeoutError):
            await read_event(r, timeout=0.4)
        self.ctl.status = {"1": {"playing": True, "watched": 2}}
        ev, data = await read_event(r)
        self.assertEqual((ev, data), ("status", self.ctl.status))

        r.close()
        for _ in range(100):
            if self.config.hub.subscriber_count == 0:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.config.hub.subscriber_count, 0)

    async def test_requires_auth(self):
        r = await self.client.get("/api/events")
        self.assertEqual(r.status, 401)
        self.assertEqual(self.config.hub.subscriber_count, 0)

    async def test_keepalive(self):
        app = webui.make_app(self.config, self.ctl, TOKEN, status_interval=10,
                             keepalive_interval=0.05, static_dir=self.static)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/events", headers=AUTH)
            await read_event(r)
            line = await asyncio.wait_for(r.content.readline(), 2)
            while line.strip() in (b"", b"event: status") or line.startswith((b"data:", b"event:")):
                line = await asyncio.wait_for(r.content.readline(), 2)
            self.assertEqual(line.strip(), b": keepalive")
            r.close()


class HardeningTests(Base):
    async def test_bad_tokens_rejected(self):
        for t in ("", "x" * 23):
            with self.assertRaises(ValueError):
                webui.make_app(self.config, self.ctl, t, static_dir=self.static)

    async def test_header_parsing(self):
        r = await self.client.get("/api/state", headers={"Authorization": f"bearer {TOKEN}"})
        self.assertEqual(r.status, 200)
        r = await self.client.get("/api/state", headers={"Authorization": f"Basic {TOKEN}"})
        self.assertEqual(r.status, 401)
        r = await self.client.get("/api/state", headers={"Authorization": "Bearer "})
        self.assertEqual(r.status, 401)


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_sse_does_not_block_cleanup(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            cfg = ConfigStore(os.path.join(d, "c.json"))
            app = webui.make_app(cfg, FakeController(), TOKEN, static_dir=d)
            runner = await webui.start_webui(app, "127.0.0.1", 0)
            port = runner.addresses[0][1]
            import aiohttp
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"http://127.0.0.1:{port}/api/events", headers=AUTH)
                await r.content.readline()
                t0 = time.monotonic()
                await runner.cleanup()
                self.assertLess(time.monotonic() - t0, 3)
            self.assertEqual(cfg.hub.subscriber_count, 0)


class TokenLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, text):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def env(self, **kw):
        base = {k: v for k, v in os.environ.items()
                if k not in ("CREDENTIALS_DIRECTORY", "WEBUI_TOKEN_FILE", "WEBUI_TOKEN")}
        base.update(kw)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_order(self):
        cred = os.path.join(self.tmp.name, "cred")
        os.mkdir(cred)
        with open(os.path.join(cred, "gui_token"), "w") as f:
            f.write("c" * 30 + "\n")
        tf = self.write("tf", "f" * 30)
        with self.env(CREDENTIALS_DIRECTORY=cred, WEBUI_TOKEN_FILE=tf, WEBUI_TOKEN="e" * 30):
            self.assertEqual(webui.load_gui_token(), "c" * 30)
        with self.env(WEBUI_TOKEN_FILE=tf, WEBUI_TOKEN="e" * 30):
            self.assertEqual(webui.load_gui_token(), "f" * 30)
        with self.env(WEBUI_TOKEN_FILE=os.path.join(self.tmp.name, "missing"), WEBUI_TOKEN=" " + "e" * 30 + " "):
            self.assertEqual(webui.load_gui_token(), "e" * 30)

    def test_none_and_short(self):
        with self.env(WEBUI_TOKEN_FILE=os.path.join(self.tmp.name, "missing")):
            self.assertIsNone(webui.load_gui_token())
        tf = self.write("short", "abc123")
        with self.env(WEBUI_TOKEN_FILE=tf):
            with self.assertLogs("webui", "WARNING") as cm:
                self.assertIsNone(webui.load_gui_token())
            self.assertNotIn("abc123", "".join(cm.output))
        with self.env(WEBUI_TOKEN_FILE=os.path.join(self.tmp.name, "missing"), WEBUI_TOKEN="x" * 23):
            self.assertIsNone(webui.load_gui_token())


class StartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            app = webui.make_app(ConfigStore(os.path.join(d, "c.json")), FakeController(), TOKEN,
                                 static_dir=d)
            runner = await webui.start_webui(app, "127.0.0.1", 0)
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
