# 多 agent 協作協定 v0.1

讓兩個以上的 AI agent（Claude Code、Codex，或人）同時處理同一個任務時不互相踩到。設計原則：

- **純檔案**：不需要伺服器、daemon 或 tmux，Windows 原生可用。
- **不綁廠商**：任何讀得到 `AGENTS.md` 的 agent 都能遵守。
- **git 可追溯**：協作狀態就是 repo 裡的文字檔。
- **低 token**：起手只讀兩個小檔，30 列的看板大約 1k tokens。

## 檔案

| 檔案 | 用途 | 誰寫 |
|---|---|---|
| `.ai/board.md` | 任務看板：誰在做什麼、範圍、租約 | 所有 agent |
| `.ai/HANDOFF.md` | 最近一次交接（格式見 `handoff` skill） | 結束或暫停的那個 agent |
| `.ai/reviews/*.md` | 交叉審查紀錄，含 commit SHA（預設不進 git） | `cross-review` 輔助腳本 |

## Agent 識別碼

格式為 `<host>:<短代號>`，例如 `claude:a1`、`codex:b2`、`human:yt`。同一個 host 開兩個 session 時，用不同代號區分。

## 看板欄位與狀態

`id | task | owner | status | scope | lease until | note`

```
open ──claim──▶ claimed ──完成實作──▶ review ──另一個 agent 驗證通過──▶ done
  ▲                │                    │
  └────release─────┴─────── blocked（note 寫原因）
```

- **claim**：同時填 `owner`、`status: claimed`、`scope`（檔案或資料夾），以及 `lease until`（現在 + 2 小時，UTC）。
- **續租**：還在做就更新 `lease until`。
- **過期**：租約時間已過，任何 agent 都可以接手，但要先讀 `HANDOFF.md`。
- **release**：完成或放棄時清掉 owner 和租約，狀態改成 `review` 或 `open`。

## 規則

1. **先讀後做**：起手只讀 `.ai/HANDOFF.md` 和 `.ai/board.md`，再看 `git status --short` 和 `git log --oneline -5`。
2. **先 claim 再改**：不要編輯別人租約中的 scope。
3. **一個 scope 只有一個寫入者**：兩個 agent 同時寫程式時，各自開 git worktree 和分支：`git worktree add ../wb-codex -b codex/T4`。
4. **看板只有一份**：worktree 裡的 agent 要讀寫**主 checkout** 的 `.ai/board.md`，也就是 `git worktree list` 第一行列出的路徑。不要改自己 worktree 裡那份副本。
5. **實作者不驗證自己**：狀態改成 `review` 之後，由另一個 agent 用 `cross-review` 或自己的驗證流程確認，並附上證據（指令與結果）。
6. **結束必交接**：暫停或換手前，用 `handoff` skill 覆寫 `.ai/HANDOFF.md`。

## 三種協作模式

| 模式 | 什麼時候用 | 流程 |
|---|---|---|
| **接力**（relay） | 一邊額度快用完、任務是線性的 | A 做到一半 → `handoff` → B 讀 HANDOFF 從 Next 1 繼續 |
| **實作／驗證分離** | 改動有風險、想要第二雙眼睛 | A 實作並把狀態改成 review → B 用 `cross-review` 或 `codex review --uncommitted` 審查 → A 修正 → done |
| **平行分工** | 任務可以切成互不重疊的檔案群 | 各自 claim 不同 scope、各開 worktree → 各自完成 → 互相 review → 合併 |

## 已知限制（v0.1）

- 規則靠 agent 自律，**沒有 hook 強制**。強制版是 `docs/OPPORTUNITIES.md` 的機會 #6。
- 兩個 agent 同一秒寫看板仍可能互相覆蓋。實務上先讀、改一列、立刻存，衝突機率很低。強制版會改用原子 lease 檔。
- 時間以 agent 自報為準，沒有時鐘同步。
