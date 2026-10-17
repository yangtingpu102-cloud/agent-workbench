# Task board

Protocol: `docs/COLLAB-PROTOCOL.md`. Keep this file under 30 rows and move finished rows to Done.
Times are UTC. Once a lease time has passed, anyone may take the task.

| id | task | owner | status | scope (files/dirs) | lease until | note |
|----|------|-------|--------|--------------------|-------------|------|
| T2 | Try `handoff` and `cross-review` in real Claude Code and Codex sessions | - | open | plugins/workbench-core/skills/ | - | Claude → Codex passed on 2026-10-16; Codex → Claude still to do |
| T3 | Write `claude plugin eval` cases for both skills | - | open | plugins/workbench-core/evals/ | - | needs Claude Code 2.1.269 or later |
| T4 | Spec opportunity #6: lease enforcement hooks for Claude and Codex | - | open | docs/specs/lease-hooks.md | - | see docs/OPPORTUNITIES.md |

## Done
| id | task | owner | finished | evidence |
|----|------|-------|----------|----------|
| T0 | Scaffold the repo, both marketplaces, seed skills and wb.py | claude | 2026-10-16 | `python scripts/wb.py validate` → 0 errors |
| T1 | Pick the repo name and owner, then push to GitHub | human + claude | 2026-10-17 | github.com/yangtingpu102-cloud/agent-workbench |
| T5 | Opportunity #2: instruction budget linter (`wb.py lint`) | claude:a1 | 2026-10-17 | 13 unit tests pass; branch feat/budget-linter |
