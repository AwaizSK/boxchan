import os
import re
import shutil
import subprocess
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

import webui
from config_store import ConfigStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
FILES = ("index.html", "app.js", "app.css")
NODE = shutil.which("node") or os.path.expanduser("~/.nvm/versions/node/v22.23.0/bin/node")


def read(n):
    with open(os.path.join(STATIC, n), encoding="utf-8") as f:
        return f.read()


class StaticContentTests(unittest.TestCase):
    def test_files_exist_and_small(self):
        total = 0
        for n in FILES:
            size = os.path.getsize(os.path.join(STATIC, n))
            self.assertGreater(size, 0, n)
            total += size
        self.assertLess(total, 61440)  # 60 KB cap (raised from 56 KB so the Messages-tab code stays readable)

    def test_voice_tab_ids_are_wired(self):
        html, js = read("index.html"), read("app.js")
        for ident in ("tab-channel", "tab-voice", "view-channel", "view-voice", "v-apply", "v-reset", "v-rescan",
                      "v-list", "v-status", "v-warn", "say-text", "say-btn", "say-note"):
            self.assertIn('id="%s"' % ident, html, ident)
            if not ident.startswith(("view-", "tab-")):  # those two are built as 'view-' + name in JS
                self.assertIn(ident, js, ident)
        self.assertIn('maxlength="200"', html)
        self.assertIn("Hello! This is how I sound.", html)
        # the global shortcuts must not hijack typing in the test box
        self.assertIn("t.tagName === 'INPUT'", js)

    def test_messages_tab_ids_are_wired(self):
        html, js = read("index.html"), read("app.js")
        for ident in ("tab-messages", "view-messages", "m-dest", "m-text", "m-count", "m-search", "m-list", "m-chips",
                      "m-preview", "m-ping", "m-send", "m-preset-ready", "m-preset-clear", "m-note", "m-why"):
            self.assertIn('id="%s"' % ident, html, ident)
            if not ident.startswith(("view-", "tab-")):
                self.assertIn(ident, js, ident)
        self.assertIn("<textarea", html)
        # global shortcuts must stay off while the Messages tab is active
        self.assertIn("S.tab === 'messages') return", js)

    def test_features_tab_ids_are_wired(self):
        html, js = read("index.html"), read("app.js")
        for ident in ("tab-features", "view-features", "f-form", "f-name", "f-desc", "f-submit", "f-list", "f-empty"):
            self.assertIn('id="%s"' % ident, html, ident)
            if not ident.startswith(("view-", "tab-")):
                self.assertIn(ident, js, ident)
        self.assertIn("S.tab === 'features') return", js)

    def test_admin_tab_ids_are_wired(self):
        html, js = read("index.html"), read("app.js")
        for ident in ("tab-admin", "view-admin", "u-form", "u-name", "u-pass", "u-groups", "u-submit", "u-list", "u-empty"):
            self.assertIn('id="%s"' % ident, html, ident)
            if not ident.startswith(("view-", "tab-")):
                self.assertIn(ident, js, ident)
        self.assertIn("S.tab === 'admin') return", js)

    def test_csp_safe_content(self):
        for n in FILES:
            s = read(n)
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function",
                        "setAttribute('style'"):
                self.assertNotIn(bad, s, n)
            self.assertIsNone(re.search(r"https?://", s), n)
            self.assertIsNone(re.search(r"\sstyle\s*=", s), n)
        html = read("index.html")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html))
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))
        self.assertNotIn("<style", html)


class StaticServeTests(unittest.IsolatedAsyncioTestCase):
    async def test_served_with_csp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        class Ctl:
            def guilds_view(self): return []
            def status_view(self): return {}

        app = webui.make_app(ConfigStore(os.path.join(tmp.name, "c.json")), Ctl(), allowed_hosts=[], static_dir=STATIC)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        for path, name in (("/", "index.html"), ("/app.js", "app.js"), ("/app.css", "app.css")):
            r = await client.get(path)
            self.assertEqual(r.status, 200, path)
            self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
            self.assertEqual(await r.text(), read(name))


@unittest.skipUnless(NODE and os.path.exists(NODE), "node not available")
class NodeLogicTests(unittest.TestCase):
    def test_js(self):
        r = subprocess.run([NODE, os.path.join(ROOT, "tests", "static_logic.js")],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
