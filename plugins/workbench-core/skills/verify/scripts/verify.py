#!/usr/bin/env python3
"""Evidence that a change was checked, and a merge gate that demands it. Stdlib only.

  python verify.py run --host H [--base REF]       run the checks in .ai/verify.json (plus REF's, with --base),
                                                   then record exit codes, timings and output hashes
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
import re
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
    checks = list(config["checks"])
    if args.base:  # also run the base's checks, so a changed or removed check is still proven against the old rule
        if not has_commit(top, args.base):
            raise VerifyError(f"unknown base {args.base}")
        own = {(c["name"], c["run"]) for c in checks}
        extra = [c for c in load_config(top, args.base)["checks"] if (c["name"], c["run"]) not in own]
        if extra:
            print(f"also running {len(extra)} check(s) required by {args.base}")
        checks += extra
    if not checks:
        print(f"no checks in {CONFIG}; recording reviews only")
    results = []
    for check in checks:
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


CODEX_BOT = "chatgpt-codex-connector[bot]"
# Codex's GitHub integration keeps a machine-readable state in its PR summary comment.
CODEX_STATE_RE = re.compile(r"<!--\s*codex-review-state:v1\s+(\{.*?\})\s*-->", re.S)
CODEX_DONE = {"completed", "complete", "done", "finished", "succeeded", "success"}


def fetch_pr_comments(repo: str, pr: int) -> list[dict]:
    """Issue comments on a pull request through the gh CLI (GH_TOKEN in CI). Tests replace this."""
    jq = ".[] | {id, login: .user.login, type: .user.type, body, created_at, updated_at, html_url} | @json"
    try:
        r = subprocess.run(["gh", "api", "--paginate", f"repos/{repo}/issues/{pr}/comments", "--jq", jq],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise VerifyError("--github-pr needs the gh CLI")
    if r.returncode != 0:
        raise VerifyError(f"could not read the comments of {repo}#{pr}: {r.stderr.strip()[:300]}")
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


def fetch_pr_base_changes(repo: str, pr: int) -> list[str]:
    """When the pull request's base branch was changed (retargeted), from its timeline. Tests replace this."""
    jq = '.[] | select(.event == "base_ref_changed") | .created_at'
    try:
        r = subprocess.run(["gh", "api", "--paginate", f"repos/{repo}/issues/{pr}/timeline", "--jq", jq],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise VerifyError("--github-pr needs the gh CLI")
    if r.returncode != 0:
        raise VerifyError(f"could not read the timeline of {repo}#{pr}: {r.stderr.strip()[:300]}")
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def parse_time(text) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def review_start(comment: dict) -> dt.datetime | None:
    """When the latest Codex review started: the earliest time the summary itself states (the table row of the
    running or finished review), otherwise when the comment was last updated."""
    stated = [t for t in map(parse_time, re.findall(r'datetime="([^"]+)"', comment.get("body") or "")) if t]
    return min(stated) if stated else parse_time(comment.get("updated_at") or comment.get("created_at"))


def change_authors(top: Path, base: str | None, rev: str) -> set[str]:
    """Agents credited in the commits since the base: a Claude co-author trailer, or Codex in the author or a
    trailer. Used so that Codex cannot supply the independent review of its own change."""
    merge_base = git(top, "merge-base", base, rev, check=False) if base else ""
    if not merge_base:
        return set()
    log = git(top, "log", "--format=%an <%ae>%x00%B%x1e", f"{merge_base}..{rev}", check=False)
    authors = set()
    for entry in log.split("\x1e"):
        who, _, body = entry.partition("\x00")
        if re.search(r"^co-authored-by:.*claude", body, re.I | re.M):
            authors.add("claude")
        if re.search(r"codex", who, re.I) or re.search(r"^co-authored-by:.*codex", body, re.I | re.M):
            authors.add("codex")
    return authors


def codex_github_reviews(top: Path, comments: list[dict], bots: set[str], rev: str, base: str | None,
                         base_changes: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """Codex's review state on the pull request, as review records the gate already understands. A GitHub
    review covers the pull request as it was when the review started. The reviewed base is not recorded, so a
    change of the PR's base after that moment (which can add unreviewed commits) makes the review stale."""
    authors = change_authors(top, base, rev)
    author = "codex" if "codex" in authors else (",".join(sorted(authors)) or "unknown")
    merge_base = git(top, "merge-base", base, rev, check=False) if base else None
    records, notes = [], []
    for comment in comments:
        if comment.get("login") not in bots or comment.get("type") != "Bot":
            continue  # only the integration's own account can speak for Codex
        match = CODEX_STATE_RE.search(comment.get("body") or "")
        if not match:
            continue
        try:
            state = json.loads(match.group(1)).get("code_review") or {}
        except (ValueError, AttributeError):
            continue
        sha, status = state.get("commit_sha"), str(state.get("status") or "").lower()
        if status not in CODEX_DONE:
            verdict = None
        elif state.get("has_unresolved_findings") is False or (
                state.get("has_unresolved_findings") is None and state.get("has_any_findings") is False):
            verdict = "ship"
        else:
            verdict = "fix-first"
        content = content_id(top, sha) if isinstance(sha, str) and has_commit(top, sha) else None
        start, stale = review_start(comment), None
        for changed in base_changes or []:
            when = parse_time(changed)
            if start is None or when is None or when > start:  # unknown times count against the review
                stale = f"the pull request's base branch was changed ({changed}) after this review started"
                break
        notes.append(f"Codex on GitHub: {status or 'unknown status'} for {str(sha)[:10]}"
                     + (f", {verdict}" if verdict else "") + ("" if content else " (commit not available here)")
                     + (", stale" if stale else ""))
        records.append({"id": f"github-{comment.get('id')}-{sha}", "at": comment.get("updated_at") or comment.get("created_at"),
                        "peer": "codex", "author": author, "content": content, "verdict": verdict,
                        "complete": True, "base_sha": merge_base, "source": "github", "status": status,
                        "stale_reason": stale, "url": comment.get("html_url")})
    return records, notes


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
    if review.get("stale_reason"):
        return (f"Codex's GitHub review of this content is stale: {review['stale_reason']}, so it may not cover the "
                "whole change. Comment @codex review on the PR again")
    if review.get("source") == "github" and review.get("verdict") is None:
        return (f"Codex's GitHub review of this content is {review.get('status') or 'not finished'}; "
                "re-run this check once it completes")
    if review.get("verdict") != "ship":
        return (f"the latest independent review ({review.get('peer')} at {review.get('at')}) says "
                f"{review.get('verdict')}. Fix the findings, review again until ship, then `verify.py collect`"
                + (" (or comment @codex review on the PR again)" if review.get("source") == "github" else ""))
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
    if args.github_pr is not None and not args.base:
        # Without the base there is no way to tell which commits are the change, so Codex could not be recognised
        # as its author, and a GitHub review could not be tied to what the pull request contains.
        raise VerifyError("--github-pr needs --base (the pull request's base commit or branch)")
    top = repo_top()
    rev = args.rev or "HEAD"
    cid = content_id(top, rev)
    config = load_config(top, rev)
    required = [(c["name"], c["run"]) for c in config["checks"]]
    require_review = config["require_review"]
    print(f"content {cid[:12]} at {rev}")
    if args.base:  # the PR may tighten the current base's policy, never loosen it
        if not has_commit(top, args.base):
            raise VerifyError(f"unknown base {args.base}")
        base_config = load_config(top, args.base)
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
            if args.github_pr is not None:
                repo = args.github_repo or os.environ.get("GITHUB_REPOSITORY")
                if not repo:
                    raise VerifyError("--github-pr needs --github-repo owner/name (CI sets GITHUB_REPOSITORY)")
                bots = set(args.codex_bot or [CODEX_BOT])
                records, notes = codex_github_reviews(top, fetch_pr_comments(repo, args.github_pr), bots, rev,
                                                      args.base, fetch_pr_base_changes(repo, args.github_pr))
                for note in notes:
                    print(f"note  {note}")
                merge_reviews(pool, records)
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
    p.add_argument("--base", help="also run the checks this branch must still satisfy, e.g. main")
    sub.add_parser("collect")
    for name in ("gate", "show", "content"):
        p = sub.add_parser(name)
        p.add_argument("--rev", help="commit to check (default: HEAD for gate, the working tree otherwise)")
        if name == "gate":
            p.add_argument("--base", help="the PR base: the ship review must cover every change since it")
            p.add_argument("--github-pr", type=int, help="also accept Codex's review on this GitHub pull request")
            p.add_argument("--github-repo", help="owner/name for --github-pr (default: $GITHUB_REPOSITORY)")
            p.add_argument("--codex-bot", action="append",
                           help=f"GitHub login of the Codex integration (default: {CODEX_BOT}); repeatable")
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
