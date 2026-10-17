# agent-workbench — agent instructions

Skills and plugins that make Claude Code and Codex cheaper and smoother, and let two or more agents work on one task. Human docs are in `docs/` (zh-TW); read one only when the task needs it.

## Layout
- `plugins/<plugin>/skills/<skill>/SKILL.md`: skills in Agent Skills format, shared by Claude Code and Codex.
- `plugins/<plugin>/.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`: one manifest per host. Keep `name`, `version` and `description` identical.
- `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json`: list every plugin in both, with relative `./plugins/<name>` sources.
- `scripts/wb.py`: `new`, `validate`, `budget`, `lint`, `link`, `unlink`. Stdlib Python only; tests in `tests/`.
- `.ai/`: coordination state between agents (see Collaboration).

## Rules
- After changing a skill or manifest, run `python scripts/wb.py validate` and make it pass. After changing scripts, run `python -m unittest discover -s tests`.
- Write skills in English, imperative, under 500 lines. Move detail into `references/` and say when to read it. Move deterministic work into `scripts/`.
- A skill's `description` is loaded in every session of every user. Keep it short: what it does, then "Use when" plus trigger phrases.
- Use only spec frontmatter keys (`name`, `description`, `license`, `metadata`, `compatibility`, `allowed-tools`) unless a host-only key is required.
- Treat Windows as a first-class target: no tmux or bash-only assumptions, and handle paths with backslashes and spaces.
- Add no script dependencies without stating why in the PR.

## Collaboration (when more than one agent works here)
1. Orient from `.ai/HANDOFF.md` and `.ai/board.md` first, not by scanning the repo.
2. Claim a task before editing: set owner, scope and lease in its board row. Do not edit files inside another agent's unexpired lease.
3. One writer per file set. When two agents write code at once, give each its own git worktree and branch.
4. When you finish or pause, write `.ai/HANDOFF.md` with the `handoff` skill and release your row.
5. The implementer does not verify their own work. The other agent reviews with the `cross-review` skill before merge.

Full protocol: `docs/COLLAB-PROTOCOL.md`.
