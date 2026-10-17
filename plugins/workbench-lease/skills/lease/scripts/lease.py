#!/usr/bin/env python3
"""File leases for AI agents sharing one git repo (Claude Code, Codex, or people). Stdlib only.

  python lease.py claim T4 --scope "src/api/, docs/api.md" [--ttl 2h] [--host claude] [--note "..."]
  python lease.py renew T4 [--ttl 2h]
  python lease.py release T4 [--force]
  python lease.py list
  python lease.py check FILE [FILE ...]      exit 1 if another branch holds a live lease on a file
  python lease.py hook [--host claude|codex] PreToolUse hook: reads the tool call on stdin and prints a
                                             deny decision as JSON. It always exits 0, because PowerShell
                                             (Codex's hook shell on Windows) turns exit code 2 into 1.

A lease belongs to a branch: agents in different worktrees/branches are kept apart, while an agent
taking over in the same checkout (a relay) keeps the lease. Leases live in
<git common dir>/agent-leases/, shared by every worktree of the repo and never committed.
The hook fails open: if anything goes wrong it allows the edit rather than stall the agent.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
TTL_RE = re.compile(r"^(\d+)([mhd])$")
TTL_UNITS = {"m": 60, "h": 3600, "d": 86400}
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"
GLOB_CHARS = "*?["
LOCK_WAIT, LOCK_STALE = 5.0, 30.0  # seconds
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}  # Claude Code file-edit tools; Codex edits arrive as patches
PATCH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


class LeaseError(Exception):
    pass


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def fmt(t: dt.datetime) -> str:
    return t.strftime(TIME_FMT)


def parse_time(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, TIME_FMT).replace(tzinfo=dt.timezone.utc)


def parse_ttl(s: str) -> dt.timedelta:
    m = TTL_RE.match(s.strip())
    if not m:
        raise LeaseError(f"bad --ttl '{s}': use a number with m, h or d, e.g. 90m or 2h")
    return dt.timedelta(seconds=int(m.group(1)) * TTL_UNITS[m.group(2)])


def git(cwd: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def fold(s: str) -> str:
    return s.lower() if os.name == "nt" else s


class Repo:
    """The git worktree containing `start`, its branch, and the lease directory shared by all worktrees."""

    def __init__(self, start: Path):
        start = start.resolve()
        while not start.is_dir():  # a new file may sit in directories that do not exist yet
            start = start.parent
        top = git(start, "rev-parse", "--show-toplevel")
        common = git(start, "rev-parse", "--git-common-dir")
        if not top or not common:
            raise LeaseError(f"not inside a git repository: {start}")
        self.top = Path(top).resolve()
        common_path = Path(common) if Path(common).is_absolute() else start / common
        self.lease_dir = common_path.resolve() / "agent-leases"
        branch = git(start, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
        if branch == "HEAD":
            branch = "detached-" + (git(start, "rev-parse", "--short", "HEAD") or "unborn")
        self.branch = branch

    def rel(self, path: Path) -> str | None:
        try:
            return path.resolve().relative_to(self.top).as_posix()
        except ValueError:
            return None

    def leases(self) -> list[dict]:
        found = []
        if self.lease_dir.is_dir():
            for f in sorted(self.lease_dir.glob("*.json")):
                try:
                    found.append(json.loads(f.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return found

    def path_of(self, lease_id: str) -> Path:
        return self.lease_dir / f"{lease_id}.json"


def live(lease: dict) -> bool:
    try:
        return parse_time(lease["until"]) > now()
    except (KeyError, ValueError):
        return False


def norm_scope(raw: str) -> list[str]:
    items = []
    for part in re.split(r"[,\n]", raw):
        p = part.strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        if p:
            items.append(p)
    return items


def covers(pattern: str, rel: str) -> bool:
    """`dir/` covers everything below it; `name` covers itself and its subtree; globs use fnmatch."""
    p, r = fold(pattern), fold(rel)
    if p.endswith("/"):
        return r.startswith(p)
    return r == p or r.startswith(p + "/") or fnmatch.fnmatchcase(r, p)


def literal_prefix(pattern: str) -> str:
    cut = [i for i in (pattern.find(c) for c in GLOB_CHARS) if i >= 0]
    return pattern[:min(cut)] if cut else pattern


def overlaps(a: str, b: str) -> bool:
    """Could two scope entries cover a common file? Conservative when globs are involved."""
    a, b = fold(a).rstrip("/"), fold(b).rstrip("/")
    if any(c in a + b for c in GLOB_CHARS):  # compare the literal parts before the first wildcard
        pa, pb = literal_prefix(a), literal_prefix(b)
        return pa.startswith(pb) or pb.startswith(pa)
    return a == b or b.startswith(a + "/") or a.startswith(b + "/")


def clean_scope(repo: Repo, items: list[str]) -> list[str]:
    cleaned = []
    for item in items:
        path = Path(item)
        if path.is_absolute():
            rel = repo.rel(path)
            if rel is None:
                raise LeaseError(f"scope '{item}' is outside this repository ({repo.top})")
            item = rel + ("/" if item.endswith("/") and not rel.endswith("/") else "")
        if ".." in item.split("/"):
            raise LeaseError(f"scope '{item}' must not contain '..'; use a path relative to the repo root")
        cleaned.append(item)
    return cleaned


@contextlib.contextmanager
def claim_lock(lease_dir: Path):
    """Serialize claims so the overlap check and the create happen as one step."""
    lease_dir.mkdir(parents=True, exist_ok=True)
    lock = lease_dir / ".claim.lock"
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE:  # left behind by a crashed claim
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise LeaseError("another claim is in progress; try again in a moment")
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def blocking(repo: Repo, rel: str) -> list[dict]:
    return [lease for lease in repo.leases()
            if live(lease) and lease.get("branch") != repo.branch
            and any(covers(p, rel) for p in lease.get("scope", []))]


def write_new(path: Path, data: dict) -> bool:
    """Publish a complete lease file only if none exists, so a reader never sees a half-written lease."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.link(tmp, path)  # atomic, and fails instead of overwriting
        return True
    except FileExistsError:
        return False
    except OSError:  # file systems without hard links: fall back to an exclusive create
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def overwrite(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def describe(lease: dict) -> str:
    return (f"{lease.get('id')} by {lease.get('host', '?')}@{lease.get('branch', '?')} "
            f"until {lease.get('until')} [{', '.join(lease.get('scope', []))}]")


def detect_host(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("CLAUDECODE"):
        return "claude"
    return os.environ.get("AGENT_HOST") or "human"


# ---------- commands ----------

def cmd_claim(args) -> int:
    if not ID_RE.match(args.id):
        raise LeaseError(f"bad lease id '{args.id}': use letters, digits, . _ - (max 64)")
    repo = Repo(Path.cwd())
    scope = clean_scope(repo, norm_scope(args.scope))
    if not scope:
        raise LeaseError("--scope needs at least one path, directory/ or glob")
    ttl = parse_ttl(args.ttl)
    with claim_lock(repo.lease_dir):
        clashes = [(lease, a, b) for lease in repo.leases()
                   if live(lease) and lease.get("branch") != repo.branch and lease.get("id") != args.id
                   for a in scope for b in lease.get("scope", []) if overlaps(a, b)]
        if clashes:
            for lease, a, b in clashes:
                print(f"conflict: '{a}' overlaps '{b}' in lease {describe(lease)}")
            return 1
        start = now()
        data = {"id": args.id, "branch": repo.branch, "host": detect_host(args.host), "scope": scope,
                "since": fmt(start), "until": fmt(start + ttl), "note": args.note or ""}
        path = repo.path_of(args.id)
        if write_new(path, data):
            print(f"claimed {describe(data)}")
            return 0
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if existing.get("branch") not in (None, repo.branch):
            if live(existing):
                print(f"lease {args.id} is held: {describe(existing)}")
                return 1
            data["took_over_from"] = f"{existing.get('host', '?')}@{existing.get('branch')}"
        overwrite(path, data)
    print(f"{'took over' if 'took_over_from' in data else 'renewed'} {describe(data)}")
    return 0


def own_lease(repo: Repo, lease_id: str) -> dict:
    path = repo.path_of(lease_id)
    if not path.is_file():
        raise LeaseError(f"no lease named {lease_id}")
    lease = json.loads(path.read_text(encoding="utf-8"))
    return lease


def cmd_renew(args) -> int:
    repo = Repo(Path.cwd())
    lease = own_lease(repo, args.id)
    if lease.get("branch") != repo.branch:
        raise LeaseError(f"lease {args.id} belongs to {lease.get('branch')}, not {repo.branch}")
    lease["until"] = fmt(now() + parse_ttl(args.ttl))
    overwrite(repo.path_of(args.id), lease)
    print(f"renewed {describe(lease)}")
    return 0


def cmd_release(args) -> int:
    repo = Repo(Path.cwd())
    lease = own_lease(repo, args.id)
    if lease.get("branch") != repo.branch and live(lease) and not args.force:
        raise LeaseError(f"lease {args.id} belongs to {lease.get('branch')}; use --force only if you are sure")
    repo.path_of(args.id).unlink()
    print(f"released {args.id}")
    return 0


def cmd_list(_args) -> int:
    repo = Repo(Path.cwd())
    leases = repo.leases()
    if not leases:
        print("no leases")
    for lease in leases:
        state = "live" if live(lease) else "expired"
        mine = " (yours)" if lease.get("branch") == repo.branch else ""
        print(f"{state:<7} {describe(lease)}{mine}")
    return 0


def cmd_check(args) -> int:
    hits = 0
    for name in args.files:
        path = Path(name)
        repo = Repo(path if path.is_absolute() else Path.cwd() / path)
        rel = repo.rel(path if path.is_absolute() else Path.cwd() / path)
        for lease in blocking(repo, rel) if rel else []:
            print(f"{rel}: leased {describe(lease)}")
            hits += 1
    return 1 if hits else 0


def iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from iter_strings(v)


def edited_paths(payload: dict) -> list[str]:
    """File paths a tool call would write: a Claude edit tool's target, or the files named in a patch.

    Patch markers are read only when the call has no explicit target, so file content that merely
    mentions `*** Update File:` is not mistaken for an edit of that file."""
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") in EDIT_TOOLS and isinstance(tool_input, dict):
        direct = [tool_input[k] for k in ("file_path", "notebook_path") if isinstance(tool_input.get(k), str)]
        if direct:
            return direct
    return [m.group(1) or m.group(2) for text in iter_strings(tool_input) for m in PATCH_RE.finditer(text)]


def cmd_hook(args) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        base = Path(payload.get("cwd") or os.getcwd())
        problems, repos = [], {}
        for raw in edited_paths(payload):
            path = Path(raw) if Path(raw).is_absolute() else base / raw
            key = str(path.parent)
            if key not in repos:
                try:
                    repos[key] = Repo(path.parent)
                except LeaseError:
                    repos[key] = None
            repo = repos[key]
            rel = repo.rel(path) if repo else None
            for lease in blocking(repo, rel) if rel else []:
                problems.append(f"{rel} is leased to {lease.get('host', '?')}@{lease.get('branch')} "
                                f"until {lease.get('until')} (lease {lease.get('id')})")
    except Exception as e:  # fail open: a broken guard must not stop the agent
        print(f"lease hook skipped: {type(e).__name__}: {e}", file=sys.stderr)
        return 0
    if problems:
        reason = ("Blocked by an agent lease: " + "; ".join(problems) + ". Another agent is working on these "
                  "files. If this call also changes other files, send those in a separate edit. Otherwise work "
                  "on other files, ask the user, or wait until the lease ends (see `lease.py list`).")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    log_path = os.environ.get("LEASE_HOOK_LOG")
    if log_path:  # debugging aid: one JSON line per hook call
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": fmt(now()), "host": args.host, "tool": payload.get("tool_name"),
                                     "paths": edited_paths(payload), "denied": bool(problems)}) + "\n")
        except OSError:
            pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("claim")
    p.add_argument("id")
    p.add_argument("--scope", required=True, help="comma-separated paths, dir/ prefixes or globs, repo-relative")
    p.add_argument("--ttl", default="2h")
    p.add_argument("--host", help="claude, codex or human (default: detected)")
    p.add_argument("--note")
    p = sub.add_parser("renew")
    p.add_argument("id")
    p.add_argument("--ttl", default="2h")
    p = sub.add_parser("release")
    p.add_argument("id")
    p.add_argument("--force", action="store_true")
    sub.add_parser("list")
    p = sub.add_parser("check")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("hook")
    p.add_argument("--host", help="host that runs the hook (informational)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    commands = {"claim": cmd_claim, "renew": cmd_renew, "release": cmd_release,
                "list": cmd_list, "check": cmd_check, "hook": cmd_hook}
    try:
        return commands[args.cmd](args)
    except LeaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
