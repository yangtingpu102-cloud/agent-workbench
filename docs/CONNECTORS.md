# 連接器與整合清單

## 現況（2026-10-16 檢查）

- **已連的 claude.ai 連接器**：Claude Docs、Notion（57 個工具）、Gmail（30 個工具）、visualize、scheduled-tasks。
- **本機 CLI**：Claude Code 2.1.263、Codex CLI 0.162（桌面版內建，不在 PATH）、Python 3.13、uv 0.12、RTK 0.47、git 2.55。
- **還缺**：GitHub CLI `gh`、git 使用者身分、Node.js（只有要用 TypeScript 寫 MCP server 時才需要）。

## 必要

| 項目 | 用途 | 怎麼接 | 誰做 |
|---|---|---|---|
| GitHub CLI `gh` | 建 repo、PR、issue、release。兩個 AI 都透過它操作 GitHub，比接 GitHub MCP 省 token，因為不用載入幾十個工具定義。 | `winget install --id GitHub.cli -e`，再執行 `gh auth login` | 你（登入要本人操作） |
| git 身分 | commit 的作者資訊 | `git config --global user.name "..."` 和 `user.email` | 你 |
| GitHub repo | 發佈 marketplace | `gh repo create <name> --public --source . --push` | 你決定公開或私有後，我可以代為執行 |

## 建議

| 項目 | 用途 | 怎麼接 |
|---|---|---|
| Claude Code 2.1.269 以上 | `claude plugin eval`，含無 plugin 的對照組 | `claude update` |
| Claude GitHub App | 讓 Claude Code 雲端 session 能操作這個 repo，也能在 PR 上 @claude | 在 Claude Code 執行 `/install-github-app`（需要 repo 管理權限） |
| Codex ↔ GitHub | Codex Cloud 任務，以及 Codex 自動審查 PR | ChatGPT → Codex 設定 → 連接 GitHub |
| openai/codex-plugin-cc | 官方的 Claude → Codex 橋接，取代已移除的 `codex mcp-server`。可以拿來和本 repo 的 `cross-review` 對照 | 依它的 README 安裝 |
| API keys（選用） | 在 CI 跑 `claude plugin eval`、`--bare` 模式和 benchmark | Anthropic key（`ANTHROPIC_API_KEY`）和 OpenAI key（`CODEX_API_KEY`），存進 GitHub Actions secrets。**不要貼到對話裡。** |

## 選用

| 項目 | 用途 | 怎麼接 |
|---|---|---|
| Context7 MCP | 查最新的函式庫文件 | Claude：`claude mcp add --transport http context7 https://mcp.context7.com/mcp`<br>Codex：`codex mcp add context7 --url https://mcp.context7.com/mcp` |
| Notion（已連） | 存放 roadmap 或研究筆記 | 已經連好。不過建議 roadmap 放在 GitHub Issues，離程式碼比較近 |

## 這個專案用不到

Gmail、Linear、Figma、monday.com 跟這裡的開發無關。Claude Code 會延遲載入 MCP 工具定義，所以留著的成本不高；想減少干擾的話，可以在 coding session 裡關掉。

## AI 之間怎麼「連」起來

| 方向 | 方法 | 狀態 |
|---|---|---|
| Claude → Codex | `codex exec --sandbox read-only`（本 repo 的 `cross-review` 用這個）、`codex review --uncommitted`，或 codex-plugin-cc | 已在本機確認這些參數存在 |
| Codex → Claude | `claude -p --tools Read,Grep,Glob --permission-mode dontAsk` | 已在本機確認這些參數存在 |
| `codex mcp-server` | 已移除 | 不要再用 |
| `claude mcp serve` | 把 Claude Code 的工具開放給其他 MCP client。對方拿到的是工具，不是 Claude 本身的推理 | 選用 |
