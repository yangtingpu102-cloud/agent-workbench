# Handoff — 2026-10-23 08:10 UTC — claude:a1 on feat/ab-bench
## Goal
Ship opportunity #1: workbench-bench, an A/B benchmark of billed cost, tokens, turns and success for Claude Code and Codex.
## State
review: PR open. Every finding from 13 Codex review rounds is fixed; the user chose to stop reviewing, so the final content has no ship review and the CI evidence gate fails by design. The user decides whether to merge.
## Done
- bench.py plan/run/report with validation of every input, safe arm-flag list, per-call Claude budget, interrupted-call markers, fingerprinted resume, paired bootstrap verdicts after 5 rounds (evidence: 115 tests OK; `verify.py run` passed on content dda16f369934)
- Live smoke: Codex default vs --ignore-user-config (20,247 vs 14,729 input tokens), Claude isolated (9,405 tokens, $0.025); manual Claude default 162,644 tokens ($0.65)
- cross-review's Claude reviewer runs isolated (no MCP, user settings or skills)
## Next
1. User: decide on merging the PR despite the failing gate
2. A real headless-overhead run with 5 rounds per arm and pinned Codex model, to replace the anecdotal readings in docs/benchmarks
3. Board T2: Codex -> Claude cross-review from inside Codex
## Files
- plugins/workbench-bench/skills/ab-bench/scripts/bench.py, tests/test_bench.py
- docs/benchmarks/2026-10-22-headless-overhead.md
## Verify
`python -m unittest discover -s tests && python scripts/wb.py validate`
## Gotchas
- Codex reviews found one new narrow issue per round; ask for an exhaustive list in one pass and do a systematic input audit before review.
- An experiment file is code: its check runs on the host. Only run experiments you trust.
- Heredocs in this shell turn \n into real newlines; use the Edit tool for Python string escapes.
