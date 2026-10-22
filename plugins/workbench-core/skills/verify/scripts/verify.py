#!/usr/bin/env python3
"""Evidence that a change was checked, and a merge gate that demands it. Stdlib only.

  python verify.py run --host claude|codex|human   run the checks in .ai/verify.json, then record exit codes,
                                                   timings and output hashes for the current content
  python verify.py collect                         add matching cross-review records without running checks
  python verify.py gate [--rev REV]                exit 1 unless REV's content has a passing run and, when
                                                   required, a "ship" review by an agent other than its author
  python verify.py show [--rev REV]                print the evidence for that content
  python verify.py content [--rev REV]             print the content id

The content id is the git tree of everything except .ai/ (coordination state). It is computed the same
way for an uncommitted working tree and for a commit, so evidence gathered before committing still
matches the commit, and any later code change invalidates it. Evidence lives in
.ai/verifications/<content id>.json and is meant to be committed with the change.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

EXCLUDE = ".ai"
CONFIG = ".ai/verify.json"
EVIDENCE_DIR = ".ai/verifications"
REVIEWS_DIR = ".ai/reviews"
TAIL_LINES = 15
DEFAULT_TIMEOUT = 900


class VerifyError(Exception):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(top: Path, *args: str, env: dict | None = None, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(top), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    if check and r.returncode != 0:
        raise VerifyError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else ""


def repo_top() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        raise VerifyError("not inside a git repository")
    return Path(r.stdout.strip())


def has_commit(top: Path, rev: str) -> bool:
    return bool(git(top, "rev-parse", "--verify", "-q", rev + "^{commit}", check=False))


def content_id(top: Path, rev: str | None = None) -> str:
    """Tree id of the repo without .ai/, for `rev`, or for the working tree when rev is None.
    Keep in sync with content_id() in cross-review/scripts/ask_peer.py."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        base = rev or "HEAD"
        if has_commit(top, base):
            git(top, "read-tree", base, env=env)
        elif rev:
            raise VerifyError(f"unknown revision {rev}")
        else:
            git(top, "read-tree", "--empty", env=env)
        if rev is None:
            git(top, "add", "-A", env=env)  # the working tree as it would be committed (.gitignore applies)
        git(top, "rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", EXCLUDE, env=env)
        return git(top, "write-tree", env=env)


def read_file(top: Path, rel: str, rev: str | None) -> str | None:
    if rev:
        r = subprocess.run(["git", "-C", str(top), "show", f"{rev}:{rel}"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 else None
    path = top / rel
    return path.read_text(encoding="utf-8") if path.is_file() else None


def load_config(top: Path, rev: str | None) -> dict:
    text = read_file(top, CONFIG, rev)
    config = json.loads(text) if text else {}
    checks = config.get("checks", [])
    for check in checks:
        if not isinstance(check, dict) or not check.get("name") or not check.get("run"):
            raise VerifyError(f"{CONFIG}: every check needs a name and a run command")
    return {"checks": checks, "require_review": bool(config.get("require_review", False))}


def evidence_rel(cid: str) -> str:
    return f"{EVIDENCE_DIR}/{cid}.json"


def load_evidence(top: Path, cid: str, rev: str | None = None) -> dict | None:
    text = read_file(top, evidence_rel(cid), rev)
    return json.loads(text) if text else None


def save_evidence(top: Path, evidence: dict) -> Path:
    path = top / evidence_rel(evidence["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return path


def matching_reviews(top: Path, cid: str) -> list[dict]:
    found = []
    for f in sorted((top / REVIEWS_DIR).glob("*.json")):
        try:
            review = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if review.get("content") == cid:
            found.append(review)
    return found


def merge_reviews(evidence: dict, reviews: list[dict]) -> int:
    seen = {(r.get("at"), r.get("peer")) for r in evidence["reviews"]}
    added = [r for r in reviews if (r.get("at"), r.get("peer")) not in seen]
    evidence["reviews"].extend(added)
    return len(added)


def run_check(top: Path, check: dict) -> dict:
    start = time.monotonic()
    try:
        r = subprocess.run(check["run"], shell=True, cwd=top, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=check.get("timeout", DEFAULT_TIMEOUT))
        code, output = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        code, output = None, f"timed out after {check.get('timeout', DEFAULT_TIMEOUT)}s"
    return {"name": check["name"], "run": check["run"], "exit": code,
            "seconds": round(time.monotonic() - start, 1),
            "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "tail": output.strip().splitlines()[-TAIL_LINES:]}


# ---------- commands ----------

def cmd_run(args) -> int:
    top = repo_top()
    cid = content_id(top)
    config = load_config(top, None)
    if not config["checks"]:
        print(f"no checks in {CONFIG}; recording reviews only")
    results = []
    for check in config["checks"]:
        result = run_check(top, check)
        results.append(result)
        print(f"{'pass' if result['exit'] == 0 else 'FAIL'}  {result['name']}  "
              f"({result['seconds']}s, exit {result['exit']})")
    if content_id(top) != cid:
        raise VerifyError("the content changed while the checks ran (a check wrote files?); fix that and run again")
    status = git(top, "status", "--porcelain", "--", ".", f":(exclude){EXCLUDE}", check=False)
    run = {"at": now(), "host": args.host, "head": git(top, "rev-parse", "HEAD", check=False) or None,
           "branch": git(top, "rev-parse", "--abbrev-ref", "HEAD", check=False) or None,
           "uncommitted": bool(status), "platform": sys.platform, "python": platform.python_version(),
           "checks": results, "passed": all(r["exit"] == 0 for r in results)}
    evidence = load_evidence(top, cid) or {"content": cid, "runs": [], "reviews": []}
    evidence["runs"].append(run)
    added = merge_reviews(evidence, matching_reviews(top, cid))
    path = save_evidence(top, evidence)
    print(f"{'PASSED' if run['passed'] else 'FAILED'}: evidence for content {cid[:12]} saved to "
          f"{path.relative_to(top).as_posix()} ({added} new review(s)). Commit it with the change.")
    return 0 if run["passed"] else 1


def cmd_collect(_args) -> int:
    top = repo_top()
    cid = content_id(top)
    evidence = load_evidence(top, cid) or {"content": cid, "runs": [], "reviews": []}
    added = merge_reviews(evidence, matching_reviews(top, cid))
    save_evidence(top, evidence)
    print(f"added {added} review(s) to the evidence for content {cid[:12]}")
    return 0


def shipping_reviews(evidence: dict) -> list[dict]:
    return [r for r in evidence.get("reviews", [])
            if r.get("verdict") == "ship" and r.get("peer") and r.get("peer") != r.get("author")]


def passing_runs(evidence: dict, required: list[str]) -> list[dict]:
    good = []
    for run in evidence.get("runs", []):
        passed = {c["name"] for c in run.get("checks", []) if c.get("exit") == 0}
        if run.get("passed") and set(required) <= passed:
            good.append(run)
    return good


def cmd_gate(args) -> int:
    top = repo_top()
    rev = args.rev or "HEAD"
    cid = content_id(top, rev)
    config = load_config(top, rev)
    evidence = load_evidence(top, cid, rev)
    required = [c["name"] for c in config["checks"]]
    print(f"content {cid[:12]} at {rev}")
    problems = []
    if not evidence:
        problems.append(f"no evidence for this content. Run `verify.py run --host <you>` after the last change "
                        f"and commit {EVIDENCE_DIR}/")
    else:
        runs = passing_runs(evidence, required)
        if runs:
            last = runs[-1]
            print(f"ok    checks passed: {', '.join(required) or '(none required)'} "
                  f"(run by {last.get('host')} on {last.get('platform')} at {last.get('at')})")
        else:
            problems.append(f"no run where every required check passed ({', '.join(required)})")
        if config["require_review"]:
            ships = shipping_reviews(evidence)
            if ships:
                r = ships[-1]
                print(f"ok    review: ship by {r.get('peer')} (author {r.get('author')}) at {r.get('at')}")
            else:
                verdicts = [f"{r.get('verdict')} by {r.get('peer')}" for r in evidence.get("reviews", [])]
                problems.append("no 'ship' review of this exact content by an agent other than its author"
                                + (f" (found: {', '.join(verdicts)})" if verdicts else "")
                                + ". Run cross-review, fix the findings, re-review until ship, then `verify.py collect`")
    for problem in problems:
        print(f"FAIL  {problem}")
    print("PASS" if not problems else "BLOCKED")
    return 1 if problems else 0


def cmd_show(args) -> int:
    top = repo_top()
    cid = content_id(top, args.rev)
    evidence = load_evidence(top, cid, args.rev)
    print(f"content {cid}")
    if not evidence:
        print("no evidence recorded")
        return 1
    for run in evidence["runs"]:
        checks = ", ".join(f"{c['name']}={c['exit']}" for c in run["checks"])
        print(f"run     {run['at']} {run['host']} {run['platform']} {'passed' if run['passed'] else 'FAILED'}: {checks}")
    for r in evidence["reviews"]:
        print(f"review  {r.get('at')} {r.get('peer')} for {r.get('author')}: {r.get('verdict')} "
              f"({r.get('findings')} finding(s), confidence {r.get('confidence')})")
    return 0


def cmd_content(args) -> int:
    print(content_id(repo_top(), args.rev))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--host", required=True, help="who runs the checks: claude, codex or human")
    sub.add_parser("collect")
    for name in ("gate", "show", "content"):
        p = sub.add_parser(name)
        p.add_argument("--rev", help="commit to check (default: HEAD for gate, the working tree otherwise)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    commands = {"run": cmd_run, "collect": cmd_collect, "gate": cmd_gate, "show": cmd_show, "content": cmd_content}
    try:
        return commands[args.cmd](args)
    except VerifyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
