---
name: ab-bench
description: Measure what a Claude Code or Codex configuration really costs (billed tokens, dollars, turns, success) with an A/B experiment and confidence intervals, instead of trusting claimed savings. Use when the user says "benchmark", "A/B", "does X save tokens", "比較成本", "實測省多少", or wants evidence before adopting a tool, hook or instruction change.
license: MIT
---

# A/B bench

Runs the same task headless in fresh workspaces under two or more configurations ("arms") and reports cost, tokens, turns and success with 95% intervals. `<skill-dir>` is the directory containing this file.

## Steps
1. Write an experiment JSON. Start from `<skill-dir>/examples/experiments/`. It needs:
   - one task: an optional fixture directory, the prompt, and what decides success: a `check` command run in the workspace, and/or `expect`, text the final answer must contain. Without either, runs only count as completed;
   - two or more arms that differ in exactly one thing: `claude_args`, `codex_args`, `files`, `files_from` or `env`;
   - the hosts and the number of runs.
2. Show the plan: `python <skill-dir>/scripts/bench.py plan <experiment.json>`. Tell the user how many agent runs it means and that each one spends quota or money. Get their go-ahead and a budget before running.
3. Run it: `python <skill-dir>/scripts/bench.py run <experiment.json> --yes --max-cost <USD>`. Start with `--runs 3`. An interrupted run resumes when you run the same command with the same `--out`.
4. Read `report.md` in the results directory and report back:
   - Call a difference real only when its 95% interval excludes zero. With fewer than 3 runs per arm, call the result anecdotal.
   - Compare cost per success, not only cost per run.

## Rules
- Change one variable per arm, and keep the model the same across arms. The report's model column shows it.
- Never start a run without the user's go-ahead on the run count and the budget.
- Claude cost comes from the CLI. Codex cost is estimated only when the experiment has `prices` (USD per million tokens). Otherwise compare tokens.
- `--max-cost` is enforced per call for Claude (each call gets the remaining budget as `--max-budget-usd`). Codex has no per-call limit, so a capped run that includes Codex needs `--allow-overrun`: tell the user that one Codex run can exceed the cap before it is checked.
- Agents get edit rights inside the temporary workspace only: Claude via `acceptEdits` plus an explicit tool list without a shell, Codex via the `workspace-write` sandbox.
- Treat an experiment file from someone else like code. Its `check` runs on this machine outside any sandbox, and `plan` prints it: read it before you run. Arm flags outside a safe list (extra directories, settings, MCP servers, permission or sandbox overrides) are refused unless you pass `--allow-any-args` after checking them. Fixtures and `files_from` sources must stay under the folder that holds the experiment's folder; a fixture's `.git` is never copied.
- For one-word answers, use `"expect_match": "exact"`, so that "not ok" does not count as "ok".
