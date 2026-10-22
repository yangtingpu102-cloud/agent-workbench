"""Tests for the ab-bench script. No real agent is called: `bench.execute` is replaced by a fake."""
from __future__ import annotations

import contextlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "plugins" / "workbench-bench" / "skills" / "ab-bench"
EXPERIMENTS = SKILL / "examples" / "experiments"
sys.path.insert(0, str(SKILL / "scripts"))
import bench  # noqa: E402

# Shapes captured from real runs (Claude Code -p --output-format json; codex exec --json, 0.162).
CLAUDE_OUTPUT = json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "total_cost_usd": 0.0248906,
    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 6047, "cache_read_input_tokens": 3293,
              "output_tokens": 4},
    "modelUsage": {"claude-sonnet-5": {"inputTokens": 2, "outputTokens": 4, "cacheReadInputTokens": 3293,
                                       "cacheCreationInputTokens": 6047, "costUSD": 0.0248906}}})
CODEX_OUTPUT = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
    {"type": "turn.completed", "usage": {"input_tokens": 20010, "cached_input_tokens": 12928,
                                         "cache_write_input_tokens": 0, "output_tokens": 5,
                                         "reasoning_output_tokens": 0}}])
FIXED_CALC = "def average(values):\n    return sum(values) / len(values)\n"


class ParseTest(unittest.TestCase):
    def test_claude_totals_come_from_every_model(self):
        m = bench.parse_claude("Warning: something on stderr\n" + CLAUDE_OUTPUT)
        self.assertEqual((m["fresh_input"], m["cache_write"], m["cache_read"], m["total_input"], m["output"]),
                         (6049, 6047, 3293, 9342, 4))
        self.assertEqual((m["cost_usd"], m["cost_source"], m["model"], m["turns"]),
                         (0.0248906, "reported", "claude-sonnet-5", 1))
        self.assertTrue(m["agent_ok"])

    def test_claude_falls_back_to_usage_and_flags_errors(self):
        data = json.loads(CLAUDE_OUTPUT)
        del data["modelUsage"]
        data["is_error"] = True
        m = bench.parse_claude(json.dumps(data))
        self.assertEqual(m["total_input"], 9342)
        self.assertFalse(m["agent_ok"])
        self.assertFalse(bench.parse_claude("")["agent_ok"])

    def test_codex_usage_and_estimated_cost(self):
        m = bench.parse_codex(CODEX_OUTPUT, None)
        self.assertEqual((m["total_input"], m["cache_read"], m["fresh_input"], m["output"], m["turns"]),
                         (20010, 12928, 7082, 5, 1))
        self.assertIsNone(m["cost_usd"])
        priced = bench.parse_codex(CODEX_OUTPUT, {"input": 1.0, "cached_input": 0.1, "output": 10.0})
        self.assertAlmostEqual(priced["cost_usd"], (7082 * 1.0 + 12928 * 0.1 + 5 * 10.0) / 1_000_000)
        self.assertEqual(priced["cost_source"], "estimated")
        failed = bench.parse_codex(CODEX_OUTPUT + "\n" + json.dumps({"type": "turn.failed"}), None)
        self.assertFalse(failed["agent_ok"])


class StatsTest(unittest.TestCase):
    def test_wilson(self):
        self.assertEqual(bench.wilson(0, 0), (0.0, 0.0))
        low, high = bench.wilson(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=3)
        self.assertAlmostEqual(high, 0.7634, places=3)

    def test_bootstrap_needs_three_values_and_detects_a_clear_difference(self):
        self.assertIsNone(bench.bootstrap_ci([1.0, 2.0], random.Random(0)))
        ci = bench.bootstrap_diff([10.0, 11.0, 12.0, 10.5], [5.0, 5.5, 6.0, 5.2], random.Random(0))
        self.assertLess(ci[1], 0)
        self.assertEqual(bench.verdict(ci), "lower")
        self.assertEqual(bench.verdict((-1.0, 2.0)), "no clear difference")
        self.assertEqual(bench.verdict(None), "too few runs")

    def test_schedule_interleaves_arms_every_round(self):
        exp = {"arms": [{"name": n} for n in "abcd"]}
        order = bench.schedule(exp, ["claude", "codex"], 3, seed=1)
        self.assertEqual(len(order), 24)
        for rnd in range(3):
            for host in ("claude", "codex"):
                arms = [a["name"] for r, h, a in order if r == rnd and h == host]
                self.assertEqual(sorted(arms), list("abcd"))
        self.assertEqual(order, bench.schedule(exp, ["claude", "codex"], 3, seed=1))
        firsts = {tuple(a["name"] for r, h, a in order if r == rnd and h == "claude") for rnd in range(3)}
        self.assertGreater(len(firsts), 1)


class ExperimentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_exp(self, data: dict) -> Path:
        path = self.dir / "exp.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_invalid_experiments_are_rejected(self):
        base = {"name": "x", "task": {"prompt": "p"}, "arms": [{"name": "a"}]}
        for bad in ({**base, "arms": [{"name": "a"}, {"name": "a"}]}, {**base, "hosts": ["gemini"]},
                    {**base, "task": {"prompt": "p", "fixture": "missing"}}, {**base, "task": {}}):
            with self.assertRaises(bench.BenchError):
                bench.load_experiment(self.write_exp(bad))

    def test_workspace_gets_fixture_arm_files_and_a_commit(self):
        exp = bench.load_experiment(EXPERIMENTS / "lean-instructions.json")
        workspace = bench.prepare_workspace(exp, exp["arms"][1])
        try:
            self.assertTrue((workspace / "calc.py").is_file())
            self.assertIn("standard library only", (workspace / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertEqual((workspace / "CLAUDE.md").read_text(encoding="utf-8").strip(), "@AGENTS.md")
            log = subprocess.run(["git", "-C", str(workspace), "log", "--oneline"], capture_output=True, text=True)
            self.assertIn("bench fixture", log.stdout)
        finally:
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)


class RunTest(unittest.TestCase):
    """End to end with a fake agent: the lean arm fixes the bug, the verbose arm does not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.out = Path(self.tmp.name) / "results"
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_execute(self, cmd, cwd, env, stdin, timeout):
        self.calls.append({"cmd": cmd, "env": env, "stdin": stdin})
        lean = len((Path(cwd) / "AGENTS.md").read_text(encoding="utf-8")) < 500
        if lean:
            (Path(cwd) / "calc.py").write_text(FIXED_CALC, encoding="utf-8")
        data = json.loads(CLAUDE_OUTPUT)
        data["total_cost_usd"] = 0.02 if lean else 0.05
        return 0, json.dumps(data), ""

    def run_bench(self, *extra: str) -> tuple[int, str]:
        argv = ["run", str(EXPERIMENTS / "lean-instructions.json"), "--yes", "--hosts", "claude", "--runs", "3",
                "--out", str(self.out), *extra]
        buf = io.StringIO()
        with mock.patch.object(bench, "execute", self.fake_execute), \
                mock.patch.object(bench, "find_binary", lambda host: "claude-bin"), \
                mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), contextlib.redirect_stdout(buf):
            code = bench.main(argv)
        return code, buf.getvalue()

    def test_runs_check_success_and_report(self):
        code, out = self.run_bench()
        self.assertEqual(code, 0, out)
        runs = bench.load_runs(self.out)
        self.assertEqual(len(runs), 6)
        by_arm = {arm: [r for r in runs if r["arm"] == arm] for arm in ("verbose", "lean")}
        self.assertTrue(all(r["success"] for r in by_arm["lean"]))
        self.assertFalse(any(r["success"] for r in by_arm["verbose"]))
        self.assertTrue(all(r["check_exit"] not in (None, 0) for r in by_arm["verbose"]))
        call = self.calls[0]
        self.assertNotIn("CLAUDECODE", call["env"])
        self.assertIn("Fix calc.py", call["stdin"])
        self.assertIn("--strict-mcp-config", call["cmd"])
        report = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertIn("| lean | 3 | 3/3", report)
        self.assertIn("| verbose | 3 | 0/3", report)
        self.assertIn("| lean | [$-0.0300, $-0.0300] | lower |", report)

    def test_resume_skips_finished_runs_and_max_cost_stops(self):
        self.run_bench("--runs", "1")
        self.assertEqual(len(bench.load_runs(self.out)), 2)
        self.run_bench("--runs", "1")
        self.assertEqual(len(bench.load_runs(self.out)), 2)
        code, out = self.run_bench("--runs", "3", "--max-cost", "0.01")
        self.assertIn("stopped", out)
        self.assertEqual(len(bench.load_runs(self.out)), 3)


if __name__ == "__main__":
    unittest.main()
