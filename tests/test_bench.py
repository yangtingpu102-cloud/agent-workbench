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
        empty = bench.parse_claude("")
        self.assertFalse(empty["agent_ok"])
        self.assertIsNone(empty["total_input"])  # unknown, not zero
        self.assertIsNone(bench.parse_codex("", None)["total_input"])

    def test_results_without_usage_are_unknown_not_zero(self):
        data = json.loads(CLAUDE_OUTPUT)
        del data["modelUsage"], data["usage"]
        m = bench.parse_claude(json.dumps(data))
        self.assertIsNone(m["total_input"])
        self.assertEqual(m["cost_usd"], 0.0248906)  # the CLI still reported its cost
        bare_turn = json.dumps({"type": "turn.completed"})
        c = bench.parse_codex(bare_turn, {"input": 1.0, "cached_input": 0.1, "output": 2.0})
        self.assertIsNone(c["total_input"])
        self.assertIsNone(c["cost_usd"])
        self.assertTrue(c["agent_ok"])
        no_cache = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
        self.assertIsNone(bench.parse_codex(no_cache, {"input": 1.0, "cached_input": 0.1, "output": 2.0})["cost_usd"])
        self.assertIsNone(bench.parse_codex("", None)["turns"])
        partial = json.loads(CLAUDE_OUTPUT)
        del partial["modelUsage"]["claude-sonnet-5"]["cacheReadInputTokens"]
        self.assertIsNone(bench.parse_claude(json.dumps(partial))["total_input"])

    def test_null_counts_are_unknown_and_a_missing_cache_write_stays_unknown(self):
        data = json.loads(CLAUDE_OUTPUT)
        data["modelUsage"]["claude-sonnet-5"]["inputTokens"] = None
        self.assertIsNone(bench.parse_claude(json.dumps(data))["total_input"])
        turn = {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2}}
        m = bench.parse_codex(json.dumps(turn), {"input": 1.0, "cached_input": 0.1, "output": 2.0})
        self.assertEqual((m["total_input"], m["cache_read"]), (10, 4))
        self.assertIsNone(m["cache_write"])
        self.assertIsNotNone(m["cost_usd"])

    def test_answers_are_captured_and_matched(self):
        self.assertEqual(bench.parse_codex(CODEX_OUTPUT, None)["answer"], "ok")
        data = json.loads(CLAUDE_OUTPUT)
        data["result"] = "OK."
        self.assertEqual(bench.parse_claude(json.dumps(data))["answer"], "OK.")
        self.assertTrue(bench.answer_matches("ok", "OK."))
        self.assertFalse(bench.answer_matches("ok", "no idea"))
        self.assertIsNone(bench.answer_matches(None, "anything"))

    def test_report_says_completed_when_nothing_judges_the_answer(self):
        run = {"host": "claude", "arm": "a", "round": 0, "success": True, "cost_usd": 0.1, "total_input": 10,
               "cache_read": 1, "output": 1, "turns": 1, "seconds": 1.0, "model": "m"}
        report = bench.build_report([run], {"name": "x", "task": {"prompt": "p"}, "arms": [{"name": "a"}]})
        self.assertIn("| completed [95% CI] |", report)
        judged = bench.build_report([run], {"name": "x", "task": {"prompt": "p", "expect": "ok"}, "arms": [{"name": "a"}]})
        self.assertIn("| success [95% CI] |", judged)

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


class CodexModelTest(unittest.TestCase):
    def test_model_comes_from_the_pinned_flag(self):
        self.assertEqual(bench.codex_model(["codex", "exec", "-m", "gpt-x", "-"]), "gpt-x")
        self.assertEqual(bench.codex_model(["codex", "exec", "--model", "gpt-y", "-"]), "gpt-y")
        self.assertEqual(bench.codex_model(["codex", "exec", "-"]), "unpinned")
        self.assertEqual(bench.codex_model(["codex", "exec", "-m", "common", "-m", "arm", "-"]), "arm")

    def test_report_warns_when_codex_model_is_not_pinned(self):
        exp = {"name": "x", "arms": [{"name": "a"}]}
        run = {"host": "codex", "arm": "a", "round": 0, "success": True, "cost_usd": None, "total_input": 10,
               "cache_read": 0, "output": 1, "turns": 1, "seconds": 1.0, "model": "unpinned"}
        self.assertIn("Pin it with `-m <model>`", bench.build_report([run], exp))


class StatsTest(unittest.TestCase):
    def test_wilson(self):
        self.assertEqual(bench.wilson(0, 0), (0.0, 0.0))
        low, high = bench.wilson(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=3)
        self.assertAlmostEqual(high, 0.7634, places=3)

    def test_bootstrap_needs_three_values_and_detects_a_clear_difference(self):
        self.assertIsNone(bench.bootstrap_ci([1.0, 2.0], random.Random(0)))
        base = dict(enumerate([10.0, 11.0, 12.0, 10.5]))
        ci = bench.bootstrap_paired(base, dict(enumerate([5.0, 5.5, 6.0, 5.2])), random.Random(0))
        self.assertLess(ci[1], 0)
        self.assertEqual(bench.verdict(ci), "lower")
        self.assertEqual(bench.verdict((-1.0, 2.0)), "no clear difference")
        self.assertEqual(bench.verdict(None), "too few runs")

    def test_paired_difference_uses_only_rounds_both_arms_ran(self):
        # Round drift hits both arms alike; pairing by round cancels it.
        base = {0: 100.0, 1: 200.0, 2: 300.0, 3: 400.0}
        other = {0: 90.0, 1: 190.0, 2: 290.0, 3: 390.0, 4: 1.0}
        self.assertEqual(bench.bootstrap_paired(base, other, random.Random(0)), (-10.0, -10.0))
        self.assertIsNone(bench.bootstrap_paired({0: 1.0, 1: 2.0}, {0: 1.0, 1: 2.0}, random.Random(0)))

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

    def test_price_tables_must_be_complete(self):
        base = {"name": "x", "task": {"prompt": "p"}, "arms": [{"name": "a"}]}
        with self.assertRaises(bench.BenchError):
            bench.load_experiment(self.write_exp({**base, "prices": {"codex": {"input": 1.0, "output": 2.0}}}))
        ok = bench.load_experiment(self.write_exp(
            {**base, "prices": {"codex": {"input": 1.0, "cached_input": 0.1, "output": 2.0}}}))
        self.assertIn("codex", ok["prices"])

    def test_arm_files_cannot_leave_the_workspace(self):
        for dest in ("../evil.txt", "/etc/evil", "C:/evil.txt", "a/../../evil.txt"):
            exp = {"name": "x", "task": {"prompt": "p"}, "arms": [{"name": "a", "files": {dest: "x"}}]}
            with self.assertRaises(bench.BenchError, msg=dest):
                bench.load_experiment(self.write_exp(exp))

    def test_claude_gets_no_shell_unless_the_experiment_asks(self):
        exp = bench.load_experiment(EXPERIMENTS / "lean-instructions.json")
        cmd = bench.build_command("claude", "claude", exp, exp["arms"][0], Path("w"))
        self.assertNotIn("Bash", cmd[cmd.index("--allowedTools") + 1])
        exp["task"]["claude_allowed_tools"] = "Read,Edit,Bash(python:*)"
        path = self.write_exp({k: v for k, v in exp.items() if not k.startswith("_")} | {
            "task": {**exp["task"], "fixture": None}, "arms": [{"name": "a"}]})
        buf = io.StringIO()
        with mock.patch.object(bench, "find_binary", lambda host: "bin"), contextlib.redirect_stdout(buf):
            bench.main(["plan", str(path), "--hosts", "claude"])
        self.assertIn("not confined to the temporary workspace", buf.getvalue())

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
                mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(buf):
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
        code, out = self.run_bench("--runs", "3", "--max-cost", "0.08")  # 0.07 already spent before resuming
        self.assertEqual(code, bench.EXIT_PARTIAL)
        self.assertIn("stopped", out)
        self.assertEqual(len(bench.load_runs(self.out)), 2)

    def test_runs_of_unknown_cost_block_a_capped_resume(self):
        real = self.fake_execute
        self.fake_execute = lambda *a: (None, "", "timed out after 600s")  # no cost recorded
        self.run_bench("--runs", "1")
        self.fake_execute = real
        code, out = self.run_bench("--runs", "2", "--max-cost", "5")
        self.assertEqual(code, bench.EXIT_PARTIAL)
        self.assertIn("no known cost", out)

    def test_a_capped_run_stops_when_a_cost_is_unknown(self):
        self.fake_execute = lambda *a: (None, "", "timed out after 600s")
        code, out = self.run_bench("--runs", "1", "--max-cost", "5")
        self.assertEqual(code, bench.EXIT_PARTIAL)
        self.assertIn("can no longer be enforced", out)
        self.assertEqual(len(bench.load_runs(self.out)), 1)

    def test_report_does_not_average_over_unknown_costs(self):
        exp = {"name": "x", "arms": [{"name": "a"}, {"name": "b"}]}
        def run(arm, rnd, cost):
            return {"host": "claude", "arm": arm, "round": rnd, "success": True, "cost_usd": cost,
                    "total_input": 100, "cache_read": 10, "output": 5, "turns": 1, "seconds": 1.0, "model": "m"}
        runs = [run("a", r, 1.0) for r in range(3)] + [run("b", 0, 0.1), run("b", 1, None), run("b", 2, 0.1)]
        report = bench.build_report(runs, exp)
        self.assertIn("n/a (1 of 3 unknown)", report)
        runs[4]["turns"] = None
        self.assertIn("| n/a | 1.0 | m |", bench.build_report(runs, exp))  # turns column, then seconds
        self.assertIn("not comparable", report)

    def test_max_cost_needs_codex_prices(self):
        code, out = self.run_bench("--hosts", "codex", "--max-cost", "1")
        self.assertEqual(code, 2)

    def test_resume_refuses_a_changed_experiment(self):
        import shutil
        copy = Path(self.tmp.name) / "examples"
        shutil.copytree(SKILL / "examples", copy)
        exp_path = copy / "experiments" / "lean-instructions.json"
        argv = ["run", str(exp_path), "--yes", "--hosts", "claude", "--runs", "1", "--out", str(self.out)]
        with mock.patch.object(bench, "execute", self.fake_execute),                 mock.patch.object(bench, "find_binary", lambda host: "claude-bin"),                 contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bench.main(argv), 0)
            data = json.loads(exp_path.read_text(encoding="utf-8"))
            data["task"]["prompt"] = "A different task."
            exp_path.write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(bench.main(argv), 2)
        self.assertIn("different experiment", err.getvalue())


if __name__ == "__main__":
    unittest.main()
