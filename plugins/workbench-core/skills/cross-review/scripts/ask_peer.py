#!/usr/bin/env python3
"""Ask the other coding agent (Codex <-> Claude Code) for a read-only review.

  python ask_peer.py --goal "what the change is for"         # uncommitted diff
  python ask_peer.py --base main --goal "..."                # diff against a branch
  python ask_peer.py --question "why does X fail?" --files a.py b.py

Prints the peer's verdict and saves it under .ai/reviews/ as markdown plus a JSON record (verdict, peer,
author, content id) that verify.py can attach as evidence. Stdlib only.
Exit codes: 0 ok, 1 peer failed, 2 peer CLI not found, 3 nothing to review.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REVIEW_FORMAT = """Reply in exactly this format and nothing else:
FINDINGS (at most 8, most severe first; write "none" if there are none):
- [high|med|low] path:line — problem — why it matters — fix
VERDICT: ship | fix-first | rethink
CONFIDENCE: high | med | low"""

ANSWER_FORMAT = """Reply in exactly this format and nothing else:
ANSWER: at most 15 lines, concrete, naming files and lines where relevant
CONFIDENCE: high | med | low"""

PRINT_LINES = 60
MAX_NEW_FILES = 50
MAX_NEW_FILE_BYTES = 100_000


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


class UsageError(Exception):
    pass


def git_ok(*args: str) -> tuple[bool, str]:
    try:
        r = run(["git", *args])
    except FileNotFoundError:
        return False, ""
    return r.returncode == 0, r.stdout


def git(*args: str) -> str:
    ok, out = git_ok(*args)
    return out if ok else ""


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated {len(text) - limit} chars; read the files yourself if you need more]"


def first_file(candidates: list[str | None]) -> str | None:
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def find_codex() -> str | None:
    cands = [os.environ.get("WB_CODEX_BIN"), shutil.which("codex")]
    local = os.environ.get("LOCALAPPDATA")
    if local:  # Codex desktop app on Windows ships the CLI here; the hash dir changes on update.
        pattern = os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe")
        cands += sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    cands.append("/Applications/Codex.app/Contents/Resources/codex")
    return first_file(cands)


def find_claude() -> str | None:
    exe = "claude.exe" if os.name == "nt" else "claude"
    return first_file([os.environ.get("WB_CLAUDE_BIN"), shutil.which("claude"), str(Path.home() / ".local" / "bin" / exe)])


def build_context(args) -> tuple[str, str, bool | None]:
    """Returns (context, reply format, complete). `complete` is False when the peer cannot see every
    change (a truncated diff, or new files that are too big, binary or too many), so the review
    must not count as evidence for the whole content."""
    if args.question:
        parts = [f"QUESTION: {args.question}"]
        for f in args.files or []:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
            parts.append(f"--- FILE {f} ---\n{text}")
        return "\n\n".join(parts), ANSWER_FORMAT, None
    if args.base:
        ok, diff = git_ok("diff", args.base)
        if not ok:
            raise UsageError(f"git diff {args.base} failed: is '{args.base}' a valid ref?")
    else:
        diff = git("diff", "HEAD") or (git("diff", "--cached") + git("diff"))  # no HEAD yet in a new repo
    untracked = [p for p in git("ls-files", "-z", "--others", "--exclude-standard").split("\0") if p]
    if not diff.strip() and not untracked:
        return "", "", None
    binary_change = re.search(r"^(Binary files .+ differ|GIT binary patch)$", diff, re.M)
    complete = len(untracked) <= MAX_NEW_FILES and not binary_change  # git shows no content for binary changes
    new_files = []
    for rel in untracked[:MAX_NEW_FILES]:
        try:
            data = Path(rel).read_bytes()
        except OSError:
            complete = False
            continue
        if len(data) > MAX_NEW_FILE_BYTES or b"\0" in data:
            complete = False
            continue
        new_files.append(f"--- NEW FILE {rel} ---\n{data.decode('utf-8', errors='replace')}")
    head = f"GOAL OF THE CHANGE: {args.goal or '(not given)'}"
    tail = "UNTRACKED FILES:\n" + "\n".join(untracked[:MAX_NEW_FILES]) if untracked else ""
    body = "\n".join([diff or "(no tracked changes)"] + new_files)
    # Truncate only the changes so the goal and the list of new files always reach the peer.
    budget = max(1000, args.max_chars - len(head) - len(tail) - 200)
    if len(body) > budget:
        complete = False
    body = truncate(body, budget)
    return "\n".join(p for p in (head, "--- CHANGES ---", body, tail) if p), REVIEW_FORMAT, complete


def evidence_complete(complete: bool | None, context: str, max_chars: int, cid_before: str | None,
                      cid_after: str | None) -> bool | None:
    """A review counts as evidence only if the peer got every change in the prompt actually sent and the
    content did not change while it was reviewing."""
    if complete is None:
        return None
    return bool(complete and len(context) <= max_chars and cid_before and cid_before == cid_after)


def build_prompt(context: str, fmt: str, max_chars: int) -> str:
    context = truncate(context, max_chars)
    return (
        "You are a senior engineer giving a second opinion to another coding agent.\n"
        "Read-only: do not modify any file. Read files in the working directory only when the "
        "context below is not enough, and do not explore unrelated code.\n\n"
        f"{context}\n\n{fmt}\n"
    )


def peer_command(peer: str, binary: str, model: str | None, out_file: str) -> list[str]:
    if peer == "codex":
        cmd = [binary, "exec", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "-o", out_file]
        if model:
            cmd += ["-m", model]
        return cmd + ["-"]  # prompt on stdin: avoids the Windows command-line length limit
    cmd = [binary, "-p", "--tools", "Read,Grep,Glob", "--permission-mode", "dontAsk",
           "--no-session-persistence", "--output-format", "text"]
    if model:
        cmd += ["--model", model]
    return cmd


def content_id(root: Path) -> str | None:
    """Tree id of the working tree without .ai/ (but with .ai/verify.json).
    Keep in sync with content_id() in verify/scripts/verify.py."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        start = ["read-tree", "HEAD"] if git("rev-parse", "--verify", "-q", "HEAD^{commit}").strip() \
            else ["read-tree", "--empty"]
        out = ""
        for step in (start, ["add", "-A"],
                     ["rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", ".ai", ":(exclude).ai/verify.json"],
                     ["write-tree"]):
            r = run(["git", "-C", str(root), *step], env=env)
            if r.returncode != 0:
                return None
            out = r.stdout.strip()
        return out or None


def detect_host() -> str:
    if os.environ.get("CLAUDECODE"):
        return "claude"
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SANDBOX"):
        return "codex"
    return "human"


def parse_verdict(text: str) -> tuple[str | None, str | None, int]:
    verdict = re.search(r"^\s*VERDICT:\s*(ship|fix-first|rethink)\b", text, re.M | re.I)
    confidence = re.search(r"^\s*CONFIDENCE:\s*(high|med|low)\b", text, re.M | re.I)
    findings = len(re.findall(r"^\s*- \[(?:high|med|low)\]", text, re.M | re.I))
    return (verdict.group(1).lower() if verdict else None,
            confidence.group(1).lower() if confidence else None, findings)


def save_review(root: Path, peer: str, author: str, cid: str | None, complete: bool | None, args,
                reply: str) -> tuple[Path, Path]:
    stamp = dt.datetime.now(dt.timezone.utc)
    out_dir = root / ".ai" / "reviews"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stamp:%Y%m%d-%H%M%S-%f}-{peer}.md"
    sha = git("rev-parse", "HEAD").strip()
    scope = f"question: {args.question}" if args.question else f"diff vs {args.base or 'HEAD (uncommitted)'}"
    header = (f"# Cross-review by {peer} — {stamp:%Y-%m-%d %H:%M} UTC @ {sha[:7] or 'no-commit'}\n"
              f"- scope: {scope}\n- goal: {args.goal or '-'}\n- content: {cid or 'unknown'}\n\n")
    path.write_text(header + reply.strip() + "\n", encoding="utf-8")
    verdict, confidence, findings = parse_verdict(reply) if not args.question else (None, None, 0)
    base_sha = None if args.question else (git("rev-parse", args.base).strip() if args.base else sha) or None
    record = {"id": uuid.uuid4().hex, "at": f"{stamp:%Y-%m-%dT%H:%M:%S.%fZ}", "peer": peer, "author": author,
              "content": cid, "complete": complete, "head": sha or None, "base_sha": base_sha, "scope": scope,
              "goal": args.goal, "verdict": verdict,
              "confidence": confidence, "findings": findings, "review_file": path.name}
    json_path = path.with_suffix(".json")
    json_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path, json_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--peer", choices=["auto", "codex", "claude"], default="auto")
    ap.add_argument("--goal", help="one line: what the change is for")
    ap.add_argument("--base", help="review the diff against this ref instead of uncommitted changes")
    ap.add_argument("--question", help="ask a question instead of reviewing a diff")
    ap.add_argument("--files", nargs="*", help="files to include with --question")
    ap.add_argument("--model", help="model for the peer")
    ap.add_argument("--max-chars", type=int, default=60000, help="cap on context sent (default 60000, about 15k tokens)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds (default 600)")
    ap.add_argument("--dry-run", action="store_true", help="print the command and prompt size; do not call the peer")
    ap.add_argument("--author", choices=["claude", "codex", "human"], help="who wrote the change (default: detected)")
    args = ap.parse_args()

    peer = args.peer if args.peer != "auto" else ("codex" if os.environ.get("CLAUDECODE") else "claude")
    author = args.author or detect_host()
    root = Path(git("rev-parse", "--show-toplevel").strip() or os.getcwd())
    cid = content_id(root)  # the exact state the peer is about to review

    try:
        context, fmt, complete = build_context(args)
    except UsageError as e:
        print(e)
        return 1
    if not context:
        print("Nothing to review: no diff and no untracked files. Use --base or --question.")
        return 3
    prompt = build_prompt(context, fmt, args.max_chars)

    binary = find_codex() if peer == "codex" else find_claude()
    if not binary and not args.dry_run:
        var = "WB_CODEX_BIN" if peer == "codex" else "WB_CLAUDE_BIN"
        print(f"{peer} CLI not found. Install it or set {var} to its full path.")
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        out_file = os.path.join(tmp, "last.md")
        cmd = peer_command(peer, binary or peer, args.model, out_file)
        if args.dry_run:
            print(f"peer: {peer}\nbinary: {binary or 'NOT FOUND'}\ncwd: {root}\ncommand: {' '.join(cmd)}")
            print(f"prompt: {len(prompt)} chars (about {len(prompt) // 4} tokens if mostly ASCII)")
            return 0
        try:
            r = run(cmd, input=prompt, cwd=root, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(f"{peer} timed out after {args.timeout}s.")
            return 1
        verdict = Path(out_file).read_text(encoding="utf-8", errors="replace") if os.path.isfile(out_file) else r.stdout
        if r.returncode != 0 or not verdict.strip():
            print(f"{peer} failed (exit {r.returncode}). Last stderr lines:")
            print("\n".join(r.stderr.strip().splitlines()[-15:]))
            return 1

    complete = evidence_complete(complete, context, args.max_chars, cid, content_id(root))
    path, json_path = save_review(root, peer, author, cid, complete, args, verdict)
    lines = verdict.strip().splitlines()
    print("\n".join(lines[:PRINT_LINES]))
    if len(lines) > PRINT_LINES:
        print(f"[{len(lines) - PRINT_LINES} more lines in the saved review]")
    print(f"\nsaved: {path.relative_to(root).as_posix()} and {json_path.name} (content {(cid or 'unknown')[:12]})")
    if complete is False:
        print("note: the peer did not see every change (truncated, new files omitted, or files changed during the "
              "review), so this review does not count as evidence. Raise --max-chars or review again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
