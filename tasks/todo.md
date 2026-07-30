# 降低上手門檻（平台／模型／憑證／台股）

## Phase A — 本輪執行

- [x] `scripts/fetch_kronos_model.py`：one-shot 下載 pinned HF revision，逐檔比對
      `model-manifest.json` 的 size/sha256，寫入 `.data/models/kronos/`。
      這是 provisioning，不是 worker runtime download，worker 端禁令不變。
- [x] `start.sh`：`start.ps1` 的 POSIX 對等版（同樣的前置檢查、同樣的參數、同樣的 `-Check`）。
- [x] 兩支 launcher 啟動前載入 repo 根目錄 `.env`（已 gitignored）並注入子行程環境；
      `gui.py:509` 既有 `os.environ` baseline 會自動 `verify_environment()`，
      不需改動 `SessionModelSettings`，key 仍不進 canonical payload／browser storage。
- [x] `.env.example`（不含任何真值）＋ README／runbook 對齊。
- [x] `start.ps1 -Mode research` 在 `.data/models/kronos` 缺失時，錯誤訊息直接指出 fetch 指令。
- [x] 改寫 launcher policy test：以 key allowlist／不 echo／外來鍵被拒／secret 不外洩
      取代原本一刀切的 `assert ".env" not in source`，並補 `start.sh` policy 與 parity 測試。
- [x] 重跑 verify gates。

## Phase B — 台股（已完成）

- [x] `composition/tw_market.py`：XTAI 2026 `ExchangeCalendar`（09:00–13:30 Asia/Taipei）
      ＋ TWSE 官方 2026 開休市表；缺權威來源前 fail closed，不得自行臆造假日。
- [x] `config/instruments/tw.yaml` 補 `provider: openbb` 的 `2330.TW` 對應與 `prices_daily` 能力。
- [x] OpenBB sidecar：`exact_target: MARKET:US/{symbol}` 改為 market-scoped；
      `MARKET:TW/{symbol}` 納入 allowlist **前必須實測** yfinance 對 `.TW` 的 historical 回傳。
- [x] 硬寫 `market="US"` 改為由 symbol 決定：`openbb_rest.py`（capability 宣告＋service
      target）、`openbb_latest.py`（fetch request）、`postgres/gui_research.py`
      （snapshot market 與 `provider_policy_id`）。`financial_datasets.py:50` 保留不動——
      那是該 adapter 自身的 US-only capability 宣告，不是硬編碼缺陷。
- [x] `gui.py:355` 的 `xnas_2026_freshness_policy()` 改為依 symbol 所屬 MIC 選 policy。
- [x] GUI 顯示 TW 資料的 provider／延遲／品質，延遲數據不得標成即時。

## Phase C — 研究導向輸出（已完成）

- [x] Kronos 維持 `shadow`／weight 0 不變；GUI 研究結果頁把 LLM research claim 與
      Kronos forecast 當成主要輸出，`blocked alpha`／`no-order` 降為次要的合規狀態列，
      不再讓使用者以為「跑完什麼都沒有」。

## Review

- 三個門檻都用既有機制解決，沒有新增依賴、沒有動 `SessionModelSettings`：
  fetch 腳本沿用 manifest 既有的 size/SHA-256、`.env` 沿用 `gui.py:509` 既有的
  environment baseline、`start.sh` 只是 `start.ps1` 的逐項對映。
- Docker 相依刻意保留：OpenBB 是 AGPL-3.0-only，process 隔離是授權邊界。
- 驗證：`uv run --frozen python scripts/verify.py --skip-audit` → `[verify] all gates passed`；
  `tests/entrypoints/test_quick_start_script.py` 11 passed；
  fetch 腳本三種路徑（乾淨下載／重跑 verified／竄改後重抓）實測；
  `start.ps1 -Check` 與 `bash ./start.sh --check` 三模式輸出逐字相同；
  `.env` 注入子行程實測（child sees `STONKS_LLM_MODEL=gpt-5`），
  `PATH=/evil` 兩支 launcher 都 exit 1。

## 教訓

- 動到 launcher 前要先讀 `tests/entrypoints/test_quick_start_script.py`：
  那裡的 `assert "X" not in source` 是刻意的安全不變量。要放寬時不能刪斷言，
  必須換成能表達新邊界的更精確斷言（本輪：`.env` 一刀切 → key allowlist＋不外洩）。
- 新增 `scripts/*.py` 後先跑 `ruff format`，否則 verify 第一關就擋下來。
