# 兩邊工作模式同步（Claude Code 與 Codex）

目標：Claude Code 和 Codex 遵守**同一套**工作規則和行動流程，差異能被一眼看出來，而且不會因為各自改檔而慢慢漂移。

## 原則

- **單一來源**：`shared/ai-shared.md`。內容以 Claude 這邊的版本為主，是從 `~/.claude/CLAUDE.md` 的 `ai-shared` 區塊逐字取出的。
- **兩邊的全域規則檔各有一個標記區塊**，內容必須和來源一致：

  ```
  <!-- ai-shared:start -->
  …（和 shared/ai-shared.md 相同）…
  <!-- ai-shared:end -->
  ```

  區塊以外的任何內容都各自保留，同步工具不會動。
- **專案層級的流程**（領任務、租約、交叉審查、證據、交接）寫在 repo 的 `AGENTS.md` 和 `docs/COLLAB-PROTOCOL.md`，兩邊讀的是同一份：Claude 用 `@AGENTS.md` 匯入。
- **投影片（PPT）、文件、PDF、試算表這類製作工具以 Codex 為主，這個 repo 不碰**：同步和檢查工具不比較、不同步、不寫入它們。模型與推理強度、graphify 和 RTK 的段落也不在同步範圍內。

## 日常指令

```bash
python scripts/wb.py parity                  # 唯讀：兩邊差在哪（規則區塊、這個 repo 的 plugin、hook）
python scripts/wb.py sync --host codex       # 預設只顯示差異（dry run），不寫入
```

`parity` 比對的是「這個 host 實際載入的那一份」：

| host | 載入來源 | 備註 |
|---|---|---|
| Claude Code | 目錄型 marketplace 直接讀 checkout；否則讀安裝快取 | 目錄型時，版本**跟著 checkout 目前切到的分支**走 |
| Codex | 安裝快取 | repo 升版後要重新安裝才會更新，`parity` 會標出版本落後 |

有差異時結束碼是 1，沒有就是 0，可以拿來當檢查。

## `sync` 怎麼保護你的檔案

- **預設是 dry run**：只印出差異，什麼都不寫。
- **`--apply` 才會寫入**，而且先在同一個資料夾留一份 `<檔名>.bak-<時間>` 備份，再用暫存檔原子替換，寫完會重新讀回比對。
- **只動標記區塊**。檔案還沒有區塊時，要明確指定怎麼加：
  - `--adopt-heading "<既有標題>"`：把那個標題底下的段落（到下一個標題或 `@` 匯入行為止）換成區塊；
  - `--insert`：把區塊加在檔案最前面。
- 標記不成對（只有開頭、重複等）時直接拒絕，不寫入。
- 保留原本的換行風格（CRLF 或 LF）。

## 讓 Codex 同步（把下面這段貼給 Codex）

```text
請把 agent-workbench 的共用工作規則同步到你的全域 AGENTS.md：
1. 在你的 agent-workbench checkout 切到 main 並 git pull。
2. 先看 shared/ai-shared.md，再執行
   python scripts/wb.py sync --host codex --adopt-heading "# Efficient execution defaults"
   （把標題換成你檔案裡現有的規則段落標題；預設只顯示差異，不會寫入）。
3. 確認差異只出現在那一個段落：你的 @ 匯入行、graphify 段落，以及任何投影片（presentations）、
   文件、PDF、試算表相關設定都不能出現在差異裡。
4. 沒問題再加上 --apply（會自動備份）。
5. 最後執行 python scripts/wb.py parity，規則區塊應該顯示 in sync。
不要修改 plugin、模型設定，也不要動任何投影片相關的東西。
```

同步會**取代**那個段落，所以原本只存在於 Codex 檔案裡的規則會消失。想保留的話，先把它們加進 `shared/ai-shared.md`（兩邊一起生效），再重新同步，不要只留在其中一邊。

## 已知限制

- **Claude 的目錄型 marketplace 會跟著 checkout 的分支變**：在同一個資料夾切到沒有某個 plugin 的分支，Claude 就會少那個 plugin。`parity` 會標示 `live from the checkout`。
- **Codex 的 plugin hook 是否已信任，從設定檔看不出來**：`parity` 只能標示 `UNVERIFIED`，要在 Codex 輸入 `/hooks` 確認。沒信任的話，租約只是建議，不會擋下編輯。
- **交接檔只能有一種寫法**：`handoff` skill 用結構化格式寫 `.ai/HANDOFF.md`。如果你有別的腳本也寫這個檔案，請確認它不會整份覆寫掉結構化內容。
