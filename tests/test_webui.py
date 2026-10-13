import asyncio
import json
import logging
import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from aiohttp import streams

import config_store
import webui
from config_store import ConfigStore

ED = {"X-Editor": "alice"}


class FakeController:
    def __init__(self):
        self.status = {"1": {"playing": False, "watched": 0}}
        self.skips, self.clears = [], []
        self.text_channels = {(1, 10), (1, 11)}
        self.voice = {"current": "amy", "configured": None, "loading": False}
        self.voice_list = [{"name": "amy", "sample_rate": 22050, "quality": "medium",
                            "language": "en-us", "size_mb": 60.5}]
        self.apply_calls, self.apply_exc, self.apply_gate, self.apply_result = [], None, None, None
        self.say_calls, self.say_exc = [], None

    def voice_summary(self):
        return dict(self.voice)

    async def list_voices(self):
        return {"voices": self.voice_list, **self.voice}

    async def apply_voice(self, name, expected_rev, editor):
        self.apply_calls.append((name, expected_rev, editor))
        if self.apply_gate is not None:
            await self.apply_gate.wait()
        if self.apply_exc is not None:
            raise self.apply_exc
        return self.apply_result or {"rev": 1, "updated_by": editor, "updated_at": 1.0, "voice": name, "guilds": {}}

    def say(self, gid, text):
        self.say_calls.append((gid, text))
        if self.say_exc is not None:
            raise self.say_exc
        return {"queued": True}

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
        self.app = webui.make_app(self.config, self.ctl, allowed_hosts=[], status_interval=0.05,
                                  static_dir=self.static)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def put_channel(self, body, headers=ED, gid="1"):
        return self.client.put(f"/api/guilds/{gid}/channel", json=body, headers=headers)


class OpenAccessTests(Base):
    async def test_api_needs_no_authorization(self):
        r = await self.client.get("/api/state")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["config"]["rev"], 0)
        self.assertEqual(body["guilds"][0]["id"], "1")

    async def test_stray_authorization_header_ignored(self):
        for h in ("Bearer nope", "Basic abc", "garbage"):
            r = await self.client.get("/api/state", headers={"Authorization": h})
            self.assertEqual(r.status, 200, h)
        r = await self.put_channel({"channel_id": "10", "rev": None}, headers={**ED, "Authorization": "Bearer x"})
        self.assertEqual(r.status, 200)

    async def test_static_and_headers(self):
        for path, ct in (("/", "text/html"), ("/app.js", "javascript"), ("/app.css", "text/css")):
            r = await self.client.get(path)
            self.assertEqual(r.status, 200)
            self.assertIn(ct, r.headers["Content-Type"])
            self.assertEqual(r.headers["Cache-Control"], "no-cache")
            self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        r = await self.client.get("/api/state")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(r.headers["Content-Security-Policy"],
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        r = await self.client.get("/nope")
        self.assertEqual(r.status, 404)
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    async def test_missing_static_does_not_crash(self):
        os.unlink(os.path.join(self.static, "app.js"))
        app = webui.make_app(self.config, self.ctl, allowed_hosts=[], static_dir=self.static)
        async with TestClient(TestServer(app)) as c:
            self.assertEqual((await c.get("/app.js")).status, 500)
            self.assertEqual((await c.get("/")).status, 200)


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

    async def test_persist_failure_is_json_500(self):
        with mock.patch.object(config_store, "atomic_write_json",
                               side_effect=PermissionError(13, "denied", "/secret/path")):
            with self.assertLogs("webui", level="ERROR"):
                r = await self.put_channel({"channel_id": "10"})
        self.assertEqual(r.status, 500)
        self.assertEqual(await r.json(), {"error": "persist_failed"})
        self.assertNotIn("secret", await r.text())
        self.assertEqual(self.config.snapshot()["rev"], 0)

    async def test_bad_channel(self):
        for ch in ("99", "abc", 10, "", "-1"):
            r = await self.put_channel({"channel_id": ch, "rev": None})
            self.assertEqual(r.status, 400, ch)
            self.assertEqual((await r.json())["error"], "bad_channel")
        self.assertEqual(self.config.rev, 0)

    async def test_editor_required_and_sanitized(self):
        r = await self.put_channel({"channel_id": "10", "rev": None}, headers={})
        self.assertEqual(r.status, 400)
        self.assertEqual((await r.json())["error"], "editor_required")
        r = await self.put_channel({"channel_id": "10", "rev": None}, headers={"X-Editor": " \t "})
        self.assertEqual(r.status, 400)
        r = await self.put_channel({"channel_id": "10", "rev": None},
                                   headers={"X-Editor": "ab   c" + "z" * 100})
        self.assertEqual(r.status, 200)
        by = (await r.json())["snapshot"]["updated_by"]
        self.assertTrue(by.startswith("ab c"))
        self.assertEqual(len(by), 40)

    async def test_editor_percent_encoded(self):
        from urllib.parse import quote
        r = await self.put_channel({"channel_id": "10", "rev": None},
                                   headers={"X-Editor": quote("\u7530\u4e2d \U0001F600 Jos\u00e9")})
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["snapshot"]["updated_by"], "\u7530\u4e2d \U0001F600 Jos\u00e9")
        r = await self.put_channel({"channel_id": "11", "rev": None},
                                   headers={"X-Editor": "al%00ic%07e%1B"})
        self.assertEqual((await r.json())["snapshot"]["updated_by"], "alice")
        r = await self.put_channel({"channel_id": "10", "rev": None}, headers={"X-Editor": "bob"})
        self.assertEqual((await r.json())["snapshot"]["updated_by"], "bob")
        for bad in ("100%", "%zz", "%E4%B8", "%"):
            r = await self.put_channel({"channel_id": "11", "rev": None}, headers={"X-Editor": "x" + bad})
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
        r = await self.client.put("/api/guilds/1/enabled", json={"enabled": True, "rev": 0})
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
        r = await self.client.post("/api/guilds/1/skip")
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
        r = await self.client.get("/api/events")
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

    async def test_keepalive(self):
        app = webui.make_app(self.config, self.ctl, allowed_hosts=[], status_interval=10,
                             keepalive_interval=0.05, static_dir=self.static)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/events")
            await read_event(r)
            line = await asyncio.wait_for(r.content.readline(), 2)
            while line.strip() in (b"", b"event: status") or line.startswith((b"data:", b"event:")):
                line = await asyncio.wait_for(r.content.readline(), 2)
            self.assertEqual(line.strip(), b": keepalive")
            r.close()


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_sse_does_not_block_cleanup(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            cfg = ConfigStore(os.path.join(d, "c.json"))
            app = webui.make_app(cfg, FakeController(), allowed_hosts=[], static_dir=d)
            quiet_loggers(self)
            with self.assertLogs("webui", "INFO"):
                runner = await webui.start_webui(app, ["127.0.0.1"], 0)
            port = runner.addresses[0][1]
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"http://127.0.0.1:{port}/api/events")
                await r.content.readline()
                t0 = time.monotonic()
                await runner.cleanup()
                self.assertLess(time.monotonic() - t0, 3)
                r.close()
            self.assertEqual(cfg.hub.subscriber_count, 0)


def quiet_loggers(case):
    """Silence per-request access logs / server banners in new tests (stderr stays pristine)."""
    for name in ("aiohttp.access", "asyncio"):
        lg = logging.getLogger(name)
        old = lg.level
        lg.setLevel(logging.WARNING)
        case.addCleanup(lg.setLevel, old)


def free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


class BindPolicyTests(unittest.TestCase):
    def test_accepted(self):
        v = webui.validate_bind_hosts
        self.assertEqual(v(["127.0.0.1"]), ["127.0.0.1"])
        self.assertEqual(v(["127.5.6.7", "::1"]), ["127.5.6.7", "::1"])
        self.assertEqual(v(["100.64.0.1", "100.127.255.254"]), ["100.64.0.1", "100.127.255.254"])
        self.assertEqual(v(["fd7a:115c:a1e0::1", "FD7A:115C:A1E0:0:0:0:0:2"]),
                         ["fd7a:115c:a1e0::1", "fd7a:115c:a1e0::2"])
        self.assertEqual(v([" 127.0.0.1 "]), ["127.0.0.1"])

    def test_localhost_mapped_and_deduped(self):
        self.assertEqual(webui.validate_bind_hosts(["localhost", "LocalHost", "127.0.0.1", "::1", "::1"]),
                         ["127.0.0.1", "::1"])

    def test_rejected_names_the_entry(self):
        for bad in ("0.0.0.0", "::", "192.168.1.5", "10.0.0.1", "172.16.0.1", "100.128.0.1", "100.63.255.255",
                    "fe80::1", "fd7b::1", "fd7a:115c:a1e1::1", "example.com", "tailnet.ts.net", "",
                    "::ffff:127.0.0.1", "fd7a:115c:a1e0::1%eth0", "8.8.8.8", "127.0.0.1/8"):
            with self.assertRaises(ValueError, msg=bad) as cm:
                webui.validate_bind_hosts(["127.0.0.1", bad])
            self.assertIn(repr(bad.strip()), str(cm.exception))

    def test_empty(self):
        with self.assertRaises(ValueError):
            webui.validate_bind_hosts([])


class TailscaleParseTests(unittest.TestCase):
    def setUp(self):
        webui._detect_found = None
        self.addCleanup(setattr, webui, "_detect_found", None)

    def test_tailscale_ip(self):
        p = webui.parse_tailscale_ip
        self.assertEqual(p("100.101.102.103\n"), ["100.101.102.103"])
        self.assertEqual(p("100.101.102.103\nfd7a:115c:a1e0::1234\n"),
                         ["100.101.102.103", "fd7a:115c:a1e0::1234"])
        self.assertEqual(p("garbage\n192.168.1.5\n0.0.0.0\n100.64.0.9\n"), ["100.64.0.9"])
        self.assertEqual(p(""), [])
        self.assertEqual(p("no ts here\n\x00\xff"), [])

    def test_ip_addr(self):
        p = webui.parse_ip_addr
        v4 = "5: tailscale0    inet 100.101.102.103/32 scope global tailscale0\\       valid_lft forever\n"
        self.assertEqual(p(v4), ["100.101.102.103"])
        both = v4 + ("5: tailscale0    inet6 fd7a:115c:a1e0::1234/128 scope global \\       valid_lft forever\n"
                     "5: tailscale0    inet6 fe80::1/64 scope link \\       valid_lft forever\n")
        self.assertEqual(p(both), ["100.101.102.103", "fd7a:115c:a1e0::1234"])
        self.assertEqual(p("5: tailscale0    inet 192.168.1.2/24 scope global\n"), [])
        self.assertEqual(p("junk inet"), [])
        self.assertEqual(p(""), [])

    def test_detect_prefers_tailscale_cli_then_ip(self):
        outs = {"tailscale": "100.64.0.5\n", "ip": "5: tailscale0 inet 100.64.0.6/32 scope global\n"}
        with mock.patch.object(webui, "_run", side_effect=lambda cmd: outs[cmd[0]]), \
                self.assertLogs("webui", "INFO"):
            self.assertEqual(webui.detect_tailscale_addrs(), ["100.64.0.5"])
            outs["tailscale"] = None
            self.assertEqual(webui.detect_tailscale_addrs(), ["100.64.0.6"])

    def test_detect_failure_is_empty(self):
        with mock.patch.object(webui, "_run", return_value=None):
            with self.assertLogs("webui", "INFO"):
                self.assertEqual(webui.detect_tailscale_addrs(), [])
        webui._detect_found = None
        with mock.patch.object(webui, "_run", side_effect=RuntimeError("boom")):
            with self.assertLogs("webui", "INFO") as cm:
                self.assertEqual(webui.detect_tailscale_addrs(), [])
        self.assertIn("RuntimeError", "".join(cm.output))

    def test_detect_logs_only_on_state_change(self):
        with mock.patch.object(webui, "_run", return_value=None):
            with self.assertLogs("webui", "INFO") as cm:
                webui.detect_tailscale_addrs()
                webui.detect_tailscale_addrs()
                webui.detect_tailscale_addrs()
            self.assertEqual(len(cm.output), 1)
        with mock.patch.object(webui, "_run", return_value="100.64.0.5\n"):
            with self.assertLogs("webui", "INFO") as cm:
                webui.detect_tailscale_addrs()
                webui.detect_tailscale_addrs()
            self.assertEqual(len(cm.output), 1)

    def test_run_handles_missing_binary_and_timeout(self):
        import subprocess
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            self.assertIsNone(webui._run(["tailscale", "ip"]))
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 3)):
            self.assertIsNone(webui._run(["tailscale", "ip"]))


class HostParseTests(unittest.TestCase):
    def test_host_of(self):
        h = webui.host_of
        self.assertEqual(h("LocalHost:8765"), "localhost")
        self.assertEqual(h("127.0.0.1"), "127.0.0.1")
        self.assertEqual(h("[::1]:8765"), "::1")
        self.assertEqual(h("[FD7A:115C:A1E0:0:0:0:0:1]"), "fd7a:115c:a1e0::1")
        self.assertEqual(h("Box.Tail.ts.net.:80"), "box.tail.ts.net")
        self.assertIsNone(h(""))
        self.assertIsNone(h("[::1"))

    def test_parse_allowed_hosts(self):
        with self.assertLogs("webui", "WARNING") as cm:
            out = webui.parse_allowed_hosts(" Box.ts.net , ,*.ts.net,a b,other.example:8080")
        self.assertEqual(out, ["box.ts.net", "other.example"])
        self.assertEqual(len(cm.output), 2)

    def test_parse_allowed_hosts_rejects_urls_and_malformed(self):
        bad = ["https://gui.example.com", "gui.example.com/path", "http://[::1]:8765", "a b", "  x y",
               "[::1", "::1]", "[::1]x", "[zzz]", "1:2:3", "fe80::1", "host:port", "host:123456",
               "user@host", "host?x", "host#x", "-bad", "a..b!", "[1.2.3.4]"]
        with self.assertLogs("webui", "WARNING") as cm:
            out = webui.parse_allowed_hosts(",".join(bad))
        self.assertEqual(out, [])
        self.assertEqual(len(cm.output), len(bad))
        self.assertNotIn("https", out)
        self.assertEqual(webui.parse_allowed_hosts("[FD7A::1]:8765,100.64.0.9:80, Gui.Example.com."),
                         ["fd7a::1", "100.64.0.9", "gui.example.com"])
        self.assertEqual(webui.parse_allowed_hosts(None), [])


class PeerPolicyTests(unittest.TestCase):
    def test_peer_allowed(self):
        ok = [("127.0.0.1", 5), ("127.9.9.9", 5), ("::1", 5, 0, 0), ("100.64.0.1", 5), ("100.127.255.255", 5),
              ("fd7a:115c:a1e0::1", 5, 0, 0), ("::ffff:127.0.0.1", 5, 0, 0), ("::ffff:100.100.1.1", 5, 0, 0),
              ("::1%lo", 5, 0, 1)]
        for p in ok:
            self.assertTrue(webui.peer_allowed(p), p)
        bad = [("192.168.1.5", 5), ("10.0.0.2", 5), ("100.128.0.1", 5), ("100.63.255.255", 5), ("8.8.8.8", 5),
               ("fe80::1", 5, 0, 0), ("fd7b::1", 5, 0, 0), ("::ffff:192.168.1.5", 5, 0, 0), ("::", 5, 0, 0),
               ("0.0.0.0", 5), ("example.com", 5), None, (), "", ("", 5)]
        for p in bad:
            self.assertFalse(webui.peer_allowed(p), p)


class HostOriginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        quiet_loggers(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for n, c in (("index.html", "<h1>hi</h1>"), ("app.js", "//js"), ("app.css", "/*css*/")):
            with open(os.path.join(self.tmp.name, n), "w") as f:
                f.write(c)
        self.config = ConfigStore(os.path.join(self.tmp.name, "c.json"))
        self.app = webui.make_app(self.config, FakeController(),
                                  allowed_hosts=["100.64.0.5", "Box.tail.ts.net"],
                                  status_interval=0.05, static_dir=self.tmp.name)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def get(self, path, host):
        return self.client.get(path, headers={"Host": host})

    async def test_allowed_hosts(self):
        for host in ("localhost", "localhost:8765", "LOCALHOST:1", "127.0.0.1:8765", "[::1]:8765", "[::1]",
                     "100.64.0.5", "100.64.0.5:8765", "box.tail.ts.net:8765", "BOX.tail.ts.net"):
            r = await self.get("/api/state", host)
            self.assertEqual(r.status, 200, host)

    async def test_denied_hosts(self):
        for host in ("evil.example", "evil.example:8765", "192.168.1.5:8765", "127.0.0.1.evil.example",
                     "[::2]:8765", "tail.ts.net", ""):
            r = await self.get("/api/state", host)
            self.assertEqual(r.status, 403, host)
            self.assertEqual(await r.json(), {"error": "bad_host"})
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])

    async def test_missing_host_header(self):
        reader, writer = await asyncio.open_connection(self.client.server.host, self.client.server.port)
        writer.write(b"GET /api/state HTTP/1.0\r\n\r\n")
        data = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        self.assertIn(b" 403 ", data.split(b"\r\n")[0])
        self.assertIn(b'"bad_host"', data)

    async def test_applies_to_static_and_events(self):
        for path in ("/", "/app.js", "/app.css", "/api/events", "/nope"):
            r = await self.get(path, "evil.example")
            self.assertEqual(r.status, 403, path)
        self.assertEqual(self.config.hub.subscriber_count, 0)
        r = await self.get("/", "box.tail.ts.net")
        self.assertEqual(r.status, 200)
        r = await self.get("/api/events", "100.64.0.5:8765")
        self.assertEqual(r.status, 200)
        ev, _ = await read_event(r)
        self.assertEqual(ev, "hello")
        r.close()

    async def test_writes_denied_on_bad_host(self):
        r = await self.client.put("/api/guilds/1/enabled", json={"enabled": False, "rev": None},
                                  headers={"Host": "evil.example", "X-Editor": "a"})
        self.assertEqual(r.status, 403)
        self.assertTrue(self.config.is_enabled(1))

    async def test_origin_rules(self):
        base = {"X-Editor": "a"}
        put = lambda h: self.client.put("/api/guilds/1/enabled", json={"enabled": False, "rev": None},
                                        headers={**base, **h})
        r = await put({"Origin": "http://box.tail.ts.net:8765", "Host": "box.tail.ts.net:8765"})
        self.assertEqual(r.status, 200)
        r = await put({"Origin": "http://[::1]:8765", "Host": "[::1]:8765"})
        self.assertEqual(r.status, 200)
        r = await put({})  # curl: no Origin
        self.assertEqual(r.status, 200)
        for origin in ("http://evil.example", "http://evil.example:8765", "null", "garbage", "http://localhost.evil.example"):
            r = await put({"Origin": origin})
            self.assertEqual(r.status, 403, origin)
            self.assertEqual(await r.json(), {"error": "bad_origin"})
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        r = await put({"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 403)
        self.assertEqual((await r.json())["error"], "bad_origin")
        for site in ("same-origin", "same-site", "none"):
            r = await put({"Sec-Fetch-Site": site})
            self.assertEqual(r.status, 200, site)

    async def test_origin_header_not_checked_on_get(self):
        r = await self.client.get("/api/state", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 200)

    async def test_cross_site_fetch_metadata_blocked_on_api_get(self):
        for path in ("/api/state", "/api/events"):
            r = await self.client.get(path, headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status, 403, path)
            self.assertEqual(await r.json(), {"error": "bad_origin"})
        self.assertEqual(self.config.hub.subscriber_count, 0)
        for site in ("same-origin", "none", "same-site"):
            r = await self.client.get("/api/state", headers={"Sec-Fetch-Site": site})
            self.assertEqual(r.status, 200, site)
        r = await self.client.get("/")  # static pages are navigations, not guarded by fetch metadata
        self.assertEqual(r.status, 200)

    async def test_peer_guard(self):
        for peer, ok in ((("192.168.1.5", 4000), False), (("::ffff:192.168.1.5", 4000, 0, 0), False),
                         (None, False), (("100.64.0.9", 4000), True), (("::ffff:100.64.0.9", 4000, 0, 0), True),
                         (("fd7a:115c:a1e0::5", 4000, 0, 0), True), (("127.0.0.1", 4000), True)):
            with mock.patch.object(webui, "_peer_of", return_value=peer):
                if not ok:
                    ctx = self.assertLogs("webui", "WARNING")
                    ctx.__enter__()
                for path in ("/", "/app.js", "/api/state", "/api/events", "/nope"):
                    if not ok:
                        r = await self.client.get(path)
                        self.assertEqual(r.status, 403, (peer, path))
                        self.assertEqual(await r.json(), {"error": "bad_peer"})
                        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
                        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
                    else:
                        r = await self.client.get(path, headers={"Host": "localhost"})
                        self.assertNotEqual(r.status, 403, (peer, path))
                        r.close()
                if not ok:
                    ctx.__exit__(None, None, None)
        self.assertEqual(self.config.hub.subscriber_count, 0)

    async def test_peer_checked_before_host_and_write_checks(self):
        with mock.patch.object(webui, "_peer_of", return_value=("192.168.1.5", 1)):
            with self.assertLogs("webui", "WARNING"):
                r = await self.client.put("/api/guilds/1/enabled", json={"enabled": False, "rev": None},
                                          headers={"Host": "evil.example", "Origin": "http://evil.example"})
            self.assertEqual(await r.json(), {"error": "bad_peer"})
        self.assertTrue(self.config.is_enabled(1))

    async def test_peer_refusals_logged_once_per_peer(self):
        with mock.patch.object(webui, "_peer_of", return_value=("192.168.1.5", 1)):
            with self.assertLogs("webui", "WARNING") as cm:
                for _ in range(5):
                    await self.client.get("/api/state")
        self.assertEqual(len(cm.output), 1)
        self.assertIn("192.168.1.5", cm.output[0])
        self.assertNotIn("/api/state", cm.output[0])

    async def test_bad_origin_does_not_change_state(self):
        r = await self.client.put("/api/guilds/1/enabled", json={"enabled": False, "rev": None},
                                  headers={"X-Editor": "a", "Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertTrue(self.config.is_enabled(1))
        self.assertEqual(self.config.rev, 0)


class StartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        quiet_loggers(self)

    def app(self, d):
        return webui.make_app(ConfigStore(os.path.join(d, "c.json")), FakeController(),
                              allowed_hosts=[], static_dir=d)

    async def test_start_and_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertLogs("webui", "INFO"):
                runner = await webui.start_webui(self.app(d), ["127.0.0.1"], 0)
            self.assertIsNone(runner.retry_task)
            await runner.cleanup()

    async def test_policy_enforced_in_start(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                await webui.start_webui(self.app(d), ["0.0.0.0"], 0)

    async def test_partial_bind_serves_and_retries(self):
        port = free_port()
        real = webui._bind_site
        blocked = {"on": True}

        async def fake(runner, host, p):
            if host == "100.64.0.5":
                if blocked["on"]:
                    raise OSError(99, "Cannot assign requested address")
                host = "127.0.0.1"
                p = port + 1
            await real(runner, host, p)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(webui, "_bind_site", fake):
            with self.assertLogs("webui", "WARNING") as cm:
                runner = await webui.start_webui(self.app(d), ["127.0.0.1", "100.64.0.5"], port,
                                                 retry_interval=0.02)
            self.assertTrue(any("100.64.0.5" in m for m in cm.output))
            try:
                async with aiohttp.ClientSession() as s:
                    r = await s.get(f"http://127.0.0.1:{port}/api/state")
                    self.assertEqual(r.status, 200)
                self.assertFalse(runner.retry_task.done())
                blocked["on"] = False
                with self.assertLogs("webui", "INFO"):
                    for _ in range(100):
                        if runner.retry_task.done():
                            break
                        await asyncio.sleep(0.02)
                self.assertTrue(runner.retry_task.done())
                async with aiohttp.ClientSession() as s:
                    r = await s.get(f"http://127.0.0.1:{port + 1}/api/state")
                    self.assertEqual(r.status, 200)
            finally:
                await runner.cleanup()

    async def test_real_unbindable_address_does_not_stop_loopback(self):
        # 100.64.0.1 is policy-valid but (almost certainly) not assigned to this host: EADDRNOTAVAIL.
        with tempfile.TemporaryDirectory() as d:
            with self.assertLogs("webui", "WARNING"):
                runner = await webui.start_webui(self.app(d), ["127.0.0.1", "100.64.0.1"], 0,
                                                 retry_interval=60)
            try:
                if runner.retry_task is None:
                    self.skipTest("100.64.0.1 is assigned on this host")
                port = runner.addresses[0][1]
                async with aiohttp.ClientSession() as s:
                    r = await s.get(f"http://127.0.0.1:{port}/api/state")
                    self.assertEqual(r.status, 200)
            finally:
                await runner.cleanup()

    async def test_auto_mode_redetects_tailnet_and_allows_host(self):
        port = free_port()
        found = []
        calls = []

        def detect():
            calls.append(1)
            return list(found)

        real = webui._bind_site
        bound = []

        async def fake(runner, host, p):
            if host == "100.64.0.5":
                bound.append(host)
                host, p = "127.0.0.1", port + 1
            await real(runner, host, p)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(webui, "_bind_site", fake):
            app = self.app(d)
            with self.assertLogs("webui", "INFO"):
                runner = await webui.start_webui(app, ["127.0.0.1"], port, retry_interval=0.02, detect=detect)
            try:
                self.assertIsNotNone(runner.retry_task)
                for _ in range(20):
                    await asyncio.sleep(0.02)
                self.assertGreaterEqual(len(calls), 2)
                self.assertFalse(runner.retry_task.done())
                found.append("100.64.0.5")
                with self.assertLogs("webui", "INFO"):
                    for _ in range(100):
                        if runner.retry_task.done():
                            break
                        await asyncio.sleep(0.02)
                self.assertTrue(runner.retry_task.done())
                self.assertEqual(bound, ["100.64.0.5"])   # bound exactly once, no duplicates
                self.assertIn("100.64.0.5", app[webui.ALLOWED_HOSTS_KEY])
                n = len(calls)
                await asyncio.sleep(0.1)
                self.assertEqual(len(calls), n)           # detection stops once found
            finally:
                await runner.cleanup()

    async def test_explicit_mode_has_no_redetection(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertLogs("webui", "INFO"):
                runner = await webui.start_webui(self.app(d), ["127.0.0.1"], 0, retry_interval=0.02)
            self.assertIsNone(runner.retry_task)
            await runner.cleanup()

    async def test_retry_loop_survives_errors(self):
        n = []

        def detect():
            n.append(1)
            if len(n) < 3:
                raise RuntimeError("flaky")
            return []

        with tempfile.TemporaryDirectory() as d:
            with self.assertLogs("webui", "INFO"):
                runner = await webui.start_webui(self.app(d), ["127.0.0.1"], 0, retry_interval=0.02, detect=detect)
            try:
                with self.assertLogs("webui", "ERROR") as cm:
                    for _ in range(100):
                        if len(n) >= 4:
                            break
                        await asyncio.sleep(0.02)
                self.assertFalse(runner.retry_task.done())
                self.assertTrue(any("retry failed" in m for m in cm.output))
            finally:
                await runner.cleanup()

    async def test_cleanup_closes_sites_even_if_retry_task_died(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertLogs("webui", "INFO"):
                runner = await webui.start_webui(self.app(d), ["127.0.0.1"], 0)
            port = runner.addresses[0][1]

            async def boom():
                raise RuntimeError("dead")

            runner.retry_task = asyncio.create_task(boom())
            await asyncio.sleep(0.01)
            await runner.cleanup()
            self.assertEqual(runner.addresses, [])
            with self.assertRaises(aiohttp.ClientConnectorError):
                async with aiohttp.ClientSession() as sess:
                    await sess.get(f"http://127.0.0.1:{port}/api/state")

    async def test_all_fail_raises_and_leaves_nothing(self):
        async def fail(runner, host, p):
            raise OSError(98, "in use")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(webui, "_bind_site", fail):
            before = {t for t in asyncio.all_tasks()}
            with self.assertLogs("webui", "WARNING"):
                with self.assertRaises(OSError):
                    await webui.start_webui(self.app(d), ["127.0.0.1", "::1"], 0)
            leftover = [t for t in asyncio.all_tasks() - before if "retry" in (t.get_name() or "")]
            self.assertEqual(leftover, [])

    async def test_cleanup_cancels_retry_and_is_quick_with_sse(self):
        import time
        real = webui._bind_site

        async def fake(runner, host, p):
            if host == "100.64.0.5":
                raise OSError(99, "nope")
            await real(runner, host, p)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(webui, "_bind_site", fake):
            with self.assertLogs("webui", "WARNING"):
                runner = await webui.start_webui(self.app(d), ["127.0.0.1", "100.64.0.5"], 0,
                                                 retry_interval=0.02)
            task = runner.retry_task
            port = runner.addresses[0][1]
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"http://127.0.0.1:{port}/api/events")
                await r.content.readline()
                t0 = time.monotonic()
                await runner.cleanup()
                self.assertLess(time.monotonic() - t0, 3)
                r.close()
            self.assertTrue(task.cancelled() or task.done())
            self.assertIsNone(runner.retry_task)


if __name__ == "__main__":
    unittest.main()
