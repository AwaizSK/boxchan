import os
import tempfile
import unittest
from aiohttp.test_utils import TestClient, TestServer

from config_store import ConfigStore
from features import FeatureStore
import webui


class FeatureStoreUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "features.json")
        self.store = FeatureStore(self.path)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_add_and_list_features(self):
        self.assertEqual(self.store.get_features(), [])
        f1 = await self.store.add_feature("Dark mode toggle", "Add auto dark mode", "alice")
        self.assertEqual(f1["name"], "Dark mode toggle")
        self.assertEqual(f1["description"], "Add auto dark mode")
        self.assertEqual(f1["author"], "alice")
        self.assertIn("id", f1)
        self.assertIn("created_at", f1)

        f2 = await self.store.add_feature("Export queue", "Export clips to wav", "bob")
        features = self.store.get_features()
        self.assertEqual(len(features), 2)
        # Newest first
        self.assertEqual(features[0]["id"], f2["id"])
        self.assertEqual(features[1]["id"], f1["id"])

    async def test_persistence_across_instances(self):
        await self.store.add_feature("Persistent request", "Check disk reload", "tester")
        store2 = FeatureStore(self.path)
        items = store2.get_features()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Persistent request")

    async def test_add_validation_errors(self):
        with self.assertRaises(ValueError):
            await self.store.add_feature("", "desc", "author")
        with self.assertRaises(ValueError):
            await self.store.add_feature("a" * 101, "desc", "author")
        with self.assertRaises(ValueError):
            await self.store.add_feature("name", "", "author")
        with self.assertRaises(ValueError):
            await self.store.add_feature("name", "d" * 2001, "author")

    async def test_delete_feature(self):
        f = await self.store.add_feature("To Delete", "Remove me", "admin")
        self.assertTrue(await self.store.delete_feature(f["id"]))
        self.assertFalse(await self.store.delete_feature(f["id"]))
        self.assertEqual(self.store.get_features(), [])


class FeatureApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "cfg.json")
        self.users_path = os.path.join(self.tmp.name, "users.json")
        self.features_path = os.path.join(self.tmp.name, "features.json")

        with open(self.users_path, "w", encoding="utf-8") as f:
            f.write(
                '[\n'
                '  {"username": "admin", "password": "adminpass", "role": "admin"},\n'
                '  {"username": "tester", "password": "testerpass", "role": "user"}\n'
                ']\n'
            )

        class FakeCtl:
            def guilds_view(self): return []
            def status_view(self): return {}

        self.cfg = ConfigStore(self.cfg_path)
        self.app = webui.make_app(
            self.cfg,
            FakeCtl(),
            allowed_hosts=[],
            users_file=self.users_path,
            features_file=self.features_path,
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def _login(self, username, password):
        r = await self.client.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status, 200)

    async def test_get_features_requires_auth(self):
        r = await self.client.get("/api/features")
        self.assertEqual(r.status, 401)

    async def test_add_and_list_features_api(self):
        await self._login("tester", "testerpass")
        r = await self.client.post("/api/features", json={
            "name": "Volume Slider",
            "description": "Adjust output audio volume"
        })
        self.assertEqual(r.status, 201)
        data = await r.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["feature"]["name"], "Volume Slider")
        self.assertEqual(data["feature"]["author"], "tester")

        r_list = await self.client.get("/api/features")
        self.assertEqual(r_list.status, 200)
        items = (await r_list.json())["features"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Volume Slider")
        self.assertEqual(items[0]["author"], "tester")

    async def test_non_admin_cannot_delete_feature(self):
        await self._login("tester", "testerpass")
        r = await self.client.post("/api/features", json={
            "name": "Feature 1",
            "description": "Description 1"
        })
        fid = (await r.json())["feature"]["id"]

        r_del = await self.client.delete(f"/api/features/{fid}")
        self.assertEqual(r_del.status, 403)

    async def test_admin_can_delete_feature(self):
        await self._login("tester", "testerpass")
        r = await self.client.post("/api/features", json={
            "name": "Feature To Delete",
            "description": "Description"
        })
        fid = (await r.json())["feature"]["id"]

        # Logout tester, login admin
        await self.client.post("/api/logout")
        await self._login("admin", "adminpass")

        r_del = await self.client.delete(f"/api/features/{fid}")
        self.assertEqual(r_del.status, 200)
        self.assertTrue((await r_del.json()).get("ok"))

        # Verify it is gone
        r_list = await self.client.get("/api/features")
        self.assertEqual((await r_list.json())["features"], [])


if __name__ == "__main__":
    unittest.main()
