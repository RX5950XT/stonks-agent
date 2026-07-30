# 代碼精簡與死碼清理

## 掃描結果（基準：2,438 passed／6 skipped、coverage 86.71%）

- [x] 刪除未被任何程式碼引用的 port 模組：`ports/instrument_repository.py`、`ports/trading_calendar.py`
- [x] 刪除未使用符號：`domain/evaluation.metric_map`、`domain/journal.LedgerAccountKind`、`composition/runtime.utc_now`、`regional/base.RegionalMarketDataAdapter`、`api/gui_research.INTENT_HEADER`、`domain/capacity.CapacityPolicy.process_budget_for`
- [x] 合併 17 份重複的 `_utc_now()` 為 `domain/clock.utc_now`
- [x] 合併 Anthropic／OpenAI adapter 重複的 credential 解析為 `_http.resolve_api_credential`
- [x] 合併 Kronos／TradingAgents adapter 重複的 worker HTTP failure 與 origin 驗證為 `adapters/_worker_http.py`
- [x] 清除 terminal.css 未使用的 9 個 CSS 變數與 2 個 class
- [x] 重跑 verify gates

## Review

- 淨刪除約 250 行死碼與重複實作，未改動任何行為分支；新增兩個共用模組
  （`domain/clock.py` 9 行、`adapters/_worker_http.py` 51 行）。
- `ports/repository.py` 的 `ReadRepositoryPort`／`WriteRepositoryPort` 只有自身測試引用，
  但屬既有 Repository Pattern 契約宣告且被 4 份 sidecar boundary denylist 參照，保留不動。
- `scripts/verify_gui_research_runtime.py`、`scripts/verify_snapshot_runtime.py` 無其他引用，
  屬操作者手動 runtime 驗證工具，保留。
- 驗證：`scripts/verify.py --skip-audit` 全綠 —— ruff format／check、strict mypy 393 files、
  2,438 passed／6 skipped、coverage 86.83%、schemas、upstream policy、secret policy；
  另實測 `start.ps1 -Check`、`stonks --help`、`stonks-gui serve --help` 正常。

## 教訓

- 用 word-boundary regex 批次改名前要先確認不會誤中 `self._name` 屬性存取；
  本輪已用 `git diff` 反查確認無誤傷。
- 背景跑 verify 時不要同時改原始碼，否則該輪結果不可信；本輪重跑一次乾淨的 gate。
