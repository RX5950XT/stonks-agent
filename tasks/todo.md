# Docker 修復、runtime 實測與 supply-chain closure（2026-08-20）

## 本輪執行

- [x] 讀 Docker backend 完整 log，鎖定 Windows registry 缺 `ReleaseId` 的 root cause。
- [x] 經 UAC 補回 legacy `ReleaseId=2009`，重啟並驗證 Docker client/server 29.6.2。
- [x] 重跑 launcher 12 tests、291 個 PostgreSQL tests 與 `start.ps1 -Mode research -Check`。
- [x] 修 Kronos verifier cold-build timeout 與跨 container clock 先後序，新增兩個 regression tests。
- [x] 跑 authenticated Kronos CPU 真推論、OpenBB→yfinance provider、durable snapshot。
- [x] 實際啟動 GUI market／paper mode，讀真實 API projection 後清理 containers。
- [x] 修 Alpine rolling repository 移除 18.4-r0：pin `libpq/postgresql18-dev` 18.6-r0。
- [x] 由 final image 重建 Alpine／Python corresponding source、canonical SBOM 與 legal evidence。
- [x] fresh Grype 修復 `cryptography` High：根與 7 份 isolated locks 全升 50.0.0。
- [x] 審查 OpenSSL QUIC-only CVE 並以 exact APK purl OpenVEX fail-closed；重掃 active High/Critical=0。
- [x] 完整 `scripts/verify.py --with-postgres`（含所有 isolated lock audits）。

## Review

- Docker：`ReleaseId=2009` 持久存在；engine 是 WSL2 kernel 6.18.33.2、12 CPUs、約 47 GiB。
  `com.docker.service` Manual/Stopped 是正常 per-user backend 狀態，Server API 可回應才是準則。
- Runtime 實測：Kronos 3 paths／actual model inference；OpenBB health + 5 source members +
  yfinance 2 rows；snapshot 33 evidence（無 fixture fallback）；GUI market AAPL 21 bars；GUI paper ready。
- Core smoke：paper-only、runtime hardening、migration、DB outage/recovery 與 persistence replay 全過。
- Supply chain：Alpine source archive兩次 byte-identical（`88ee6894…`）；Python source archive兩次
  byte-identical（`dbd1b088…`）。Exact final image `stonks-agent-core:a234cc7a5996` 的 canonical
  SBOM 為 97 packages／865 components（`e80fe0cc…`）。
- Grype DB v6.1.9（2026-08-19）：17 Medium／4 Low／1 Negligible，active High/Critical=0；
  12 個 reviewed High 由 11 份 exact VEX 抑制。`cryptography 49.0.0` 升 50.0.0；OpenSSL
  CVE-2026-14456 只存在 QUIC server listener，core 沒有 QUIC。
- 完整 gate：824 files formatted、Mypy 396 files、2,777 passed／10 skipped、coverage 86.18%；
  schema、migration、secret、policy 與根＋8 個 isolated runtime dependency audits 全綠。

---

# README：Kronos 整合狀態與使用說明（2026-08-20）

## 本輪執行

- [x] 查證 Kronos 整合是否真的存在（contracts、worker、adapter、alpha mapper、GUI 接入、config）。
- [x] 在本機 Docker 不可用的情況下，用 `workers/kronos/.venv` 直接跑 in-process 真實推論驗證。
- [x] README 新增「能做什麼」能力→入口對照表，取代原本較抽象的「它是什麼、不是什麼」。
- [x] README 新增「Kronos 價格預測」章節：做什麼／不做什麼／pinned 版本表／三步用法。
- [x] 狀態表與上游整合表的 Kronos 列加上章節錨點連結。
- [x] `CONTEXT.md` 記錄 Docker 壞掉時的 Kronos in-process 驗證路徑與其限制。

## Review

- 結論：Kronos **已整合完成**，不是佔位。範圍涵蓋 frozen contracts
  （`packages/contracts/.../kronos.py`）、隔離 worker（`workers/kronos/`，CPU/CUDA 各自
  lock 與 Dockerfile target）、HTTP adapter、snapshot-bound research adapter、
  deterministic alpha mapper、evaluation、GUI sidecar lifecycle 與 `start.ps1` 前置檢查。
- 實測（2026-08-20，未經 Docker）：`Kronos-small` @ `901c26c` 於 `workers/kronos/.venv`
  （torch 2.12.1+cpu）載入成功，3 個 seed 產出 3 條 1-bar 路徑，末值報酬
  -0.000819／+0.000551／-0.002517，latency 234 ms，`result_artifact_hash`
  `edba348a…70deea`。
- 既有 Docker 證據：`.data/runtime/kronos-verify-20260728133116.stdout.json`，
  `actual_model_inference: true`、`paper_eligible: false`、`paper_weight: "0"`。
- 測試：8 個 Kronos 測試模組 91 passed；`tests/policy` 188 passed／3 skipped
  （含 `test_docs_handoff.py` 與 GUI 資產預算）。
- README 只描述已驗證能力：`cuda` profile 明確標為 `unsupported`（CI runner 無 GPU），
  Kronos 明確標為 `shadow`／paper weight 0／alpha typed `blocked`／paper 決策 no-order。

---

# GUI 排版與可讀性修正（2026-08-20）

## 本輪執行

- [x] 以 `tests/e2e/test_gui.py` 的 fake ports 起本機 preview server，在瀏覽器實測而非讀 CSS 猜。
- [x] 收斂字級：25 個散落 `rem` 值 → 7 個 `--fs-*` token，最小 12px（CJK 筆畫下限）。
- [x] 修 primary action 對比：白字 3.04:1 → 深色 ink 6.49:1（新增 `--color-on-primary`）。
- [x] 修 `.context-grid` 沒有 `align-items: start` 造成的約 840px 空白面板。
- [x] 修 `#model-settings` 缺 `scroll-margin-top`，capability map 錨點被 sticky topbar 蓋住。
- [x] 移除 Paper 面板 6 個與下方標題重複的全大寫 eyebrow，及 7 個未被引用的 palette 變數。
- [x] 維持 `tests/policy/test_gui_assets.py` 的 150,000 bytes 資產預算。

## Review

- 量測（1769px viewport、research succeeded 狀態）：可見文字字級由 24 種降為 8 種，
  最小值 9.6px → 12px；WCAG AA 對比失敗 2 → 0。
- 資產預算：149,362 → 149,865 bytes（上限 150,000）。token 名稱用 `--fs-*` 而非
  `--text-*`、刪掉 7 個死變數與 6 段 eyebrow 才擠得進去；剩 135 bytes headroom，
  下一輪若要再加 CSS 需先找出等量死碼。
- `--sidebar-width` 13.5rem → 15rem：側欄副標在 12px 下不再折行。
- 沒有動的：`.analysis-grid` 右欄（chart）與左欄（research）高度差造成的空白。
  兩欄式 dashboard 的固有結果，sticky chart 需要包在 min-width media query 內，
  這輪不值那些 bytes。
- 驗證：`uv run python scripts/verify.py` → ruff format／check、strict mypy 396 files
  全過；`pytest -m "not postgres"` 2,474 passed／6 skipped、coverage 86.87%。
  唯一 failure 是 `tests/entrypoints/test_quick_start_script.py` 的 4 個 Docker 依賴測試，
  在 `git stash` 後的 HEAD 上失敗結果完全相同——本機 Docker Desktop 4.84 啟動即 crash
  （`retrieving ReleaseId: The system cannot find the file specified`），與本輪改動無關。

---

## 歷史輪次摘要


較早輪次的完整 review 見 git log；以下只保留結論。

| 輪次 | 結果 |
|---|---|
| 文件整理與精簡（2026-08-02） | README 396→331 行、CONTEXT 425→118 行、todo 201→34 行；`AGENTS.md`／`CLAUDE.md` 維持 byte-identical。commit `c359b9c` 已進 `origin/main`，`Hardened core Compose` 首次失敗經 rerun 判定 transient。 |
| GUI 功能分支合併至 main（2026-08-02） | PR #12 以 merge commit `8a0c834` 併入 `main`。初次 CI 的 5 個 failure 來自兩個根因：clean runner 缺 Kronos model directory、capacity revision 停在 `0017`；兩者以 regression 修正，未弱化 launcher fail-closed。修正後 14/14 checks 全綠。 |
| Pre-push 完整驗證與發布（2026-08-02） | 三個唯讀子代理完成 code／安全／docs 獨立審查；修正 truthful capability（`configured` ≠ `verified`）、async deep-link、market label suffix mapping 與 secret lifecycle。完整 PostgreSQL gate 2,772 passed／coverage 86.18%。 |
| 前端品質／安全／死碼稽核（2026-08-01） | 修正 loading 永久 busy、deep link、degraded state、320px footer、44px touch target；loopback `/api/` 在 provider 呼叫前拒絕 cross-site `Origin`／Fetch Metadata；移除未引用的 freshness rail chain 與 DOM binding。 |
| GUI 完全重設計（2026-08-01） | 移除 dark-fintech 金色／霓虹模板與卡片海，改為低彩度 graphite dark evidence workbench。首屏 capability map 由 actual backend 反推，market state 從 hash 移至 query string，quiet refresh 不再清空畫面。 |
| 降低上手門檻（2026-07-30） | Phase A：`fetch_kronos_model.py`、`start.sh`、`.env` 載入。Phase B：台股接入（`market_region.py`、TWSE 官方行事曆、per-market 時區）。Phase C：研究輸出重排，`blocked alpha`／`no-order` 降為合規狀態列。 |

## Windows 一鍵啟動（2026-08-28）

- [x] 新增 `start.cmd`，雙擊後轉交既有 `start.ps1 -Mode research`，並使用 `55434` 避開
      本機 ChatGPT 程序占用的 `55433`。
- [x] 依 exact allowlist 清除專案可重建暫存：116 個目標、114,669,079 bytes。
- [x] 清除 2 個舊 `stonks-agent-core` tag 與 5.327 GB、超過 7 天的 `desktop-linux` build cache；
      保留 `.data`、Kronos／OpenBB／PostgreSQL image 與 paper volume。

### Review

- `uv run --frozen python -m pytest -q --no-cov tests/entrypoints/test_quick_start_script.py`
  → `10 passed, 3 skipped`。
- `start.cmd` 實際啟動 research：Alembic migration 完成，`127.0.0.1:8787` 回 HTTP 200，
  PostgreSQL／Kronos／OpenBB 三個 container healthy／running。
- 啟動中的 Python process 會重新產生少量 `__pycache__`；allowlist 根目錄快取仍為 0，
  不為清理可重建快取中斷正在使用的 GUI。
- Docker cleanup 後仍保留約 8.444 GB 較新的可重用 build cache；未使用廣域 `docker system prune`。
- GUI 驗證後再用 `scripts/clean_workspace.py` exact allowlist 清除 39 個可重建目標、
  2,752,319 bytes；清理後 dry-run 為 0 目標。

## Docker 精準清理（2026-08-29）

- [x] 移除已退出的舊 Kronos GUI container 與 2 個空 network。
- [x] 移除未被專案引用的 `alpine:latest` image；保留 pinned runtime／稽核 images。
- [x] 清除 `desktop-linux` 可重建 build cache，不碰 running containers、`.data` 或 PostgreSQL volume。
- [x] 清理後重查 Docker 資源、服務健康狀態與資料庫 volume。

### Review

- 清理前：4 個 container（3 running、1 個舊 exited）、9 個 images、1 個 PostgreSQL volume、
  14.71 GB build cache。
- 清理：1 個舊 container、2 個空 network、1 個未引用 image，回收 14.71 GB build cache。
- 清理後：3 個 running container、8 個 project images、1 個 50.41 MB PostgreSQL volume、
  0 B build cache；`/healthz`、55434、17200 均通過。
- 另用 `scripts/clean_workspace.py` exact allowlist 清掉 13 個 `__pycache__`、884,438 bytes；
  最終 dry-run 為 `planned_count=0`、`reclaimed_bytes=0`。

## GUI 模型設定與自然語言操作（2026-08-28）

- [x] 盤點現有 GUI、模型設定、K 線互動與進階命令列資料流。
- [x] 先補資產契約測試，再簡化模型設定與中文介面。
- [x] 修復 K 線圖拖曳、觸控滑動、滾輪平移與鍵盤查看。
- [x] 讓進階命令列接受受限中文意圖，維持既有安全命令白名單。
- [x] 啟動實際 GUI，驗證窄版、鍵盤、圖表與研究入口。

### Review

- 模型表單由多個進階欄位收斂為 3 個必要輸入，其他安全參數沿用 backend defaults。
- 首屏移除重複服務診斷，改成簡短工作狀態列；研究模式與帳戶狀態改用中文顯示。
- K 線以有限視窗繪製，pointer/touch、滾輪與方向鍵可查看不同資料區段。
- 中文自然語言只映射到既有 allowlist command；不新增任意命令或 LLM 執行權限。
- 驗證：GUI 實測「查看 NVDA」、「切換 5 分鐘」、拖曳／滾輪；390×844 無橫向溢出。
  `test_gui_assets.py`、GUI model settings／e2e 測試與 JS syntax check 於收尾重跑。

## GUI 單欄排版與工作區清理（2026-08-30）

- [x] 盤點目前 16:9 排版與 exact cleanup allowlist。
- [x] 將桌面主內容由 2×2 收斂為單欄直排，保留窄版可用性與既有互動。
- [x] 以瀏覽器檢查 16:9、窄版、鍵盤焦點與無橫向溢出。
- [x] 執行相關 GUI policy／E2E 測試。
- [x] 執行 exact cleanup，確認清理後沒有可重建垃圾。

### Review

- 16:9 實測（1920×1080）：`.analysis-grid` 與 `.context-grid` 都是單欄，五個主面板
  依序完整排列；`scrollWidth=1905`、`clientWidth=1905`，無橫向溢出。
- 圖表 `.chart-stage` 高度約 368px，說明列起點等於 stage 終點；窄版 390×845 仍無溢出、
  保留底部 navigation，canvas 鍵盤方向鍵仍能更新 crosshair。
- 驗證：`uv run --frozen python -m pytest -q --no-cov tests/policy/test_gui_assets.py`
  → `16 passed`；`uv run --frozen python -m pytest -q --no-cov tests/e2e/test_gui.py`
  → `55 passed`；5 個 GUI JS `node --check` 全過；`git diff --check` 無錯誤。
- 清理：`uv run --frozen python scripts/clean_workspace.py` 刪除 16 個 exact allowlist
  目標、1,718,025 bytes；收尾 dry-run → `planned_count=0, reclaimed_bytes=0`。

## 行情資料、研究對話與工作區狀態（2026-08-30）

- [x] 盤點價格／成交量顯示、行情 provider 路由與既有安全命令白名單。
- [x] 修復行情顯示的根因，移除重複的上方工作狀態列。
- [x] 將中文操作與進階命令併入研究區聊天室，保留原有安全限制。
- [x] 強化行情來源的多來源、逾時與失敗回報，禁止用假資料補洞。
- [x] 更新測試、交接紀錄，並完成瀏覽器與相關測試驗證。

### Review

- 價格／成交量改由 `latest` 或最後一根 bar 映射；缺資料顯示 typed failure，不留下空白。
- 移除上方重複工作狀態列，中文操作與 `ADD`／`DROP`／`RESEARCH`／`REFRESH`／`HELP`
  移入研究區聊天室，仍沿用既有安全 allowlist；聊天室只保留必要提示。
- GUI 預設仍走 OpenBB → yfinance；只有設定 `STONKS_FINANCIAL_DATASETS_API_KEY` 且主來源
  失敗時才嘗試 Financial Datasets US daily，fallback 會標 degraded 並保留來源 warning。
- 驗證：provider／GUI／e2e 組合測試 `78 passed`，runtime／market 組合測試 `64 passed`、
  `ruff check`、5 個 GUI JS `node --check` 與瀏覽器桌面／窄版互動測試均通過；真實
  Financial Datasets key runtime 未驗證。
- 清理：`scripts/clean_workspace.py` 依 exact allowlist 刪除 43 個目標、83,271,005 bytes；
  收尾 dry-run 為 `planned_count=0`、`reclaimed_bytes=0`。
- 收尾後測試產生的可重建檔再清除 14 個目標、828,676 bytes；最新 dry-run 仍為 0。

## 圖表歷史資料回復（2026-08-30）

- [x] 重現截圖中的日線只有兩根問題，確認資料回應與 chart window 的根因。
- [x] 以最小改動恢復完整可用的歷史資料顯示，不改壞既有拖曳／鍵盤操作。
- [x] 以 16:9 瀏覽器驗證 K 線、成交量、日期軸與無溢出，並重跑相關測試。

### Review

- 根因是先前假預覽使用 `tests/e2e` 固定兩根 bar；真實預覽已改用 market mode 的 OpenBB/yfinance。
- OpenBB 回應混入 non-finite OHLCV 時，只排除壞列並加上 `invalid_record` warning；本次保留 123 根有效 bar。
- 1920×1080 驗證：AAPL 價格 314.58、成交量 32.42M、圖表與日期軸正常、無橫向溢出。
- 驗證：provider／GUI／e2e 測試 117 passed；adapter 補強後 provider 測試 46 passed。

## K 線拖曳與時間選擇（2026-08-30）

- [x] 追查拖曳卡頓／方向異常，確認資料視窗與 pointer 狀態的根因。
- [x] 增加更多 K 線週期與明確時間範圍選擇，維持 URL、API 與鍵盤操作一致。
- [x] 以 16:9 瀏覽器驗證週期／範圍切換、拖曳方向、成交量與無溢出，並重跑測試。

### Review

- 根因：桌面一次繪製全部歷史 bar，且拖曳未完整限制 primary pointer／左鍵與邊界；現改為最多 100 根可視視窗、pointer capture、滾輪平移與邊界限制。
- 週期新增 `2m`、`30m`、`90m`、`1W`、`1M`；範圍提供 1日／5日／1週／1月／3月／6月／1年，選擇寫入 query string。
- 核心目前最多接受 20,000 根 bar，因此 `2m` bounded 到 21 天並預設 1 週；不合法組合會自動收斂，不送出會失敗的請求。
- authenticated OpenBB 實測：`2m=5850`、`30m=520`、`90m=200`、`1W=52`、`1M=12`（provider raw capability）；GUI 實測 `2m=975`、週線 25、月線 5，均為 `openbb:yfinance` 真資料。
- 1920×1080 實測：10 個週期、7 個範圍、價格／成交量存在、水平溢出 `0`；拖曳後日期軸改變，放開後 `data-dragging=null`。
- 驗證：相關 pytest `189 passed, 1 warning`；OpenBB surface／manifest 測試包含新增週期；`ruff check`、`mypy`、兩個 GUI `node --check` 與 `git diff --check` 通過。
- 清理：`scripts/clean_workspace.py` 依 exact allowlist 刪除 43 個可重建目標、41,837,554 bytes；dry-run 為 `planned_count=0`、`reclaimed_bytes=0`。

## 標的儀錶板與 K 線密度（2026-08-30）

- [x] 收窄週線／月線等少量 K 線的顯示間距，不破壞日線拖曳。
- [x] 移除前端「資料檢查」面板與側欄入口，保留後端品質判定。
- [x] 加入標的行情概覽，盤點並標示公司資料／財報／估值缺口。

### Review

- K 線少於畫面寬度時改用有限 bar slot 並置中繪製；日線仍可 pointer drag、wheel、鍵盤平移。
- 「資料檢查」只從 GUI 顯示層移除，provider metadata 與 fail-closed 邏輯仍在後端。
- 新增標的儀錶板直接由目前真實 bars 衍生區間報酬、高低點、成交量、波動、漲跌統計與資料涵蓋；不宣稱公司簡介、財報或估值已完成，因目前沒有已驗證的可用資料 API。
- 驗證：GUI／E2E `76 passed, 1 warning`；5 個 GUI JS `node --check` 通過；1920×1080 與 390×845 溢出均為 `0`，日線拖曳放開後 `data-dragging=null`。

## 年線與長時間範圍（2026-08-30）

- [x] 增加年線，並以已驗證的月線資料在 core 聚合。
- [x] 增加 YTD、5 年、10 年與全部，放寬 EOD 查詢邊界並保留安全上限。
- [x] 補 provider／API／GUI 測試，重開最新預覽驗證切換與實際資料。

### Review

- `1Y` 不直接送未驗證的 OpenBB 年線；OpenBB 仍取已驗證的 `1M`，core 依年度合併 open／high／low／close／volume。
- `lookback_days` 放寬到 36,525 天，EOD response 上限調為 20,000 根 bar；OpenBB latest transport 上限調為 4 MiB。
- GUI 範圍新增 YTD、5 年、10 年、全部；年線只顯示可由月線穩定取得的 3 月、6 月、YTD、1 年、5 年、10 年、全部。
- OpenBB/yfinance 實際驗證：年線全部 42 根、日線 10 年 2,511 根、日線全部 11,519 根、週線全部 2,386 根、月線全部 500 根；皆為 `openbb:yfinance`。
- 1920×1080 預覽 URL 為 `i=1Y&r=max`，年線全部 42 根、水平溢出 `0`、console errors/warnings 均為 `0`。
- 驗證：相關 pytest `98 passed, 1 warning`；`ruff check`、`ruff format --check`、兩個 GUI JS `node --check` 與 `git diff --check` 通過。
## 免費資料源與 Agent 資料快照（2026-08-30）

- [x] 盤點官方與 GitHub 實際可用的免費資料種類，標記免費成本與授權限制。
- [x] 接入 SEC company facts／filings 與 TWSE 公開財報資料，加入 bounded fetch、PIT、cache、rate limit 與 schema 驗證。
- [x] 將公司基本資料、財報、申報紀錄接入標的儀錶板。
- [x] 讓研究 Agent 在同一份 snapshot 內主動讀取行情、基本面與申報資料。
- [x] 補 provider contract、GUI E2E、Agent tool 與實際官方端點驗證。

### Review

- SEC／TWSE vertical slice 已完成；BLS/FRED、新聞、內部人、13F、估值等仍維持未組合，因尚未同時通過 credential、權利、PIT 與 actual runtime 門檻。
- 驗證：官方資料源測試 `4 passed`；bundle 測試 `1 passed`；Agent／研究／GUI 組合測試 `109 passed, 1 warning`；完整非 PostgreSQL 測試 `2521 passed, 9 skipped`；strict `mypy` 402 個來源檔通過；`ruff check`、`ruff format --check` 與 `git diff --check` 通過。
- PostgreSQL 整合測試未執行，因目前環境沒有 `STONKS_TEST_DATABASE_URL`；完整測試因此排除 292 個 PostgreSQL cases。

## 標的儀錶板排版與歷史財務（2026-08-31）

- [x] 將標的儀錶板改為摘要、行情／公司左右分欄，申報與歷史資料跨欄呈現。
- [x] 讓「近期申報」可展開，並只連到已驗證的 SEC／TWSE 官方來源。
- [x] 擴充 SEC／TWSE 財報欄位，保存 bounded historical observations，提供指標切換比較。
- [x] 用實際 AAPL／2330.TW API 與 16:9 瀏覽器驗證資料、互動與版面。

### Review

- 1440×900 實測無水平溢出；`.dashboard-columns` 為左右兩欄，窄版才收斂為單欄。
- AAPL actual runtime 為 `sec`、14 項 facts、20 筆申報；2330.TW 為 `twse_openapi`、19 項 facts。
- AAPL 歷史表可切換營收／毛利等指標，切換後表格值會更新；申報有 12 個官方連結。
- TWSE 公開 OpenAPI 目前只回傳各端點的最新彙總列，因此台股歷史比較仍受上游限制，沒有用假資料補齊。
- 驗證：focused pytest `83 passed, 1 warning`；Playwright `console` 為 0 errors、0 warnings；`node --check`、`ruff check` 通過。

## 自選股與側欄導航清理（2026-08-31）

- [x] 移除 watchlist 固定 12 檔限制，保留必要的輸入與 provider 邊界。
- [x] 移除側欄四個工作區導航按鈕與其孤兒 CSS／hash/deep-link 程式碼。
- [x] 同步 OpenAPI、runbook、回歸測試與交接文件。

### Review

- watchlist API 可接受超過 12 檔，仍拒絕空值／重複值，query 最多 4,096 字元；provider gate 與 4 workers 保留。
- 側欄目前只有觀察清單與安全提示，沒有底部重複導航或無效 active state。
- 驗證：GUI／E2E `80 passed, 1 warning`；`export_openapi.py` 成功；OpenAPI 無 `WatchlistView.quotes.maxItems` 且 query `maxLength=4096`。

## 全專案死碼清理（2026-08-31）

- [x] 盤點 source、scripts、tests、GUI 資產、依賴與孤兒檔案，建立有證據的候選清單。
- [x] 追完整 caller／入口／測試，刪除確定無用的死碼，不碰仍有契約用途的介面。
- [x] 重跑格式、lint、typecheck、測試、security／license gate 與 workspace dry-run。

### Review

- 移除無 caller 的 `run_bounded_command()`、`_provider_policy()`、舊 generic repository port 與其測試，以及已被現行資料品質契約取代的 `EntityId`／`QualityAssessment` 模組與測試。
- 清掉 GUI 未再產生的 `data-certainty` selector 與空的 `58rem` media block；保留 Typer／FastAPI 裝飾器入口、可選 adapter、migration、golden fixture 與仍被使用的 provenance。
- 驗證：`ruff format --check`、`ruff check`、`mypy src packages`、相關測試 `49 passed`、schema／upstream policy／secrets gate 通過；canonical 非 PostgreSQL verify 為 `2516 passed, 4 failed`，4 筆均因本機 Docker Engine 未啟動。
- `clean_workspace.py --dry-run` 僅盤點到 `planned_count=115`、`reclaimed_bytes=113516519`，未執行刪除。

## README 重寫與主線整合（2026-08-31）

- [x] 依目前實際能力重寫根目錄 README，移除過期的歷次工作紀錄。
- [x] 先檢查遠端所有開放 PR 的 mergeability、CI 與變更範圍。
- [x] 提交並推送目前工作樹的功能、文件與測試改動。
- [x] 合併檢查全綠且屬於本專案的 PR，處理過期或失敗的 PR。

### Review

- README 收斂成安裝、啟動、資料來源、K 線、研究聊天室、paper 邊界與文件索引。
- 修復 core／RD-Agent 的 OpenSSL runtime package 與 LEAN .NET base digest，更新 legal／VEX
  證據；本機固定 Grype 掃描三個 image 均無未抑制 High/Critical。
- 官方 Alpine aports archive 下載加入 3 次 bounded network retry，仍維持最後 fail closed；
  相關單元測試 `40 passed`。
- #13、#14 已合併；#1、#2、#4、#5、#6、#7、#8、#9、#10、#11 已在最新主線上全綠合併。
- #7 的舊失敗來自過期 merge base；rebase 到最新 `main` 後 14 個 required checks 全綠，已合併為
  `1923db3`，沒有繞過保護檢查。

## PR #7 檢查失敗修復（2026-08-31）

- [x] 讀取四個失敗 job 的完整錯誤並確認不是 `main` 失敗。
- [x] 將 PR #7 以最新 `main` rebase，保留原本單一 `mypy` 版本範圍變更。
- [x] 重跑本機 frozen gate 與 GitHub 全部 required checks。
- [x] 以最新完整 `headRefOid` 合併 PR #7，清理遠端暫存分支。

### Review

- 舊 run 的 Windows、Compose、LEAN 與 release verification failure 都在過期合併基底；rebase 後
  同四個 check 全部通過，主線仍維持綠色。
- 本機 `uv run --frozen python scripts/verify.py --skip-audit`：`2,518 passed`、coverage `86.60%`，
  schema／upstream policy／secrets gate 全綠。

## 檢查失敗修復（2026-08-31）

- [x] 同步 PR #7 合併後的 `uv.lock`，確認 `uv lock --check` 通過。
- [x] 修正 `mypy 2.3.1` 報出的冗餘 `cast`，同步所有受影響的 runtime identity hash。
- [x] 更新 Kronos evaluation fixture 並重跑相關測試、文件測試與完整 frozen verify。

### Review

- 完整驗證：`ruff format --check`、`ruff check`、`mypy src packages`、`2518 passed, 9 skipped`、
  coverage `86.60%`、schema／upstream policy／secrets gate 全部通過。
- 首次推送的 Supply-chain 只有 `corresponding_sources` 失敗；原因是 `uv.lock` 更新後
  `config/release-policy.json` 仍是舊的 Python source archive／manifest hash，已重新生成摘要並同步。
- 修正後 `tests/policy/test_release_supply_chain.py`、`tests/unit/test_release_source_contracts.py`、
  `tests/unit/test_generate_python_source.py` 共 `50 passed`，再跑完整 frozen verify 全綠。
