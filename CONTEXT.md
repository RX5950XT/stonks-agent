# Stonks Agent 開發交接

更新日期：2026-08-31

這份文件只記錄「讀程式碼與 git log 看不出來」的東西：目前狀態、決策理由、踩過的坑。
專案規範見 `AGENTS.md`／`CLAUDE.md`，使用方式見 `README.md`，教訓見 `tasks/lessons.md`。

## 目前狀態

- 分支 `main`。GUI 排版／可讀性、README 能力與 Kronos 說明、Docker/runtime/security 修復
  已提交並推上遠端；core／RD-Agent 的 OpenSSL runtime package 與 LEAN .NET base digest
  也已更新，對應 legal／VEX 證據同步完成。
- CI 注意：`Hardened core Compose` job 會偶發在 `compose_build_core` 失敗。smoke runner
  刻意 capture 子行程輸出以防 secret 外洩，因此 CI log 只有 typed envelope 沒有 build
  細節。同一 commit rerun 即通過，本機 `scripts/smoke_core_deployment.py` 亦 success；
  遇到時先 rerun 判定 transient，再本機重現拿完整輸出。
- 版本：正式 immutable release 是 `v0.1.2`（不含 GUI）。目前工作樹是**未發布**的 `0.2.0`
  candidate，Local GUI 只在此。不得用 `v0.1.2` 的簽章或 runtime 證據替 `0.2.0` 背書。
- 成熟度 pre-alpha、paper-only。Default deployment 只有 health／readiness，尚未組合
  production business API 或常駐 dispatcher。
- 最近一次完整 gate（`scripts/verify.py --with-postgres`，fresh disposable DB）：
  824 files formatted、Mypy 396 files、2,777 passed／10 skipped、coverage 86.18%，
  schema／migration／security／license／根與 8 個 isolated dependency audits 全綠。

## 目前的能力邊界（容易誤判的部分）

- **行情**：預設 active 來源是 OpenBB → yfinance；GUI 另有需明確 key 才啟用的 Financial
  Datasets US daily fallback。Yahoo 的 `price/quote`、`profile`、`fundamental/*`、
  `discovery/*` 仍不在 allowlist；公司簡介與財報指標改由官方 SEC/TWSE adapter 提供，
  漲跌幅排行仍未實作。
- **市場區域**：`domain/market_region.py` 是 market/MIC/exchange-timezone 單一來源，market
  由 symbol 後綴決定（`.TW`／`.TWO`→TW、`.HK`→HK、其餘 US；`BRK.B` 仍為 US）。US＋TW 行事曆
  已驗證，HK 只會 typed fail closed。TW 假日來自 TWSE 官方 OpenAPI。
- **Kronos**：真 CPU inference 已接進 GUI research artifact，但策略仍是 `shadow`、paper
  weight 0。畫面顯示真 forecast，但 alpha 為 typed `blocked`、paper 決策為 no-order。
  沒有 genuine evaluation／promotion artifact 前不得為了展示閉環放寬門檻。
- **LLM**：需要使用者自備 endpoint／model／key。缺設定時 research POST 回 typed 503，
  durable history 仍可讀。Secret 只存在 process memory。
- **GUI launcher**：只支援完整 source checkout（需要 repo 內 Compose 與 OpenBB
  corresponding-source build context）。Windows `start.cmd` 是 research 雙擊 wrapper，使用
  DB port `55434`；direct `start.ps1` 預設仍是 `55433`。standalone wheel、core image 與
  `v0.1.2` 不支援。

## GUI 操作簡化（2026-08-28）

- 模型表單只保留模型網址、模型名稱、存取金鑰；預算、重試、timeout 等值由 backend
  預設管理，key 仍只存在本次 process memory。
- 首屏保留單一 runtime readiness，移除重複的服務診斷區；可見說明以中文為主，技術來源值
  仍保留以免掩蓋實際資料來源。
- 中文操作與進階命令已移入研究區聊天室，仍只轉成既有安全命令（查行情、切換週期、關注、
  研究、重新整理），不執行任意 shell；目前沒有把 LLM 接到命令執行權限。
- K 線使用有限資料視窗，支援 pointer/touch 拖曳、滾輪與方向鍵。GUI asset 啟動時讀入，
  修改 asset 後必須重啟 server。

## GUI 單欄排版（2026-08-30）

- 16:9 桌面版的研究／圖表與投資組合／資料來源不再排成 2×2；`.analysis-grid`、
  `.context-grid` 與研究內的 argument 區塊都採單欄直排，讓每個工作區完整使用橫向寬度。
- K 線的 canvas 放在獨立 `.chart-stage`，說明文字在 stage 外獨立成列，避免固定高度造成文字
  疊在圖表上。窄版仍維持底部 navigation、無橫向溢出與不 autofocus。
- 研究區內的 `.research-chat` 同時承接中文操作與 `ADD`／`DROP`／`RESEARCH`／`REFRESH`／
  `HELP`；`RESEARCH` 沿用既有 research event，不新增命令執行權限。
- K 線週期支援 `1m`／`2m`／`5m`／`15m`／`30m`／`90m`／`1h`／日線／週線／月線／年線；範圍由 GUI
  映射成 bounded `lookback_days` 並寫入 query string。Canvas 使用最多 100 根的可視視窗與
  pointer-capture pan；`2m` 受 provider 上限限制為最多 21 天，EOD 長範圍由最新能力段落定義。

## 行情來源與研究聊天室（2026-08-30）

- GUI 預設以 `OpenBB -> yfinance` 為主；只有 local process 設定
  `STONKS_FINANCIAL_DATASETS_API_KEY` 時，OpenBB 失敗才會嘗試 Financial Datasets 的 US
  daily fallback。這是 paid optional path，未設定 key 或能力不符就 fail closed。
- fallback 成功會保留 `financial_datasets` provider 與 `fallback_source_used` warning，畫面標成
  degraded；不使用 replay、fixture、hard-coded quote 或舊快取補資料。Financial Datasets 真實
  外部 key runtime 尚未在本機驗證，contract tests 只驗證 mapping／failover。
- OpenBB/yfinance 若在有效歷史回應混入 non-finite OHLCV，adapter 只排除該筆並在 metadata
  標記 `invalid_record` warning；沒有任何有效 bar 時仍 fail closed。
- 價格與成交量顯示優先使用 `latest`，缺少時取最後一根有效 bar；缺資料會顯示明確失敗狀態，
  不再留下空白數值。
- **本機 Docker**：Docker Desktop 4.84 原本因 Windows 11 25H2（build 26200）缺少 legacy
  `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ReleaseId` 而在 backend 初始化時
  crash。2026-08-20 以系統管理員補回 `ReleaseId=2009` 後已恢復，Docker client／server
  29.6.2、WSL2 kernel 6.18.33.2；launcher 12 tests、PostgreSQL gate、core deployment smoke、
  authenticated Kronos/OpenBB、GUI market/paper 均重驗通過。`com.docker.service` 維持
  Manual/Stopped 是正常 per-user backend 狀態，不代表 engine 未啟動；以 `docker version`
  的 Server 回應為準。
- **Docker 精準清理（2026-08-29）**：移除 1 個舊 exited Kronos container、2 個空 network、
  未引用的 `alpine:latest`，並清空 14.71 GB `desktop-linux` build cache。保留 3 個 running
  GUI services、pinned core／PostgreSQL／Python／Alpine／Grype／Syft images 與 PostgreSQL
  volume；清理後為 3 containers、8 project images、1 volume、0 B build cache。
- **Core supply chain**：Alpine v3.23 rolling repository 已移除 `libpq/postgresql18-dev`
  18.4-r0，main 改 pin 18.6-r0（aports `c2ee21f8…`，security release）。實際 final image
  重建後已同步 deterministic Alpine／Python source closure、canonical SBOM hash 與 legal
  notice。Exact image `stonks-agent-core:a234cc7a5996` 的 SBOM 是 97 packages／865 components
  （component hash `e80fe0cc…`）；fresh Grype DB（2026-08-19）為 17 Medium／4 Low／1
  Negligible、active High/Critical=0，12 個 reviewed High 由 11 份 exact VEX 抑制。根與 7 份
  isolated locks 的 `cryptography` 已升 50.0.0；TradingAgents／OpenBB 的 `aiohttp` 及
  TradingAgents 的 `langgraph-checkpoint-sqlite` 亦升至無已知漏洞版本。正式 release 前仍須
  由 registry exact digest 重跑 unsigned candidate、scanner、signing/attestation gate。

## 已完成階段摘要

| 階段 | 內容 |
|---|---|
| P0 | in-memory fake／replay 全閉環與最小 security/reliability 基線 |
| P1 | PostgreSQL 0001–0017、PIT evidence/snapshot、repositories/UoW、durable job/outbox/inbox |
| P2 | research／LLM contracts、PIT context builder、model adapters、TradingAgents worker、ai-hedge-fund PEAD/event-study、report/render/delivery、research pipeline |
| P3 | strategy registry／evaluation／promotion、baselines、evaluation policy、opinion mapper、Kronos manifest＋worker＋evaluation、Qlib quant-lab worker、strategy API/CLI |
| P4 | paper trading domain、schema 0010–0014、portfolio construction、risk authorization、deterministic broker、ledger、cycle runner、monitoring、operator commands、read projections |
| P5 | external platform contracts、AI-Trader adapter、community policy、backtest contracts、Nautilus／LEAN sidecars、cross-engine parity、RD-Agent sandbox、`config/features.yaml` |
| P6 | security composition、secret refs、rate limit、telemetry、budgets/SLO、S3 artifact transport、hardened deployment、release bundle＋keyless signing、resilience drills、capacity report、formal `v0.1.2` closure |
| P7–P12 | Local GUI：OpenBB live path、Stonks Desk、fenced worker dispatcher、durable research、Kronos GUI 接入、`start.ps1` launcher、`clean_workspace.py` |
| P13–P16 | Kronos snapshot-bound artifact、citation laundering／tool timeout／ingress 修復、session-scoped LLM settings、backend-owned freshness/quality |
| P17–P19 | 死碼清理與重複實作收斂、`start.sh`＋`.env`＋`fetch_kronos_model.py` 降低上手門檻、台股接入、研究輸出重排 |
| P20–P23 | GUI 全面重設計（graphite dark evidence workbench）、UI/安全/死碼三路稽核、pre-push 完整驗證、PR #12 合併至 `main` |

各階段的 exact 測試數字、hash 與 CI run ID 見 git log 與
`docs/verification/p6-handoff-evidence.md`；不在本檔重複。

## 已確定架構決策

1. 自有 canonical contracts 與 orchestration authority，不讓 upstream internal types 跨 process。
2. Authority chain：Evidence/Artifact → Opinion/Signal → deterministic Target → Risk →
   Reservation → OrderIntent → Receipt/Fill → balanced Journal。
3. Stochastic LLM/Kronos output 先封存 artifact，deterministic replay 從 artifact 開始，
   不宣稱 fresh inference bit-identical。
4. Core runner 是 transaction owner；remote worker 無 DB credentials，late result 用 lease
   generation/nonce fencing。
5. 同帳戶序列化並先 reserve cash／sellable position，防並行雙花與超賣。
6. OpenBB、Kronos、TradingAgents、Qlib、RD-Agent、LEAN／Nautilus 各自獨立 lock/image，
   不進 core environment。Docker 相依刻意保留：OpenBB 是 AGPL-3.0-only，process 隔離是
   授權邊界而非效能選擇。
7. AI-Trader 只作 external community HTTP adapter，不提交 canonical order。
8. GUI 政策是「只允許同源本地 script」：CSP `default-src 'none'` 加全部 `'self'`，
   禁 inline／eval／外部 origin／`data:`；未引入 npm、node_modules 或打包器。
9. GUI 字級只用 `terminal.css` 的 `--fs-2xs..--fs-2xl`（12/13/14/16/20/24/32px）。
   12px 是 CJK 下限，不要再新增字級或寫死 `rem`。`tests/policy/test_gui_assets.py`
   的 150,000 bytes 總量目前約剩 5,494 bytes headroom，新增 CSS 前仍先刪等量死碼。

## 上游研究結論

`.research/upstreams/` 有 9 個 shallow snapshots（ai-hedge-fund、Dexter、TradingAgents、
Kronos、daily_stock_analysis、AI-Trader、OpenBB、Qlib、RD-Agent），只供閱讀、不進版控、
不得直接 import 或 vendor。固定 commits 與授權證據在 `docs/research/`。

- ai-hedge-fund：MIT，可選擇性移植（已移植 PEAD／event study）。
- Dexter：缺完整 MIT license text，禁止複製 source／prompt／assets。
- TradingAgents：Apache-2.0，可作 isolated research worker。
- AI-Trader：server 授權聲明矛盾，禁止複用程式碼。
- OpenBB：AGPL-3.0-only，只能作 optional sidecar，process boundary 不是法律豁免。

## GUI 本機預覽（不需 Docker／PostgreSQL）

改 `src/stonks_agent/gui/assets/` 時不必起完整 stack。用 `tests/e2e/test_gui.py` 的 fake
ports 直接組 app 就能在瀏覽器實測所有狀態：

```python
# 以 importlib 載入 tests/e2e/test_gui.py，取 Source／ResearchFacade／research_options，
# 再 create_gui_app(...) 交給 uvicorn 跑 127.0.0.1:8787。
```

資產以 `max-age=300` 快取且在 app 啟動時讀進記憶體，改 CSS 後要重啟 server，
瀏覽器端再對 `<link>` 加 query string 破快取。
UI fixture 只供視覺狀態測試；latest chart 預覽必須使用 actual market mode，不能把
`tests/e2e` 的固定兩根 bar 當成真實行情。

## Kronos 本機驗證（Docker 壞掉時的替代路徑）

`scripts/verify_kronos_runtime.py` 需要 Docker。這台機器 Docker 不可用時，可用
`workers/kronos/.venv` 直接跑 in-process inference 確認模型與推論本身沒壞：

1. 把 pinned 上游 source 解到 repo 外的暫存目錄（`.data/research-downloads/
   kronos-67b630e.tar.gz`，sha256 對 `model-manifest.json` 的 `source_archive_sha256`），
   因為 `create_native_runtime()` 會 `import model`，該套件只存在於上游 tree。
2. `PYTHONPATH=<repo>;<解開的 kronos 源碼>` 用 `workers/kronos/.venv/Scripts/python.exe`
   建 `WarmOnceModelLoader` + `KronosWorker`，直接呼叫 `forecast()`。

注意 `KronosWorker.forecast` 會比對 `request.runtime` 與 policy 的 exact runtime identity，
而本機 venv 的 torch 版本與 `config/workers/kronos_cpu.yaml`（Docker image 的
`2.13.0+cpu`）不同，所以請用本機實際組出的 `policy.runtime_identity`，不要用 config。
此路徑只證明推論可跑，不能取代 authenticated container 的 runtime identity 驗證。

## 可重跑驗證

```powershell
uv sync --frozen
uv run python scripts/verify.py
$env:STONKS_TEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:55432/stonks_test'
uv run python scripts/verify.py --with-postgres
uv run stonks fake-cycle --symbol AAPL --as-of 2026-01-02T21:00:00Z --idempotency-key smoke
```

`verify.py` 涵蓋 format、lint、strict mypy、tests/coverage、schema drift、upstream/license
policy、secret scan 與每份 isolated lock 的 dependency audit；`--with-postgres` 另驗
migration drift 與真實 DB 整合。

關鍵 regression（改動相關區域前先讀）：

- `tests/e2e/test_fake_cycle.py`：next-session fill、balanced journal、replay、future
  evidence fail-closed、concurrent no-double-spend。
- `tests/application/test_execution_authority.py`：research/forecast 與未授權 principal
  無法觸發 `ExecutionPort`。
- `tests/application/test_fake_job_fencing.py`：duplicate result 不重複寫 event/outbox。
- `tests/entrypoints/test_quick_start_script.py`：launcher 的 `assert "X" not in source`
  是刻意的安全不變量，放寬時必須換成更精確的斷言，不能直接刪。

## 下一個代理的起點

1. 先讀 `AGENTS.md`、本檔、`tasks/lessons.md` 與 `docs/runbooks/local-gui.md`。
2. 不得移動或刪除任何 protected release tag，也不得弱化 required-reviewer、exact identity
   或五證據 closure gate。
3. Research principals 只能讀 canonical evidence／artifacts，不能取得 DB、queue、risk 或
   execution authority。
4. 需要 runtime artifact 的測試必須自建 scoped state，只清理自己建立的路徑；gitignored
   `.data`／`.research` 會在本機掩蓋 CI 才會爆的缺失。
5. 每輪任務完成同步精簡 `AGENTS.md`、`CLAUDE.md`、本檔與 `tasks/todo.md` review。

## 標的儀錶板與 K 線密度（2026-08-30）

- `panel-overview` 是目前標的的行情概覽，直接由已驗證的 bars 衍生區間報酬、高低點、成交量、
  單根波動、漲跌統計與資料涵蓋；少量週線／月線使用有限 bar slot 置中，避免 K 線間距過大。
- GUI 已移除「資料檢查」面板與側欄入口；後端仍保留 provider metadata、quality、freshness 與
  fail-closed 行為，不能因 UI 隱藏而刪除資料治理。
- 公司簡介、損益表、資產負債表已由官方 SEC/TWSE adapter 組合進儀錶板與研究 snapshot；
  現金流、估值、segments、新聞與宏觀仍未組合，研究聊天不能代替缺少的結構化資料。

## 年線與長時間範圍（2026-08-30）

- `BarInterval.YEAR` 對外是 `1Y`，但 OpenBB/yfinance 沒有直接採用未驗證的 `1Y`；
  `OpenBBLatestMarketDataSource` 取已驗證的 `1M`，在 core 依年度聚合 OHLCV。
- EOD `lookback_days` 上限為 36,525 天，canonical bars 上限為 20,000；latest OpenBB response
  上限為 4 MiB。這讓 AAPL 日線全部 11,519 根、週線 2,386 根、月線 500 根可安全回傳。
- GUI 範圍按 interval 收斂：YTD 依瀏覽器當年 1 月 1 日計算；長 EOD 可選 5 年、10 年、全部；
  年線可選 3 月、6 月、YTD、1 年、5 年、10 年、全部。狀態寫入 query string 的 `i`／`r`。
- 2026-08-30 actual runtime：AAPL 年線全部 42 根、日線 10 年 2,511 根，均為
  `openbb:yfinance`；1920×1080 年線全部預覽水平溢出 `0`，console errors/warnings 均為 `0`。

## 免費資料源與 Agent 資料快照（2026-08-30）

- 官方 SEC `submissions`／XBRL `companyfacts` 與 TWSE OpenAPI 月營收／綜合損益／資產負債已接入；
  `GET /api/v1/instrument/overview` 直接供儀錶板讀取。
- `us-research/1` 與 `tw-research/1` 會在研究啟動前封存 OpenBB 行情＋官方公司資料，保持同一
  `as_of`／`observed_at`；研究 Agent 新增 `fundamental_snapshot`、`filing_history` 兩個唯讀工具，
  不能建立 target/order。
- 來源只以 fixed allowlist、8 秒 timeout、12 MiB body cap、process request budget、cache、
  JSON／schema／PIT 檢查接入；BLS/FRED、OpenBB 其他 extensions 與 TradingAgents news／insider／
  macro／prediction markets 目前只完成能力盤點，未冒充 active。
- 參考專案的完整差距與資料權利記錄在 `docs/research/free-market-data-sources.md`、
  `docs/research/virattt-projects.md`；OpenBB AGPL、Dexter license 不完整的界線不變。

## 標的儀錶板歷史財務（2026-08-31）

- `InstrumentFact.history` 使用 bounded immutable `InstrumentObservation`；SEC 最多保留 12 筆並保留期間、發布時間與 available time。
- `panel-overview` 桌面版是摘要列、行情／公司財報左右分欄，近期申報與歷史財務跨欄；窄版才改單欄。
- 「近期申報」用原生 `<details>` 展開，連結只接受 `data.sec.gov`、`www.sec.gov` 與 `openapi.twse.com.tw` 的 HTTPS URL。
- 實際 API：AAPL 為 SEC 14 facts／20 filings；2330.TW 為 TWSE 19 facts。TWSE 公開財報端點目前只有最新彙總列，歷史比較不可宣稱已完整取得。

## 自選股與側欄導航清理（2026-08-31）

- 側欄只保留觀察清單與安全提示；四個工作區導航按鈕及 `product.js` 的 active/hash/deep-link 死碼已移除。
- watchlist 不再有固定 12 檔上限；`/api/v1/market/quotes` 保留 4,096 字元 query 邊界、去重、provider 每分鐘 30 次與 4 workers。
- URL 讀取保留所有符合 `SYMBOL_PATTERN` 的項目；OpenAPI、runbook 與回歸測試已同步。

## 全專案死碼清理（2026-08-31）

- 移除無 caller 的 `run_bounded_command()`、`_provider_policy()`、舊 generic repository port 與其測試，以及已被現行資料品質契約取代的 `EntityId`／`QualityAssessment`。
- GUI 未再產生的 `data-certainty` selector 與空 media block 已移除；Typer／FastAPI 入口、optional adapter、migration、golden fixture 與仍被使用的 provenance 保留。
- `ruff`、`mypy src packages`、49 筆相關測試、schema／policy／secret gate 通過；完整 canonical verify 的 4 筆失敗是本機 Docker Engine 未啟動，workspace cleaner 只做 dry-run 未刪除。

## README 與遠端 PR 整合（2026-08-31）

- 根 README 已重寫成目前可用能力的短入口：Stonks Desk、K 線範圍、儀錶板、研究聊天室、資料來源、paper 邊界與驗證命令；歷次紀錄不再堆在 README。
- 遠端 PR #13（`actions/attest`）與 #14（`docker/login-action`）的 required checks 全綠且可合併；#1–#11 為較舊的 Dependabot PR，CI 仍失敗，合併前需先更新或關閉，不能跳過 required checks。
