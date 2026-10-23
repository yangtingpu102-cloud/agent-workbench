# 量測紀錄：無頭呼叫的基本開銷（2026-10-22）

**樣本數：每組 1 次，只能當參考，不能當結論。** 正式結論請用 `headless-overhead` 實驗跑至少 3 輪。

任務：只回覆「ok」，不使用任何工具。環境：Windows 11，Claude Code 2.1.26x（`claude -p`），Codex CLI 0.162（`codex exec`）。使用者裝了多個 claude.ai 連接器（Notion、Gmail、Canva 等）和三個 plugin。

| Host | 組別 | 輸入 token（含快取） | 寫入快取 | 讀取快取 | 費用（API 牌價） |
|---|---|---|---|---|---|
| Claude | 預設 | 162,646 | 162,644 | 0 | $0.65 |
| Claude | 隔離：`--strict-mcp-config --setting-sources project,local --disable-slash-commands` | 9,342～9,405 | 約 6,000 | 約 3,300 | $0.025 |
| Codex | 預設 | 20,247 | - | 64% 命中 | 未回報 |
| Codex | `--ignore-user-config` | 14,729 | - | 84% 命中 | 未回報 |

## 讀法

- 在這台機器上，Claude 的無頭呼叫在做任何事之前就要載入約 16 萬 token，隔離後只剩約 9 千，**費用差約 26 倍**。最可能的來源是連接器和 plugin 帶進來的工具定義與 skill，這次沒有逐項拆解。
- 因此 `cross-review` 從 Codex 呼叫 Claude 時，已經改為隔離模式。
- Codex 的 `--ignore-user-config` 少了約 27% 的輸入，但**它可能同時換掉模型**。Codex 的 JSON 輸出不會回報模型，所以這兩組不一定可比。正式實驗請在 `common.codex_args` 用 `-m` 固定模型。
- 想拆解各項的影響（例如只關連接器、只關 skill），就在實驗裡再加組別，每組只改一個變因。
