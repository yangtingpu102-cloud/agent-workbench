# agent-workbench

Skills and plugins that make **Claude Code** and **OpenAI Codex** cheaper and smoother to use, and let two or more agents work on one task. One repo installs into both hosts.

## Install

| | Claude Code | Codex |
|---|---|---|
| Add marketplace | `claude plugin marketplace add yangtingpu102-cloud/agent-workbench` | `codex plugin marketplace add yangtingpu102-cloud/agent-workbench` |
| Install | `claude plugin install workbench-core@agent-workbench` | `codex plugin add workbench-core@agent-workbench` |

## Plugins

| Plugin | Skill | What it does |
|---|---|---|
| `workbench-core` | `handoff` | Writes or resumes a 40-line `.ai/HANDOFF.md`, so the next agent continues without re-exploring the repo |
| | `cross-review` | Asks the other agent (Claude ↔ Codex) for a read-only review of your diff and saves the verdict with the commit SHA |

Always-loaded cost of `workbench-core` is about 190 tokens per session (`python scripts/wb.py budget`).

## Multi-agent protocol

Plain files, no server, works natively on Windows: `.ai/board.md` for claims and leases, `.ai/HANDOFF.md` for handoffs, and `.ai/reviews/` for review evidence. See [docs/COLLAB-PROTOCOL.md](docs/COLLAB-PROTOCOL.md).

## Develop

```bash
python scripts/wb.py new <plugin> <skill>   # scaffold
python scripts/wb.py link                   # try it in Claude Code and Codex
python scripts/wb.py validate               # also: claude plugin validate . --strict
python scripts/wb.py budget                 # always-loaded token cost of these plugins
python scripts/wb.py lint [PATH]            # token budget of any project's CLAUDE.md, AGENTS.md and skills
```

The full loop is in [docs/WORKFLOW.md](docs/WORKFLOW.md), and what to build next is in [docs/OPPORTUNITIES.md](docs/OPPORTUNITIES.md).

## 中文說明

這個 repo 收集讓 Claude Code 和 Codex 更省 token、更順暢的 skill 與 plugin，也包含讓多個 AI 協作同一個任務的協定。同一份 repo 可以同時安裝到兩邊。開發流程見 `docs/WORKFLOW.md`，開發機會見 `docs/OPPORTUNITIES.md`，需要接的連接器見 `docs/CONNECTORS.md`。

## License

MIT
