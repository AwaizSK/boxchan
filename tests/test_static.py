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
        self.assertLess(total, 350000)  # ~350 KB cap for React SPA bundle

    def test_voice_tab_ids_are_wired(self):
        html, js = read("index.html"), read("app.js")
        self.assertIn('id="root"', html)
        for ident in ("tab-channel", "tab-voice", "view-channel", "view-voice", "v-apply", "v-reset", "v-rescan",
                      "v-list", "v-status", "v-warn", "say-text", "say-btn", "say-note"):
            self.assertIn(ident, js, ident)
        self.assertIn("Hello! This is how I sound.", js)
        self.assertTrue(re.search(r'tagName===["\']INPUT["\']', js))

    def test_messages_tab_ids_are_wired(self):
        js = read("app.js")
        for ident in ("tab-messages", "view-messages", "m-dest", "m-text", "m-count", "m-search", "m-list", "m-chips",
                      "m-preview", "m-ping", "m-send", "m-preset-ready", "m-preset-clear", "m-note", "m-why"):
            self.assertIn(ident, js, ident)
        self.assertIn("textarea", js)
        self.assertIn("messages", js)

    def test_features_tab_ids_are_wired(self):
        js = read("app.js")
        for ident in ("tab-features", "view-features", "f-form", "f-name", "f-desc", "f-submit", "f-list", "f-empty"):
            self.assertIn(ident, js, ident)
        self.assertIn("features", js)

    def test_admin_tab_ids_are_wired(self):
        js = read("app.js")
        for ident in ("tab-admin", "view-admin", "u-form", "u-name", "u-pass", "u-groups", "u-submit", "u-list", "u-empty"):
            self.assertIn(ident, js, ident)
        self.assertIn("admin", js)

    def test_csp_safe_content(self):
        # 1. Author source code in frontend/src/ must be clean
        src_dir = os.path.join(ROOT, "frontend", "src")
        for root_dir, _, filenames in os.walk(src_dir):
            for fname in filenames:
                if fname.endswith((".js", ".jsx", ".css")):
                    path = os.path.join(root_dir, fname)
                    with open(path, encoding="utf-8") as f:
                        s = f.read()
                    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function",
                                "setAttribute('style'"):
                        self.assertNotIn(bad, s, fname)
                    self.assertIsNone(re.search(r"https?://", s), fname)
                    self.assertIsNone(re.search(r"\sstyle\s*=", s), fname)

        # 2. Bundle must not contain eval or dynamic code execution
        js = read("app.js")
        self.assertNotIn("eval(", js)
        self.assertNotIn("new Function", js)

        # 3. HTML template must not have inline scripts, styles, or inline handlers
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
