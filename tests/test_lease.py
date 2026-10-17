"""Tests for the workbench-lease script. Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "plugins" / "workbench-lease" / "skills" / "lease" / "scripts"
LEASE = SCRIPTS / "lease.py"
sys.path.insert(0, str(SCRIPTS))
import lease  # noqa: E402

PATCH = "*** Begin Patch\n*** Update File: src/app.py\n@@\n-x\n+y\n*** End Patch\n"


class RepoCase(unittest.TestCase):
    """A repo on branch main plus a worktree on branch codex/T1."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(self.tmp.name)
        self.main, self.other = base / "main", base / "other"
        self.main.mkdir()
        self.git(self.main, "init", "-q", "-b", "main")
        (self.main / "src").mkdir()
        (self.main / "src" / "app.py").write_text("x\n", encoding="utf-8")
        (self.main / "README.md").write_text("readme\n", encoding="utf-8")
        self.git(self.main, "add", "-A")
        self.git(self.main, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        self.git(self.main, "worktree", "add", "-q", "-b", "codex/T1", str(self.other))

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

    def run_lease(self, cwd: Path, *args: str, stdin: str = "") -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AGENT_HOST")}
        r = subprocess.run([sys.executable, str(LEASE), *args], cwd=cwd, input=stdin, capture_output=True,
                           text=True, encoding="utf-8", env=env)
        return r.returncode, r.stdout + r.stderr

    def hook(self, cwd: Path, payload) -> tuple[bool, str]:
        """Returns (denied, output). The hook must always exit 0 and deny through JSON on stdout."""
        text = payload if isinstance(payload, str) else json.dumps(payload)
        code, out = self.run_lease(cwd, "hook", stdin=text)
        self.assertEqual(code, 0, out)
        return '"permissionDecision": "deny"' in out, out

    def claim_src_in_main(self, *extra: str) -> None:
        code, out = self.run_lease(self.main, "claim", "T1", "--scope", "src/", "--host", "claude", *extra)
        self.assertEqual(code, 0, out)


class LeaseTest(RepoCase):
    def test_lease_blocks_other_worktree_but_not_its_owner(self):
        self.claim_src_in_main()
        self.assertEqual(self.run_lease(self.main, "check", "src/app.py")[0], 0)
        code, out = self.run_lease(self.other, "check", "src/app.py")
        self.assertEqual(code, 1)
        self.assertIn("T1 by claude@main", out)

    def test_lease_dir_is_shared_through_the_git_common_dir(self):
        code, out = self.run_lease(self.other, "claim", "T9", "--scope", "docs/", "--host", "codex")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.main / ".git" / "agent-leases" / "T9.json").is_file())

    def test_hook_blocks_a_claude_edit(self):
        self.claim_src_in_main()
        payload = {"tool_name": "Edit", "cwd": str(self.other),
                   "tool_input": {"file_path": str(self.other / "src" / "app.py"), "old_string": "x", "new_string": "y"}}
        denied, out = self.hook(self.other, payload)
        self.assertTrue(denied)
        self.assertIn("Blocked by an agent lease: src/app.py is leased to claude@main", out)

    def test_hook_blocks_a_codex_patch_in_dict_or_raw_form(self):
        self.claim_src_in_main()
        for tool_name, tool_input in (("apply_patch", {"command": PATCH}), ("apply_patch", PATCH),
                                      ("Bash", {"command": "apply_patch <<'EOF'\n" + PATCH + "EOF"})):
            denied, out = self.hook(self.other, {"tool_name": tool_name, "cwd": str(self.other),
                                                 "tool_input": tool_input})
            self.assertTrue(denied, out)

    def test_hook_blocks_a_new_file_in_a_new_directory(self):
        self.claim_src_in_main()
        target = self.other / "src" / "new" / "deep.py"
        denied, _ = self.hook(self.other, {"tool_name": "Write", "cwd": str(self.other),
                                           "tool_input": {"file_path": str(target), "content": "z"}})
        self.assertTrue(denied)

    def test_hook_allows_reads_unleased_files_bad_input_and_non_repos(self):
        self.claim_src_in_main()
        leased = str(self.other / "src" / "app.py")
        self.assertFalse(self.hook(self.other, {"tool_name": "Read", "tool_input": {"file_path": leased}})[0])
        self.assertFalse(self.hook(self.other, {"tool_name": "Edit", "cwd": str(self.other),
                                                "tool_input": {"file_path": "README.md"}})[0])
        self.assertFalse(self.hook(self.other, {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}})[0])
        denied, out = self.hook(self.other, "not json")
        self.assertFalse(denied)
        self.assertIn("lease hook skipped", out)
        outside = Path(self.tmp.name) / "loose.txt"
        self.assertFalse(self.hook(self.other, {"tool_name": "Write", "tool_input": {"file_path": str(outside)}})[0])

    def test_same_branch_relay_keeps_the_lease(self):
        self.claim_src_in_main()
        payload = {"tool_name": "apply_patch", "cwd": str(self.main), "tool_input": {"command": PATCH}}
        self.assertFalse(self.hook(self.main, payload)[0])

    def test_overlapping_claim_from_another_branch_is_refused(self):
        self.claim_src_in_main()
        code, out = self.run_lease(self.other, "claim", "T2", "--scope", "src/app.py", "--host", "codex")
        self.assertEqual(code, 1)
        self.assertIn("conflict", out)
        self.assertEqual(self.run_lease(self.other, "claim", "T3", "--scope", "docs/", "--host", "codex")[0], 0)

    def test_expired_lease_stops_blocking_and_can_be_taken_over(self):
        self.claim_src_in_main()
        path = self.main / ".git" / "agent-leases" / "T1.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["until"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.run_lease(self.other, "check", "src/app.py")[0], 0)
        code, out = self.run_lease(self.other, "claim", "T1", "--scope", "src/", "--host", "codex")
        self.assertEqual(code, 0, out)
        self.assertIn("took over", out)

    def test_release_rules(self):
        self.claim_src_in_main()
        self.assertEqual(self.run_lease(self.other, "release", "T1")[0], 1)
        self.assertEqual(self.run_lease(self.main, "release", "T1")[0], 0)
        self.assertIn("no leases", self.run_lease(self.main, "list")[1])

    def test_write_content_mentioning_a_patch_is_not_misread(self):
        self.claim_src_in_main()
        payload = {"tool_name": "Write", "cwd": str(self.other),
                   "tool_input": {"file_path": str(self.other / "README.md"), "content": PATCH}}
        self.assertFalse(self.hook(self.other, payload)[0])

    def test_shell_writes_are_not_checked(self):
        """Documented boundary: only edit tools and patches are guarded, not shell redirection."""
        self.claim_src_in_main()
        payload = {"tool_name": "Bash", "cwd": str(self.other), "tool_input": {"command": "echo y > src/app.py"}}
        self.assertFalse(self.hook(self.other, payload)[0])

    def test_scope_rejects_parent_dirs_and_paths_outside_the_repo(self):
        self.assertEqual(self.run_lease(self.main, "claim", "T5", "--scope", "../elsewhere/")[0], 1)
        outside = str(Path(self.tmp.name) / "loose")
        self.assertEqual(self.run_lease(self.main, "claim", "T6", "--scope", outside)[0], 1)
        code, out = self.run_lease(self.main, "claim", "T7", "--scope", str(self.main / "src") + "/")
        self.assertEqual(code, 0, out)
        self.assertIn("[src/]", out)

    def test_concurrent_overlapping_claims_only_one_wins(self):
        trees = [self.main, self.other]
        for i in range(2):
            tree = Path(self.tmp.name) / f"extra{i}"
            self.git(self.main, "worktree", "add", "-q", "-b", f"extra/{i}", str(tree))
            trees.append(tree)
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AGENT_HOST")}
        procs = [subprocess.Popen([sys.executable, str(LEASE), "claim", f"C{i}", "--scope", "src/"], cwd=tree,
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                 for i, tree in enumerate(trees)]
        codes = [p.wait(timeout=60) for p in procs]
        self.assertEqual(codes.count(0), 1, codes)

    def test_renew_only_by_owner(self):
        self.claim_src_in_main("--ttl", "5m")
        self.assertEqual(self.run_lease(self.other, "renew", "T1")[0], 1)
        code, out = self.run_lease(self.main, "renew", "T1", "--ttl", "3h")
        self.assertEqual(code, 0, out)


class HookConfigTest(RepoCase):
    """Run the exact commands from the plugin's hook files the way each host runs them."""

    PLUGIN = ROOT / "plugins" / "workbench-lease"

    def handler(self, name: str) -> dict:
        config = json.loads((self.PLUGIN / "hooks" / name).read_text(encoding="utf-8"))
        return config["hooks"]["PreToolUse"][0]["hooks"][0]

    def run_hook_command(self, cmd: list[str], env_extra: dict, payload: dict) -> str:
        env = {**os.environ, **env_extra}
        r = subprocess.run(cmd, input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
                           env=env, cwd=self.other)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_codex_command_denies_through_the_platform_shell(self):
        self.claim_src_in_main()
        h = self.handler("codex.hooks.json")
        if os.name == "nt":  # Codex runs Windows hooks with PowerShell -NoProfile -Command
            cmd = ["powershell.exe", "-NoProfile", "-Command", h["commandWindows"]]
        else:
            cmd = [os.environ.get("SHELL") or "/bin/sh", "-c", h["command"]]
        payload = {"tool_name": "apply_patch", "cwd": str(self.other), "tool_input": {"command": PATCH}}
        out = self.run_hook_command(cmd, {"PLUGIN_ROOT": str(self.PLUGIN)}, payload)
        self.assertIn('"permissionDecision": "deny"', out)

    def test_claude_exec_form_denies(self):
        self.claim_src_in_main()
        h = self.handler("hooks.json")
        cmd = [h["command"]] + [a.replace("${CLAUDE_PLUGIN_ROOT}", str(self.PLUGIN)) for a in h["args"]]
        payload = {"tool_name": "Edit", "cwd": str(self.other),
                   "tool_input": {"file_path": str(self.other / "src" / "app.py")}}
        out = self.run_hook_command(cmd, {}, payload)
        self.assertIn('"permissionDecision": "deny"', out)


class MatchingTest(unittest.TestCase):
    def test_covers(self):
        self.assertTrue(lease.covers("src/", "src/a/b.py"))
        self.assertTrue(lease.covers("src", "src/a.py"))
        self.assertTrue(lease.covers("*.md", "README.md"))
        self.assertFalse(lease.covers("src/", "srcx/a.py"))
        self.assertFalse(lease.covers("src/a.py", "src/a.pyc"))

    def test_overlaps(self):
        self.assertTrue(lease.overlaps("src/", "src/app.py"))
        self.assertTrue(lease.overlaps("docs/*.md", "docs/a.md"))
        self.assertTrue(lease.overlaps("src/*.py", "src/a*"))
        self.assertTrue(lease.overlaps("*.md", "src/"))
        self.assertFalse(lease.overlaps("src/", "docs/"))
        self.assertFalse(lease.overlaps("src/*.py", "docs/*.md"))

    @unittest.skipUnless(os.name == "nt", "Windows paths are case-insensitive")
    def test_case_insensitive_on_windows(self):
        self.assertTrue(lease.covers("SRC/", "src/a.py"))

    def test_scope_and_ttl_parsing(self):
        self.assertEqual(lease.norm_scope(r" ./src\api/, docs/a.md ,"), ["src/api/", "docs/a.md"])
        self.assertEqual(lease.parse_ttl("90m").total_seconds(), 5400)
        with self.assertRaises(lease.LeaseError):
            lease.parse_ttl("soon")


if __name__ == "__main__":
    unittest.main()
