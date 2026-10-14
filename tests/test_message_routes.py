import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_webui as tw  # noqa: E402
import botimport  # noqa: E402  (hermetic import of bot.py)
import discord  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from test_controller import make_channel, make_guild  # noqa: E402
from test_messages import Clock  # noqa: E402

import webui  # noqa: E402
from messages import MessageError, SendLimiter  # noqa: E402

bot = botimport.bot

ED = {"X-Editor": "alice"}
SECRETS = ("/", "Traceback", "Error:")


class Routes(tw.Base):
    async def asyncSetUp(self):
        tw.quiet_loggers(self)
        await super().asyncSetUp()

    def send(self, body, headers=ED, gid="1"):
        return self.client.post(f"/api/guilds/{gid}/send", json=body, headers=headers)

    async def assert_error(self, r, status, code, **extra):
        self.assertEqual(r.status, status)
        raw = await r.text()
        self.assertEqual(json.loads(raw), {"error": code, **extra})
        for bad in SECRETS:
            self.assertNotIn(bad, raw)


class MembersRouteTests(Routes):
    async def test_members_passthrough_ids_are_strings(self):
        self.ctl.members_result = {"members": [{"id": "1234567890123456789", "name": "Bob",
                                                "username": "bob", "bot": False}], "truncated": True}
        r = await self.client.get("/api/guilds/1/members?q=bo")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        body = await r.json()
        self.assertEqual(body["members"][0]["id"], "1234567890123456789")
        self.assertTrue(body["truncated"])
        self.assertEqual(self.ctl.members_calls, [(1, "bo")])

    async def test_empty_and_long_query(self):
        await self.client.get("/api/guilds/1/members")
        await self.client.get("/api/guilds/1/members?q=" + "x" * 500)
        self.assertEqual(self.ctl.members_calls[0], (1, ""))
        self.assertEqual(len(self.ctl.members_calls[1][1]), 100)

    async def test_errors(self):
        self.ctl.members_exc = MessageError("no_channel")
        await self.assert_error(await self.client.get("/api/guilds/1/members"), 409, "no_channel")
        self.assertEqual((await self.client.get("/api/guilds/2/members")).status, 404)
        self.assertEqual((await self.client.get("/api/guilds/x/members")).status, 404)
        self.ctl.members_exc = RuntimeError("boom at /home/x")
        with self.assertLogs("webui", "ERROR"):
            r = await self.client.get("/api/guilds/1/members")
        await self.assert_error(r, 500, "internal")

    async def test_no_editor_needed_for_read(self):
        self.assertEqual((await self.client.get("/api/guilds/1/members")).status, 200)


class RolesRouteTests(Routes):
    async def test_roles_passthrough_and_query(self):
        self.ctl.roles_result = {"roles": [{"id": "99", "name": "Admins", "color": "#ff0000", "is_role": True}], "truncated": False}
        r = await self.client.get("/api/guilds/1/roles?q=adm")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        body = await r.json()
        self.assertEqual(body["roles"][0]["name"], "Admins")
        self.assertEqual(self.ctl.roles_calls, [(1, "adm")])

    async def test_empty_and_long_query(self):
        await self.client.get("/api/guilds/1/roles")
        await self.client.get("/api/guilds/1/roles?q=" + "x" * 500)
        self.assertEqual(self.ctl.roles_calls[0], (1, ""))
        self.assertEqual(len(self.ctl.roles_calls[1][1]), 100)

    async def test_errors(self):
        self.ctl.roles_exc = MessageError("no_channel")
        await self.assert_error(await self.client.get("/api/guilds/1/roles"), 409, "no_channel")
        self.assertEqual((await self.client.get("/api/guilds/2/roles")).status, 404)
        self.assertEqual((await self.client.get("/api/guilds/x/roles")).status, 404)
        self.ctl.roles_exc = RuntimeError("boom")
        with self.assertLogs("webui", "ERROR"):
            r = await self.client.get("/api/guilds/1/roles")
        await self.assert_error(r, 500, "internal")


class SendRouteTests(Routes):
    async def test_success(self):
        r = await self.send({"content": "hello <@42>"})
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.json(), {"ok": True, "message_id": "123456789012345678"})
        self.assertEqual(self.ctl.send_calls, [(1, "hello <@42>", "alice")])

    async def test_editor_required(self):
        await self.assert_error(await self.send({"content": "hi"}, headers={}), 400, "editor_required")
        self.assertEqual(self.ctl.send_calls, [])

    async def test_bad_bodies(self):
        for body in ({}, {"content": 5}, {"content": None}, {"content": ["a"]}, {"content": ""},
                     {"content": "  \n "}, {"content": "x" * 2001}, {"content": "hi", "rev": "1"}):
            await self.assert_error(await self.send(body), 400, "bad_request")
        r = await self.client.post("/api/guilds/1/send", data=b"nope", headers=ED)
        await self.assert_error(r, 400, "bad_request")
        self.assertEqual(self.ctl.send_calls, [])

    async def test_unknown_guild(self):
        self.assertEqual((await self.send({"content": "hi"}, gid="2")).status, 404)
        self.assertEqual((await self.send({"content": "hi"}, gid="x")).status, 404)

    async def test_error_mapping(self):
        for code, status in (("bad_request", 400), ("bad_mention", 400), ("missing_permission", 403),
                             ("no_channel", 409), ("discord_error", 502)):
            self.ctl.send_exc = MessageError(code)
            await self.assert_error(await self.send({"content": "hi"}), status, code)

    async def test_route_rejects_11_distinct_mentions_with_bad_mention(self):
        """Route rejects 11 distinct mentions, HTTP 400 bad_mention."""
        content = " ".join(f"<@{i}>" for i in range(11))
        self.ctl.send_exc = MessageError("bad_mention")
        r = await self.send({"content": content})
        await self.assert_error(r, 400, "bad_mention")

    async def test_route_accepts_exactly_10_mentions(self):
        """Route accepts exactly 10 distinct mentions."""
        content = " ".join(f"<@{i}>" for i in range(10))
        r = await self.send({"content": content})
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.json(), {"ok": True, "message_id": "123456789012345678"})

    async def test_rate_limited_has_retry_after(self):
        self.ctl.send_exc = MessageError("rate_limited", retry_after=7)
        await self.assert_error(await self.send({"content": "hi"}), 429, "rate_limited", retry_after=7)

    async def test_unexpected_error_is_generic(self):
        self.ctl.send_exc = RuntimeError("boom at /home/x")
        with self.assertLogs("webui", "ERROR"):
            r = await self.send({"content": "hi"})
        await self.assert_error(r, 500, "internal")

    async def test_body_limit_16k(self):
        content = "田" * 2000  # JSON-escaped by the client library (~12 KB)
        raw = json.dumps({"content": content})
        self.assertGreater(len(raw), 4096)
        self.assertLess(len(raw), webui.MAX_BODY)
        r = await self.client.post("/api/guilds/1/send", data=raw.encode(), headers={**ED, "Content-Type": "application/json"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.ctl.send_calls[0][1], content)
        r = await self.client.post("/api/guilds/1/send", data=b"x" * (webui.MAX_BODY + 1), headers=ED)
        self.assertEqual(r.status, 413)

    async def test_client_disconnect_does_not_cancel_or_double_send(self):
        self.ctl.send_gate = asyncio.Event()
        finished = []
        orig = self.ctl.send_message

        async def tracking(*a):
            res = await orig(*a)
            finished.append(res)
            return res

        self.ctl.send_message = tracking
        route = next(r for r in self.app.router.routes()
                     if r.method == "POST" and r.resource.canonical == "/api/guilds/{gid}/send")
        from aiohttp import streams
        from aiohttp.test_utils import make_mocked_request
        payload = streams.StreamReader(mock.Mock(_reading_paused=False), 2 ** 16, loop=asyncio.get_running_loop())
        payload.feed_data(json.dumps({"content": "hi"}).encode())
        payload.feed_eof()
        req = make_mocked_request("POST", "/api/guilds/1/send", headers={"X-Editor": "bob"}, app=self.app,
                                  payload=payload, match_info={"gid": "1"})
        task = asyncio.ensure_future(route.handler(req))
        for _ in range(100):
            if self.ctl.send_calls:
                break
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(finished, [])
        self.ctl.send_gate.set()
        for _ in range(100):
            if finished:
                break
            await asyncio.sleep(0)
        self.assertEqual(len(finished), 1)
        self.assertEqual(len(self.ctl.send_calls), 1)  # one send, never repeated


class RealControllerMentionCapTests(Routes):
    """The REAL bot.Controller behind webui.make_app: the 10-distinct-user cap end to end."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ch = make_channel(100, "general", 0)
        self.ch.send = mock.AsyncMock(return_value=mock.MagicMock(id=999))
        self.guild = make_guild(1, "G", [self.ch])
        self.guild.me = mock.MagicMock()
        self.guild.get_member.side_effect = lambda uid: mock.MagicMock(id=uid)  # all can view (default perms)
        self.limiter = mock.Mock(wraps=SendLimiter(clock=Clock()))
        self.real = bot.Controller(limiter=self.limiter)
        for p in (mock.patch.object(type(bot.bot), "guilds", new_callable=mock.PropertyMock, return_value=[self.guild]),
                  mock.patch.object(bot.bot, "get_guild", side_effect=lambda g: self.guild if g == 1 else None),
                  mock.patch.object(bot.config, "channel_for", return_value=100)):
            p.start()
            self.addCleanup(p.stop)
        app = webui.make_app(self.config, self.real, allowed_hosts=[], status_interval=0.05, static_dir=self.static)
        self.real_client = TestClient(TestServer(app))
        await self.real_client.start_server()
        self.addAsyncCleanup(self.real_client.close)

    def post(self, content):
        return self.real_client.post("/api/guilds/1/send", json={"content": content}, headers=ED)

    async def test_11_distinct_mentions_rejected_400_without_send_or_slot(self):
        r = await self.post(" ".join(f"<@{i}>" for i in range(1, 12)))
        await self.assert_error(r, 400, "bad_mention")
        self.ch.send.assert_not_awaited()
        self.limiter.reserve.assert_not_called()

    async def test_10_distinct_mentions_with_duplicates_succeeds(self):
        ids = list(range(1, 11))
        r = await self.post(" ".join(f"<@{i}>" for i in ids) + " again <@3> <@!3> <@3>")
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.json(), {"ok": True, "message_id": "999"})
        self.ch.send.assert_awaited_once()
        allowed = self.ch.send.await_args.kwargs["allowed_mentions"]
        self.assertEqual(sorted(u.id for u in allowed.users), ids)
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)
        self.limiter.reserve.assert_called_once_with(1)


class GuardTests(Routes):
    def both(self):
        return (lambda h=None: self.send({"content": "hi"}, headers={**ED, **(h or {})}),
                lambda h=None: self.client.get("/api/guilds/1/members", headers=h or {}))

    async def test_bad_host(self):
        for call in self.both():
            r = await call({"Host": "evil.example"})
            await self.assert_error(r, 403, "bad_host")
        self.assertEqual((self.ctl.send_calls, self.ctl.members_calls), ([], []))

    async def test_bad_origin_and_cross_site(self):
        send, _ = self.both()
        for h in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            await self.assert_error(await send(h), 403, "bad_origin")
        r = await self.client.get("/api/guilds/1/members", headers={"Sec-Fetch-Site": "cross-site"})
        await self.assert_error(r, 403, "bad_origin")
        self.assertEqual((self.ctl.send_calls, self.ctl.members_calls), ([], []))

    async def test_bad_peer(self):
        with mock.patch.object(webui, "_peer_of", return_value=("192.168.1.5", 1)):
            with self.assertLogs("webui", "WARNING"):
                for call in self.both():
                    await self.assert_error(await call(), 403, "bad_peer")
        self.assertEqual((self.ctl.send_calls, self.ctl.members_calls), ([], []))

    async def test_security_headers_on_new_routes(self):
        for r in (await self.send({"content": "hi"}), await self.client.get("/api/guilds/1/members")):
            self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(r.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
