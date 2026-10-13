import logging
import os
import socket
import tempfile
import unittest
from unittest import mock

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import botimport  # noqa: F401,E402  (hermetic env + import of bot; must come first)

import aiohttp  # noqa: E402

bot = botimport.bot
from config_store import ConfigStore  # noqa: E402
from test_webui import FakeController  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MaybeStartWebui(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = ConfigStore(os.path.join(self.tmp.name, "c.json"))
        env = mock.patch.dict(os.environ, {"WEBUI_HOSTS": "127.0.0.1", "WEBUI_AUTH": "off"}, clear=False)  # these tests cover bind/Host policy, not login
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WEBUI_ALLOWED_HOSTS", None)
        for name in ("aiohttp.access", "asyncio"):
            lg = logging.getLogger(name)
            self.addCleanup(lg.setLevel, lg.level)
            lg.setLevel(logging.WARNING)

    def start(self, port=None):
        return bot.maybe_start_webui(self.cfg, FakeController(), port if port is not None else free_port())

    async def start_ok(self, port=None):
        with self.assertLogs("bot", "INFO"), self.assertLogs("webui", "INFO"):
            return await self.start(port)

    async def test_invalid_hosts_disable_gui(self):
        for bad in ("0.0.0.0", "127.0.0.1,192.168.1.5", "example.com", ","):
            os.environ["WEBUI_HOSTS"] = bad
            with self.assertLogs("bot", "ERROR"):
                self.assertIsNone(await self.start(), bad)

    async def test_unexpected_error_contained(self):
        with mock.patch.object(bot, "start_webui", side_effect=RuntimeError("boom")):
            with self.assertLogs("bot", "ERROR"):
                self.assertIsNone(await self.start())

    async def test_make_app_error_contained(self):
        with mock.patch.object(bot, "make_app", side_effect=ValueError("bad")):
            with self.assertLogs("bot", "ERROR"):
                self.assertIsNone(await self.start())

    async def test_out_of_range_port(self):
        with self.assertLogs("bot", "ERROR"):
            self.assertIsNone(await self.start(99999))

    async def test_default_hosts_use_detection(self):
        del os.environ["WEBUI_HOSTS"]
        with mock.patch.object(bot, "detect_tailscale_addrs", return_value=["100.64.0.5"]) as det, \
                mock.patch.object(bot, "start_webui", new=mock.AsyncMock(return_value="R")) as st:
            with self.assertLogs("bot", "INFO"):
                self.assertEqual(await self.start(), "R")
        det.assert_called_once()
        self.assertEqual(st.call_args.args[1], ["127.0.0.1", "100.64.0.5"])

    async def test_default_hosts_without_tailscale(self):
        del os.environ["WEBUI_HOSTS"]
        with mock.patch.object(bot, "detect_tailscale_addrs", return_value=[]), \
                mock.patch.object(bot, "start_webui", new=mock.AsyncMock(return_value="R")) as st:
            with self.assertLogs("bot", "INFO"):
                self.assertEqual(await self.start(), "R")
        self.assertEqual(st.call_args.args[1], ["127.0.0.1"])

    async def test_allowed_hosts_env_reaches_app(self):
        os.environ["WEBUI_ALLOWED_HOSTS"] = "Box.tail.ts.net"
        port = free_port()
        runner = await self.start_ok(port)
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"http://127.0.0.1:{port}/api/state", headers={"Host": "box.tail.ts.net"})
                self.assertEqual(r.status, 200)
                r = await s.get(f"http://127.0.0.1:{port}/api/state", headers={"Host": "other.example"})
                self.assertEqual(r.status, 403)
        finally:
            await runner.cleanup()

    async def test_auth_required_by_default_and_fails_closed(self):
        # No WEBUI_AUTH, USERS_FILE points at a file that does not exist: nobody gets in.
        del os.environ["WEBUI_AUTH"]
        missing = os.path.join(self.tmp.name, "nope", "users.json")
        port = free_port()
        with mock.patch.object(bot, "USERS_FILE", missing):
            with self.assertLogs("webui", "INFO") as errs, self.assertLogs("bot", "INFO"):
                runner = await self.start(port)
        self.assertTrue(any("ERROR" in m and "does not exist" in m for m in errs.output))
        try:
            async with aiohttp.ClientSession() as s:
                base = f"http://127.0.0.1:{port}"
                self.assertEqual((await s.get(base + "/api/state")).status, 401)
                r = await s.get(base + "/api/session")
                self.assertTrue((await r.json())["auth_required"])
                r = await s.post(base + "/api/login", json={"username": "admin", "password": "changeme"})
                self.assertEqual(r.status, 401)
        finally:
            await runner.cleanup()

    async def test_webui_auth_off_opens_gui_and_warns(self):
        port = free_port()
        with self.assertLogs("bot", "WARNING") as cm:
            runner = await self.start(port)
        self.assertTrue(any("authentication is DISABLED (WEBUI_AUTH=off)" in m for m in cm.output))
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"http://127.0.0.1:{port}/api/state")
                self.assertEqual(r.status, 200)
        finally:
            await runner.cleanup()

    async def test_only_exact_off_disables_auth(self):
        for value in ("0", "false", "no", "offf", "off ", "", "on"):
            os.environ["WEBUI_AUTH"] = value
            with mock.patch.object(bot, "make_app", return_value=mock.sentinel.app) as mk, \
                    mock.patch.object(bot, "start_webui", new=mock.AsyncMock(return_value="R")):
                with self.assertLogs("bot", "INFO"):
                    await self.start()
            self.assertEqual(mk.call_args.kwargs["users_file"], bot.USERS_FILE, repr(value))
        os.environ["WEBUI_AUTH"] = "OFF"
        with mock.patch.object(bot, "make_app", return_value=mock.sentinel.app) as mk, \
                mock.patch.object(bot, "start_webui", new=mock.AsyncMock(return_value="R")):
            with self.assertLogs("bot", "WARNING"):
                await self.start()
        self.assertIsNone(mk.call_args.kwargs["users_file"])

    def test_empty_users_file_env_means_default_not_no_auth(self):
        import subprocess
        code = "import sys; sys.path.insert(0, 'tests'); import botimport; print(botimport.bot.USERS_FILE)"
        env = {**os.environ, "USERS_FILE": ""}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                             cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertTrue(out.stdout.strip().endswith("users.json"), out.stdout + out.stderr)

    def test_parse_port(self):
        for raw in ("abc", "0", "99999", "-1", "1.5"):
            self.assertEqual(bot.parse_port(raw), 8765, raw)
        self.assertEqual(bot.parse_port(None), 8765)
        self.assertEqual(bot.parse_port(""), 8765)
        self.assertEqual(bot.parse_port("9000"), 9000)

    async def test_port_in_use(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            with self.assertLogs("webui", "WARNING"), self.assertLogs("bot", "ERROR"):
                self.assertIsNone(await self.start(port))

    async def test_good_config_serves_and_cleans_up(self):
        port = free_port()
        runner = await self.start_ok(port)
        self.assertIsNotNone(runner)
        try:
            async with aiohttp.ClientSession() as s:
                base = f"http://127.0.0.1:{port}"
                r = await s.get(base + "/")
                self.assertEqual(r.status, 200)
                r = await s.get(base + "/api/state")
                self.assertEqual(r.status, 200)
                self.assertIn("guilds", await r.json())
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
