# Handoff — 2026-10-17 06:10 UTC — claude:a1 on feat/budget-linter @ e6f8445+1
## Goal
Ship opportunity #2: `wb.py lint`, a cross-host token-budget linter for CLAUDE.md, AGENTS.md and skill metadata.
## State
done: implemented, cross-reviewed by Codex, fixes committed; PR open against main.
## Done
- `wb.py lint [PATH] [--no-global] [--max-tokens N] [--exact]` (evidence: `python -m unittest discover -s tests` → 13 tests OK)
- Follows Claude @imports (depth 5, skips code spans and emails), Codex root-to-leaf doc chain with the 32 KiB cap, duplicate paragraphs, CLAUDE.md/AGENTS.md drift, CJK note (evidence: realistic temp fixture → expected tables)
- `--exact` uses the optional `anthropic` SDK and falls back to estimates (evidence: run without the package → "skipped" warning, exit 0)
- Codex cross-review: 5 findings; fixed nested CLAUDE.md chains, --max-tokens on every chain, --exact mid-run fallback, line counting; plugin skills stay out of lint totals (that is `wb.py budget`) with an info pointer (evidence: 18 tests OK)
- CI runs the unit tests and `lint --no-global --max-tokens 3000`
## Next
1. Merge the PR once CI passes on Ubuntu and Windows
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
