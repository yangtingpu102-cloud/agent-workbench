# 工作流程

## 0. 一次性設定（打勾清單）

本機狀態檢查於 2026-10-16。

- [ ] 把這個 repo 搬到固定位置，例如 `Documents\agent-workbench`。
- [ ] 設定 git 身分（目前沒有設定）：
  ```bash
  git config --global user.name "你的名字"
  git config --global user.email "你的 email"
  ```
- [ ] 安裝 GitHub CLI 並登入（登入要你本人操作）：
  ```bash
  winget install --id GitHub.cli -e
  gh auth login
  ```
- [ ] 把 Claude Code 更新到 2.1.269 以上，才能用 `claude plugin eval`（目前是 2.1.263）：`claude update`。
- [ ] Codex CLI 是桌面版內建的，位置在 `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe`，不在 PATH 上，而且 hash 會隨更新改變。`cross-review` 會自動找到它；若想在終端直接打 `codex`，就把該資料夾加進 PATH，或設 `WB_CODEX_BIN`。
- [ ] 已知問題：你的 RTK hook 只攔 `Bash` 工具，Claude Code 透過 `PowerShell` 工具跑的指令不會被壓縮（RTK issue #1319）。不要手動改 matcher 之後又執行 `rtk init`，它會把設定蓋掉。

## 1. 開發一個 skill／plugin（標準循環）

1. **選題**：從 `docs/OPPORTUNITIES.md` 挑一個題目，開 GitHub issue，在 `.ai/board.md` 認領。
2. **建骨架**：
   ```bash
   python scripts/wb.py new <plugin> <skill>
   ```
   新 plugin 會自動產生兩份 manifest，並登記到兩個 marketplace。
3. **寫內容**：先寫 `description`（它決定何時觸發，也是每個 session 的常駐成本），再寫步驟。確定性的工作放進 `scripts/`，長篇參考資料放進 `references/`。
4. **本機試用**：
   - 兩邊同時試：`python scripts/wb.py link`，會連結到 `~/.claude/skills` 和 `~/.agents/skills`。試完執行 `python scripts/wb.py unlink`。
   - 只在 Claude 試一個 session：`claude --plugin-dir plugins/<plugin>`。
   - 在 Codex 開新 session，用 `$<skill>` 呼叫。
5. **驗證**：
   ```bash
   python scripts/wb.py validate
   claude plugin validate . --strict
   ```
6. **量測**：
   - `python scripts/wb.py budget`：估算常駐成本。
   - `claude plugin details <plugin>`：官方的 projected token cost。
   - `claude plugin eval plugins/<plugin> --max-cost-usd 5`：有／無 plugin 的對照組，會花錢。
7. **交叉審查**：請另一個 AI 用 `cross-review` 看 diff。
8. **發佈**：開 PR，等 CI 綠燈後合併。接著把兩份 manifest 的 `version` 一起升版，執行 `claude plugin tag plugins/<plugin>`，再推送 tag。
9. **使用者安裝**：

   | | Claude Code | Codex |
   |---|---|---|
   | 加入 marketplace | `claude plugin marketplace add yangtingpu102-cloud/agent-workbench` | `codex plugin marketplace add yangtingpu102-cloud/agent-workbench` |
   | 安裝 | `claude plugin install workbench-core@agent-workbench` | `codex plugin add workbench-core@agent-workbench` |

   正式安裝前先執行 `wb.py unlink`，避免同一個 skill 載入兩次。

## 2. 兩個 AI 協作的日常

規則與三種模式見 `docs/COLLAB-PROTOCOL.md`。最常用的三種組合：

- **Claude 實作、Codex 審查**：在 Claude Code 裡說「cross-review」，或直接在終端執行 `codex review --uncommitted`。
- **額度用完換手**：在當前 agent 裡說「handoff」；到另一個 agent 裡說「讀 HANDOFF 繼續」。
- **平行**：在 `.ai/board.md` 各自 claim 不同的 scope，各開 `git worktree`，最後互相 review 再合併。

## 3. 開發本 repo 時的省 token 守則

- `AGENTS.md` 維持精簡，目前約 540 tokens，每個 session 都會載入。
- skill 用英文寫，`docs/` 用中文給人看。agent 只在需要時讀 docs。
- 起手讀 `HANDOFF.md` 和 `board.md`，不要全 repo 掃描。
- peer review 只送 diff，`--max-chars` 預設 6 萬字元。
- coding session 不需要的連接器（例如 Gmail）可以關掉，見 `docs/CONNECTORS.md`。
