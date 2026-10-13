"""Regression test: no untrusted code may reach the self-hosted runner (stdlib only, no YAML parser).

check_workflow(text) returns a list of violations; the real workflow must yield none and every
mutated variant below must yield at least the expected one."""
import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"


def _strip(text):
    return [l.rstrip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def parse_jobs(text):
    """{job: [lines]} for the 2-space-indented keys under top-level `jobs:`."""
    jobs, cur, in_jobs = {}, None, False
    for line in _strip(text):
        if re.match(r"^\S", line):
            in_jobs = line.startswith("jobs:")
            cur = None
            continue
        if not in_jobs:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            cur = m.group(1)
            jobs[cur] = []
        elif cur is not None:
            jobs[cur].append(line)
    return jobs


def top_level(text):
    """Lines outside `jobs:` (triggers, permissions, concurrency)."""
    out, in_jobs = [], False
    for line in _strip(text):
        if re.match(r"^\S", line):
            in_jobs = line.startswith("jobs:")
        if not in_jobs:
            out.append(line)
    return out


def check_workflow(text):
    v = []
    text = "\n".join(_strip(text))  # comments never count
    jobs = parse_jobs(text)
    top = "\n".join(top_level(text))
    # (1) self-hosted only in deploy
    for name, lines in jobs.items():
        body = "\n".join(lines)
        if re.search(r"runs-on:.*self-hosted", body) and name != "deploy":
            v.append("self-hosted outside deploy: " + name)
    if "self-hosted" in top:
        v.append("self-hosted at top level")
    dep = "\n".join(jobs.get("deploy", []))
    tst = "\n".join(jobs.get("test", []))
    if not re.search(r"^    runs-on:\s*self-hosted\s*$", dep, re.M):
        v.append("deploy is not self-hosted")
    # (2) deploy gating
    m = re.search(r"^    if:\s*(.+)$", dep, re.M)
    if not m or "github.event_name == 'push'" not in m.group(1) or "refs/heads/main" not in m.group(1):
        v.append("deploy if: missing push/main gate")
    nm = re.search(r"^    needs:\s*(.+)$", dep, re.M)
    if not nm or not re.search(r"\btest\b", nm.group(1)):
        v.append("deploy does not need test")
    # (3) test on GitHub-hosted
    rm = re.search(r"^    runs-on:\s*(\S+)", tst, re.M)
    if not rm or not rm.group(1).startswith("ubuntu-"):
        v.append("test job not on ubuntu-")
    # (4) no pull_request_target
    if "pull_request_target" in text:
        v.append("pull_request_target present")
    # (5) permissions
    pm = re.search(r"^permissions:\s*\n((?:  .+\n?)+)", text + "\n", re.M)
    if not pm or [l.strip() for l in pm.group(1).splitlines()] != ["contents: read"]:
        v.append("top-level permissions is not exactly contents: read")
    if re.search(r"^\s*[A-Za-z-]+:\s*write\s*$", text, re.M):
        v.append("a write permission is requested")
    # (6) deploy job content
    if not re.search(r"tts-bot-deploy\s*$", dep, re.M):
        v.append("deploy does not end tts-bot-deploy with no args")
    for bad in ("scripts/deploy.sh", "npm", "pip"):
        if bad in dep:
            v.append("deploy job contains " + bad)
    # (7) persist-credentials on every checkout
    for name, lines in jobs.items():
        for i, l in enumerate(lines):
            if "uses: actions/checkout@" in l:
                window = "\n".join(lines[i + 1:i + 4])
                if not re.search(r"persist-credentials:\s*false", window):
                    v.append("checkout without persist-credentials: false in " + name)
    return v


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()

    def test_real_workflow_is_clean(self):
        self.assertEqual(check_workflow(self.text), [])

    def test_real_workflow_shape(self):
        jobs = parse_jobs(self.text)
        self.assertEqual(set(jobs), {"test", "deploy"})

    def _mutate(self, old, new, expect):
        self.assertIn(old, self.text)
        got = check_workflow(self.text.replace(old, new, 1))
        self.assertTrue(any(expect in g for g in got), (expect, got))

    def test_negative_cases(self):
        T = self.text
        cases = [
            ("runs-on: ubuntu-latest", "runs-on: self-hosted", "self-hosted outside deploy"),
            ("runs-on: ubuntu-latest", "runs-on: self-hosted", "test job not on ubuntu-"),
            ("runs-on: self-hosted", "runs-on: ubuntu-latest", "deploy is not self-hosted"),
            ("    if: github.event_name == 'push' && github.ref == 'refs/heads/main'\n    runs-on: self-hosted",
             "    runs-on: self-hosted", "deploy if: missing"),
            ("github.event_name == 'push' && github.ref == 'refs/heads/main'\n    runs-on: self-hosted",
             "github.ref == 'refs/heads/main'\n    runs-on: self-hosted", "deploy if: missing"),
            ("github.event_name == 'push' && github.ref == 'refs/heads/main'\n    runs-on: self-hosted",
             "github.event_name == 'push'\n    runs-on: self-hosted", "deploy if: missing"),
            ("    needs: test\n", "", "deploy does not need test"),
            ("on:\n  push:", "on:\n  pull_request_target:\n    branches: [main]\n  push:", "pull_request_target"),
            ("permissions:\n  contents: read", "permissions:\n  contents: write", "permissions is not exactly"),
            ("permissions:\n  contents: read", "permissions:\n  contents: read\n  packages: read", "permissions is not exactly"),
            ("    timeout-minutes: 15\n", "    timeout-minutes: 15\n    permissions:\n      contents: write\n", "write permission"),
            ("run: sudo -n /usr/local/sbin/tts-bot-deploy", "run: sudo -n /usr/local/sbin/tts-bot-deploy --src x", "no args"),
            ("run: sudo -n /usr/local/sbin/tts-bot-deploy", "run: sudo /opt/workspace/boxchan/scripts/deploy.sh", "scripts/deploy.sh"),
            ("      - name: Link Models Directory", "      - name: x\n        run: npm ci\n      - name: Link Models Directory", "contains npm"),
            ("      - name: Link Models Directory", "      - name: x\n        run: pip install foo\n      - name: Link Models Directory", "contains pip"),
        ]
        for old, new, expect in cases:
            with self.subTest(expect=expect, old=old[:40]):
                self._mutate(old, new, expect)

    def test_persist_credentials_each_checkout(self):
        lines = self.text.split("\n")
        idx = [i for i, l in enumerate(lines) if "persist-credentials: false" in l]
        self.assertEqual(len(idx), 2)
        for i in idx:
            mutated = "\n".join(lines[:i] + lines[i + 1:])
            got = check_workflow(mutated)
            self.assertTrue(any("persist-credentials" in g for g in got), got)

    def test_comments_do_not_hide_violations(self):
        # a commented-out self-hosted must not count, a real one in test must
        got = check_workflow(self.text.replace("runs-on: ubuntu-latest", "runs-on: ubuntu-latest\n    # runs-on: self-hosted"))
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
