#!/usr/bin/env python3
"""A/B benchmark for Claude Code and Codex: what a configuration really costs. Stdlib only.

  python bench.py plan EXPERIMENT.json                    show what would run; spends nothing
  python bench.py run EXPERIMENT.json --yes [--runs N] [--hosts claude,codex] [--out DIR]
                                         [--max-cost USD] [--keep] [--seed N]
  python bench.py report RESULTS_DIR                      statistics and a markdown report

Each run copies the task fixture into a fresh temporary git workspace, adds the arm's files, runs the
agent headless, runs the task's check, and appends one JSON line to RESULTS_DIR/runs.jsonl. Arms are
interleaved in a shuffled order every round, and an interrupted run resumes where it stopped.
Claude cost is the CLI's own total_cost_usd (API list prices, also shown on subscriptions). Codex
reports tokens only, so its cost is estimated when the experiment gives "prices".
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

HOSTS = ("claude", "codex")
CLAUDE_ALLOWED = "Read,Edit,Write,Glob,Grep,Bash(python:*)"
BOOTSTRAP_ITERS = 2000


class BenchError(Exception):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------- experiment ----------

def load_experiment(path: Path) -> dict:
    try:
        exp = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BenchError(f"cannot read {path}: {e}")
    base = path.resolve().parent
    exp["_base"] = base
    if not exp.get("name") or not exp.get("arms") or not exp.get("task", {}).get("prompt"):
        raise BenchError("an experiment needs name, task.prompt and at least one arm")
    names = [a.get("name") for a in exp["arms"]]
    if len(set(names)) != len(names) or not all(names):
        raise BenchError("every arm needs a unique name")
    exp["hosts"] = [h for h in exp.get("hosts", list(HOSTS))]
    unknown = [h for h in exp["hosts"] if h not in HOSTS]
    if unknown:
        raise BenchError(f"unknown host(s): {', '.join(unknown)}")
    fixture = exp["task"].get("fixture")
    if fixture and not (base / fixture).is_dir():
        raise BenchError(f"fixture {fixture} not found next to {path.name}")
    for arm in exp["arms"]:
        for rel in (arm.get("files_from") or {}).values():
            if not (base / rel).is_file():
                raise BenchError(f"arm {arm['name']}: file {rel} not found")
    return exp


def first_file(candidates: list[str | None]) -> str | None:
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def find_binary(host: str) -> str | None:
    """Same discovery as cross-review's ask_peer.py: env override, PATH, then the desktop apps' copies."""
    if host == "codex":
        cands = [os.environ.get("WB_CODEX_BIN"), shutil.which("codex")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            pattern = os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe")
            cands += sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        cands.append("/Applications/Codex.app/Contents/Resources/codex")
        return first_file(cands)
    exe = "claude.exe" if os.name == "nt" else "claude"
    return first_file([os.environ.get("WB_CLAUDE_BIN"), shutil.which("claude"), str(Path.home() / ".local" / "bin" / exe)])


def build_command(host: str, binary: str, exp: dict, arm: dict, workspace: Path) -> list[str]:
    common, task = exp.get("common") or {}, exp["task"]
    if host == "claude":
        cmd = [binary, "-p", "--output-format", "json", "--no-session-persistence", "--permission-mode", "acceptEdits",
               "--allowedTools", task.get("claude_allowed_tools", CLAUDE_ALLOWED)]
        if task.get("claude_tools") is not None:
            cmd += ["--tools", task["claude_tools"]]
        return cmd + list(common.get("claude_args", [])) + list(arm.get("claude_args", []))
    cmd = [binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
           "--sandbox", task.get("codex_sandbox", "workspace-write"), "-C", str(workspace)]
    return cmd + list(common.get("codex_args", [])) + list(arm.get("codex_args", [])) + ["-"]


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=bench", "-c", "user.email=bench@localhost", *args],
                   check=True, capture_output=True)


def prepare_workspace(exp: dict, arm: dict) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="wb-bench-"))
    fixture = exp["task"].get("fixture")
    if fixture:
        shutil.copytree(exp["_base"] / fixture, workspace, dirs_exist_ok=True)
    for rel, src in (arm.get("files_from") or {}).items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(exp["_base"] / src, target)
    for rel, text in (arm.get("files") or {}).items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(workspace, "init", "-q")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-q", "--allow-empty", "-m", "bench fixture")
    return workspace


def execute(cmd: list[str], cwd: Path, env: dict, stdin: str, timeout: int) -> tuple[int | None, str, str]:
    """Run one agent call. Tests replace this function."""
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout}s"


def run_check(check: str | None, cwd: Path, timeout: int) -> int | None:
    if not check:
        return None
    try:
        return subprocess.run(check, shell=True, cwd=cwd, capture_output=True, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        return -1


# ---------- parsing ----------

def last_json_object(text: str) -> dict:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    try:
        return json.loads(text)
    except ValueError:
        return {}


def parse_claude(stdout: str) -> dict:
    data = last_json_object(stdout)
    per_model = data.get("modelUsage") or {}
    if per_model:  # covers every model the session used, not just the main one
        def total(key: str) -> int:
            return sum(int(m.get(key) or 0) for m in per_model.values())
        fresh, write, read, out = (total("inputTokens"), total("cacheCreationInputTokens"),
                                   total("cacheReadInputTokens"), total("outputTokens"))
    else:
        usage = data.get("usage") or {}
        fresh, write, read, out = (int(usage.get(k) or 0) for k in
                                   ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                                    "output_tokens"))
    cost = data.get("total_cost_usd")
    return {"cost_usd": cost, "cost_source": "reported" if cost is not None else None,
            "fresh_input": fresh + write, "cache_write": write, "cache_read": read,
            "total_input": fresh + write + read, "output": out, "turns": data.get("num_turns"),
            "model": ",".join(sorted(per_model)) or None,
            "agent_ok": bool(data) and not data.get("is_error") and data.get("subtype", "success") == "success"}


def parse_codex(stdout: str, prices: dict | None) -> dict:
    total_in = cached = written = out = turns = 0
    failed = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type", "")
        if kind == "turn.completed":
            usage = event.get("usage") or {}
            total_in += int(usage.get("input_tokens") or 0)
            cached += int(usage.get("cached_input_tokens") or 0)
            written += int(usage.get("cache_write_input_tokens") or 0)
            out += int(usage.get("output_tokens") or 0)
            turns += 1
        elif kind in ("turn.failed", "error"):
            failed = True
    cost = None
    if prices and turns:  # USD per million tokens
        cost = ((total_in - cached) * prices.get("input", 0) + cached * prices.get("cached_input", 0)
                + out * prices.get("output", 0)) / 1_000_000
    return {"cost_usd": cost, "cost_source": "estimated" if cost is not None else None,
            "fresh_input": total_in - cached, "cache_write": written, "cache_read": cached,
            "total_input": total_in, "output": out, "turns": turns, "model": None,
            "agent_ok": turns > 0 and not failed}


# ---------- running ----------

def results_path(out_dir: Path) -> Path:
    return out_dir / "runs.jsonl"


def load_runs(out_dir: Path) -> list[dict]:
    path = results_path(out_dir)
    if not path.is_file():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            runs.append(json.loads(line))
        except ValueError:
            continue
    return runs


def schedule(exp: dict, hosts: list[str], runs: int, seed: int) -> list[tuple[int, str, dict]]:
    """Round by round, every host, arms in a shuffled order, so slow drift does not favor one arm."""
    order = []
    for rnd in range(runs):
        for host in hosts:
            arms = list(exp["arms"])
            random.Random(f"{seed}-{rnd}-{host}").shuffle(arms)
            order += [(rnd, host, arm) for arm in arms]
    return order


def agent_env(exp: dict, arm: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}  # allow a nested headless call
    env.update((exp.get("common") or {}).get("env", {}))
    env.update(arm.get("env") or {})
    return env


def codex_model(cmd: list[str]) -> str:
    """Codex's JSON events do not name the model, so record the one pinned on the command line, if any."""
    for flag in ("-m", "--model"):
        if flag in cmd[:-1]:
            return cmd[cmd.index(flag) + 1]
    return "unpinned"


def run_one(exp: dict, host: str, arm: dict, rnd: int, binary: str, keep: bool) -> dict:
    task = exp["task"]
    workspace = prepare_workspace(exp, arm)
    cmd = build_command(host, binary, exp, arm, workspace)
    start = time.monotonic()
    code, stdout, stderr = execute(cmd, workspace, agent_env(exp, arm), task["prompt"], task.get("timeout", 600))
    seconds = round(time.monotonic() - start, 1)
    metrics = parse_claude(stdout) if host == "claude" else parse_codex(stdout, (exp.get("prices") or {}).get(host))
    if host == "codex":
        metrics["model"] = codex_model(cmd)
    check_exit = run_check(task.get("check"), workspace, task.get("timeout", 600))
    agent_ok = metrics.pop("agent_ok") and code == 0
    record = {"id": uuid.uuid4().hex, "at": now(), "experiment": exp["name"], "host": host, "arm": arm["name"],
              "round": rnd, "seconds": seconds, "exit": code, "agent_ok": agent_ok, "check_exit": check_exit,
              "success": agent_ok and check_exit in (None, 0), **metrics,
              "error": None if agent_ok else (stderr.strip().splitlines() or ["no output"])[-1][:300]}
    if keep:
        record["workspace"] = str(workspace)
    else:
        shutil.rmtree(workspace, ignore_errors=True)
    return record


def cmd_plan(args) -> int:
    exp = load_experiment(Path(args.experiment))
    hosts = args.hosts.split(",") if args.hosts else exp["hosts"]
    runs = args.runs or exp.get("runs", 3)
    print(f"experiment {exp['name']}: {exp.get('description', '')}")
    for host in hosts:
        binary = find_binary(host)
        print(f"\n{host}: {binary or 'NOT FOUND (set WB_' + host.upper() + '_BIN)'}")
        for arm in exp["arms"]:
            cmd = build_command(host, binary or host, exp, arm, Path("<workspace>"))
            print(f"  {arm['name']}: {' '.join(cmd)}  < prompt on stdin")
    total = runs * len(hosts) * len(exp["arms"])
    print(f"\n{total} agent runs ({runs} rounds x {len(hosts)} host(s) x {len(exp['arms'])} arm(s)). "
          "Each one spends real quota or money. Add --yes to `run` to start, and --max-cost to cap the spend.")
    return 0


def cmd_run(args) -> int:
    exp = load_experiment(Path(args.experiment))
    if not args.yes:
        cmd_plan(args)
        print("\nnot started: pass --yes to run")
        return 1
    hosts = args.hosts.split(",") if args.hosts else exp["hosts"]
    runs = args.runs or exp.get("runs", 3)
    binaries = {h: find_binary(h) for h in hosts}
    missing = [h for h, b in binaries.items() if not b]
    if missing:
        raise BenchError(f"CLI not found for: {', '.join(missing)}")
    out_dir = Path(args.out or Path(".ai") / "bench" / f"{exp['name']}-{dt.datetime.now():%Y%m%d-%H%M%S}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "experiment.json").write_text(
        json.dumps({k: v for k, v in exp.items() if k != "_base"}, indent=2, ensure_ascii=False), encoding="utf-8")
    done = {(r["host"], r["arm"], r["round"]) for r in load_runs(out_dir)}
    spent = 0.0
    with results_path(out_dir).open("a", encoding="utf-8") as fh:
        for rnd, host, arm in schedule(exp, hosts, runs, args.seed):
            if (host, arm["name"], rnd) in done:
                continue
            if args.max_cost is not None and spent >= args.max_cost:
                print(f"stopped: spent ${spent:.2f}, reaching --max-cost {args.max_cost}. Run again to resume.")
                break
            record = run_one(exp, host, arm, rnd, binaries[host], args.keep)
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            spent += record.get("cost_usd") or 0.0
            cost = f"${record['cost_usd']:.4f}" if record.get("cost_usd") is not None else "cost n/a"
            print(f"round {rnd} {host:<6} {arm['name']:<12} {'ok  ' if record['success'] else 'FAIL'} "
                  f"{record['total_input']:>8} in {record['output']:>6} out {cost} {record['seconds']}s"
                  + (f"  ({record['error']})" if record.get("error") else ""))
    report = build_report(load_runs(out_dir), exp)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"\nresults: {out_dir.as_posix()}/runs.jsonl, report: {out_dir.as_posix()}/report.md")
    return 0


# ---------- statistics and report ----------

def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, center - half), min(1.0, center + half)


def bootstrap_ci(values: list[float], rng: random.Random) -> tuple[float, float] | None:
    if len(values) < 3:
        return None
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(BOOTSTRAP_ITERS))
    return means[int(0.025 * BOOTSTRAP_ITERS)], means[int(0.975 * BOOTSTRAP_ITERS) - 1]


def bootstrap_diff(base: list[float], other: list[float], rng: random.Random) -> tuple[float, float] | None:
    if len(base) < 3 or len(other) < 3:
        return None
    diffs = sorted(statistics.fmean(rng.choices(other, k=len(other))) - statistics.fmean(rng.choices(base, k=len(base)))
                   for _ in range(BOOTSTRAP_ITERS))
    return diffs[int(0.025 * BOOTSTRAP_ITERS)], diffs[int(0.975 * BOOTSTRAP_ITERS) - 1]


def fmt_num(value: float | None, money: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"${value:.4f}" if money else f"{value:,.0f}"


def fmt_ci(ci: tuple[float, float] | None, money: bool = False) -> str:
    return f"[{fmt_num(ci[0], money)}, {fmt_num(ci[1], money)}]" if ci else ""


def verdict(ci: tuple[float, float] | None) -> str:
    if not ci:
        return "too few runs"
    if ci[1] < 0:
        return "lower"
    if ci[0] > 0:
        return "higher"
    return "no clear difference"


def build_report(runs: list[dict], exp: dict, seed: int = 0) -> str:
    rng = random.Random(seed)
    lines = [f"# Benchmark: {exp['name']}", "", exp.get("description", ""), "",
             f"{len(runs)} runs. Cost is reported by Claude Code and estimated for Codex when prices are given. "
             "Intervals are 95% (Wilson for success, bootstrap for means); fewer than 3 runs per arm shows none.", ""]
    arm_order = [a["name"] for a in exp["arms"]]
    for host in [h for h in HOSTS if any(r["host"] == h for r in runs)]:
        lines += [f"## {host}", "",
                  "| arm | runs | success [95% CI] | cost per run [95% CI] | cost per success | total input [95% CI] "
                  "| cached | output | turns | seconds | model |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        groups = {name: [r for r in runs if r["host"] == host and r["arm"] == name] for name in arm_order}
        for name, group in groups.items():
            if not group:
                continue
            n, ok = len(group), sum(1 for r in group if r.get("success"))
            low, high = wilson(ok, n)
            costs = [r["cost_usd"] for r in group if r.get("cost_usd") is not None]
            inputs = [float(r.get("total_input") or 0) for r in group]
            cached = sum(r.get("cache_read") or 0 for r in group) / max(1, sum(inputs))
            per_success = sum(costs) / ok if costs and ok else None
            models = ",".join(sorted({r.get("model") for r in group if r.get("model")})) or "-"
            lines.append(
                f"| {name} | {n} | {ok}/{n} [{low:.0%}, {high:.0%}] | "
                f"{fmt_num(statistics.fmean(costs) if costs else None, True)} {fmt_ci(bootstrap_ci(costs, rng), True)} | "
                f"{fmt_num(per_success, True)} | {fmt_num(statistics.fmean(inputs))} {fmt_ci(bootstrap_ci(inputs, rng))} | "
                f"{cached:.0%} | {fmt_num(statistics.fmean([float(r.get('output') or 0) for r in group]))} | "
                f"{statistics.fmean([float(r.get('turns') or 0) for r in group]):.1f} | "
                f"{statistics.fmean([float(r.get('seconds') or 0) for r in group]):.1f} | {models} |")
        base_name = next((name for name in arm_order if groups.get(name)), None)
        others = [name for name in arm_order if name != base_name and groups.get(name)]
        if base_name and others:
            lines += ["", f"Difference from `{base_name}` (negative means the arm uses less):", "",
                      "| arm | cost per run [95% CI] | verdict | total input [95% CI] | verdict |", "|---|---|---|---|---|"]
            base = groups[base_name]
            for name in others:
                group = groups[name]
                cost_ci = bootstrap_diff([r["cost_usd"] for r in base if r.get("cost_usd") is not None],
                                         [r["cost_usd"] for r in group if r.get("cost_usd") is not None], rng)
                input_ci = bootstrap_diff([float(r.get("total_input") or 0) for r in base],
                                          [float(r.get("total_input") or 0) for r in group], rng)
                lines.append(f"| {name} | {fmt_ci(cost_ci, True) or 'n/a'} | {verdict(cost_ci)} | "
                             f"{fmt_ci(input_ci) or 'n/a'} | {verdict(input_ci)} |")
        if host == "codex" and any(r.get("model") == "unpinned" for r in runs if r["host"] == host):
            lines += ["", "Codex does not report its model. Pin it with `-m <model>` in `common.codex_args` so every "
                          "arm uses the same one; otherwise an arm such as `--ignore-user-config` may change it."]
        failures = [r for r in runs if r["host"] == host and r.get("error")]
        if failures:
            lines += ["", "Errors: " + "; ".join(f"{r['arm']} round {r['round']}: {r['error']}" for r in failures[:5])]
        lines.append("")
    return "\n".join(lines)


def cmd_report(args) -> int:
    out_dir = Path(args.results)
    runs = load_runs(out_dir)
    exp_path = out_dir / "experiment.json"
    if not runs or not exp_path.is_file():
        raise BenchError(f"no runs.jsonl and experiment.json in {out_dir}")
    report = build_report(runs, json.loads(exp_path.read_text(encoding="utf-8")))
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("experiment")
        p.add_argument("--runs", type=int, help="rounds (default: the experiment's runs)")
        p.add_argument("--hosts", help="comma-separated subset, e.g. claude")
        if name == "run":
            p.add_argument("--yes", action="store_true", help="confirm that the runs may spend quota or money")
            p.add_argument("--out", help="results directory (default: .ai/bench/<name>-<time>)")
            p.add_argument("--max-cost", type=float, help="stop once reported or estimated cost reaches this USD")
            p.add_argument("--keep", action="store_true", help="keep each run's workspace for inspection")
            p.add_argument("--seed", type=int, default=0, help="seed for the arm order")
    p = sub.add_parser("report")
    p.add_argument("results")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"plan": cmd_plan, "run": cmd_run, "report": cmd_report}[args.cmd](args)
    except BenchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
