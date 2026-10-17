# Handoff — 2026-10-17 06:10 UTC — claude:a1 on feat/budget-linter @ e6f8445+1
## Goal
Ship opportunity #2: `wb.py lint`, a cross-host token-budget linter for CLAUDE.md, AGENTS.md and skill metadata.
## State
done: implemented and committed on feat/budget-linter; not pushed, not cross-reviewed yet.
## Done
- `wb.py lint [PATH] [--no-global] [--max-tokens N] [--exact]` (evidence: `python -m unittest discover -s tests` → 13 tests OK)
- Follows Claude @imports (depth 5, skips code spans and emails), Codex root-to-leaf doc chain with the 32 KiB cap, duplicate paragraphs, CLAUDE.md/AGENTS.md drift, CJK note (evidence: realistic temp fixture → expected tables)
- `--exact` uses the optional `anthropic` SDK and falls back to estimates (evidence: run without the package → "skipped" warning, exit 0)
- CI runs the unit tests and `lint --no-global --max-tokens 3000`
## Next
1. Codex cross-review of this branch (implementer is not the verifier), then push and open a PR
2. Live-test `--exact` with credentials: `uv run --with anthropic python scripts/wb.py lint --exact`
3. Codex → Claude cross-review from inside Codex (board T2)
## Files
- scripts/wb.py: `Lint` class and `cmd_lint`; `main` now uses `build_parser()`
- tests/test_wb.py: lint and helper tests (use WB_HOME for a fake home)
- docs/WORKFLOW.md §3: user-facing description (zh-TW)
## Verify
`python -m unittest discover -s tests && python scripts/wb.py lint`
## Gotchas
- Codex is not documented to expand @imports; lint reports it as info, not an error (unverified either way).
- The background worktree session failed with `config-hooks-refused`, likely the `!gh auth git-credential` helper in global git config (unverified).
- Skill metadata is always estimated, even with --exact.
