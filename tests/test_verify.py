"""Tests for the verify skill and the evidence records written by cross-review."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "plugins" / "workbench-core" / "skills"
VERIFY = SKILLS / "verify" / "scripts" / "verify.py"
sys.path.insert(0, str(SKILLS / "verify" / "scripts"))
sys.path.insert(0, str(SKILLS / "cross-review" / "scripts"))
import ask_peer  # noqa: E402
import verify  # noqa: E402

PASS = "python -c \"print('ok')\""
PASS_B = "python -c \"print('b')\""
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

    def test_policy_comes_from_the_current_base_not_the_merge_base(self):
        self.git("switch", "-q", "-c", "feature")
        self.write("app.py", "x = 2\n")
        self.commit("feature work")
        self.git("switch", "-q", "main")
        self.config([PASS, PASS_B], commit=True)  # main gains a check after the branch was cut
        self.git("switch", "-q", "feature")
        self.verify("run", "--host", "claude")
        self.commit("evidence without the new check")
        code, out = self.verify("gate", "--base", "main")
        self.assertEqual(code, 1)
        self.assertIn("check 1", out)
        code, out = self.verify("run", "--host", "claude", "--base", "main")
        self.assertEqual(code, 0, out)
        self.assertIn("also running 1 check(s) required by main", out)
        self.commit("evidence with base checks")
        self.assertEqual(self.verify("gate", "--base", "main")[0], 0)

    def test_changing_a_check_command_merges_when_old_and_new_both_pass(self):
        self.git("switch", "-q", "-c", "retool")
        self.config([PASS_B], commit=True)  # "check 0" gets a new command
        code, out = self.verify("run", "--host", "claude", "--base", "main")
        self.assertEqual(code, 0, out)
        self.commit("evidence")
        code, out = self.verify("gate", "--base", "main")
        self.assertEqual(code, 0, out)

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


    # ---- Codex's review on GitHub as evidence ----

    def codex_comment(self, sha: str, status: str = "completed", unresolved: bool | None = False,
                      any_findings: bool | None = None, login: str = "chatgpt-codex-connector[bot]",
                      kind: str = "Bot", comment_id: int = 1, raw_state: str | None = None) -> dict:
        state = raw_state or json.dumps({"code_review": {
            "commit_sha": sha, "status": status, "has_unresolved_findings": unresolved,
            "has_any_findings": unresolved if any_findings is None else any_findings},
            "security_review": {"commit_sha": None, "status": None}})
        body = ("<!-- codex-pull-request-review-summary -->\n"
                f"<!-- codex-review-state:v1 {state} -->\n\nCodex Review Summary")
        return {"id": comment_id, "login": login, "type": kind, "body": body,
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:01Z", "html_url": "u"}

    def feature_with_evidence(self, trailer: str = "") -> str:
        """A feature branch off main with passing evidence committed; returns the commit that holds the work."""
        self.config([PASS], require_review=True, commit=True)
        self.git("switch", "-q", "-c", "feature")
        self.write("app.py", "x = 2\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "feature work" + trailer)
        work = self.git("rev-parse", "HEAD")
        code, out = self.verify("run", "--host", "claude", "--base", "main")
        self.assertEqual(code, 0, out)
        self.commit("evidence")
        return work

    def gate_github(self, comments: list[dict], *extra: str, base_changes: list[str] | None = None,
                    base_args: tuple[str, ...] = ("--base", "main")) -> tuple[int, str]:
        old = os.getcwd()
        os.chdir(self.repo)
        buf = io.StringIO()
        try:
            with mock.patch.object(verify, "fetch_pr_comments", lambda repo, pr: comments), \
                    mock.patch.object(verify, "fetch_pr_base_changes", lambda repo, pr: base_changes or []), \
                    contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = verify.main(["gate", *base_args, "--github-pr", "4", "--github-repo", "o/r", *extra])
        finally:
            os.chdir(old)
        return code, buf.getvalue()

    def test_a_github_review_needs_a_base(self):
        """Codex review of PR #5 (P2): without --base the author was always 'unknown', so Codex could review
        work credited to itself. A GitHub review is now judged only against an explicit base."""
        work = self.feature_with_evidence("\n\nCo-authored-by: Codex <codex@example.com>")
        code, out = self.gate_github([self.codex_comment(work)], base_args=())
        self.assertEqual(code, 2)
        self.assertIn("--github-pr needs --base", out)

    def test_a_review_started_before_the_base_was_changed_is_stale(self):
        """Codex review of PR #5 (P2): retargeting the PR to an earlier base after the review adds commits Codex
        never saw, so the review must not be credited with the new scope."""
        work = self.feature_with_evidence()
        comment = self.codex_comment(work)  # state is completed; the summary says the review began at 07:16
        comment["body"] += '\nRunning since <relative-time datetime="2026-01-01T07:16:07.585424Z">x</relative-time>'
        code, out = self.gate_github([comment], base_changes=["2026-01-01T08:00:00Z"])
        self.assertEqual(code, 1)
        self.assertIn("is stale", out)
        self.assertIn("@codex review", out)

    def test_a_base_change_before_the_review_started_does_not_count_against_it(self):
        work = self.feature_with_evidence()
        comment = self.codex_comment(work)
        comment["body"] += '\nRunning since <relative-time datetime="2026-01-01T07:16:07.585424Z">x</relative-time>'
        code, out = self.gate_github([comment], base_changes=["2026-01-01T06:00:00Z"])
        self.assertEqual(code, 0, out)

    def test_without_stated_times_a_later_base_change_still_counts(self):
        work = self.feature_with_evidence()
        comment = self.codex_comment(work)  # updated_at is 2026-01-01T00:00:01Z
        code, out = self.gate_github([comment], base_changes=["2026-01-02T00:00:00Z"])
        self.assertEqual(code, 1)
        self.assertIn("is stale", out)
        code, out = self.gate_github([comment], base_changes=["not a time"])  # unreadable times are not trusted
        self.assertEqual(code, 1)

    def test_a_finished_codex_review_without_findings_satisfies_the_gate(self):
        work = self.feature_with_evidence()
        code, out = self.gate_github([self.codex_comment(work)])
        self.assertEqual(code, 0, out)
        self.assertIn("ship by codex", out)
        self.assertNotEqual(self.verify("gate", "--base", "main")[0], 0)  # without --github-pr there is no review

    def test_unresolved_codex_findings_block_the_gate(self):
        work = self.feature_with_evidence()
        code, out = self.gate_github([self.codex_comment(work, unresolved=True)])
        self.assertEqual(code, 1)
        self.assertIn("says fix-first", out)
        self.assertIn("@codex review", out)

    def test_a_codex_review_that_is_still_running_blocks_until_it_finishes(self):
        work = self.feature_with_evidence()
        code, out = self.gate_github([self.codex_comment(work, status="running", unresolved=None)])
        self.assertEqual(code, 1)
        self.assertIn("is running", out)

    def test_a_codex_review_of_older_content_does_not_count(self):
        work = self.feature_with_evidence()
        self.write("app.py", "x = 9\n")
        self.commit("later change")
        self.assertEqual(self.verify("run", "--host", "claude", "--base", "main")[0], 0)
        self.commit("evidence again")
        code, out = self.gate_github([self.codex_comment(work)])
        self.assertEqual(code, 1)
        self.assertIn("no review of this content", out)

    def test_only_the_codex_integration_account_can_speak_for_codex(self):
        work = self.feature_with_evidence()
        for forged in (self.codex_comment(work, login="mallory", kind="User"),
                       self.codex_comment(work, kind="User"),  # right name, but not a bot account
                       self.codex_comment(work, login="mallory", kind="Bot")):
            code, out = self.gate_github([forged])
            self.assertEqual(code, 1, forged["login"])
        code, out = self.gate_github([self.codex_comment(work, login="my-codex[bot]")], "--codex-bot", "my-codex[bot]")
        self.assertEqual(code, 0, out)

    def test_codex_cannot_be_the_independent_reviewer_of_its_own_change(self):
        work = self.feature_with_evidence("\n\nCo-authored-by: Codex <codex@example.com>")
        code, out = self.gate_github([self.codex_comment(work)])
        self.assertEqual(code, 1)
        self.assertIn("other than its author", out)

    def test_github_review_needs_a_repository(self):
        self.feature_with_evidence()
        old = os.getcwd()
        os.chdir(self.repo)
        try:
            env = {k: v for k, v in os.environ.items() if k != "GITHUB_REPOSITORY"}
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = verify.main(["gate", "--base", "main", "--github-pr", "4"])
        finally:
            os.chdir(old)
        self.assertEqual(code, 2)

    def test_codex_state_parsing_is_conservative(self):
        sha = self.git("rev-parse", "HEAD")
        bots = {verify.CODEX_BOT}

        def verdicts(comment):
            records, _ = verify.codex_github_reviews(self.repo, [comment], bots, "HEAD", None)
            return [r["verdict"] for r in records]

        self.assertEqual(verdicts(self.codex_comment(sha, unresolved=None, any_findings=False)), ["ship"])
        self.assertEqual(verdicts(self.codex_comment(sha, unresolved=None, any_findings=None)), ["fix-first"])
        self.assertEqual(verdicts(self.codex_comment(sha, unresolved=True, any_findings=True)), ["fix-first"])
        self.assertEqual(verdicts(self.codex_comment(sha, status="failed", unresolved=False)), [None])
        self.assertEqual(verdicts(self.codex_comment(sha, raw_state="{not json")), [])
        records, notes = verify.codex_github_reviews(self.repo, [self.codex_comment("f" * 40)], bots, "HEAD", None)
        self.assertIsNone(records[0]["content"])
        self.assertIn("commit not available here", notes[0])


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

    def test_base_review_diffs_from_the_merge_base(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        run = lambda *a: subprocess.run(git + list(a), check=True, capture_output=True, text=True).stdout.strip()
        run("init", "-q", "-b", "main")
        (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "-m", "A")
        fork = run("rev-parse", "HEAD")
        run("switch", "-q", "-c", "feature")
        (repo / "feature.py").write_text("print('feature change')\n", encoding="utf-8")
        (repo / ".ai" / "verifications").mkdir(parents=True)
        (repo / ".ai" / "verifications" / "abc.json").write_text("{}\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "-m", "B")
        run("switch", "-q", "main")
        (repo / "later.py").write_text("print('main moved on')\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "-m", "C")
        run("switch", "-q", "feature")
        args = type("Args", (), {"question": None, "base": "main", "goal": "g", "max_chars": 60000})()
        old = os.getcwd()
        os.chdir(repo)
        try:
            context, _, complete = ask_peer.build_context(args)
        finally:
            os.chdir(old)
        self.assertIn("feature change", context)
        self.assertNotIn("main moved on", context)
        self.assertNotIn("verifications", context)
        self.assertEqual(args.review_base, fork)
        self.assertTrue(complete)

    def test_a_tracked_binary_change_marks_the_review_incomplete(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
        (repo / "img.bin").write_bytes(b"\x00\x01\x02")
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "i"], check=True)
        (repo / "img.bin").write_bytes(b"\x00\x09\x09")
        args = type("Args", (), {"question": None, "base": None, "goal": "g", "max_chars": 60000})()
        old = os.getcwd()
        os.chdir(repo)
        try:
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
