"""`python webui.py add-user|list-users|remove-user` bootstrap CLI."""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webui
from webui import AuthManager, main_cli, verify_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW = "correct horse battery"


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "users.json")

    def prompts(self, *answers):
        it = iter(answers)
        self.asked = []

        def fake(prompt=""):
            self.asked.append(prompt)
            return next(it)
        return fake

    def run_cli(self, argv, answers=(), environ=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):  # argparse writes usage errors to the real stderr
            code = main_cli(argv, getpass_fn=self.prompts(*answers), environ=environ if environ is not None else {},
                            stdout=out, stderr=err)
        self.output = out.getvalue() + err.getvalue()
        return code

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)


class AddUser(CliCase):
    def test_creates_file_0600_with_scrypt_hash_and_user_can_log_in(self):
        self.assertEqual(self.run_cli(["--users-file", self.path, "add-user", "alice"], [PW, PW]), 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        (rec,) = self.read()
        self.assertEqual((rec["username"], rec["groups"]), ("alice", ["user"]))
        self.assertTrue(rec["password"].startswith("scrypt$"))
        self.assertTrue(verify_hash(rec["password"], PW))
        mgr = AuthManager(self.path)
        self.assertTrue(mgr.verify_password("alice", PW))
        self.assertFalse(mgr.is_admin("alice"))
        self.assertEqual(len(self.asked), 2)  # prompted twice
        self.assertNotIn(PW, self.output)
        self.assertNotIn("scrypt", self.output)

    def test_admin_flag(self):
        self.assertEqual(self.run_cli(["add-user", "root1", "--admin", "--users-file", self.path], [PW, PW]), 0)
        self.assertEqual(self.read()[0]["groups"], ["admin"])
        self.assertTrue(AuthManager(self.path).is_admin("root1"))

    def test_path_from_env_and_flag_wins(self):
        env = {"USERS_FILE": self.path}
        self.assertEqual(self.run_cli(["add-user", "alice"], [PW, PW], environ=env), 0)
        other = os.path.join(self.tmp.name, "other.json")
        env = {"USERS_FILE": other}
        self.assertEqual(self.run_cli(["--users-file", self.path, "add-user", "bob"], [PW, PW], environ=env), 0)
        self.assertFalse(os.path.exists(other))
        self.assertEqual([u["username"] for u in self.read()], ["alice", "bob"])

    def test_no_path_is_an_error(self):
        self.assertEqual(self.run_cli(["add-user", "alice"], [PW, PW], environ={}), 2)
        self.assertIn("USERS_FILE", self.output)
        self.assertEqual(self.asked, [])

    def test_mismatch_and_weak_passwords_write_nothing(self):
        self.assertEqual(self.run_cli(["--users-file", self.path, "add-user", "alice"], [PW, PW + "x"]), 1)
        for weak in ("short", "changeme", "x" * 201, ""):
            self.assertEqual(self.run_cli(["--users-file", self.path, "add-user", "alice"], [weak, weak]), 1, weak)
            if "x" in weak:
                self.assertNotIn(weak, self.output)
        self.assertFalse(os.path.exists(self.path))

    def test_bad_usernames(self):
        for bad in ("", "a b", "x" * 41, "../etc", "ü"):
            self.assertEqual(self.run_cli(["--users-file", self.path, "add-user", bad], [PW, PW]), 1, bad)
        self.assertFalse(os.path.exists(self.path))

    def test_password_never_taken_from_argv_or_env(self):
        code = self.run_cli(["--users-file", self.path, "add-user", "alice", "--password", PW], [PW, PW])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.path))
        env = {"PASSWORD": "env-secret-value", "TTS_PASSWORD": "env-secret-value"}
        self.assertEqual(self.run_cli(["--users-file", self.path, "add-user", "alice"], [PW, PW], environ=env), 0)
        self.assertTrue(verify_hash(self.read()[0]["password"], PW))
        self.assertFalse(verify_hash(self.read()[0]["password"], "env-secret-value"))

    def test_update_keeps_other_users_fields_and_groups(self):
        self.write({"site": "keep", "users": [
            {"username": "alice", "password": "legacy-plain-pw", "groups": ["ops"], "note": "n"},
            {"username": "bob", "password": "sha256:abc", "role": "tester"},
        ]})
        self.assertEqual(self.run_cli(["--users-file", self.path, "add-user", "ALICE"], [PW, PW]), 0)
        data = self.read()
        self.assertEqual(data["site"], "keep")
        alice, bob = data["users"]
        self.assertEqual((alice["groups"], alice["note"]), (["ops"], "n"))
        self.assertTrue(verify_hash(alice["password"], PW))
        self.assertEqual(bob, {"username": "bob", "password": "sha256:abc", "role": "tester"})
        self.assertEqual(self.run_cli(["--users-file", self.path, "add-user", "alice", "--admin"], [PW, PW]), 0)
        self.assertEqual(self.read()["users"][0]["groups"], ["admin", "ops"])
        self.assertEqual(len(self.read()["users"]), 2)

    def test_corrupt_file_is_never_overwritten(self):
        for content in ("{not json", "5"):
            with open(self.path, "w") as f:
                f.write(content)
            self.assertEqual(self.run_cli(["--users-file", self.path, "add-user", "alice"], [PW, PW]), 1)
            with open(self.path) as f:
                self.assertEqual(f.read(), content)


class ListAndRemove(CliCase):
    def test_list_shows_names_and_groups_only(self):
        self.write([{"username": "alice", "password": "hunter2-secret-x", "groups": ["admin", "ops"]},
                    {"username": "bob", "password": webui.hash_password(PW)},
                    {"username": "carl", "password": "x", "role": "tester"}])
        self.assertEqual(self.run_cli(["--users-file", self.path, "list-users"]), 0)
        self.assertEqual(self.output.splitlines(), ["alice\tadmin,ops", "bob\tuser", "carl\ttester"])
        self.assertNotIn("hunter2", self.output)
        self.assertNotIn("scrypt", self.output)
        self.assertEqual(self.asked, [])

    def test_list_missing_file(self):
        self.assertEqual(self.run_cli(["--users-file", self.path, "list-users"]), 1)
        self.assertFalse(os.path.exists(self.path))

    def test_remove(self):
        self.write({"users": [{"username": "alice", "password": "a", "groups": ["admin"]},
                              {"username": "bob", "password": "b", "groups": ["user"]}], "site": "keep"})
        self.assertEqual(self.run_cli(["--users-file", self.path, "remove-user", "BOB"]), 0)
        self.assertEqual(self.read(), {"users": [{"username": "alice", "password": "a", "groups": ["admin"]}], "site": "keep"})
        self.assertEqual(self.run_cli(["--users-file", self.path, "remove-user", "bob"]), 1)
        self.assertIn("no such user", self.output)


class EntryPoint(unittest.TestCase):
    def test_import_has_no_side_effects_and_does_not_import_bot(self):
        code = "import sys, webui; assert 'bot' not in sys.modules; print('ok')"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr), (0, "ok", ""))

    def test_script_entry_point(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "users.json")
            with open(path, "w") as f:
                json.dump([{"username": "alice", "password": "x", "groups": ["admin"]}], f)
            env = {k: v for k, v in os.environ.items() if k != "USERS_FILE"}
            env["USERS_FILE"] = path
            r = subprocess.run([sys.executable, os.path.join(ROOT, "webui.py"), "list-users"],
                               capture_output=True, text=True, env=env, cwd=d)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "alice\tadmin\n", ""))


if __name__ == "__main__":
    unittest.main()
