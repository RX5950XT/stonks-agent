# 前端品質／安全／死碼稽核（2026-08-01）

## 本輪執行

- [x] 以實際瀏覽器審查桌面、窄版、鍵盤、狀態與 console，整理可重現的 UI 小錯誤。
- [x] 審查 GUI/API 的輸入驗證、XSS／CSRF、secret、rate limit、錯誤洩漏與依賴漏洞。
- [x] 掃描 GUI 與相鄰 composition 的未引用程式、重複樣式、失效 selector／DOM contract。
- [x] 僅修復有證據且低風險的問題，新增 regression tests 並跑相稱的完整驗證。
- [x] 同步精簡 `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md`、`tasks/lessons.md` 與本檔 review。

## Review

- 三個子代理分別完成 UI micro-audit、安全 source-to-sink review 與 dead-code audit；UI
  初評 18／20，發現的 loading、deep link、degraded／runtime state、320px footer、placeholder
  與 44px touch target 問題均已修正。
- loopback API 現在會在 provider 呼叫前拒絕 cross-site `Origin`／Fetch Metadata；API key
  關閉 autocomplete 並在 `pagehide` 清除。既有 CSP、DOM text sink、SSRF pinning、mutation
  proof、ownership 與 rate limit 邊界維持通過，locked dependency audit 無已知漏洞。
- 移除未使用的 `freshnessRatio`／`--rail-decay`、`dom.save`、catch bindings 與舊 favicon；
  `PaperCapability.rows` 因外部 API schema 相容風險保留。GUI local assets 共 149,362 bytes（上限 150,000）。
- Playwright 實測 1440×1000／320×844、direct hash、degraded、typed market failure 與 quiet
  refresh 429；320px 無 overflow／footer overlap，最小 touch target 44px，乾淨 console 0 error。
- Focused GUI gate 72 passed；完整 `scripts/verify.py` 通過：2,478 passed／9 skipped、coverage
  86.87%，Ruff、Mypy 396 files、schema、secret、upstream 與全部 isolated dependency audits 全綠。

---

# GUI 完全重設計（2026-08-01）

## 設計 brief

- 使用情境：個人投資者在桌面瀏覽器做 10–30 分鐘的標的研究，需要先看結論、證據與
  資料時效，再決定是否深入；窄版以快速檢視與追蹤研究進度為主。
- 視覺方向：低彩度 graphite dark research workbench、ink text、單一 cobalt action color。
  以分隔線、留白、表格與清楚層級組織資訊，不使用 dark-fintech 金色／霓虹模板、glow、glass、
  滿版 rounded cards、彩色側條、tiny uppercase kickers 或 nested metric grids。
- IA：固定 utility header → backend capability map → instrument context → 四個 task views；
  市場、研究、模型、Kronos、Paper／Risk、資料品質全部在首屏揭露 truthful state 與入口，
  詳細設定與執行透明度才採 progressive disclosure。
- 技術邊界：保留既有同源 CSP、所有 DOM/API contracts、typed failure、paper-only、
  provider freshness、model secret 與 research authority 不變量；不引入 UI framework 或外部字型。

## 本輪執行

- [x] 重排 semantic HTML 與 task navigation，修正 navigation hash 覆寫 market state。
- [x] 重寫 design tokens、layout、typography、controls、loading／empty／failed／success states。
- [x] 套用 graphite dark tokens，新增 actual backend capability map 與 combined readiness。
- [x] 修正 quiet auto-refresh 清空 quote/chart、覆寫輸入及 transient failure 擦除成功資料的行為。
- [x] 修正 composite search focus ring 疊加造成的粗藍內外雙框。
- [x] 桌面 1440×1000、tablet 1024×768、mobile 390×844／320×844 實際瀏覽器驗收。
- [x] 驗證 AAPL、2330.TW、無 LLM、invalid model route、research history、navigation、console、overflow。
- [x] 跑 focused GUI tests、Ruff／policy gate，更新專案規範與交接文件。

## Review

- 完全移除舊 dark-fintech／金色 CTA／卡片海語彙，改為 graphite dark evidence workbench；沒有
  新增 framework、外部字型或 CSP 例外，既有 DOM/API 與安全／交易 authority 邊界不變。
- 首屏 capability map 直接列出市場資料、AI 研究、模型連線、Kronos、Paper／Risk、資料品質；
  每項由實際 capability／market event 更新。LLM blocker、Kronos shadow weight 0、Paper
  integrity、kill switch 與非 tick 語意均可見，未實作的交易 mutation 沒有假入口。
- Runtime：research mode 一鍵啟動後 OpenBB／PostgreSQL／Kronos／research 全為 ready；
  AAPL 與 `2330.TW` actual bars 成功，台股市場標籤正確，invalid model route typed fail closed。
- Playwright：1440×1000、1024×768、390×844、320×844；320px `scrollWidth=innerWidth=320`、
  boot active element 為空、navigation 保留 query market state、console 0 error。連續觀察 31 秒
  quiet refresh：無「更新中」閃白、無資料擦除、`2330.TW` 未送出的輸入保持不變。
- Dark Playwright：body 11.7:1、surface 11.15:1、heading 16.73:1、capability state 8.61:1；
  390px 首屏完整顯示六項功能，320px 無 overflow 且 boot 不 autofocus。
- Focus ring regression：鍵盤聚焦搜尋框時 inner input `box-shadow=none`，wrapper 保留單一
  3px ring 與 primary border；截圖確認粗藍雙框消失，console 0 error，focused 65 passed。
- Focused gate：78 passed／3 skipped；GUI assets 149,362 bytes（上限 150,000），所有 JS
  小於 800 行。
- Full gate：`scripts/verify.py --skip-audit` 全綠；Ruff、Mypy 396 files、schemas、upstream／
  secret policy 通過，2,470 passed／9 skipped，coverage 86.86%。

---

# 一鍵啟動與產品級預覽驗收（2026-08-01，已被重設計任務取代）

## 本輪執行

- [ ] 驗證 `start.ps1` 三種模式的 preflight，確認預覽所需依賴與 runtime。
- [ ] 以預設 research mode 一鍵啟動本機 GUI，保留可診斷的 launcher log。
- [ ] 用 headed Playwright 實測桌面主流程、窄版、鍵盤與失敗／降級狀態。
- [ ] 依實際證據判定目前是否達產品級，不以測試全綠代替 UX／部署驗收。
- [ ] 同步精簡 `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md` 與本檔 review。

## Review

- 待本輪驗收完成。

---

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

---

# Pre-push 完整驗證與發布（2026-08-02）

## 本輪執行

- [x] 盤點目前分支、遠端、工作樹與待發布變更範圍。
- [x] 獨立審查程式碼、安全／敏感內容與文件一致性。
- [x] 依審查結果以 TDD 修正 truthful capability、deep-link、market label、secret lifecycle
      與文件漂移。
- [x] 執行完整驗證 gate，所有失敗修到通過。
- [x] 檢查 staged diff、建立符合規範的 commit。
- [x] push 後驗證遠端 commit、tracked tree、敏感內容與 CI 狀態。

## Review

- 三個唯讀子代理完成 code、安全與 docs/tests 獨立審查；gitleaks、repository secret scan
  與 generated artifact path scan 均無 finding。
- 新增 regression 先得到 4 failures，再修到 policy 14 passed；GUI focused suite 85 passed。
- Playwright 實測 AAPL direct hash、`6488.TWO`、mocked unverified model／failed research service、
  `pagehide` secret reset；正常 runtime console 0 error。
- `uv run --frozen python scripts/verify.py --with-postgres` 使用 fresh disposable DB：824 files
  formatted、Mypy 396 files、2,772 passed／10 skipped、coverage 86.18%，全部 security、schema、
  migration、upstream 與 dependency gates 通過；disposable DB 已清除。
- 功能 commit `6afc07f` 已推送至 `origin/feat/local-gui-research-console`；本機與遠端 tip 一致。
  遠端 exact path scan 無 runtime output／database／private env／key artifact，高風險 token scan
  排除刻意測試 fixtures 後為 0；該 branch 沒有設定 push-triggered GitHub Actions run。
