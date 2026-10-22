# Handoff — 2026-10-22 03:40 UTC — claude:a1 on main @ b28edf2
## Goal
Grow agent-workbench one opportunity at a time (docs/OPPORTUNITIES.md), each verified on both hosts before merge.
## State
done: opportunities #2 (`wb.py lint`, PR #1) and #6 (`workbench-lease`, PR #2) are merged; both plugins are installed at user scope in Claude Code.
## Done
- workbench-lease live in Claude Code: in a worktree of a temp repo with `src/` leased by main, Edit on README.md succeeded, Edit on src/app.py and Write of src/new_module.py were denied with the lease reason (evidence: files unchanged afterwards)
- workbench-lease live in Codex 0.162: apply_patch on a leased README.md denied (PR #2 test plan)
- 39 unit tests pass (`python -m unittest discover -s tests`)
## Next
1. Opportunity #7: evidence-based cross-verification (JSON review records with commit SHA, command, exit code; a merge gate)
2. Opportunity #1: A/B token benchmark across Claude Code and Codex
3. Board T2: Codex → Claude cross-review from inside Codex, after `claude` CLI re-login
## Files
- docs/OPPORTUNITIES.md: ranked list; #2 and #6 marked implemented
- plugins/workbench-lease/skills/lease/scripts/lease.py, plugins/workbench-core/skills/cross-review/scripts/ask_peer.py
## Verify
`python -m unittest discover -s tests && python scripts/wb.py validate`
## Gotchas
- The CLI `claude` reported "OAuth session expired"; hooks of installed plugins can be tested inside a desktop session instead.
- Codex plugin hooks run only after they are trusted in Codex `/hooks`.
- The scratchpad path is too long for git on Windows; use `mktemp -d` for throwaway repos.
