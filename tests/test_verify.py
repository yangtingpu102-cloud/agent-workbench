"""Tests for the verify skill and the evidence records written by cross-review."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "plugins" / "workbench-core" / "skills"
VERIFY = SKILLS / "verify" / "scripts" / "verify.py"
sys.path.insert(0, str(SKILLS / "verify" / "scripts"))
sys.path.insert(0, str(SKILLS / "cross-review" / "scripts"))
import ask_peer  # noqa: E402
import verify  # noqa: E402

PASS = "python -c \"print('ok')\""
FAIL = "python -c \"import sys; print('boom'); sys.exit(3)\""


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.write("app.py", "x = 1\n")
        self.config([PASS])
        self.commit("init")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def config(self, runs: list[str], require_review: bool = False) -> None:
        checks = [{"name": f"check {i}", "run": run} for i, run in enumerate(runs)]
        self.write(".ai/verify.json", json.dumps({"checks": checks, "require_review": require_review}))

    def verify(self, *args: str) -> tuple[int, str]:
        r = subprocess.run([sys.executable, str(VERIFY), *args], cwd=self.repo, capture_output=True, text=True,
                           encoding="utf-8")
        return r.returncode, r.stdout + r.stderr

    def cid(self) -> str:
        return self.verify("content")[1].strip()

    def add_review(self, verdict: str, peer: str = "codex", author: str = "claude", at: str = "2026-01-01T00:00:00Z"):
        record = {"at": at, "peer": peer, "author": author, "content": self.cid(), "verdict": verdict,
                  "confidence": "high", "findings": 0}
        self.write(f".ai/reviews/{at.replace(':', '')}-{peer}.json", json.dumps(record))

    def test_content_id_is_stable_across_commit_and_ignores_ai_dir(self):
        self.write("app.py", "x = 2\n")
        before = self.cid()
        self.write(".ai/board.md", "board changes do not count\n")
        self.assertEqual(self.cid(), before)
        self.commit("change")
        self.assertEqual(self.verify("content", "--rev", "HEAD")[1].strip(), before)
        self.write("app.py", "x = 3\n")
        self.assertNotEqual(self.cid(), before)

    def test_cross_review_and_verify_compute_the_same_content_id(self):
        self.write("new.py", "y = 1\n")
        old = os.getcwd()
        os.chdir(self.repo)
        try:
            self.assertEqual(ask_peer.content_id(self.repo), verify.content_id(self.repo))
        finally:
            os.chdir(old)

    def test_run_records_passing_checks(self):
        code, out = self.verify("run", "--host", "claude")
        self.assertEqual(code, 0, out)
        evidence = json.loads((self.repo / ".ai" / "verifications" / f"{self.cid()}.json").read_text(encoding="utf-8"))
        check = evidence["runs"][0]["checks"][0]
        self.assertEqual(check["exit"], 0)
        self.assertEqual(len(check["output_sha256"]), 64)
        self.assertEqual(evidence["runs"][0]["host"], "claude")

    def test_run_records_a_failing_check(self):
        self.config([PASS, FAIL])
        code, out = self.verify("run", "--host", "claude")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL  check 1", out)
        evidence = json.loads((self.repo / ".ai" / "verifications" / f"{self.cid()}.json").read_text(encoding="utf-8"))
        self.assertFalse(evidence["runs"][0]["passed"])
        self.assertIn("boom", evidence["runs"][0]["checks"][1]["tail"])

    def test_run_refuses_checks_that_change_the_content(self):
        self.config(["python -c \"open('app.py', 'w').write('changed')\""])
        code, out = self.verify("run", "--host", "claude")
        self.assertEqual(code, 2)
        self.assertIn("content changed while the checks ran", out)

    def test_gate_blocks_without_evidence(self):
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("no evidence", out)

    def test_gate_passes_with_a_run_and_a_ship_review_by_the_other_agent(self):
        self.config([PASS], require_review=True)
        self.add_review("ship")
        self.assertEqual(self.verify("run", "--host", "claude")[0], 0)
        self.commit("change with evidence")
        code, out = self.verify("gate")
        self.assertEqual(code, 0, out)
        self.assertIn("review: ship by codex (author claude)", out)

    def test_gate_rejects_self_review_and_non_ship_verdicts(self):
        self.config([PASS], require_review=True)
        self.add_review("ship", peer="claude", author="claude", at="2026-01-01T00:00:01Z")
        self.add_review("fix-first", at="2026-01-01T00:00:02Z")
        self.verify("run", "--host", "claude")
        self.commit("evidence")
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("found: ship by claude, fix-first by codex", out)

    def test_code_change_after_evidence_blocks_the_gate(self):
        self.verify("run", "--host", "claude")
        self.commit("evidence")
        self.assertEqual(self.verify("gate")[0], 0)
        self.write("app.py", "x = 99\n")
        self.commit("late change")
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("no evidence", out)

    def test_collect_attaches_a_later_review_without_rerunning(self):
        self.config([PASS], require_review=True)
        self.verify("run", "--host", "claude")
        self.add_review("ship")
        code, out = self.verify("collect")
        self.assertEqual(code, 0, out)
        self.assertIn("added 1 review", out)
        self.commit("evidence")
        self.assertEqual(self.verify("gate")[0], 0)


class ParseVerdictTest(unittest.TestCase):
    def test_parse_verdict(self):
        reply = "FINDINGS:\n- [high] a.py:1 — x\n- [low] b.py:2 — y\nVERDICT: fix-first\nCONFIDENCE: med\n"
        self.assertEqual(ask_peer.parse_verdict(reply), ("fix-first", "med", 2))
        self.assertEqual(ask_peer.parse_verdict("no verdict here"), (None, None, 0))


if __name__ == "__main__":
    unittest.main()
