# Handoff — 2026-10-22 05:15 UTC — claude:a1 on feat/verify-gate
## Goal
Ship opportunity #7: evidence records tied to exact content, and a merge gate that needs a passing run plus a full, independent ship review.
## State
review: Codex said ship on round 6; evidence recorded; PR open against main.
## Done
- verify.py run/collect/gate/show/content; content id = git tree without .ai/ but with .ai/verify.json (evidence: 66 tests OK)
- Gate: latest run passes the PR's checks and the current base's checks by name and command; latest independent review is ship, complete, and covers the change since the merge base (evidence: one test per Codex finding, 17 findings over 6 rounds)
- cross-review writes JSON records (id, verdict, complete, base_sha = merge base, content id)
- CI `gate` job runs `verify.py gate --base <PR base sha>` on pull requests
## Next
1. Merge the PR when CI is green; from then on every PR needs committed evidence
2. Opportunity #1: A/B token benchmark across Claude Code and Codex
3. Board T2: Codex -> Claude cross-review from inside Codex (needs `claude` CLI re-login)
## Files
- plugins/workbench-core/skills/verify/scripts/verify.py, plugins/workbench-core/skills/cross-review/scripts/ask_peer.py
- .ai/verify.json: checks and require_review; .ai/verifications/: committed evidence
## Verify
`python plugins/workbench-core/skills/verify/scripts/verify.py gate --base origin/main`
## Gotchas
- Review against the remote base (`--base origin/main`) when local main has unpushed commits, or CI's coverage check fails.
- `run` refuses a dirty tree; changes under .ai/ (except verify.json) never invalidate evidence.
- Heredocs in this shell turn `\n` into real newlines; edit Python string escapes with the Edit tool.
