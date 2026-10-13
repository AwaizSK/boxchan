import json
import os
import tempfile
import unittest
from aiohttp.test_utils import TestClient, TestServer

from config_store import ConfigStore
import webui


class AdminUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "users.json")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps([
                {"username": "admin", "password": "adminpass", "groups": ["admin"]},
                {"username": "tester", "password": "testerpass", "groups": ["tester"]},
            ]))
        self.auth = webui.AuthManager(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_groups_and_admin_check(self):
        self.assertTrue(self.auth.is_admin("admin"))
        self.assertTrue(self.auth.in_group("admin", "admin"))
        self.assertFalse(self.auth.is_admin("tester"))
        self.assertTrue(self.auth.in_group("tester", "tester"))
        self.assertFalse(self.auth.in_group("tester", "admin"))

    def test_save_and_delete_user(self):
        # Add new user with admin group
        self.auth.save_user("bob", "bobpass", ["admin", "tester"])
        self.assertTrue(self.auth.is_admin("bob"))
        self.assertTrue(self.auth.in_group("bob", "tester"))
        self.assertTrue(self.auth.verify_password("bob", "bobpass"))

        # Verify listed users
        users = self.auth.list_users()
        unames = [u["username"] for u in users]
        self.assertIn("bob", unames)
        self.assertIn("admin", unames)
        self.assertIn("tester", unames)

        # Delete user
        self.assertTrue(self.auth.delete_user("bob"))
        self.assertFalse(self.auth.is_admin("bob"))
        self.assertFalse(self.auth.delete_user("bob"))


class AdminApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "cfg.json")
        self.users_path = os.path.join(self.tmp.name, "users.json")
        with open(self.users_path, "w", encoding="utf-8") as f:
            f.write(json.dumps([
                {"username": "admin", "password": "adminpass", "groups": ["admin"]},
                {"username": "tester", "password": "testerpass", "groups": ["tester"]},
            ]))

        class FakeCtl:
            def guilds_view(self): return []
            def status_view(self): return {}

        self.cfg = ConfigStore(self.cfg_path)
        self.app = webui.make_app(
            self.cfg,
            FakeCtl(),
            allowed_hosts=[],
            users_file=self.users_path,
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def _login(self, username, password):
        r = await self.client.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status, 200)

    async def test_get_users_forbidden_for_non_admin(self):
        await self._login("tester", "testerpass")
        r = await self.client.get("/api/admin/users")
        self.assertEqual(r.status, 403)

    async def test_get_users_allowed_for_admin(self):
        await self._login("admin", "adminpass")
        r = await self.client.get("/api/admin/users")
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertIn("users", data)
        self.assertEqual(len(data["users"]), 2)
        # Check passwords are not exposed
        for u in data["users"]:
            self.assertNotIn("password", u)

    async def test_admin_add_user_and_permissions(self):
        await self._login("admin", "adminpass")
        r = await self.client.post("/api/admin/users", json={
            "username": "newadmin",
            "password": "secretpassword",
            "groups": ["admin", "tester"]
        })
        self.assertEqual(r.status, 200)

        # Logout admin and login as newadmin
        await self.client.post("/api/logout")
        await self._login("newadmin", "secretpassword")

        # newadmin should be able to access admin users endpoint
        r_get = await self.client.get("/api/admin/users")
        self.assertEqual(r_get.status, 200)

    async def test_admin_delete_user(self):
        await self._login("admin", "adminpass")
        # Cannot delete self
        r_self = await self.client.delete("/api/admin/users/admin")
        self.assertEqual(r_self.status, 400)

        # Delete tester
        r_del = await self.client.delete("/api/admin/users/tester")
        self.assertEqual(r_del.status, 200)

        # Verify tester is gone
        r_get = await self.client.get("/api/admin/users")
        users = (await r_get.json())["users"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["username"], "admin")


if __name__ == "__main__":
    unittest.main()
