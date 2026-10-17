# Handoff — 2026-10-16 UTC — claude:a1 on main @ no-commit
## Goal
A cross-host (Claude Code + Codex) plugin repo with a working dev loop, a collaboration protocol, and a ranked opportunity list.
## State
done: scaffold complete; initial commit pushed to github.com/yangtingpu102-cloud/agent-workbench (public).
## Done
- Both marketplaces and the workbench-core plugin (evidence: `claude plugin validate . --strict` → passed; `codex plugin list` with a `-c marketplaces.*` override → lists workbench-core@agent-workbench)
- Skills handoff and cross-review (evidence: `python scripts/wb.py validate` → 0 errors, 0 warnings)
- wb.py link/unlink (evidence: run against a temp WB_HOME → junctions created, then removed; targets intact)
- ask_peer.py (evidence: `--dry-run` finds codex.exe and claude.exe and builds the commands)
- Live Claude → Codex cross-review (evidence: 2m12s run, 5 findings, all confirmed and fixed: invalid --base, truncation dropping the untracked list, mklink via cmd, duplicate marketplace names, spaced filenames; each fix covered by a one-off test)
## Next
1. Run one live Codex → Claude cross-review from inside Codex (board T2); this spends Claude quota
2. Check that the first GitHub Actions run of validate.yml passes on Ubuntu and Windows
3. Spec the lease-enforcement hooks (board T4, opportunity #6)
## Files
- AGENTS.md: canonical rules for every agent (CLAUDE.md imports it)
- scripts/wb.py: new, validate, budget, link, unlink
- plugins/workbench-core/: manifests for both hosts and the two skills
- docs/OPPORTUNITIES.md, docs/CONNECTORS.md, docs/WORKFLOW.md, docs/COLLAB-PROTOCOL.md
## Verify
`python scripts/wb.py validate && claude plugin validate . --strict`
## Gotchas
- `claude plugin init <name>` with no path writes into ~/.claude/skills, where it auto-loads. Run it from an explicit temp dir.
- codex.exe lives in %LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\ and the hash changes when Codex updates.
- `codex mcp-server` was removed (2026-08). `--max-turns` is not in `claude --help`, so the scripts do not use it (unverified).
- The Codex → Claude direction has not been run live (unverified).
- The scratchpad path is too long for git on Windows ("Filename too long"). Run throwaway git tests in `mktemp -d`.
