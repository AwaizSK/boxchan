import json
import os
import sys
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_store import ConfigStore
from test_webui import FakeController
import webui

ED = {"X-Editor": "t"}


class GuildScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.users_file = os.path.join(self.tmp.name, "users.json")
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump([
                {"username": "root", "password": webui.hash_password("rootpassword1"), "groups": ["admin"]},
                {"username": "bob", "password": webui.hash_password("bobpassword1"), "groups": ["user"], "guilds": ["1"]},
                {"username": "nobody", "password": webui.hash_password("nobodypass11"), "groups": ["user"]},
            ], f)
        self.cfg = ConfigStore(os.path.join(self.tmp.name, "c.json"))
        await self.cfg.seed_channel(1, 10)
        await self.cfg.seed_channel(2, 20)
        self.ctl = FakeController()
        self.ctl.guilds_view = lambda: [{"id": "1", "name": "One", "channels": []},
                                        {"id": "2", "name": "Two", "channels": []}]
        self.ctl.status_view = lambda: {"1": {}, "2": {}}
        app = webui.make_app(self.cfg, self.ctl, allowed_hosts=[], users_file=self.users_file,
                             static_dir=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static"))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def login(self, user, pw):
        r = await self.client.post("/api/login", json={"username": user, "password": pw})
        self.assertEqual(r.status, 200)

    async def test_admin_sees_everything(self):
        await self.login("root", "rootpassword1")
        d = await (await self.client.get("/api/state")).json()
        self.assertEqual([g["id"] for g in d["guilds"]], ["1", "2"])
        self.assertEqual(set(d["config"]["guilds"]), {"1", "2"})

    async def test_scoped_user_sees_only_own_server(self):
        await self.login("bob", "bobpassword1")
        d = await (await self.client.get("/api/state")).json()
        self.assertEqual([g["id"] for g in d["guilds"]], ["1"])
        self.assertEqual(set(d["config"]["guilds"]), {"1"})

    async def test_other_server_routes_404_and_own_work(self):
        await self.login("bob", "bobpassword1")
        r = await self.client.post("/api/guilds/2/skip", headers=ED)
        self.assertEqual(r.status, 404)
        r = await self.client.put("/api/guilds/2/enabled", json={"enabled": False}, headers=ED)
        self.assertEqual(r.status, 404)
        r = await self.client.post("/api/guilds/1/skip", headers=ED)
        self.assertEqual(r.status, 200)

    async def test_user_without_servers_sees_none(self):
        await self.login("nobody", "nobodypass11")
        d = await (await self.client.get("/api/state")).json()
        self.assertEqual(d["guilds"], [])
        r = await self.client.post("/api/guilds/1/skip", headers=ED)
        self.assertEqual(r.status, 404)

    async def test_voice_apply_is_platform_admin_only(self):
        await self.login("bob", "bobpassword1")
        r = await self.client.put("/api/voice", json={"name": "amy", "rev": None}, headers=ED)
        self.assertEqual(r.status, 403)
        await self.client.post("/api/logout")
        await self.login("root", "rootpassword1")
        r = await self.client.put("/api/voice", json={"name": "amy", "rev": None}, headers=ED)
        self.assertEqual(r.status, 200)

    async def test_admin_api_creates_scoped_user(self):
        await self.login("root", "rootpassword1")
        r = await self.client.post("/api/admin/users", headers=ED, json={
            "username": "carol", "password": "carolpassword1", "groups": ["user"], "guilds": ["2"]})
        self.assertEqual(r.status, 200)
        mgr = webui.AuthManager(self.users_file)
        self.assertEqual(mgr.allowed_guilds("carol"), {"2"})
        self.assertIsNone(mgr.allowed_guilds("root"))
        self.assertEqual(mgr.allowed_guilds("nobody"), set())
        bad = await self.client.post("/api/admin/users", headers=ED, json={
            "username": "dave", "password": "davepassword1", "guilds": ["x"]})
        self.assertEqual(bad.status, 400)


if __name__ == "__main__":
    unittest.main()
