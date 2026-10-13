"""Fail-closed login: a configured users file that is missing/unreadable/invalid/empty admits nobody."""
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_store import ConfigStore
import webui
from webui import AuthManager, hash_password

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
PW = "correct horse battery"


def _broken_variants(tmp):
    """name -> function(path) that puts the users file into a broken state."""
    def missing(p):
        pass

    def directory(p):
        os.mkdir(p)  # open() fails with IsADirectoryError: "unreadable"

    def write(text):
        def f(p):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        return f

    def raw_bytes(p):
        with open(p, "wb") as fh:
            fh.write(b"\xff\xfe\x00bad")

    return {
        "missing": missing, "unreadable": directory, "invalid_json": write("{not json"),
        "bad_encoding": raw_bytes, "scalar": write("5"), "empty_list": write("[]"),
        "empty_object": write("{}"), "empty_users": write('{"users": []}'),
        "no_passwords": write('[{"username": "admin"}]'), "blank_names": write('[{"username": " ", "password": "x"}]'),
    }


class _Quiet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "users.json")


class FailClosedManager(_Quiet):
    def test_every_broken_state_is_enabled_and_admits_nobody(self):
        for name, breaker in _broken_variants(self.tmp.name).items():
            with self.subTest(name):
                path = os.path.join(self.tmp.name, name + ".json")
                breaker(path)
                mgr = AuthManager(path)
                self.assertTrue(mgr.enabled)
                with self.assertLogs("webui", "ERROR"):
                    self.assertEqual(mgr.load_users(), {})
                for user in ("admin", "root", ""):
                    self.assertFalse(mgr.verify_password(user, PW))
                    self.assertFalse(mgr.verify_password(user, "changeme"))
                    self.assertFalse(mgr.is_admin(user))
                    self.assertFalse(mgr.in_group(user, "user"))
                    self.assertFalse(mgr.user_exists(user))
                    self.assertEqual(mgr.get_user_groups(user), set())
                self.assertEqual(mgr.list_users(), [])

    def test_one_error_per_condition_not_per_request(self):
        mgr = AuthManager(os.path.join(self.tmp.name, "missing.json"))
        with self.assertLogs("webui", "ERROR") as cm:
            for _ in range(10):
                mgr.load_users()
                mgr.verify_password("a", PW)
                mgr.is_admin("a")
        self.assertEqual(len(cm.records), 1)
        self.assertIn("does not exist", cm.output[0])

    def test_distinct_conditions_log_separately_and_rearm_after_fix(self):
        mgr = AuthManager(self.path)
        with self.assertLogs("webui", "ERROR") as cm:
            mgr.load_users()  # missing
            with open(self.path, "w") as f:
                f.write("{oops")
            mgr.load_users()  # invalid
            mgr.load_users()
            with open(self.path, "w") as f:
                json.dump([{"username": "u1", "password": hash_password(PW)}], f)
            self.assertIn("u1", mgr.load_users())  # fixed: re-arms
            with open(self.path, "w") as f:
                f.write("{oops again")
            os.utime(self.path, (1, 1))
            mgr.load_users()  # invalid again: a new report
        self.assertEqual(len(cm.records), 3)

    def test_transient_error_keeps_last_good_cache(self):
        with open(self.path, "w") as f:
            json.dump([{"username": "u1", "password": hash_password(PW), "groups": ["admin"]}], f)
        mgr = AuthManager(self.path)
        self.assertTrue(mgr.verify_password("u1", PW))
        with open(self.path, "w") as f:
            f.write("{truncated")
        with self.assertLogs("webui", "ERROR") as cm:
            self.assertTrue(mgr.verify_password("u1", PW))
            self.assertTrue(mgr.is_admin("u1"))
            mgr.load_users()
        self.assertEqual(len(cm.records), 1)

    def test_file_vanishing_after_good_load_keeps_last_good_cache_and_reports(self):
        with open(self.path, "w") as f:
            json.dump([{"username": "u1", "password": hash_password(PW)}], f)
        mgr = AuthManager(self.path)
        self.assertTrue(mgr.verify_password("u1", PW))
        os.remove(self.path)
        with self.assertLogs("webui", "ERROR") as cm:
            self.assertTrue(mgr.verify_password("u1", PW))
            self.assertTrue(mgr.verify_password("u1", PW))
        self.assertEqual(len(cm.records), 1)

    def test_valid_file_emptied_later_admits_nobody(self):
        with open(self.path, "w") as f:
            json.dump([{"username": "u1", "password": hash_password(PW)}], f)
        mgr = AuthManager(self.path)
        self.assertTrue(mgr.verify_password("u1", PW))
        with open(self.path, "w") as f:
            f.write("[]")
        with self.assertLogs("webui", "ERROR"):
            self.assertFalse(mgr.verify_password("u1", PW))
        self.assertFalse(mgr.user_exists("u1"))

    def test_no_path_means_open_library_mode(self):
        for none in (None, ""):
            mgr = AuthManager(none)
            self.assertFalse(mgr.enabled)
            self.assertTrue(mgr.is_admin("anyone"))
            self.assertTrue(mgr.in_group(None, "admin"))
            self.assertEqual(mgr.get_user_groups("x"), {"admin"})


class HardCodedAdmin(_Quiet):
    def write(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f)

    def test_admin_is_group_membership_per_file_only(self):
        self.write([
            {"username": "admin", "password": hash_password(PW), "groups": ["user"]},
            {"username": "Boss", "password": hash_password(PW), "groups": ["Admin"]},
            {"username": "root", "password": hash_password(PW), "groups": ["user"]},
        ])
        mgr = AuthManager(self.path)
        self.assertFalse(mgr.is_admin("admin"))  # the name alone grants nothing
        self.assertFalse(mgr.in_group("ADMIN", "admin"))
        self.assertTrue(mgr.is_admin("boss"))
        self.assertFalse(mgr.is_admin("root"))
        self.assertFalse(mgr.is_admin("ghost"))
        self.assertFalse(mgr.is_admin(None))

    def test_unknown_user_named_admin_is_not_admin_when_file_lacks_it(self):
        self.write([{"username": "alice", "password": hash_password(PW), "groups": ["admin"]}])
        mgr = AuthManager(self.path)
        self.assertTrue(mgr.enabled)
        self.assertFalse(mgr.is_admin("admin"))
        self.assertFalse(mgr.in_group("admin", "admin"))
        self.assertEqual(mgr.get_user_groups("admin"), set())

    def test_legacy_admin_without_groups_is_migrated_on_load(self):
        self.write([{"username": "admin", "password": "legacy-plain-pw"},
                    {"username": "bob", "password": "bob-plain-pw"}])
        mgr = AuthManager(self.path)
        self.assertTrue(mgr.is_admin("admin"))
        self.assertTrue(mgr.is_admin("ADMIN"))
        self.assertFalse(mgr.is_admin("bob"))
        self.assertEqual(mgr.get_user_groups("bob"), {"user"})
        # persisted on the next write: afterwards the file itself carries the group
        self.assertTrue(mgr.save_user("bob", None, ["user"]))
        with open(self.path) as f:
            admin = [u for u in json.load(f) if u["username"] == "admin"][0]
        self.assertTrue(mgr.is_admin("admin"))
        self.assertEqual(admin["groups"], ["admin"])
        self.assertEqual(admin["password"], "legacy-plain-pw")  # untouched until that user logs in

    def test_legacy_admin_with_explicit_groups_is_not_forced(self):
        self.write({"admin": {"password": "legacy-plain-pw", "groups": ["tester"]}})
        mgr = AuthManager(self.path)
        self.assertFalse(mgr.is_admin("admin"))

    def test_save_user_named_admin_gets_no_implicit_group(self):
        self.write([{"username": "root", "password": hash_password(PW), "groups": ["admin"]}])
        mgr = AuthManager(self.path)
        mgr.save_user("admin", "a-fine-password", ["user"])
        self.assertFalse(mgr.is_admin("admin"))


class ExampleFile(_Quiet):
    EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "users.example.json")

    def test_example_ships_no_usable_credentials(self):
        with open(self.EXAMPLE, encoding="utf-8") as f:
            text = f.read()
        data = json.loads(text)
        self.assertTrue(data)
        for known in ("changeme", "testerpassword"):
            self.assertNotIn(known, text)
        for rec in data:
            self.assertTrue(rec["password"].startswith("scrypt$"), rec["username"])
            self.assertFalse(webui.verify_hash(rec["password"], rec["password"]))

    def test_copy_of_example_logs_nobody_in_with_old_defaults_or_anything(self):
        with open(self.EXAMPLE, encoding="utf-8") as f:
            data = json.load(f)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        mgr = AuthManager(self.path)
        self.assertTrue(mgr.enabled)
        self.assertEqual({u["username"] for u in mgr.list_users()}, {r["username"] for r in data})
        guesses = ("changeme", "testerpassword", "password", "admin", "REPLACE_ME", PW,
                   "scrypt$16384$8$1$REPLACE_ME$REPLACE_ME", "")
        with self.assertLogs("webui", "ERROR"):  # known defaults are logged as refused
            for rec in data:
                for g in guesses:
                    self.assertFalse(mgr.verify_password(rec["username"], g), (rec["username"], g))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), data)  # nothing "upgraded"


class FailClosedRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_webui import FakeController
        nh = logging.NullHandler()
        logging.getLogger("webui").addHandler(nh)
        self.addCleanup(logging.getLogger("webui").removeHandler, nh)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = ConfigStore(os.path.join(self.tmp.name, "c.json"))
        self.ctl = FakeController()

    async def client_for(self, users_file):
        app = webui.make_app(self.cfg, self.ctl, allowed_hosts=[], static_dir=STATIC, users_file=users_file)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return app, client

    async def test_every_broken_state_returns_401_everywhere(self):
        for name, breaker in _broken_variants(self.tmp.name).items():
            path = os.path.join(self.tmp.name, name + ".json")
            breaker(path)
            _, c = await self.client_for(path)
            sess = await (await c.get("/api/session")).json()
            self.assertTrue(sess["auth_required"], name)
            self.assertFalse(sess["authenticated"], name)
            for pw in (PW, "changeme", "testerpassword", "password", "admin"):
                r = await c.post("/api/login", json={"username": "admin", "password": pw})
                self.assertEqual((name, r.status), (name, 401))
            for method, url in (("get", "/api/state"), ("get", "/api/voices"), ("get", "/api/admin/users"),
                                ("get", "/api/features"), ("put", "/api/guilds/1/enabled"),
                                ("post", "/api/admin/users"), ("delete", "/api/admin/users/x"),
                                ("get", "/api/events")):
                r = await getattr(c, method)(url)
                self.assertEqual((name, url, r.status), (name, url, 401))

    async def test_session_forged_for_unknown_user_is_rejected(self):
        path = os.path.join(self.tmp.name, "u.json")
        with open(path, "w") as f:
            json.dump([{"username": "alice", "password": hash_password(PW), "groups": ["admin"]}], f)
        app, c = await self.client_for(path)
        mgr = app[webui.AUTH_MANAGER_KEY]
        ghost = mgr.create_session("admin")  # named admin, but not in the file
        r = await c.get("/api/state", headers={"Authorization": f"Bearer {ghost}"})
        self.assertEqual(r.status, 401)
        self.assertNotIn(ghost, mgr.sessions)
        ok = mgr.create_session("alice")
        r = await c.get("/api/admin/users", headers={"Authorization": f"Bearer {ok}"})
        self.assertEqual(r.status, 200)
        with open(path, "w") as f:  # alice removed from the file: her session dies with her
            json.dump([{"username": "bob", "password": hash_password(PW)}], f)
        os.utime(path, (2, 2))
        r = await c.get("/api/state", headers={"Authorization": f"Bearer {ok}"})
        self.assertEqual(r.status, 401)

    async def test_admin_routes_denied_for_non_admin(self):
        path = os.path.join(self.tmp.name, "u.json")
        with open(path, "w") as f:
            json.dump([{"username": "alice", "password": hash_password(PW), "groups": ["admin"]},
                       {"username": "admin", "password": hash_password(PW), "groups": ["user"]}], f)
        app, c = await self.client_for(path)
        mgr = app[webui.AUTH_MANAGER_KEY]
        # a user literally called "admin" without the group is a normal user
        r = await c.post("/api/login", json={"username": "admin", "password": PW})
        self.assertEqual(r.status, 200)
        self.assertFalse((await r.json())["is_admin"])
        self.assertEqual((await c.get("/api/admin/users")).status, 403)
        self.assertEqual((await c.post("/api/admin/users", json={"username": "x", "password": "a-fine-password"})).status, 403)
        self.assertEqual((await c.delete("/api/admin/users/alice")).status, 403)
        self.assertEqual((await c.delete("/api/features/abc")).status, 403)

    async def test_make_app_ignores_users_file_env(self):
        path = os.path.join(self.tmp.name, "u.json")
        with mock.patch.dict(os.environ, {"USERS_FILE": path}):
            app, c = await self.client_for(None)
        self.assertFalse(app[webui.AUTH_MANAGER_KEY].enabled)
        self.assertEqual((await c.get("/api/state")).status, 200)
        self.assertEqual((await (await c.get("/api/session")).json())["auth_required"], False)


if __name__ == "__main__":
    unittest.main()
