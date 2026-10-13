import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tts-bot-wiring-")
os.environ.pop("DISCORD_TOKEN", None)
os.environ.pop("CREDENTIALS_DIRECTORY", None)
os.environ["DISCORD_TOKEN_FILE"] = os.path.join(_TMP, "no-such-token")
os.environ["STATE_FILE"] = os.path.join(_TMP, "state.json")
os.environ["CONFIG_FILE"] = os.path.join(_TMP, "config.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

import bot  # noqa: E402
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
        self.tokfile = os.path.join(self.tmp.name, "gui_token")
        env = mock.patch.dict(os.environ, {"WEBUI_TOKEN_FILE": self.tokfile}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WEBUI_TOKEN", None)
        os.environ.pop("CREDENTIALS_DIRECTORY", None)

    def write_token(self, tok):
        with open(self.tokfile, "w") as f:
            f.write(tok)

    async def test_no_token(self):
        self.assertIsNone(await bot.maybe_start_webui(self.cfg, FakeController(), "127.0.0.1", free_port()))

    async def test_short_token(self):
        self.write_token("short")
        self.assertIsNone(await bot.maybe_start_webui(self.cfg, FakeController(), "127.0.0.1", free_port()))

    async def test_make_app_valueerror_is_contained(self):
        self.write_token("x" * 40)
        with mock.patch.object(bot, "make_app", side_effect=ValueError("bad")):
            self.assertIsNone(await bot.maybe_start_webui(self.cfg, FakeController(), "127.0.0.1", free_port()))

    async def test_port_in_use(self):
        self.write_token("x" * 40)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            self.assertIsNone(await bot.maybe_start_webui(self.cfg, FakeController(), "127.0.0.1", port))

    async def test_good_token_serves_and_cleans_up(self):
        tok = "g" * 40
        self.write_token(tok)
        port = free_port()
        runner = await bot.maybe_start_webui(self.cfg, FakeController(), "127.0.0.1", port)
        self.assertIsNotNone(runner)
        try:
            async with aiohttp.ClientSession() as s:
                base = f"http://127.0.0.1:{port}"
                r = await s.get(base + "/")
                self.assertEqual(r.status, 200)
                r = await s.get(base + "/api/state")
                self.assertEqual(r.status, 401)
                r = await s.get(base + "/api/state", headers={"Authorization": f"Bearer {tok}"})
                self.assertEqual(r.status, 200)
                self.assertIn("guilds", await r.json())
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
