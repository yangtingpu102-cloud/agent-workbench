---
name: lease
description: Claim, renew, release and list file leases so agents working in parallel (Claude Code, Codex, other worktrees) do not edit the same files; a hook blocks edits to files another branch has leased. Use when starting work in a shared repo, when the user says "claim", "lease", "認領", "lock these files", or when an edit is blocked by a lease.
license: MIT
---

# Lease

A lease reserves repo paths for your branch for a limited time, and this plugin's PreToolUse hook blocks agents on other branches from editing them. Leases live in the git common dir, so every worktree sees them and they are never committed.

`<skill-dir>` is the directory containing this file. Run the commands from inside the repo.

## Before editing in a shared repo
1. See who holds what: `python <skill-dir>/scripts/lease.py list`
2. Claim the files you will change. Use the `.ai/board.md` task id when there is one:
   `python <skill-dir>/scripts/lease.py claim T4 --scope "src/api/, docs/api.md" --host <claude|codex> --ttl 2h`
   Scope entries are repo-relative paths, `dir/` prefixes or globs. Keep the scope as small as the task allows.
3. A conflict means another branch holds an overlapping live lease. Pick other work or ask the user. Never delete or edit another branch's lease file.

## While working
- Still working when the lease is about to end: `python <skill-dir>/scripts/lease.py renew T4 --ttl 2h`
- If an edit is blocked, tell the user who holds the lease and until when. Do not retry the leased file or work around the hook with shell commands.

## When done or pausing
Run `python <skill-dir>/scripts/lease.py release T4`, then update `.ai/board.md` and `.ai/HANDOFF.md`.

## Limits
- Leases belong to a branch. Agents sharing one checkout share its leases, so give each concurrent writer its own worktree.
- The hook checks file-edit tools (Claude Code Edit, Write, NotebookEdit; Codex apply_patch, including patches applied through the shell), not shell commands such as `echo > file` that write files directly.
- A patch that touches a leased file is blocked as a whole. Send edits to other files in a separate patch.
- The hook fails open: if it hits an internal error, it allows the edit.
