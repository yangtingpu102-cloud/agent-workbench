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
Exit codes: 0 done, 1 not started (no --yes), 2 error, 3 stopped early at --max-cost (partial report).
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import math
import os
import random
import re
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
# Claude confines Edit and Write to its working directory; Bash would not be confined, so it is opt-in.
CLAUDE_ALLOWED = "Read,Edit,Write,Glob,Grep"
BOOTSTRAP_ITERS = 2000
FIXTURE_IGNORE = ("__pycache__", "*.pyc")
PRICE_KEYS = ("input", "cached_input", "output")
EXIT_PARTIAL = 3  # `run` stopped at --max-cost before finishing


class BenchError(Exception):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------- experiment ----------

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TOP_KEYS = {"name", "description", "hosts", "runs", "common", "task", "arms", "prices"}
TASK_KEYS = {"prompt", "fixture", "check", "expect", "timeout", "claude_tools", "claude_allowed_tools",
             "codex_sandbox"}
ARM_KEYS = {"name", "claude_args", "codex_args", "env", "files", "files_from"}
COMMON_KEYS = {"claude_args", "codex_args", "env"}
SANDBOXES = ("read-only", "workspace-write")  # never danger-full-access: the agent must stay in its workspace


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(exp, base: Path) -> list[str]:
    """Every problem in an experiment, found before anything runs. Unknown keys are errors: a typo such as
    "claude_arg" would otherwise make an arm silently identical to the baseline."""
    errors: list[str] = []

    def keys(obj, allowed: set, label: str) -> bool:
        if not isinstance(obj, dict):
            errors.append(f"{label} must be an object")
            return False
        errors.extend(f"{label}: unknown key '{k}' (allowed: {', '.join(sorted(allowed))})"
                      for k in obj if k not in allowed)
        return True

    def strings(value, label: str) -> None:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            errors.append(f"{label} must be a list of strings")

    def mapping(value, label: str) -> None:
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            errors.append(f"{label} must map strings to strings")

    def optional_str(obj: dict, key: str, label: str, non_empty: bool = False) -> None:
        if key in obj and obj[key] is not None and (not isinstance(obj[key], str) or (non_empty and not obj[key])):
            errors.append(f"{label}.{key} must be {'a non-empty ' if non_empty else 'a '}string or null")

    if not keys(exp, TOP_KEYS, "experiment"):
        return errors
    if not isinstance(exp.get("name"), str) or not NAME_RE.match(exp["name"]):
        errors.append("name must be 1-64 letters, digits, '.', '_' or '-' (it names the results folder)")
    if "description" in exp and not isinstance(exp["description"], str):
        errors.append("description must be a string")
    if not is_count(exp.get("runs", 3)) or exp.get("runs", 3) < 1:
        errors.append(f"runs must be a whole number >= 1, not {exp.get('runs')!r}")
    try:
        check_hosts(exp.get("hosts", list(HOSTS)), "hosts")
    except BenchError as e:
        errors.append(str(e))

    common = exp.get("common", {})
    if keys(common, COMMON_KEYS, "common"):
        for key in ("claude_args", "codex_args"):
            if key in common:
                strings(common[key], f"common.{key}")
        if "env" in common:
            mapping(common["env"], "common.env")

    task = exp.get("task")
    if keys(task, TASK_KEYS, "task"):
        if not isinstance(task.get("prompt"), str) or not task["prompt"].strip():
            errors.append("task.prompt must be a non-empty string")
        for key, non_empty in (("check", True), ("expect", True), ("claude_tools", False),
                               ("claude_allowed_tools", True), ("fixture", True)):
            optional_str(task, key, "task", non_empty)
        if isinstance(task.get("fixture"), str) and task["fixture"] and not (base / task["fixture"]).is_dir():
            errors.append(f"task.fixture '{task['fixture']}' is not a directory next to the experiment")
        timeout = task.get("timeout", 600)
        if not is_number(timeout) or timeout <= 0:
            errors.append(f"task.timeout must be a number of seconds > 0, not {timeout!r}")
        if task.get("codex_sandbox", "workspace-write") not in SANDBOXES:
            errors.append(f"task.codex_sandbox must be one of {', '.join(SANDBOXES)}")

    arms = exp.get("arms")
    if not isinstance(arms, list) or not arms:
        errors.append("arms must be a non-empty list")
        arms = []
    names = []
    for i, arm in enumerate(arms):
        label = f"arms[{i}]"
        if not keys(arm, ARM_KEYS, label):
            continue
        name = arm.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            errors.append(f"{label}.name must be 1-64 letters, digits, '.', '_' or '-'")
        names.append(name)
        for key in ("claude_args", "codex_args"):
            if key in arm:
                strings(arm[key], f"{label}.{key}")
        for key in ("env", "files", "files_from"):
            if key in arm:
                mapping(arm[key], f"{label}.{key}")
        for dest in [*(arm.get("files") or {}), *(arm.get("files_from") or {})] if \
                isinstance(arm.get("files", {}), dict) and isinstance(arm.get("files_from", {}), dict) else []:
            if not isinstance(dest, str) or not safe_relative(dest):
                errors.append(f"{label}: '{dest}' must be a relative path inside the workspace")
        if isinstance(arm.get("files_from"), dict):
            errors.extend(f"{label}: file '{src}' not found" for src in arm["files_from"].values()
                          if isinstance(src, str) and not (base / src).is_file())
    if len(set(map(str, names))) != len(names):
        errors.append("arm names must be unique")

    prices = exp.get("prices", {})
    if keys(prices, {"codex"}, "prices") and "codex" in prices:  # Claude reports its own cost
        table = prices["codex"]
        if keys(table, set(PRICE_KEYS), "prices.codex"):
            missing = [k for k in PRICE_KEYS if not is_number(table.get(k)) or table.get(k) < 0]
            if missing:
                errors.append(f"prices.codex needs numbers >= 0 (USD per million tokens) for {', '.join(missing)}")
    return errors


def load_experiment(path: Path) -> dict:
    try:
        exp = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BenchError(f"cannot read {path}: {e}")
    base = path.resolve().parent
    errors = validate(exp, base)
    if errors:
        raise BenchError(f"{path.name} has {len(errors)} problem(s):\n  - " + "\n  - ".join(errors))
    exp["_base"] = base
    exp["hosts"] = check_hosts(exp.get("hosts", list(HOSTS)), "hosts")
    return exp


def check_hosts(hosts, label: str) -> list[str]:
    """Supported, non-empty and unique: a repeated host would schedule extra paid calls per arm and round."""
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise BenchError(f"{label} must be a list of host names")
    hosts = [h.strip() for h in hosts]
    unknown = [h for h in hosts if h not in HOSTS]
    if not hosts or unknown:
        raise BenchError(f"{label}: use one or more of {', '.join(HOSTS)}" + (f" (unknown: {', '.join(unknown)})"
                                                                                if unknown else ""))
    if len(set(hosts)) != len(hosts):
        raise BenchError(f"{label} lists a host more than once: {', '.join(hosts)}")
    return hosts


def safe_relative(rel: str) -> bool:
    parts = Path(rel.replace("\\", "/")).parts
    return bool(parts) and not Path(rel).is_absolute() and not rel.startswith(("/", "\\")) \
        and ":" not in parts[0] and ".." not in parts


def fingerprint(exp: dict, seed: int) -> str:
    """Identity of an experiment for resuming: its settings (except the run count), the seed, and the bytes of
    its fixture and arm files. Results made under a different fingerprint must not be mixed."""
    h = hashlib.sha256()
    h.update(json.dumps({k: v for k, v in exp.items() if not k.startswith("_") and k != "runs"},
                        sort_keys=True).encode("utf-8"))
    h.update(f"seed={seed}".encode())
    base = exp["_base"]
    sources = []
    fixture = exp["task"].get("fixture")
    if fixture:
        root = base / fixture
        sources += [(f"fixture/{p.relative_to(root).as_posix()}", p) for p in sorted(root.rglob("*"))
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    for arm in exp["arms"]:
        sources += [(f"{arm['name']}/{dest}", base / src) for dest, src in (arm.get("files_from") or {}).items()]
    for label, path in sources:
        h.update(label.encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


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


def build_command(host: str, binary: str, exp: dict, arm: dict, workspace: Path,
                  budget: float | None = None) -> list[str]:
    common, task = exp.get("common") or {}, exp["task"]
    if host == "claude":
        allowed = task.get("claude_allowed_tools", CLAUDE_ALLOWED)
        tools = task.get("claude_tools")
        if tools is None:  # --allowedTools only pre-approves; --tools decides what exists, so Bash stays opt-in
            names = [rule.split("(")[0].strip() for rule in allowed.split(",") if rule.strip()]
            tools = ",".join(dict.fromkeys(names))
        cmd = [binary, "-p", "--output-format", "json", "--no-session-persistence", "--permission-mode", "acceptEdits",
               "--allowedTools", allowed, "--tools", tools]
        cmd += list(common.get("claude_args", [])) + list(arm.get("claude_args", []))
        if budget is not None:  # the CLI enforces this per call, so one run cannot overrun the cap
            cmd += ["--max-budget-usd", f"{max(budget, 0.0):.4f}"]
        return cmd
    cmd = [binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
           "--sandbox", task.get("codex_sandbox", "workspace-write"), "-C", str(workspace)]
    return cmd + list(common.get("codex_args", [])) + list(arm.get("codex_args", [])) + ["-"]


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=bench", "-c", "user.email=bench@localhost", *args],
                   check=True, capture_output=True)


def prepare_workspace(exp: dict, arm: dict) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="wb-bench-")).resolve()
    fixture = exp["task"].get("fixture")
    if fixture:
        shutil.copytree(exp["_base"] / fixture, workspace, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(*FIXTURE_IGNORE))

    def target(rel: str) -> Path:
        path = (workspace / rel).resolve()
        if not path.is_relative_to(workspace):
            raise BenchError(f"arm {arm['name']}: '{rel}' resolves outside the workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    for rel, src in (arm.get("files_from") or {}).items():
        shutil.copyfile(exp["_base"] / src, target(rel))
    for rel, text in (arm.get("files") or {}).items():
        target(rel).write_text(text, encoding="utf-8")
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


UNKNOWN_USAGE = {"cost_usd": None, "cost_source": None, "fresh_input": None, "cache_write": None,
                 "cache_read": None, "total_input": None, "output": None}


def is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_claude(stdout: str) -> dict:
    data = last_json_object(stdout)
    if not data:  # no result object (crash or timeout): usage is unknown, not zero
        return {**UNKNOWN_USAGE, "turns": None, "model": None, "agent_ok": False}
    per_model = data.get("modelUsage") or {}
    usage = data.get("usage") or {}
    model_keys = ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens", "outputTokens")
    usage_keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    if per_model and all(all(is_count(m.get(k)) for k in model_keys) for m in per_model.values()):
        # modelUsage covers every model the session used, not just the main one
        tokens = tuple(sum(m[k] for m in per_model.values()) for k in model_keys)
    elif not per_model and all(is_count(usage.get(k)) for k in usage_keys):
        tokens = tuple(usage[k] for k in usage_keys)
    else:
        tokens = None  # usage not fully reported: unknown, not zero
    cost = data.get("total_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        cost = None
    usage_fields = UNKNOWN_USAGE.copy()
    if tokens:
        fresh, write, read, out = tokens
        usage_fields.update({"fresh_input": fresh + write, "cache_write": write, "cache_read": read,
                             "total_input": fresh + write + read, "output": out})
    usage_fields.update({"cost_usd": cost, "cost_source": "reported" if cost is not None else None})
    return {**usage_fields, "turns": data.get("num_turns") if is_count(data.get("num_turns")) else None,
            "model": ",".join(sorted(per_model)) or None, "answer": str(data.get("result") or ""),
            "agent_ok": not data.get("is_error") and data.get("subtype", "success") == "success"}


def parse_codex(stdout: str, prices: dict | None) -> dict:
    total_in = cached = written = out = turns = 0
    failed = usage_missing = False
    written_known = True
    answer = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type", "")
        if kind == "turn.completed":
            usage = event.get("usage") or {}
            if not all(is_count(usage.get(k)) for k in ("input_tokens", "cached_input_tokens", "output_tokens")):
                usage_missing = True
            else:
                total_in += usage["input_tokens"]
                cached += usage["cached_input_tokens"]
                out += usage["output_tokens"]
            if is_count(usage.get("cache_write_input_tokens")):
                written += usage["cache_write_input_tokens"]  # display only; not part of the cost estimate
            else:
                written_known = False
            turns += 1
        elif kind in ("turn.failed", "error"):
            failed = True
        elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
            answer = str(event["item"].get("text") or "")
    ok = turns > 0 and not failed
    if not turns or usage_missing:  # usage or turns the CLI did not report are unknown, not zero
        return {**UNKNOWN_USAGE, "turns": turns or None, "model": None, "answer": answer, "agent_ok": ok}
    cost = None
    if prices:  # USD per million tokens
        cost = ((total_in - cached) * prices["input"] + cached * prices["cached_input"] + out * prices["output"]) \
            / 1_000_000
    return {"cost_usd": cost, "cost_source": "estimated" if cost is not None else None,
            "fresh_input": total_in - cached, "cache_write": written if written_known else None,
            "cache_read": cached, "total_input": total_in, "output": out, "turns": turns, "model": None,
            "answer": answer, "agent_ok": ok}


# ---------- running ----------

def results_path(out_dir: Path) -> Path:
    return out_dir / "runs.jsonl"


def load_lines(out_dir: Path) -> list[dict]:
    path = results_path(out_dir)
    if not path.is_file():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            continue
    return lines


def load_runs(out_dir: Path) -> list[dict]:
    """Finished runs only. A line with status "started" marks a paid call before it begins."""
    return [r for r in load_lines(out_dir) if r.get("status") != "started"]


def interrupted_attempts(lines: list[dict]) -> list[dict]:
    """Calls that started but never recorded a result: they may have cost money, by an unknown amount."""
    finished = {r.get("id") for r in lines if r.get("status") != "started"}
    return [r for r in lines if r.get("status") == "started" and r.get("id") not in finished]


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


def answer_matches(expect: str | None, answer: str | None) -> bool | None:
    """With task.expect set, the final answer must contain it (case-insensitive); None means not judged."""
    if not expect:
        return None
    return expect.lower() in (answer or "").lower()


def codex_model(cmd: list[str]) -> str:
    """Codex's JSON events do not name the model, so record the one pinned on the command line, if any."""
    model = "unpinned"
    for i, arg in enumerate(cmd[:-1]):
        if arg in ("-m", "--model"):
            model = cmd[i + 1]  # the last occurrence wins, as for the CLI itself
    return model


def run_one(exp: dict, host: str, arm: dict, rnd: int, binary: str, keep: bool,
            budget: float | None = None) -> dict:
    task = exp["task"]
    workspace = prepare_workspace(exp, arm)
    cmd = build_command(host, binary, exp, arm, workspace, budget)
    start = time.monotonic()
    try:
        code, stdout, stderr = execute(cmd, workspace, agent_env(exp, arm), task["prompt"], task.get("timeout", 600))
    except BaseException:  # interrupted: do not leave the workspace behind
        if not keep:
            shutil.rmtree(workspace, ignore_errors=True)
        raise
    seconds = round(time.monotonic() - start, 1)
    metrics = parse_claude(stdout) if host == "claude" else parse_codex(stdout, (exp.get("prices") or {}).get(host))
    if host == "codex":
        metrics["model"] = codex_model(cmd)
    check_exit = run_check(task.get("check"), workspace, task.get("timeout", 600))
    agent_ok = metrics.pop("agent_ok") and code == 0
    answer_ok = answer_matches(task.get("expect"), metrics.get("answer"))  # judged on the full answer
    metrics["answer"] = (metrics.get("answer") or "")[-2000:]
    record = {"id": uuid.uuid4().hex, "at": now(), "experiment": exp["name"], "host": host, "arm": arm["name"],
              "round": rnd, "seconds": seconds, "exit": code, "agent_ok": agent_ok, "check_exit": check_exit,
              "answer_ok": answer_ok, "success": agent_ok and check_exit in (None, 0) and answer_ok is not False,
              **metrics,
              "error": None if agent_ok else (stderr.strip().splitlines() or ["no output"])[-1][:300]}
    if keep:
        record["workspace"] = str(workspace)
    else:
        shutil.rmtree(workspace, ignore_errors=True)
    return record


def run_count(args, exp: dict) -> int:
    """--runs when given (0 is not "unset"), otherwise the experiment's own count."""
    runs = exp.get("runs", 3) if args.runs is None else args.runs
    if runs < 1:
        raise BenchError(f"--runs must be at least 1, not {runs}")
    return runs


def cmd_plan(args) -> int:
    exp = load_experiment(Path(args.experiment))
    hosts = check_hosts(args.hosts.split(","), "--hosts") if args.hosts is not None else exp["hosts"]
    runs = run_count(args, exp)
    print(f"experiment {exp['name']}: {exp.get('description', '')}")
    for host in hosts:
        binary = find_binary(host)
        print(f"\n{host}: {binary or 'NOT FOUND (set WB_' + host.upper() + '_BIN)'}")
        for arm in exp["arms"]:
            cmd = build_command(host, binary or host, exp, arm, Path("<workspace>"))
            print(f"  {arm['name']}: {' '.join(cmd)}  < prompt on stdin")
            if host == "claude" and any("Bash" in a for a in cmd):
                print("    warning: this grants Claude a shell, which is not confined to the temporary workspace")
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
    hosts = check_hosts(args.hosts.split(","), "--hosts") if args.hosts is not None else exp["hosts"]
    runs = run_count(args, exp)
    binaries = {h: find_binary(h) for h in hosts}
    missing = [h for h, b in binaries.items() if not b]
    if missing:
        raise BenchError(f"CLI not found for: {', '.join(missing)}")
    if args.max_cost is not None and "codex" in hosts and not args.allow_overrun:
        raise BenchError("Codex has no per-call spending limit, so --max-cost can only be checked after each "
                         "Codex run, and one run may exceed it. Pass --allow-overrun to accept that, or run "
                         "Claude only (its CLI enforces the remaining budget per call)")
    if args.max_cost is not None and "codex" in hosts and not (exp.get("prices") or {}).get("codex"):
        raise BenchError("--max-cost needs a measurable cost, but Codex reports tokens only. Add "
                         '"prices": {"codex": {"input": .., "cached_input": .., "output": ..}} or drop --max-cost')
    fp = fingerprint(exp, args.seed)
    out_dir = Path(args.out or Path(".ai") / "bench" / f"{exp['name']}-{dt.datetime.now():%Y%m%d-%H%M%S}")
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = out_dir / "experiment.json"
    if saved.is_file():
        previous = json.loads(saved.read_text(encoding="utf-8")).get("fingerprint")
        if previous != fp:
            raise BenchError(f"{out_dir} holds results of a different experiment, seed or fixture "
                             f"({previous} vs {fp}); use a new --out")
    else:
        saved.write_text(json.dumps({"fingerprint": fp, "seed": args.seed,
                                     **{k: v for k, v in exp.items() if not k.startswith("_")}},
                                    indent=2, ensure_ascii=False), encoding="utf-8")
    history = load_runs(out_dir)
    interrupted = interrupted_attempts(load_lines(out_dir))
    done = {(r["host"], r["arm"], r["round"]) for r in history}
    spent = sum(r.get("cost_usd") or 0.0 for r in history)  # a resumed run counts what was already spent
    stopped = False
    with results_path(out_dir).open("a", encoding="utf-8") as fh:
        for rnd, host, arm in schedule(exp, hosts, runs, args.seed):
            if (host, arm["name"], rnd) in done:
                continue
            unknown = [r for r in history if r.get("cost_usd") is None] + interrupted
            if args.max_cost is not None and unknown:
                print(f"stopped: {len(unknown)} recorded run(s) or interrupted call(s) have no known cost, so "
                      "--max-cost cannot be enforced. Inspect them, then resume without --max-cost or use a new --out.")
                stopped = True
                break
            if args.max_cost is not None:
                same = [r["cost_usd"] for r in history
                        if r["host"] == host and r["arm"] == arm["name"] and r.get("cost_usd") is not None]
                estimate = statistics.fmean(same) if same else 0.0
                if spent >= args.max_cost or spent + estimate > args.max_cost:
                    print(f"stopped before {host}/{arm['name']} round {rnd}: spent ${spent:.4f}; the next run is "
                          f"estimated at ${estimate:.4f} against --max-cost {args.max_cost}. A single run can still "
                          "exceed the estimate. Run again with a higher cap to resume.")
                    stopped = True
                    break
            budget = args.max_cost - spent if args.max_cost is not None else None
            attempt = uuid.uuid4().hex  # written before the paid call, so an interruption is never invisible
            fh.write(json.dumps({"id": attempt, "status": "started", "at": now(), "host": host, "arm": arm["name"],
                                 "round": rnd, "fingerprint": fp}) + "\n")
            fh.flush()
            record = run_one(exp, host, arm, rnd, binaries[host], args.keep, budget)
            record.update({"id": attempt, "status": "done", "fingerprint": fp})
            history.append(record)
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            spent += record.get("cost_usd") or 0.0
            cost = f"${record['cost_usd']:.4f}" if record.get("cost_usd") is not None else "cost n/a"
            tokens = (f"{record['total_input']:>8} in {record['output']:>6} out" if record.get("total_input") is not None
                      else "   usage unknown")
            print(f"round {rnd} {host:<6} {arm['name']:<12} {'ok  ' if record['success'] else 'FAIL'} "
                  f"{tokens} {cost} {record['seconds']}s" + (f"  ({record['error']})" if record.get("error") else ""))
            if args.max_cost is not None and record.get("cost_usd") is None:
                print("stopped: that run's cost is unknown, so --max-cost can no longer be enforced.")
                stopped = True
                break
    report = build_report(load_lines(out_dir), exp)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"\nresults: {out_dir.as_posix()}/runs.jsonl, report: {out_dir.as_posix()}/report.md"
          + (" (partial: stopped before every run finished)" if stopped else ""))
    return EXIT_PARTIAL if stopped else 0


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


def bootstrap_paired(base: dict[int, float], other: dict[int, float], rng: random.Random) -> tuple[float, float] | None:
    """95% interval of the mean per-round difference (other - base). Arms run in the same rounds, so pairing by
    round removes drift that hits both arms alike."""
    rounds = sorted(set(base) & set(other))
    if len(rounds) < 3:
        return None
    diffs = [other[r] - base[r] for r in rounds]
    means = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(BOOTSTRAP_ITERS))
    return means[int(0.025 * BOOTSTRAP_ITERS)], means[int(0.975 * BOOTSTRAP_ITERS) - 1]


def by_round(group: list[dict], key: str) -> dict[int, float]:
    return {r["round"]: float(r[key]) for r in group if r.get(key) is not None}


def complete(group: list[dict], key: str) -> bool:
    return all(r.get(key) is not None for r in group)


def summarize(group: list[dict], key: str, rng: random.Random, money: bool = False,
              interval: bool = True) -> tuple[str, list[float] | None]:
    """Mean (and 95% interval) of a column, or "n/a" when any run lacks the value: averaging only the known
    runs would quietly favor the arm whose expensive runs failed to report."""
    missing = sum(1 for r in group if r.get(key) is None)
    if missing:
        return f"n/a ({missing} of {len(group)} unknown)", None
    values = [float(r[key]) for r in group]
    text = fmt_num(statistics.fmean(values), money)
    if interval and (ci := bootstrap_ci(values, rng)):
        text += f" {fmt_ci(ci, money)}"
    return text, values


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
             "Intervals are 95%. Success uses a Wilson interval at any run count (wide when runs are few); means and "
             "differences use a bootstrap and need at least 3 runs per arm. Unknown values show n/a and are never "
             "counted as zero.", ""]
    lost = interrupted_attempts(runs)
    if lost:
        lines += [f"**Note:** {len(lost)} call(s) were interrupted before recording a result. They may have cost money "
                  "and are not in the tables.", ""]
    runs = [r for r in runs if r.get("status") != "started"]
    fingerprints = {r.get("fingerprint") for r in runs if r.get("fingerprint")}
    if len(fingerprints) > 1:
        lines += [f"**Warning:** these runs come from {len(fingerprints)} different experiment versions; "
                  "compare them only with care.", ""]
    arm_order = [a["name"] for a in exp["arms"]]
    task = exp.get("task") or {}
    judged = bool(task.get("check") or task.get("expect"))
    outcome = "success" if judged else "completed"
    if not judged:
        lines += ["No `check` or `expect` is defined, so a run only counts as completed (the agent finished "
                  "without an error); it does not mean the answer was right.", ""]
    for host in [h for h in HOSTS if any(r["host"] == h for r in runs)]:
        lines += [f"## {host}", "",
                  f"| arm | runs | {outcome} [95% CI] | cost per run [95% CI] | cost per {outcome} | "
                  "total input [95% CI] | cached | output | turns | seconds | model |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        groups = {name: [r for r in runs if r["host"] == host and r["arm"] == name] for name in arm_order}
        for name, group in groups.items():
            if not group:
                continue
            n, ok = len(group), sum(1 for r in group if r.get("success"))
            low, high = wilson(ok, n)
            cost_text, costs = summarize(group, "cost_usd", rng, money=True)
            input_text, inputs = summarize(group, "total_input", rng)
            output_text, _ = summarize(group, "output", rng, interval=False)
            per_success = fmt_num(sum(costs) / ok, True) if costs is not None and ok else "n/a"
            reads = [r.get("cache_read") for r in group]
            cached = (f"{sum(reads) / sum(inputs):.0%}" if inputs and sum(inputs) and None not in reads else "n/a")
            models = ",".join(sorted({r.get("model") for r in group if r.get("model")})) or "-"
            turns = ("n/a" if not complete(group, "turns")
                     else f"{statistics.fmean([float(r['turns']) for r in group]):.1f}")
            lines.append(
                f"| {name} | {n} | {ok}/{n} [{low:.0%}, {high:.0%}] | {cost_text} | {per_success} | {input_text} | "
                f"{cached} | {output_text} | {turns} | "
                f"{statistics.fmean([float(r.get('seconds') or 0) for r in group]):.1f} | {models} |")
        base_name = next((name for name in arm_order if groups.get(name)), None)
        others = [name for name in arm_order if name != base_name and groups.get(name)]
        if base_name and others:
            lines += ["", f"Difference from `{base_name}`, paired by round (negative means the arm uses less):", "",
                      "| arm | cost per run [95% CI] | verdict | total input [95% CI] | verdict |", "|---|---|---|---|---|"]
            base = groups[base_name]
            for name in others:
                group = groups[name]
                cells = []
                for key, money in (("cost_usd", True), ("total_input", False)):
                    if not (complete(base, key) and complete(group, key)):
                        cells += ["n/a (some values unknown)", "not comparable"]
                        continue
                    ci = bootstrap_paired(by_round(base, key), by_round(group, key), rng)
                    cells += [fmt_ci(ci, money) or "n/a", verdict(ci)]
                lines.append(f"| {name} | " + " | ".join(cells) + " |")
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
    runs = load_lines(out_dir)
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
            p.add_argument("--max-cost", type=float, help="total budget in USD; Claude calls get the remainder as "
                                                           "--max-budget-usd, Codex runs are checked afterwards")
            p.add_argument("--allow-overrun", action="store_true",
                           help="accept that a Codex run may exceed --max-cost before it can be checked")
            p.add_argument("--keep", action="store_true", help="keep each run's workspace for inspection")
            p.add_argument("--seed", type=int, default=0, help="seed for the arm order")
    p = sub.add_parser("report")
    p.add_argument("results")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "max_cost", None) is not None and not (math.isfinite(args.max_cost) and args.max_cost >= 0):
            raise BenchError("--max-cost must be a finite number >= 0")  # NaN would never trigger the stop
        return {"plan": cmd_plan, "run": cmd_run, "report": cmd_report}[args.cmd](args)
    except BenchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
