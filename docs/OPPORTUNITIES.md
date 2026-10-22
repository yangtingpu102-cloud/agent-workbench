# 開發機會清單

調研日期：2026-10-16。星數是當天 GitHub 的快照。「最接近的現有專案」指已經有人碰過、但沒有完整解決的。

## A. 已經有人做得很好：不要重做，直接用或整合

| 需求 | 現成工具 |
|---|---|
| Shell 輸出壓縮 | RTK（83.6k★）、context-mode（30k）、Headroom（75k） |
| 用量與成本分析 | ccusage（含 `@ccusage/codex`）、codeburn（有 Windows 匣）、tokscale |
| 符號級或語意程式碼檢索 | Serena（LSP）、claude-context、code-review-graph |
| 把 repo 打包給 AI | Repomix |
| 最新函式庫文件 | Context7 |
| 跨 session 記憶 | claude-mem（101k） |
| 同步 AGENTS.md 與 CLAUDE.md | ruler、rulesync |
| Claude 呼叫 Codex 審查或接手 | openai/codex-plugin-cc（34k，官方） |
| Codex 呼叫 Claude 審查 | sendbird/cc-plugin-codex |
| 多模型共識與辯論 | PAL MCP（舊名 zen）、agent-council |
| tmux 加 worktree 的多 agent 管理 | claude-squad、Gas Town（完整功能需 WSL） |
| 給 agent 用的任務追蹤 | beads |
| 同一家的多 agent | Claude agent teams（實驗中）、Codex subagents（內建） |

> 已經不能用的：`codex mcp-server` 在 2026-08 棄用，約 0.154 版移除。Vibe Kanban 停止服務。Crystal 已改名 Nimbalyst。

## B. 尚未被做好的機會

難度分低、中、高。⭐ 代表本 repo 已經有種子。

### 省 token 與順暢度

| # | 題目 | 缺口 | 最接近的現有專案 | MVP | 難度 |
|---|---|---|---|---|---|
| 1 | **跨 Claude 與 Codex 的省 token A/B 實測框架**（量帳單成本，不是 bytes） | 獨立研究只測過 Claude Code，沒測 Codex。RTK `gain` 算的是刪掉的 bytes。研究發現 token 減少和成本減少的相關性只有約 0.15，RTK 對帳單大致持平（±5%）。 | PointFiveLabs/ai-efficiency-benchmark（只測 Claude，且有商業利益）、codeburn（沒有 A/B 模式） | `task.yaml` 定義任務與「有／無工具」兩組 → 用 `claude -p --output-format json` 和 `codex exec --json` 收集 token、cache 命中、輪數、成功率 → 產出 markdown 報告與信賴區間 | 中 |
| 2 | **指令檔預算 linter**（跨兩邊；v0.1 已實作為 `wb.py lint`） | 量測 AGENTS.md、CLAUDE.md 與 skill description 的常駐成本和重複內容。Codex 有 32 KiB 的 `project_doc_max_bytes` 上限，Claude 建議 200 行內。ruler／rulesync 只同步、不 lint。 | codeburn（只會標出過大的 CLAUDE.md） | 把 `wb.py budget` 拆成獨立 CLI，接上 token count API 量精準值，再加 pre-commit 與 GitHub Action | 低 ⭐ |
| 3 | **中文（CJK）token 稅量測與雙軌精簡器** | 沒人量過中文 CLAUDE.md、skill 和工具輸出在 agent 流程裡的實際開銷。RTK 和 squeez 用 bytes/4 估算，對中文不準。 | caveman `/megacave`（沒有證據） | 量測報告，加上「中文給人看、英文給 agent」的轉換工具 | 低–中 |
| 4 | **Windows-first hook 套件** | RTK #1319（PowerShell 工具的 matcher）還沒修，而且 `rtk init` 會覆蓋手動設定的 matcher。squeez 需要 Git Bash。很少專案有 Windows 測試矩陣。 | caveman（有 PowerShell 安裝器） | PowerShell 工具也能套用的 hooks、不覆蓋使用者設定的安裝器、Windows CI | 中 |
| 5 | **Prompt cache 安全檢查器** | 沒有工具會警告 hook、proxy 或動態指令檔打破了 cache 前綴，結果是省了 token 卻多付錢。 | Headroom 的 cache 模式（在研究中表現最差） | 讀 transcript 裡的 `cache_read` 和 `cache_creation`，標出造成 cache miss 的變動 | 中–高 |

### 多 agent 協作

| # | 題目 | 缺口 | 最接近的現有專案 | MVP | 難度 |
|---|---|---|---|---|---|
| 6 | **不綁廠商的檔案式 lease，加上雙邊 hook 強制**（v0.1 已實作為 `workbench-lease` plugin） | Claude agent teams 的 claim 放在 `~/.claude/teams`，只限 Claude。mcp_agent_mail 需要伺服器。agent-sync 只有 Claude 端會強制。 | agent-sync（4★）、OpenMOSS/claude-codex-handoff（42★） | 本 repo 的協定 v0.1，加上 Claude 和 Codex 的 `PreToolUse` hook 擋下他人租約中的檔案，並支援 worktree 共用看板 | 中 ⭐ |
| 7 | **附證據的交叉驗證紀錄**（v0.1 已實作為 `verify` skill 與 CI gate） | 現有審查只回傳文字，沒有紀錄跑了什麼指令、exit code 和測的是哪個 commit。 | codex-plugin-cc 的 Stop-hook review gate | 把 `cross-review` 的紀錄改成 JSON schema，再加 `wb.py verify-gate` 在 merge 前檢查 | 低–中 ⭐ |
| 8 | **結構化交接 schema，加上接續成本量測** | 現有轉換器讀的是未公開的 session 格式，會掉 tool calls，也沒人公布「接續」對比「重來」省多少。 | cli-continues | `handoff` 的 JSON 版本，量測和 #1 共用同一個框架 | 低 ⭐ |
| 9 | **訂閱額度感知的任務分派**（Claude Max、ChatGPT Plus） | 現有 router 都只針對 API 費用。Claude 沒有直接提供剩餘額度百分比，只能在碰到 rate-limit 錯誤後才反應。 | quota-router（0★，不支援 Windows）、codeburn `quota` | 依剩餘額度和 reset 時間建議下一個任務交給誰 | 高（資料來源不穩，建議後期再做） |
| 10 | **編輯前的跨 worktree 衝突偵測**（雙邊） | Clash 只有 Claude 端的 hook，而且只看到檔案層級。 | Clash（65★） | 在 `PreToolUse` 時對其他 worktree 跑 `git merge-tree` | 中（可以併入 #6） |
| 11 | **協作開銷計量** | 兩家的文件都警告多 agent 很耗 token，但沒人量過協作本身花了多少。 | 無 | 標記協作相關的 tool call（讀看板、訊息、review），算出開銷占比 | 中（可以併入 #1） |
| 12 | **給其他 MCP client 用的 Codex 橋接** | `codex mcp-server` 移除後，替代品 `codex app-server` 用的是 JSON-RPC，不是 MCP。Claude 端官方已經包好了。 | openai/codex-plugin-cc | 一個薄的 MCP 轉接層，讓 Gemini CLI 這類 MCP client 呼叫 Codex | 中（優先度低） |

## C. 建議起手順序

1. **#2 指令檔預算 linter**：最快上架，程式碼可以和 `wb.py` 共用，結果容易驗證。
2. **#6 加 #7 協作協定強制版**：本 repo 已經有種子，Windows 原生、不綁廠商，正好是現有工具最弱的地方。
3. **#1 A/B 實測框架**：生態系最大的缺口是「證據」。這個框架能替你其他每個工具提供「到底省多少」的數據，可以當作整個 repo 的差異化核心。

之後再做 #3（中文使用者的天然優勢）和 #4（你本身就在 Windows 上，等於現成的測試環境）。
