import base64
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("monitor", ROOT / "scripts/rollout_monitor.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
BRANCH, FILE, HEAD = "feat/test", ".github/workflows/test.yml", "a" * 40


def item(n):
    return {"repository_url": "https://api.github.com/repos/acme/widget", "number": n, "pull_request": {}}


def page(items=(), total=None, incomplete=False):
    return {"items": list(items), "total_count": len(items) if total is None else total,
            "incomplete_results": incomplete}


class Tests(unittest.TestCase):
    def scan(self, content=b"canonical\n", move=False, fuzzy=False, fail=False):
        calls = []
        def read(endpoint, paginated=False):
            calls.append(endpoint)
            if fail:
                raise m.ScanError("test failure")
            if endpoint.startswith("search/"):
                return [page([item(7)])]
            if "/contents/" in endpoint:
                self.assertTrue(endpoint.endswith(f"?ref={HEAD}"))
                return {"encoding": "base64", "content": base64.b64encode(content).decode()}
            count = sum("/pulls/" in call for call in calls)
            return {"state": "open", "head": {"ref": "other" if fuzzy else BRANCH,
                    "sha": "b" * 40 if move and count > 1 else HEAD}, "draft": True}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / FILE).parent.mkdir(parents=True)
            (root / FILE).write_bytes(b"canonical\n")
            rows, errors = m.scan("acme", root=root, read=read, patterns={BRANCH: FILE})
        return rows, errors, calls

    def test_complete_and_draft_hold(self):
        rows, errors, _ = self.scan()
        self.assertEqual(errors, [])
        self.assertFalse(rows[0]["drift"])
        self.assertIn("draft; preserve hold", m.report(rows, errors))

    def test_exact_byte_drift(self):
        rows, errors, _ = self.scan(content=b"canonical")
        self.assertEqual(errors, [])
        self.assertTrue(rows[0]["drift"])

    def test_failure_never_reports_complete(self):
        rows, errors, _ = self.scan(fail=True)
        self.assertEqual(rows, [])
        self.assertIn("INCOMPLETE", m.report(rows, errors))
        self.assertNotIn("Pattern scan complete", m.report(rows, errors))

    def test_fuzzy_branch_is_not_actionable(self):
        rows, errors, calls = self.scan(fuzzy=True)
        self.assertEqual((rows, errors), ([], []))
        self.assertFalse(any("/contents/" in call for call in calls))

    def test_moved_head_is_incomplete(self):
        rows, errors, _ = self.scan(move=True)
        self.assertEqual(rows, [])
        self.assertTrue(errors)

    def test_pagination(self):
        read = lambda *args, **kwargs: [page([item(1)], 2), page([item(2)], 2)]
        self.assertEqual(len(m.discover("acme", BRANCH, read)), 2)

    def test_bad_search(self):
        for pages in [[], [page([], 1)], [page([], 1001)], [page(incomplete=True)],
                      [page([item(1), item(1)])], [page(), page([], 1)], [{}]]:
            with self.subTest(pages=pages), self.assertRaises(m.ScanError):
                m.discover("acme", BRANCH, lambda *args, **kwargs: pages)

    def test_api_errors_are_sanitized_and_get_only(self):
        for result in [subprocess.CompletedProcess([], 0, "bad json", ""),
                       subprocess.CompletedProcess([], 1, "secret", "secret")]:
            with patch.object(m.subprocess, "run", return_value=result) as run:
                with self.assertRaises(m.ScanError) as error:
                    m.api("search/issues", True)
                self.assertNotIn("secret", str(error.exception))
                self.assertEqual(run.call_args.args[0][:4], ["gh", "api", "--method", "GET"])

    def test_failure_exit(self):
        with patch.object(m, "scan", return_value=([], ["forced failure"])), patch("builtins.print"):
            self.assertEqual(m.main(), 1)

    def test_security_shape(self):
        workflow = (ROOT / ".github/workflows/rollout-monitor.yml").read_text()
        source = (ROOT / "scripts/rollout_monitor.py").read_text()
        for forbidden in ["--admin", "--auto", '"PUT"', '"PATCH"', '"POST"', '"DELETE"']:
            self.assertNotIn(forbidden, source + workflow)
        self.assertNotIn("write", workflow.split("permissions:", 1)[1].split("concurrency:", 1)[0])
        self.assertIn("persist-credentials: false", workflow)


if __name__ == "__main__":
    unittest.main()
