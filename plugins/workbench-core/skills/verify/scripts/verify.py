#!/usr/bin/env python3
"""Evidence that a change was checked, and a merge gate that demands it. Stdlib only.

  python verify.py run --host claude|codex|human   run the checks in .ai/verify.json, then record exit codes,
                                                   timings and output hashes for the current content
  python verify.py collect                         add matching cross-review records without running checks
  python verify.py gate [--rev REV] [--base REF]   exit 1 unless REV's latest run passed and, when required,
                                                   its latest review by another agent says "ship" (and, with
                                                   --base, covers the whole change since REF)
  python verify.py show [--rev REV]                print the evidence for that content
  python verify.py content [--rev REV]             print the content id

The content id is the git tree of everything except .ai/ (coordination state), keeping .ai/verify.json so
that weakening the checks invalidates old evidence. It is computed the same way for a working tree and
for a commit. `run` requires a clean tree, so evidence always describes a commit; any later change to
code or checks invalidates it. Evidence lives in .ai/verifications/<content id>.json and is committed.
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
import uuid
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
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_at(value: str | None) -> dt.datetime:
    """Accepts timestamps with or without fractional seconds; unknown sorts first."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return dt.datetime.strptime(value or "", fmt)
        except ValueError:
            continue
    return dt.datetime.min


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
        git(top, "rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", EXCLUDE, f":(exclude){CONFIG}", env=env)
        return git(top, "write-tree", env=env)


def uncommitted(top: Path) -> list[str]:
    """Changes that would make the working tree differ from HEAD's content (ignoring .ai/ except the config)."""
    lines = git(top, "status", "--porcelain", "--", ".", f":(exclude){EXCLUDE}", check=False).splitlines()
    lines += git(top, "status", "--porcelain", "--", CONFIG, check=False).splitlines()
    return [line for line in lines if line.strip()]


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


def review_key(review: dict) -> tuple:
    return (review.get("id"),) if review.get("id") else (review.get("at"), review.get("peer"))


def merge_reviews(evidence: dict, reviews: list[dict]) -> int:
    seen = {review_key(r) for r in evidence["reviews"]}
    added = []
    for review in reviews:
        if review_key(review) not in seen:
            seen.add(review_key(review))
            added.append(review)
    evidence["reviews"].extend(added)
    return len(added)


def committed_reviews(top: Path, rev: str, cid: str) -> list[dict]:
    """Review records committed under .ai/reviews/ at `rev` (normally none: the folder is gitignored)."""
    found = []
    for name in git(top, "ls-tree", "--name-only", f"{rev}:{REVIEWS_DIR}", check=False).splitlines():
        if name.endswith(".json"):
            try:
                review = json.loads(read_file(top, f"{REVIEWS_DIR}/{name}", rev) or "{}")
            except ValueError:
                continue
            if review.get("content") == cid:
                found.append(review)
    return found


def run_check(top: Path, check: dict) -> dict:
    start = time.monotonic()
    try:
        r = subprocess.run(check["run"], shell=True, cwd=top, capture_output=True,
                           timeout=check.get("timeout", DEFAULT_TIMEOUT))
        code, raw = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        code, raw = None, f"timed out after {check.get('timeout', DEFAULT_TIMEOUT)}s".encode()
    text = raw.decode("utf-8", errors="replace")
    return {"name": check["name"], "run": check["run"], "exit": code,
            "seconds": round(time.monotonic() - start, 1),
            "output_sha256": hashlib.sha256(raw).hexdigest(),  # the exact bytes, before any newline handling
            "tail": text.strip().splitlines()[-TAIL_LINES:]}


# ---------- commands ----------

def cmd_run(args) -> int:
    top = repo_top()
    dirty = uncommitted(top)
    if dirty:
        raise VerifyError("commit the change first, so the evidence describes exactly what is committed. "
                          f"Uncommitted: {', '.join(line[3:] for line in dirty[:5])}")
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
    run = {"id": uuid.uuid4().hex, "at": now(), "host": args.host,
           "head": git(top, "rev-parse", "HEAD", check=False) or None,
           "branch": git(top, "rev-parse", "--abbrev-ref", "HEAD", check=False) or None,
           "platform": sys.platform, "python": platform.python_version(),
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


def latest(items: list[dict]) -> dict | None:
    """Newest by timestamp; records with the same timestamp are ordered by their position."""
    if not items:
        return None
    return max(enumerate(items), key=lambda pair: (parse_at(pair[1].get("at")), pair[0]))[1]


def is_ancestor(top: Path, older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", str(top), "merge-base", "--is-ancestor", older, newer],
                          capture_output=True).returncode == 0


def check_run(evidence: dict, required: list[tuple[str, str]]) -> str | None:
    """The latest run decides: an older pass does not outweigh a newer failure. A required check matches by
    name and command, so a same-named no-op does not count."""
    names = ", ".join(name for name, _ in required) or "none configured"
    run = latest(evidence.get("runs", []))
    if not run:
        return f"no recorded run of the required checks ({names})"
    passed = {(c.get("name"), c.get("run")) for c in run.get("checks", []) if c.get("exit") == 0}
    missing = [f"{name} (`{cmd}`)" for name, cmd in required if (name, cmd) not in passed]
    if not run.get("passed") or missing:
        return (f"the latest run ({run.get('host')} on {run.get('platform')} at {run.get('at')}) did not pass"
                + (f": {', '.join(missing)}" if missing else ""))
    print(f"ok    latest run passed {names} ({run.get('host')} on {run.get('platform')} at {run.get('at')})")
    return None


def check_review(top: Path, reviews: list[dict], cid: str, rev: str, base: str | None) -> str | None:
    """The latest complete-or-not review by an agent other than the author decides: it must say ship, must have
    seen every change, and with --base must cover the whole change since the base."""
    independent = [r for r in reviews
                   if r.get("content") == cid and r.get("peer") and r.get("peer") != r.get("author")]
    review = latest(independent)
    if not review:
        found = [f"{r.get('verdict')} by {r.get('peer')} for {r.get('author')}" for r in reviews]
        return ("no review of this content by an agent other than its author"
                + (f" (found: {', '.join(found)})" if found else "") + ". Run cross-review, then `verify.py collect`")
    if review.get("verdict") != "ship":
        return (f"the latest independent review ({review.get('peer')} at {review.get('at')}) says "
                f"{review.get('verdict')}. Fix the findings, review again until ship, then `verify.py collect`")
    if review.get("complete") is not True:
        return (f"the latest independent review ({review.get('peer')} at {review.get('at')}) did not see every "
                "change (truncated, or new files omitted). Review again with a larger --max-chars")
    if base:
        merge_base = git(top, "merge-base", base, rev, check=False)
        reviewed_from = review.get("base_sha")
        if not merge_base or not reviewed_from or not is_ancestor(top, reviewed_from, merge_base):
            return (f"the ship review compared against {reviewed_from or 'an unknown base'}, which does not cover "
                    f"the whole change since {base}. Review again with `--base {base}`")
    print(f"ok    latest independent review: ship by {review.get('peer')} (author {review.get('author')}) "
          f"at {review.get('at')}" + ("" if base else "; coverage not checked (pass --base)"))
    return None


def cmd_gate(args) -> int:
    top = repo_top()
    rev = args.rev or "HEAD"
    cid = content_id(top, rev)
    config = load_config(top, rev)
    required = [(c["name"], c["run"]) for c in config["checks"]]
    require_review = config["require_review"]
    print(f"content {cid[:12]} at {rev}")
    if args.base:  # the PR may tighten the policy of its base, never loosen it
        merge_base = git(top, "merge-base", args.base, rev, check=False)
        base_config = load_config(top, merge_base) if merge_base else {"checks": [], "require_review": False}
        required += [(c["name"], c["run"]) for c in base_config["checks"] if (c["name"], c["run"]) not in required]
        require_review = require_review or base_config["require_review"]
        print(f"policy checks: {', '.join(name for name, _ in required) or '(none)'}; review required: "
              f"{require_review} (at least the rules of {args.base})")
    evidence = load_evidence(top, cid, rev)
    problems = []
    if not evidence:
        problems.append(f"no evidence for this content. Commit the change, run `verify.py run --host <you>`, "
                        f"and commit {EVIDENCE_DIR}/")
    elif evidence.get("content") != cid:
        problems.append(f"{evidence_rel(cid)} records content {evidence.get('content')}, not {cid}")
    else:
        problems.append(check_run(evidence, required))
        if require_review:
            pool = {"reviews": list(evidence.get("reviews", []))}
            merge_reviews(pool, committed_reviews(top, rev, cid))
            if git(top, "rev-parse", rev, check=False) == git(top, "rev-parse", "HEAD", check=False):
                merge_reviews(pool, matching_reviews(top, cid))  # local reviews nobody collected yet
            problems.append(check_review(top, pool["reviews"], cid, rev, args.base))
    problems = [p for p in problems if p]
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
        if name == "gate":
            p.add_argument("--base", help="the PR base: the ship review must cover every change since it")
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
