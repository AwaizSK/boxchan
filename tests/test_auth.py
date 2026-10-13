import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config_store import ConfigStore
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_webui import FakeController
import webui
from webui import AuthManager


class TestAuthManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.users_file = os.path.join(self.tmp.name, "users.json")

    def test_disabled_only_without_a_configured_path(self):
        for none in (None, ""):
            mgr = AuthManager(none)
            self.assertFalse(mgr.enabled)
            self.assertEqual(mgr.load_users(), {})

    def test_missing_file_is_enabled_and_admits_nobody(self):
        mgr = AuthManager(os.path.join(self.tmp.name, "missing.json"))
        self.assertTrue(mgr.enabled)
        with self.assertLogs("webui", "ERROR"):
            self.assertEqual(mgr.load_users(), {})
            self.assertFalse(mgr.verify_password("admin", "pass"))

    def test_load_list_format(self):
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump([{"username": "admin", "password": "secret"}, {"username": "tester", "password": "123"}], f)
        mgr = AuthManager(self.users_file)
        self.assertTrue(mgr.enabled)
        self.assertTrue(mgr.verify_password("admin", "secret"))
        self.assertTrue(mgr.verify_password("Admin", "secret"))  # Case-insensitive
        self.assertTrue(mgr.verify_password("tester", "123"))
        self.assertFalse(mgr.verify_password("admin", "wrong"))
        self.assertFalse(mgr.verify_password("nobody", "secret"))

    def test_load_dict_formats(self):
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump({"alice": "pwd1", "bob": {"password": "pwd2"}}, f)
        mgr = AuthManager(self.users_file)
        self.assertTrue(mgr.verify_password("alice", "pwd1"))
        self.assertTrue(mgr.verify_password("bob", "pwd2"))
        self.assertFalse(mgr.verify_password("alice", "wrong"))

    def test_load_users_wrapper_format(self):
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump({"users": [{"username": "charlie", "password": "pwd3"}]}, f)
        mgr = AuthManager(self.users_file)
        self.assertTrue(mgr.verify_password("charlie", "pwd3"))

    def test_sha256_hashed_password(self):
        h = hashlib.sha256("mysecret".encode("utf-8")).hexdigest()
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump([{"username": "secureuser", "password": f"sha256:{h}"}], f)
        mgr = AuthManager(self.users_file)
        self.assertTrue(mgr.verify_password("secureuser", "mysecret"))
        self.assertFalse(mgr.verify_password("secureuser", "wrong"))

    def test_mtime_reloads_users(self):
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump([{"username": "v1", "password": "p1"}], f)
        mgr = AuthManager(self.users_file)
        self.assertTrue(mgr.verify_password("v1", "p1"))

        # Overwrite with new user
        time.sleep(0.05)
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump([{"username": "v2", "password": "p2"}], f)
        # Update mtime explicitly if filesystem timer resolution is low
        os.utime(self.users_file, (time.time() + 1, time.time() + 1))
        self.assertTrue(mgr.verify_password("v2", "p2"))
        self.assertFalse(mgr.verify_password("v1", "p1"))

    def test_session_lifecycle(self):
        mgr = AuthManager(self.users_file)
        sid = mgr.create_session("alice")
        self.assertIsNotNone(sid)
        self.assertEqual(mgr.get_session(sid), "alice")

        # Expiry
        mgr.sessions[sid]["expires_at"] = time.time() - 10
        self.assertIsNone(mgr.get_session(sid))
        self.assertNotIn(sid, mgr.sessions)

        # Revocation
        sid2 = mgr.create_session("bob")
        mgr.revoke_session(sid2)
        self.assertIsNone(mgr.get_session(sid2))

    def test_rate_limiting(self):
        mgr = AuthManager(self.users_file)
        ip = "127.0.0.1"
        self.assertTrue(mgr.check_rate_limit(ip))
        for _ in range(4):
            mgr.record_failure(ip)
        self.assertTrue(mgr.check_rate_limit(ip))
        mgr.record_failure(ip)
        # 5 failures recorded
        self.assertFalse(mgr.check_rate_limit(ip))
        # Success clears
        mgr.record_success(ip)
        self.assertTrue(mgr.check_rate_limit(ip))


class TestAuthRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.users_file = os.path.join(self.tmp.name, "users.json")
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump([{"username": "admin", "password": "secretpassword"}], f)

        self.cfg = ConfigStore(os.path.join(self.tmp.name, "c.json"))
        self.ctl = FakeController()
        self.static = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

        self.app = webui.make_app(
            self.cfg, self.ctl,
            allowed_hosts=[],
            static_dir=self.static,
            users_file=self.users_file
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_unauthenticated_api_access_blocked(self):
        # Public static files are allowed
        r_index = await self.client.get("/")
        self.assertEqual(r_index.status, 200)
        r_js = await self.client.get("/app.js")
        self.assertEqual(r_js.status, 200)

        # Session check tells unauthenticated client auth is required
        r_sess = await self.client.get("/api/session")
        self.assertEqual(r_sess.status, 200)
        sess_data = await r_sess.json()
        self.assertFalse(sess_data["authenticated"])
        self.assertTrue(sess_data["auth_required"])

        # Protected API endpoints return 401
        r_state = await self.client.get("/api/state")
        self.assertEqual(r_state.status, 401)
        data = await r_state.json()
        self.assertEqual(data["error"], "unauthorized")

        r_voices = await self.client.get("/api/voices")
        self.assertEqual(r_voices.status, 401)

        r_write = await self.client.put("/api/guilds/1/enabled", json={"enabled": True})
        self.assertEqual(r_write.status, 401)

    async def test_login_success_and_logout(self):
        # Bad login
        r_bad = await self.client.post("/api/login", json={"username": "admin", "password": "wrong"})
        self.assertEqual(r_bad.status, 401)
        self.assertEqual((await r_bad.json())["error"], "invalid_credentials")

        # Good login
        r_good = await self.client.post("/api/login", json={"username": "admin", "password": "secretpassword"})
        self.assertEqual(r_good.status, 200)
        good_data = await r_good.json()
        self.assertTrue(good_data["ok"])
        self.assertEqual(good_data["username"], "admin")
        self.assertIn("tts_session", self.client.session.cookie_jar.filter_cookies(self.client.make_url("/")))

        # Check session status
        r_sess = await self.client.get("/api/session")
        self.assertEqual(r_sess.status, 200)
        sess_data = await r_sess.json()
        self.assertTrue(sess_data["authenticated"])
        self.assertEqual(sess_data["username"], "admin")

        # Now protected API endpoints succeed
        r_state = await self.client.get("/api/state")
        self.assertEqual(r_state.status, 200)

        # Write endpoint works and falls back to authenticated username if X-Editor not specified
        r_write = await self.client.put("/api/guilds/1/enabled", json={"enabled": True})
        self.assertEqual(r_write.status, 200)
        snap = (await r_write.json())["snapshot"]
        self.assertEqual(snap["updated_by"], "admin")

        # Logout
        r_logout = await self.client.post("/api/logout")
        self.assertEqual(r_logout.status, 200)

        # Subsequent protected API request is 401
        r_state_after = await self.client.get("/api/state")
        self.assertEqual(r_state_after.status, 401)

    async def test_bearer_authorization_header(self):
        # Create session directly
        mgr = self.app[webui.AUTH_MANAGER_KEY]
        sid = mgr.create_session("admin")

        # Make request with Authorization: Bearer <sid>
        headers = {"Authorization": f"Bearer {sid}"}
        r = await self.client.get("/api/state", headers=headers)
        self.assertEqual(r.status, 200)

    async def test_login_rate_limiting(self):
        for _ in range(5):
            r = await self.client.post("/api/login", json={"username": "admin", "password": "wrong"})
            self.assertEqual(r.status, 401)

        # 6th attempt is throttled with 429
        r_throttled = await self.client.post("/api/login", json={"username": "admin", "password": "wrong"})
        self.assertEqual(r_throttled.status, 429)
        self.assertEqual((await r_throttled.json())["error"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
