import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_webui as tw  # noqa: E402  (module import: does not re-collect its test classes)

import webui  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from config_store import ConfigStore, ConflictError  # noqa: E402
from voices import BadVoiceError, SayError, VoiceLoadError  # noqa: E402

ED = {"X-Editor": "alice"}
SECRETS = ("/", "Traceback", "Error:", "onnx")  # bodies of error responses must not leak these


class Routes(tw.Base):
    def put_voice(self, body, headers=ED):
        return self.client.put("/api/voice", json=body, headers=headers)

    def say(self, body, headers=ED, gid="1"):
        return self.client.post(f"/api/guilds/{gid}/say", json=body, headers=headers)

    async def assert_error(self, r, status, code):
        self.assertEqual(r.status, status)
        raw = await r.text()
        self.assertEqual(json.loads(raw), {"error": code})
        for bad in SECRETS:
            self.assertNotIn(bad, raw)


class ListVoicesTests(Routes):
    async def test_get_voices(self):
        r = await self.client.get("/api/voices")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        body = await r.json()
        self.assertEqual(body["voices"], self.ctl.voice_list)
        self.assertEqual((body["current"], body["configured"], body["loading"]), ("amy", None, False))

    async def test_rescans_every_call(self):
        await self.client.get("/api/voices")
        self.ctl.voice_list = []
        self.assertEqual((await (await self.client.get("/api/voices")).json())["voices"], [])


class PutVoiceTests(Routes):
    async def test_success_returns_wire_snapshot(self):
        self.ctl.apply_result = {"rev": 3, "updated_by": "alice", "updated_at": 1.0, "voice": "amy",
                                 "guilds": {"1": {"channel_id": 10, "enabled": True}}}
        r = await self.put_voice({"name": "amy", "rev": 2})
        self.assertEqual(r.status, 200)
        snap = (await r.json())["snapshot"]
        self.assertEqual((snap["voice"], snap["rev"], snap["guilds"]["1"]["channel_id"]), ("amy", 3, "10"))
        self.assertEqual(self.ctl.apply_calls, [("amy", 2, "alice")])

    async def test_null_name_means_default(self):
        r = await self.put_voice({"name": None, "rev": None})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.ctl.apply_calls, [(None, None, "alice")])

    async def test_editor_required(self):
        r = await self.put_voice({"name": "amy"}, headers={})
        await self.assert_error(r, 400, "editor_required")
        self.assertEqual(self.ctl.apply_calls, [])

    async def test_bad_bodies(self):
        for body in ({}, {"name": 5}, {"name": ["a"]}, {"name": "amy", "rev": "1"}, {"name": "amy", "rev": True}):
            r = await self.put_voice(body)
            await self.assert_error(r, 400, "bad_request")
        r = await self.client.put("/api/voice", data=b"nope", headers=ED)
        await self.assert_error(r, 400, "bad_request")
        self.assertEqual(self.ctl.apply_calls, [])

    async def test_malformed_names_are_bad_voice_before_controller(self):
        for name in ("../x", "a/b", "/etc/passwd", "", "x" * 101, "sp ace", "amy\n"):
            r = await self.put_voice({"name": name})
            await self.assert_error(r, 400, "bad_voice")
        self.assertEqual(self.ctl.apply_calls, [])

    async def test_controller_bad_voice(self):
        self.ctl.apply_exc = BadVoiceError("unknown voice")
        await self.assert_error(await self.put_voice({"name": "ghost"}), 400, "bad_voice")

    async def test_conflict_carries_snapshot(self):
        self.ctl.apply_exc = ConflictError({"rev": 9, "updated_by": "bob", "updated_at": 1.0, "voice": None,
                                            "guilds": {"1": {"channel_id": 10, "enabled": True}}})
        r = await self.put_voice({"name": "amy", "rev": 1})
        self.assertEqual(r.status, 409)
        body = await r.json()
        self.assertEqual(body["error"], "conflict")
        self.assertEqual(body["snapshot"]["rev"], 9)
        self.assertEqual(body["snapshot"]["guilds"]["1"]["channel_id"], "10")

    async def test_load_failure_422_without_details(self):
        self.ctl.apply_exc = VoiceLoadError("cuda exploded at /opt/tts-bot/models/x.onnx")
        r = await self.put_voice({"name": "amy"})
        self.assertEqual(r.status, 422)
        self.assertEqual(await r.json(), {"error": "voice_load_failed"})

    async def test_persist_failure_500_without_details(self):
        self.ctl.apply_exc = OSError(28, "No space left", "/var/lib/tts-bot/config.json")
        with self.assertLogs("webui", "ERROR"):
            r = await self.put_voice({"name": "amy"})
        await self.assert_error(r, 500, "persist_failed")

    async def test_unexpected_error_is_generic_json(self):
        self.ctl.apply_exc = RuntimeError("boom at /home/x")
        with self.assertLogs("webui", "ERROR"):
            r = await self.put_voice({"name": "amy"})
        await self.assert_error(r, 500, "internal")

    async def test_client_disconnect_does_not_cancel_the_swap(self):
        self.ctl.apply_gate = asyncio.Event()
        finished = []
        orig = self.ctl.apply_voice

        async def tracking(*a):
            res = await orig(*a)
            finished.append(res)
            return res

        self.ctl.apply_voice = tracking
        route = next(r for r in self.app.router.routes() if r.method == "PUT" and r.resource.canonical == "/api/voice")
        from aiohttp import streams
        from aiohttp.test_utils import make_mocked_request
        payload = streams.StreamReader(mock.Mock(_reading_paused=False), 2 ** 16, loop=asyncio.get_running_loop())
        payload.feed_data(json.dumps({"name": "amy", "rev": None}).encode())
        payload.feed_eof()
        req = make_mocked_request("PUT", "/api/voice", headers={"X-Editor": "bob"}, app=self.app, payload=payload)
        task = asyncio.ensure_future(route.handler(req))
        for _ in range(100):
            if self.ctl.apply_calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(finished, [])
        self.ctl.apply_gate.set()  # the shielded swap continues and completes
        for _ in range(100):
            if finished:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(finished), 1)


class SayRouteTests(Routes):
    async def test_success(self):
        r = await self.say({"text": "Hello there"})
        self.assertEqual((r.status, await r.json()), (200, {"queued": True}))
        self.assertEqual(self.ctl.say_calls, [(1, "Hello there")])

    async def test_validation(self):
        for body in ({}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": None}):
            await self.assert_error(await self.say(body), 400, "bad_request")
        await self.assert_error(await self.say({"text": "x" * 201}), 400, "too_long")
        self.assertEqual((await self.say({"text": "x" * 200})).status, 200)

    async def test_editor_and_guild(self):
        await self.assert_error(await self.say({"text": "hi"}, headers={}), 400, "editor_required")
        r = await self.say({"text": "hi"}, gid="2")
        self.assertEqual(r.status, 404)
        r = await self.say({"text": "hi"}, gid="abc")
        self.assertEqual(r.status, 404)
        self.assertEqual(self.ctl.say_calls, [])

    async def test_controller_refusals(self):
        for code, status, wire in (("not_connected", 409, "not_connected"), ("queue_full", 409, "queue_full"),
                                   ("tts_unavailable", 409, "tts_unavailable"), ("bad_text", 400, "bad_request")):
            self.ctl.say_exc = SayError(code)
            await self.assert_error(await self.say({"text": "hi"}), status, wire)


class SSEVoiceTests(Routes):
    async def test_hello_includes_voice_summary(self):
        self.ctl.voice = {"current": "amy", "configured": "kristin", "loading": True}
        r = await self.client.get("/api/events")
        ev, data = await tw.read_event(r)
        self.assertEqual(ev, "hello")
        self.assertEqual(data["voices"], {"current": "amy", "configured": "kristin", "loading": True})
        self.assertIsNone(data["snapshot"]["voice"])
        r.close()

    async def test_hello_tolerates_controller_without_voice_support(self):
        class Old:
            def guilds_view(self): return []
            def status_view(self): return {}

        app = webui.make_app(self.config, Old(), allowed_hosts=[], static_dir=self.static)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/events")
            ev, data = await tw.read_event(r)
            self.assertEqual((ev, data["voices"]), ("hello", None))
            r.close()

    async def test_voice_events_forwarded_unchanged(self):
        r = await self.client.get("/api/events")
        await tw.read_event(r)
        self.config.hub.publish({"type": "voice", "loading": True})
        self.config.hub.publish({"type": "voice", "loading": False, "current": "kristin", "error": None})
        seen = []
        while len([e for e in seen if e[0] == "voice"]) < 2:
            seen.append(await tw.read_event(r))
        voice = [d for e, d in seen if e == "voice"]
        self.assertEqual(voice[0], {"type": "voice", "loading": True})
        self.assertEqual(voice[1], {"type": "voice", "loading": False, "current": "kristin", "error": None})
        r.close()

    async def test_voice_config_event_has_null_guild_and_voice_field(self):
        r = await self.client.get("/api/events")
        await tw.read_event(r)
        await self.config.set_voice("amy", expected_rev=None, editor="alice")
        seen = []
        while "config" not in [e for e, _ in seen]:
            seen.append(await tw.read_event(r))
        cfg = dict(seen)["config"]
        self.assertEqual(cfg["change"], {"guild_id": None, "field": "voice", "old": None, "new": "amy"})
        self.assertEqual(cfg["snapshot"]["voice"], "amy")
        r.close()


class WireTests(unittest.TestCase):
    def test_wire_event_keeps_null_guild_and_voice(self):
        ev = {"type": "config", "snapshot": {"rev": 1, "voice": "v", "guilds": {}},
              "change": {"guild_id": None, "field": "voice", "old": None, "new": "v"}}
        out = webui.wire_event(ev)
        self.assertIsNone(out["change"]["guild_id"])
        self.assertEqual(out["snapshot"]["voice"], "v")
        self.assertEqual(webui.wire_snapshot({"rev": 1, "voice": "v", "guilds": {}})["voice"], "v")

    def test_wire_event_still_stringifies_guild_ids(self):
        ev = {"type": "config", "snapshot": {"guilds": {}},
              "change": {"guild_id": 5, "field": "channel_id", "old": 1, "new": None}}
        self.assertEqual(webui.wire_event(ev)["change"], {"guild_id": "5", "field": "channel_id", "old": "1", "new": None})


class GuardTests(unittest.IsolatedAsyncioTestCase):
    """The existing guards must apply to the new routes unchanged."""

    ROUTES = (("GET", "/api/voices", None), ("PUT", "/api/voice", {"name": "amy", "rev": None}),
              ("POST", "/api/guilds/1/say", {"text": "hi"}))

    def call(self, method, path, body, headers):
        kw = {"headers": headers}
        if body is not None:
            kw["json"] = body
        return self.client.request(method, path, **kw)

    async def asyncSetUp(self):
        tw.quiet_loggers(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for n in ("index.html", "app.js", "app.css"):
            with open(os.path.join(self.tmp.name, n), "w") as f:
                f.write("x")
        self.config = ConfigStore(os.path.join(self.tmp.name, "c.json"))
        self.app = webui.make_app(self.config, tw.FakeController(), allowed_hosts=[], static_dir=self.tmp.name)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_bad_host_everywhere(self):
        for m, p, b in self.ROUTES:
            r = await self.call(m, p, b, {"Host": "evil.example", "X-Editor": "a"})
            self.assertEqual((r.status, (await r.json())["error"]), (403, "bad_host"), p)

    async def test_bad_origin_and_cross_site_on_writes_and_gets(self):
        for m, p, b in self.ROUTES:
            r = await self.call(m, p, b, {"Origin": "http://evil.example", "X-Editor": "a"}) \
                if m != "GET" else await self.call(m, p, b, {"Sec-Fetch-Site": "cross-site"})
            self.assertEqual((r.status, (await r.json())["error"]), (403, "bad_origin"), p)
        for m, p, b in self.ROUTES[1:]:
            r = await self.call(m, p, b, {"Sec-Fetch-Site": "cross-site", "X-Editor": "a"})
            self.assertEqual((r.status, (await r.json())["error"]), (403, "bad_origin"), p)

    async def test_bad_peer_everywhere(self):
        with mock.patch.object(webui, "_peer_of", return_value=("192.168.1.5", 1)):
            with self.assertLogs("webui", "WARNING"):
                for m, p, b in self.ROUTES:
                    r = await self.call(m, p, b, {"X-Editor": "a"})
                    self.assertEqual((r.status, (await r.json())["error"]), (403, "bad_peer"), p)

    async def test_missing_editor_on_writes(self):
        for m, p, b in self.ROUTES[1:]:
            r = await self.call(m, p, b, {})
            self.assertEqual((r.status, (await r.json())["error"]), (400, "editor_required"), p)

    async def test_security_headers_on_new_routes(self):
        r = await self.client.get("/api/voices")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
