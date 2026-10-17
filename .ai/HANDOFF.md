# Handoff — 2026-10-17 07:00 UTC — claude:a1 on feat/lease-hooks @ 4eafd64+1
## Goal
Ship opportunity #6: the workbench-lease plugin, file leases per branch enforced by PreToolUse hooks in Claude Code and Codex.
## State
in progress: implemented and committed on feat/lease-hooks; not pushed, not cross-reviewed, not live-tested in a real agent session.
## Done
- lease.py claim/renew/release/list/check/hook; leases in <git common dir>/agent-leases, owned by branch (evidence: `python -m unittest discover -s tests` → 35 tests OK)
- Hook denies through stdout JSON and always exits 0, because PowerShell turns exit 2 into 1 (evidence: HookConfigTest runs the exact codex.hooks.json command via `powershell -NoProfile -Command` → deny)
- Claude hooks/hooks.json (exec form `python` + args) and Codex hooks/codex.hooks.json (`commandWindows`, `$env:PLUGIN_ROOT`, matcher apply_patch|Bash) (evidence: `claude plugin validate plugins/workbench-lease --strict` → passed)
## Next
1. After 17:57 local (Codex quota reset): Codex cross-review `--base main`, then push and open a PR
2. Live test in Codex: temp repo, `.codex/hooks.json` from codex.hooks.json, `PLUGIN_ROOT` and `LEASE_HOOK_LOG` set, `codex exec --dangerously-bypass-hook-trust --sandbox workspace-write`
3. Live test in Claude after the user re-logs in the CLI: `claude -p --plugin-dir plugins/workbench-lease --permission-mode acceptEdits` in a worktree of a leased temp repo (unset CLAUDECODE first)
## Files
- plugins/workbench-lease/skills/lease/scripts/lease.py: all logic; `LEASE_HOOK_LOG` writes one JSON line per hook call
- plugins/workbench-lease/hooks/: hooks.json (Claude), codex.hooks.json (Codex, referenced from .codex-plugin/plugin.json)
- tests/test_lease.py: RepoCase builds a repo + worktree; HookConfigTest runs the configured commands
## Verify
`python -m unittest discover -s tests && python scripts/wb.py validate`
## Gotchas
- Codex hook payload for edits: tool_name apply_patch, tool_input {"command": "<patch>"}; shell calls are tool_name Bash (from source, rust-v0.162.0; not yet seen live).
- Codex skips plugin hooks until trusted in /hooks.
- Claude exec form needs `python` on PATH; without it the hook does not run (fails open).
- The CLI `claude` here reported "OAuth session expired"; the desktop app is unaffected.
