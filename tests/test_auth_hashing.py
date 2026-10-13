"""scrypt password hashing, legacy-hash migration, login off the event loop, password policy."""
import asyncio
import base64
import hashlib
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_store import ConfigStore
import webui
from webui import AuthManager, hash_password, verify_hash

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
GOOD_PW = "correct horse battery"


def _forge(n=2 ** 14, r=8, p=1, salt=b"s" * 16, dk=b"d" * 32):
    return "scrypt$%s$%s$%s$%s$%s" % (n, r, p, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


class HashFunctions(unittest.TestCase):
    def test_round_trip_and_format(self):
        h = hash_password(GOOD_PW)
        parts = h.split("$")
        self.assertEqual(parts[:4], ["scrypt", "16384", "8", "1"])
        self.assertEqual(len(base64.b64decode(parts[4])), 16)
        self.assertEqual(len(base64.b64decode(parts[5])), 32)
        self.assertTrue(verify_hash(h, GOOD_PW))
        self.assertFalse(verify_hash(h, GOOD_PW + "x"))
        self.assertFalse(verify_hash(h, ""))
        self.assertNotIn(GOOD_PW, h)

    def test_salted_and_unicode(self):
        self.assertNotEqual(hash_password(GOOD_PW), hash_password(GOOD_PW))
        h = hash_password("pässwörd-日本語-ok")
        self.assertTrue(verify_hash(h, "pässwörd-日本語-ok"))
        self.assertFalse(verify_hash(h, "passwörd-日本語-ok"))

    def test_malformed_and_tampered_values_are_false_never_raise(self):
        good = hash_password(GOOD_PW)
        p = good.split("$")
        bad = [
            "", "scrypt", "plain", "sha256:abcd", good + "$extra", "$".join(p[:5]),
            "bcrypt$" + "$".join(p[1:]),
            _forge(n=2 ** 30), _forge(n=2 ** 18), _forge(n=2 ** 9), _forge(n=3 * 2 ** 12),  # N range / power of 2
            _forge(r=0), _forge(r=17), _forge(r=10 ** 9), _forge(p=0), _forge(p=5), _forge(p=10 ** 6),
            _forge(salt=b"1234567"), _forge(salt=b"x" * 65),  # salt 8..64
            _forge(dk=b"x" * 15), _forge(dk=b"x" * 65), _forge(dk=b""),  # hash 16..64
            "scrypt$-16384$8$1$%s$%s" % (p[4], p[5]), "scrypt$+16384$8$1$%s$%s" % (p[4], p[5]),
            "scrypt$16384.0$8$1$%s$%s" % (p[4], p[5]), "scrypt$١٦$8$1$%s$%s" % (p[4], p[5]),
            "scrypt$16384$8$1$!!!$%s" % p[5], "scrypt$16384$8$1$%s$REPLACE_ME" % p[4],
            "scrypt$16384$8$1$REPLACE_ME$REPLACE_ME",
        ]
        junk = "scrypt$16384$8$1$%s$%s" % (p[4][:4] + "!!" + p[4][4:], p[5])  # lenient b64 would still decode
        bad.append(junk)
        for value in bad:
            with self.subTest(value=value[:60]):
                # rejected by the parameter checks themselves, before any expensive scrypt call
                with mock.patch.object(webui.hashlib, "scrypt", side_effect=AssertionError("scrypt called")) as sc:
                    self.assertIs(verify_hash(value, GOOD_PW), False)
                    sc.assert_not_called()
        for weird in (None, 5, b"scrypt$"):
            self.assertIs(verify_hash(weird, GOOD_PW), False)

    def test_wrong_hash_same_shape_is_false(self):
        self.assertFalse(verify_hash(_forge(), GOOD_PW))

    def test_in_range_non_default_parameters_verify(self):
        salt = b"0123456789abcdef"
        dk = hashlib.scrypt(GOOD_PW.encode(), salt=salt, n=2 ** 10, r=2, p=2, dklen=24)
        self.assertTrue(verify_hash(_forge(n=2 ** 10, r=2, p=2, salt=salt, dk=dk), GOOD_PW))

    def test_policy(self):
        self.assertIsNone(webui.password_policy_error(GOOD_PW))
        for pw in ("short", "a" * 9, "a" * 201, "", None, 12345678901, ["x" * 12]):
            self.assertEqual(webui.password_policy_error(pw), "weak_password", pw)
        self.assertIsNone(webui.password_policy_error("a" * 10))
        self.assertIsNone(webui.password_policy_error("a" * 200))


class _FileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "users.json")

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


class LegacyMigration(_FileCase):
    def test_plaintext_login_upgrades_to_scrypt_preserving_everything_else(self):
        self.write({"site": "keep-me", "users": [
            {"username": "alice", "password": "alice-plain-pw", "groups": ["admin", "ops"], "note": "x"},
            {"username": "bob", "password": "bob-plain-pw", "role": "tester"},
        ]})
        mgr = AuthManager(self.path)
        self.assertTrue(mgr.verify_password("Alice", "alice-plain-pw"))
        data = self.read()
        alice, bob = data["users"]
        self.assertTrue(alice["password"].startswith("scrypt$"))
        self.assertTrue(verify_hash(alice["password"], "alice-plain-pw"))
        self.assertEqual((alice["groups"], alice["note"], alice["username"]), (["admin", "ops"], "x", "alice"))
        self.assertEqual(bob, {"username": "bob", "password": "bob-plain-pw", "role": "tester"})
        self.assertEqual(data["site"], "keep-me")
        self.assertEqual(mgr.get_user_groups("alice"), {"admin", "ops"})
        # later logins keep working against the hash, and do not rewrite the file again
        before = os.stat(self.path).st_mtime_ns
        self.assertTrue(mgr.verify_password("alice", "alice-plain-pw"))
        self.assertFalse(mgr.verify_password("alice", "wrong-password"))
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)
        # the other legacy user is still migratable
        self.assertTrue(mgr.verify_password("bob", "bob-plain-pw"))
        self.assertTrue(self.read()["users"][1]["password"].startswith("scrypt$"))
        self.assertEqual(self.read()["users"][1]["role"], "tester")

    def test_sha256_login_upgrades(self):
        h = hashlib.sha256(b"sha-user-pw1").hexdigest()
        self.write([{"username": "carol", "password": "sha256:" + h, "groups": ["user"]},
                    {"username": "dave", "password": "dave-plain-pw"}])
        mgr = AuthManager(self.path)
        self.assertFalse(mgr.verify_password("carol", "nope-nope-nope"))
        self.assertEqual(self.read()[0]["password"], "sha256:" + h)  # failed login upgrades nothing
        self.assertTrue(mgr.verify_password("carol", "sha-user-pw1"))
        data = self.read()
        self.assertTrue(verify_hash(data[0]["password"], "sha-user-pw1"))
        self.assertEqual(data[0]["groups"], ["user"])
        self.assertEqual(data[1], {"username": "dave", "password": "dave-plain-pw"})

    def test_dict_layouts_survive_upgrade(self):
        self.write({"alice": {"password": "alice-plain-pw", "role": "admin"}, "bob": "bob-plain-pw"})
        mgr = AuthManager(self.path)
        self.assertTrue(mgr.verify_password("bob", "bob-plain-pw"))
        self.assertTrue(mgr.is_admin("alice"))
        self.assertTrue(mgr.verify_password("alice", "alice-plain-pw"))
        self.assertTrue(mgr.is_admin("alice"))
        self.assertTrue(mgr.verify_password("bob", "bob-plain-pw"))
        self.assertFalse(mgr.is_admin("bob"))

    def test_failed_upgrade_does_not_fail_login_and_warns(self):
        self.write([{"username": "erin", "password": "erin-plain-pw"}])
        mgr = AuthManager(self.path)
        with mock.patch.object(webui, "atomic_write_json", side_effect=OSError("disk full")):
            with self.assertLogs("webui", "WARNING") as cm:
                self.assertTrue(mgr.verify_password("erin", "erin-plain-pw"))
        self.assertIn("erin", "\n".join(cm.output))
        self.assertNotIn("erin-plain-pw", "\n".join(cm.output))
        self.assertEqual(self.read()[0]["password"], "erin-plain-pw")

    def test_upgrade_does_not_clobber_concurrent_change(self):
        self.write([{"username": "frank", "password": "frank-plain-pw"}])
        mgr = AuthManager(self.path)
        mgr.load_users()
        newer = hash_password("changed-meanwhile")
        self.write([{"username": "frank", "password": newer}])
        mgr._upgrade_hash("frank", "frank-plain-pw", "frank-plain-pw")
        self.assertEqual(self.read()[0]["password"], newer)

    def test_non_ascii_plaintext_compare_does_not_crash(self):
        self.write([{"username": "gina", "password": "pässwörd-ünï"}])
        mgr = AuthManager(self.path)
        self.assertFalse(mgr.verify_password("gina", "passwörd-ünï"))
        self.assertTrue(mgr.verify_password("gina", "pässwörd-ünï"))

    def test_placeholder_hash_never_falls_back_to_plaintext(self):
        stored = "scrypt$16384$8$1$REPLACE_ME$REPLACE_ME"
        self.write([{"username": "hank", "password": stored}])
        mgr = AuthManager(self.path)
        self.assertFalse(mgr.verify_password("hank", stored))
        self.assertFalse(mgr.verify_password("hank", "REPLACE_ME"))


class KnownBadAndTiming(_FileCase):
    def test_known_default_passwords_refused_even_when_stored(self):
        for i, pw in enumerate(("changeme", "testerpassword", "password", "admin", "ChangeMe")):
            user = f"victim{i}"
            for stored in (pw, hash_password(pw), "sha256:" + hashlib.sha256(pw.encode()).hexdigest()):
                self.write([{"username": user, "password": stored}])
                mgr = AuthManager(self.path)
                with self.assertLogs("webui", "ERROR") as cm:
                    self.assertFalse(mgr.verify_password(user, pw))
                out = "\n".join(cm.output)
                self.assertIn(user, out)
                self.assertNotIn(pw, out)
                self.assertNotIn("scrypt$", out)

    def test_unknown_user_runs_dummy_verify(self):
        self.write([{"username": "ivy", "password": hash_password(GOOD_PW)}])
        mgr = AuthManager(self.path)
        with mock.patch.object(webui, "verify_hash", wraps=webui.verify_hash) as spy:
            self.assertFalse(mgr.verify_password("nobody", GOOD_PW))
        spy.assert_called_once_with(webui._DUMMY_HASH, GOOD_PW)
        with mock.patch.object(webui, "verify_hash", wraps=webui.verify_hash) as spy:
            self.assertTrue(mgr.verify_password("ivy", GOOD_PW))
        spy.assert_called_once()
        self.assertNotEqual(spy.call_args.args[0], webui._DUMMY_HASH)
        self.assertFalse(webui.verify_hash(webui._DUMMY_HASH, GOOD_PW))


class LoginRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_webui import FakeController
        nh = logging.NullHandler()  # keep expected login-failure warnings off stderr
        logging.getLogger("webui").addHandler(nh)
        self.addCleanup(logging.getLogger("webui").removeHandler, nh)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.users = os.path.join(self.tmp.name, "users.json")
        with open(self.users, "w", encoding="utf-8") as f:
            json.dump([{"username": "root1", "password": hash_password(GOOD_PW), "groups": ["admin"]},
                       {"username": "tester", "password": hash_password("tester-password"), "groups": ["user"]}], f)
        self.app = webui.make_app(ConfigStore(os.path.join(self.tmp.name, "c.json")), FakeController(),
                                  allowed_hosts=[], static_dir=STATIC, users_file=self.users)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def login(self, u, p):
        return await self.client.post("/api/login", json={"username": u, "password": p})

    async def test_unknown_and_wrong_password_are_indistinguishable(self):
        a = await self.login("nobody", GOOD_PW)
        b = await self.login("root1", "wrong-password-1")
        self.assertEqual((a.status, await a.json()), (401, {"error": "invalid_credentials"}))
        self.assertEqual((b.status, await b.json()), (401, {"error": "invalid_credentials"}))

    async def test_throttle_checked_before_any_hashing(self):
        for _ in range(webui.MAX_LOGIN_FAILURES):
            self.assertEqual((await self.login("root1", "wrong-password-1")).status, 401)
        with mock.patch.object(webui, "verify_hash") as vh, mock.patch.object(webui, "hash_password") as hp:
            r = await self.login("root1", GOOD_PW)
            self.assertEqual(r.status, 429)
            vh.assert_not_called()
            hp.assert_not_called()

    async def test_login_hashing_runs_in_executor_and_loop_stays_responsive(self):
        mgr = self.app[webui.AUTH_MANAGER_KEY]
        started, release = threading.Event(), threading.Event()
        seen = []
        real = mgr.verify_password

        def slow(u, p):
            seen.append(threading.current_thread())
            started.set()
            release.wait(10)
            return real(u, p)

        with mock.patch.object(mgr, "verify_password", slow):
            login = asyncio.create_task(self.login("root1", GOOD_PW))
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            r = await asyncio.wait_for(self.client.get("/api/session"), 3)  # served while login hashes
            self.assertEqual(r.status, 200)
            self.assertFalse(login.done())
            release.set()
            resp = await login
        self.assertEqual(resp.status, 200)
        self.assertIsNot(seen[0], threading.main_thread())

    async def test_no_secrets_in_responses_or_logs(self):
        with self.assertLogs("webui", "WARNING") as cm:
            bad = await self.login("root1", "wrong-password-1")
        out = "\n".join(cm.output) + await bad.text()
        self.assertNotIn("wrong-password-1", out)
        ok = await self.login("root1", GOOD_PW)
        self.assertEqual(ok.status, 200)
        text = await ok.text()
        self.assertNotIn(GOOD_PW, text)
        self.assertNotIn("scrypt", text)

    async def test_admin_save_policy_hashing_and_no_leaks(self):
        await self.login("root1", GOOD_PW)
        for bad in ("short", "x" * 9, "y" * 201):
            r = await self.client.post("/api/admin/users", json={"username": "newbie", "password": bad, "groups": ["user"]})
            self.assertEqual((r.status, (await r.json())["error"]), (400, "weak_password"), bad[:10])
        r = await self.client.post("/api/admin/users", json={"username": "newbie", "password": 12345678901234})
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/admin/users", json={"username": "newbie", "groups": ["user"]})
        self.assertEqual((r.status, (await r.json())["error"]), (400, "password_required"))
        with open(self.users, encoding="utf-8") as f:
            self.assertNotIn("newbie", f.read())

        r = await self.client.post("/api/admin/users", json={"username": "newbie", "password": "a-fine-password", "groups": ["user"]})
        self.assertEqual(r.status, 200)
        body = await r.text()
        self.assertNotIn("a-fine-password", body)
        self.assertNotIn("scrypt", body)
        with open(self.users, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("a-fine-password", raw)
        rec = [u for u in json.loads(raw) if u["username"] == "newbie"][0]
        self.assertTrue(verify_hash(rec["password"], "a-fine-password"))

        # groups-only change keeps the stored hash
        r = await self.client.post("/api/admin/users", json={"username": "newbie", "groups": ["user", "ops"]})
        self.assertEqual(r.status, 200)
        with open(self.users, encoding="utf-8") as f:
            rec2 = [u for u in json.load(f) if u["username"] == "newbie"][0]
        self.assertEqual(rec2["password"], rec["password"])
        self.assertEqual(rec2["groups"], ["user", "ops"])

        listing = await (await self.client.get("/api/admin/users")).text()
        self.assertNotIn("scrypt", listing)
        self.assertNotIn("password", listing)

    async def test_admin_save_hashes_off_the_event_loop(self):
        await self.login("root1", GOOD_PW)
        mgr = self.app[webui.AUTH_MANAGER_KEY]
        threads = []
        real = mgr.save_user

        def spy(*a):
            threads.append(threading.current_thread())
            return real(*a)

        with mock.patch.object(mgr, "save_user", spy):
            r = await self.client.post("/api/admin/users", json={"username": "zed", "password": "a-fine-password", "groups": ["user"]})
        self.assertEqual(r.status, 200)
        self.assertIsNot(threads[0], threading.main_thread())


if __name__ == "__main__":
    unittest.main()
