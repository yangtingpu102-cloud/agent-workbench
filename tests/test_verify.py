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
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def config(self, runs: list[str], require_review: bool = False, commit: bool = False) -> None:
        checks = [{"name": f"check {i}", "run": run} for i, run in enumerate(runs)]
        self.write(".ai/verify.json", json.dumps({"checks": checks, "require_review": require_review}))
        if commit:
            self.commit("config")

    def verify(self, *args: str) -> tuple[int, str]:
        r = subprocess.run([sys.executable, str(VERIFY), *args], cwd=self.repo, capture_output=True, text=True,
                           encoding="utf-8")
        return r.returncode, r.stdout + r.stderr

    def cid(self) -> str:
        return self.verify("content")[1].strip()

    def evidence_path(self) -> Path:
        return self.repo / ".ai" / "verifications" / f"{self.cid()}.json"

    def add_review(self, verdict: str, at: str, peer: str = "codex", author: str = "claude",
                   base_sha: str | None = None, complete: bool = True, rid: str | None = None) -> None:
        record = {"id": rid or f"{at}-{peer}-{verdict}", "at": at, "peer": peer, "author": author,
                  "content": self.cid(), "complete": complete, "verdict": verdict, "confidence": "high",
                  "findings": 0, "base_sha": base_sha}
        self.write(f".ai/reviews/{record['id'].replace(':', '')}.json", json.dumps(record))

    def test_content_id_ignores_coordination_files_but_not_the_check_config(self):
        self.write("app.py", "x = 2\n")
        before = self.cid()
        self.write(".ai/board.md", "board changes do not count\n")
        self.assertEqual(self.cid(), before)
        self.commit("change")
        self.assertEqual(self.verify("content", "--rev", "HEAD")[1].strip(), before)
        self.config([])
        self.assertNotEqual(self.cid(), before)

    def test_cross_review_and_verify_compute_the_same_content_id(self):
        self.write("new.py", "y = 1\n")
        self.write(".ai/board.md", "ignored\n")
        old = os.getcwd()
        os.chdir(self.repo)
        try:
            self.assertEqual(ask_peer.content_id(self.repo), verify.content_id(self.repo))
        finally:
            os.chdir(old)

    def test_run_records_passing_checks(self):
        code, out = self.verify("run", "--host", "claude")
        self.assertEqual(code, 0, out)
        evidence = json.loads(self.evidence_path().read_text(encoding="utf-8"))
        check = evidence["runs"][0]["checks"][0]
        self.assertEqual((check["exit"], len(check["output_sha256"]), evidence["runs"][0]["host"]), (0, 64, "claude"))

    def test_run_records_a_failing_check(self):
        self.config([PASS, FAIL], commit=True)
        code, out = self.verify("run", "--host", "claude")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL  check 1", out)
        evidence = json.loads(self.evidence_path().read_text(encoding="utf-8"))
        self.assertFalse(evidence["runs"][0]["passed"])
        self.assertIn("boom", evidence["runs"][0]["checks"][1]["tail"])

    def test_run_requires_a_clean_tree(self):
        self.write("app.py", "x = 2\n")
        code, out = self.verify("run", "--host", "claude")
        self.assertEqual(code, 2)
        self.assertIn("commit the change first", out)

    def test_run_refuses_checks_that_change_the_content(self):
        self.config(["python -c \"open('app.py', 'w').write('changed')\""], commit=True)
        code, out = self.verify("run", "--host", "claude")
        self.assertEqual(code, 2)
        self.assertIn("content changed while the checks ran", out)

    def test_gate_blocks_without_evidence(self):
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("no evidence", out)

    def test_gate_passes_with_a_run_and_a_ship_review_by_the_other_agent(self):
        self.config([PASS], require_review=True, commit=True)
        self.add_review("ship", "2026-01-01T00:00:01Z")
        self.assertEqual(self.verify("run", "--host", "claude")[0], 0)
        self.commit("evidence")
        code, out = self.verify("gate")
        self.assertEqual(code, 0, out)
        self.assertIn("ship by codex (author claude)", out)

    def test_gate_uses_the_latest_independent_review(self):
        self.config([PASS], require_review=True, commit=True)
        self.add_review("ship", "2026-01-01T00:00:01Z")
        self.add_review("fix-first", "2026-01-01T00:00:02Z")
        self.add_review("ship", "2026-01-01T00:00:03Z", peer="claude", author="claude")  # self-review: ignored
        self.verify("run", "--host", "claude")
        self.commit("evidence")
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("says fix-first", out)

    def test_gate_uses_the_latest_run(self):
        self.verify("run", "--host", "claude")
        path = self.evidence_path()
        evidence = json.loads(path.read_text(encoding="utf-8"))
        failed = dict(evidence["runs"][0], at="2999-01-01T00:00:00Z", platform="linux", passed=False)
        evidence["runs"].append(failed)
        path.write_text(json.dumps(evidence), encoding="utf-8")
        self.commit("evidence")
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("the latest run", out)

    def test_weakening_the_checks_invalidates_old_evidence(self):
        self.config([PASS, FAIL], commit=True)
        self.verify("run", "--host", "claude")
        self.commit("failing evidence")
        self.config([PASS], commit=True)  # dropping the failing check changes the content id
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("no evidence", out)

    def test_evidence_saved_under_another_content_name_is_rejected(self):
        self.verify("run", "--host", "claude")
        old = self.evidence_path()
        self.commit("evidence")
        self.write("app.py", "x = 3\n")
        self.commit("change")
        self.evidence_path().write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
        self.commit("copied evidence")
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("records content", out)

    def test_ship_review_must_cover_the_whole_change_with_base(self):
        self.config([PASS], require_review=True, commit=True)
        main_sha = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "-c", "feature")
        self.write("app.py", "x = 2\n")
        self.commit("part 1")
        part1 = self.git("rev-parse", "HEAD")
        self.write("app.py", "x = 3\n")
        self.commit("part 2")
        self.add_review("ship", "2026-01-01T00:00:01Z", base_sha=part1)  # saw only part 2
        self.verify("run", "--host", "claude")
        self.commit("evidence")
        code, out = self.verify("gate", "--base", "main")
        self.assertEqual(code, 1)
        self.assertIn("does not cover", out)
        self.add_review("ship", "2026-01-01T00:00:02Z", base_sha=main_sha)
        self.verify("collect")
        self.commit("full review")
        code, out = self.verify("gate", "--base", "main")
        self.assertEqual(code, 0, out)

    def test_collect_attaches_a_later_review_without_rerunning(self):
        self.config([PASS], require_review=True, commit=True)
        self.verify("run", "--host", "claude")
        self.add_review("ship", "2026-01-01T00:00:01Z")
        code, out = self.verify("collect")
        self.assertEqual(code, 0, out)
        self.assertIn("added 1 review", out)
        self.commit("evidence")
        self.assertEqual(self.verify("gate")[0], 0)

    def test_code_change_after_evidence_blocks_the_gate(self):
        self.verify("run", "--host", "claude")
        self.commit("evidence")
        self.assertEqual(self.verify("gate")[0], 0)
        self.write("app.py", "x = 99\n")
        self.commit("late change")
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("no evidence", out)


    def test_base_policy_cannot_be_loosened_by_the_pr(self):
        self.config([PASS], require_review=True, commit=True)
        self.git("switch", "-q", "-c", "loosen")
        self.config([], require_review=False, commit=True)  # the PR drops its checks and review rule
        self.verify("run", "--host", "claude")
        self.commit("evidence")
        self.assertEqual(self.verify("gate")[0], 0)  # without --base the PR's own rules apply
        code, out = self.verify("gate", "--base", "main")
        self.assertEqual(code, 1)
        self.assertIn("did not pass: check 0", out)
        self.assertIn("no review of this content", out)

    def test_a_same_named_noop_does_not_satisfy_the_base_check(self):
        self.config([PASS], commit=True)
        self.git("switch", "-q", "-c", "noop")
        self.config(["python -c \"pass\""], commit=True)  # same name "check 0", different command
        self.verify("run", "--host", "claude")
        self.commit("evidence")
        code, out = self.verify("gate", "--base", "main")
        self.assertEqual(code, 1)
        self.assertIn("did not pass: check 0", out)

    def test_an_uncollected_newer_review_still_blocks_locally(self):
        self.config([PASS], require_review=True, commit=True)
        self.add_review("ship", "2026-01-01T00:00:01Z")
        self.verify("run", "--host", "claude")
        self.commit("evidence")
        self.add_review("fix-first", "2026-01-01T00:00:02Z")  # saved by cross-review, never collected
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("says fix-first", out)

    def test_an_incomplete_review_does_not_count(self):
        self.config([PASS], require_review=True, commit=True)
        self.add_review("ship", "2026-01-01T00:00:01Z", complete=False)
        self.verify("run", "--host", "claude")
        self.commit("evidence")
        code, out = self.verify("gate")
        self.assertEqual(code, 1)
        self.assertIn("did not see every change", out)

    def test_records_with_the_same_timestamp_follow_insertion_order(self):
        reviews = [{"id": "a", "at": "2026-01-01T00:00:01Z", "verdict": "ship"},
                   {"id": "b", "at": "2026-01-01T00:00:01Z", "verdict": "fix-first"}]
        self.assertEqual(verify.latest(reviews)["id"], "b")
        mixed = [{"id": "new", "at": "2026-01-01T00:00:01.500000Z"}, {"id": "old", "at": "2026-01-01T00:00:01Z"}]
        self.assertEqual(verify.latest(mixed)["id"], "new")


class AskPeerContextTest(unittest.TestCase):
    def test_new_files_are_shown_and_truncation_marks_the_review_incomplete(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "i"], check=True)
        (repo / "new.py").write_text("print('new file body')\n", encoding="utf-8")
        args = type("Args", (), {"question": None, "base": None, "goal": "g", "max_chars": 60000})()
        old = os.getcwd()
        os.chdir(repo)
        try:
            context, _, complete = ask_peer.build_context(args)
            self.assertIn("new file body", context)
            self.assertTrue(complete)
            (repo / "big.py").write_text("y = 2\n" * 5000, encoding="utf-8")
            args.max_chars = 2000
            _, _, complete = ask_peer.build_context(args)
            self.assertFalse(complete)
        finally:
            os.chdir(old)


class EvidenceCompleteTest(unittest.TestCase):
    def test_review_counts_only_when_whole_and_unchanged(self):
        self.assertTrue(ask_peer.evidence_complete(True, "x" * 10, 100, "abc", "abc"))
        self.assertFalse(ask_peer.evidence_complete(True, "x" * 200, 100, "abc", "abc"))  # cut by build_prompt
        self.assertFalse(ask_peer.evidence_complete(True, "x", 100, "abc", "def"))  # edited during review
        self.assertFalse(ask_peer.evidence_complete(False, "x", 100, "abc", "abc"))
        self.assertIsNone(ask_peer.evidence_complete(None, "x", 100, "abc", "abc"))  # questions are not reviews


class ParseVerdictTest(unittest.TestCase):
    def test_parse_verdict(self):
        reply = "FINDINGS:\n- [high] a.py:1 — x\n- [low] b.py:2 — y\nVERDICT: fix-first\nCONFIDENCE: med\n"
        self.assertEqual(ask_peer.parse_verdict(reply), ("fix-first", "med", 2))
        self.assertEqual(ask_peer.parse_verdict("no verdict here"), (None, None, 0))


if __name__ == "__main__":
    unittest.main()
