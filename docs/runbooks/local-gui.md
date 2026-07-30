# Stonks Terminal（local GUI）

Stonks Desk 是 loopback-only、依後端能力設計的 AI 投資研究工作台。它透過 isolated
OpenBB／yfinance sidecar 讀取美股日線與日內 K 線，推導延遲報價，並可選擇性組合
本機 PostgreSQL 與 durable research worker。介面沒有券商登入、直接下單或任何繞過
canonical risk 的操作。

此功能位於目前工作樹的未發布 `0.2.0` candidate，immutable `v0.1.2` 不含 GUI。Launcher
必須在完整 source checkout 根目錄執行，因為它會使用 repository 內的 Compose 與
OpenBB corresponding-source build context；standalone wheel 或 core image 不支援啟動。

## 啟動

前置需求：

- Git、Python 3.12、`uv` 與已完成的 `uv sync --frozen`。
- Docker Engine／Docker Desktop 與 Compose v2。
- 可連線至 OpenBB／yfinance 使用的外部資料來源。
- 本機 `127.0.0.1:6900` 與 `127.0.0.1:8787` 未被占用；使用 research 時另需
  `127.0.0.1:55433` 與 Kronos `127.0.0.1:17200`。
- Research mode 另需先備妥 `.data/models/kronos/` 下的 pinned Kronos 權重。
  Worker runtime 禁止自行下載，改由 one-shot provisioning 腳本取得：
  `uv run --frozen python scripts/fetch_kronos_model.py`。它只抓 manifest 記載的
  exact repository／revision，逐檔比對 size 與 SHA-256，不符即刪檔並非零結束；
  重跑只驗證既有檔案。缺檔時 launcher 會以 exit code 2 與
  `Kronos CPU model is missing` 直接停止。目錄結構與 pinned revision 見
  [Kronos worker 的 Model preparation](../../workers/kronos/README.md)。
  只用 market 或 paper 模式時不需要模型。

在 repository 根目錄執行：

```powershell
.\start.ps1 -Mode market
```

Linux／macOS 使用對等的 `./start.sh --mode market`。以下 PowerShell 參數在 shell 版
分別對應 `--mode`、`--port`、`--database-port`、`--kronos-port`、`--no-browser`、
`--skip-sync` 與 `--check`；兩者的檢查順序、失敗訊息與 exit code 一致。

加上 paper 投資組合面板（會啟動本機 PostgreSQL、執行 migration 並建立帳戶）：

```powershell
.\start.ps1 -Mode paper
```

加上 live snapshot、durable LLM research 與 actual Kronos CPU forecast
（會隱含啟用 paper）：

```powershell
.\start.ps1
```

LLM 不需要在啟動前設定。Research GUI 啟動後可在模型設定面板輸入 base URL、Model ID
與 API key；只有 bounded structured completion 驗證成功才會啟用下一筆研究。

要免除每次重開重打，把 `.env.example` 複製為根目錄 `.env`（已 gitignored）並填值。
Launcher 會在檢查工具前載入它並注入子行程環境，GUI 以既有的 environment baseline
自動驗證一次。`.env` 只接受 `STONKS_*` 鍵，遇到任何其他鍵直接以錯誤停止；launcher
本身仍不接受 key 參數、不輸出也不記錄 secret，secret 也不會進入 canonical payload、
DB、artifact 或 browser storage。`-Check` 只驗證 source
checkout、必要工具、Compose 與 research runtime，再顯示
將執行的 redacted 命令；`-SkipSync` 可略過增量 `uv sync --frozen`。

預設瀏覽器會開啟 `http://127.0.0.1:8787`。`--no-open-browser` 可停用自動開啟，
腳本對應參數為 `-NoBrowser`、`-Port`、`-DatabasePort` 與 `-KronosPort`。底層仍可直接執行
`uv run --frozen stonks-gui serve`。

停止命令後，entrypoint 會先停止 research supervisor，再關閉它擁有的 PostgreSQL、
Kronos／OpenBB Compose projects、移除 Kronos no-masquerade loopback bridge，最後刪除
暫時 public JWKS。
Paper 資料庫使用具名 volume，`down` 不帶 `--volumes`，因此 ledger 不會被關閉終端清除。
第一次啟動需要 build sidecar，後續可使用 Docker cache。

## 操作

主要流程不需要記憶命令：

1. 在頂端輸入美股代號並按「載入市場資料」。
2. 預設使用 `1m`；以週期控制切換 `1m`／`5m`／`15m`／`1h`／`1d`，並先檢查
   provider、非 tick 語意、freshness／quality、latest event、served time、cache、
   資料年齡與 warnings。
3. 在「LLM 模型連線」輸入 OpenAI-compatible base URL、Model ID 與 API key，按
   「儲存並驗證」。API key 送出後立即從欄位清除，不寫入 browser storage、DB 或檔案。
4. 驗證成功後按「開始 AI 研究」。按鈕會在 POST 前立刻鎖定
   single-flight；研究面板依序顯示市場快照、evidence、AI 分析與報告。
5. 終態顯示 confidence、evidence-backed claims、反方觀點、風險，以及本次 daily
   snapshot-bound Kronos model/revision、path count、expected/median return、
   direction probability、volatility、downside、quality、alpha eligibility、paper decision
   與可展開報告。Typed failure 不沿用舊 forecast／claims。
6. 「最近研究」即使尚未設定模型仍可讀，可重新開啟 durable run；claim 的 evidence 按鈕只會顯示該 run
   snapshot 中實際引用的 cited evidence，並列出 event／available time、quality、
   content hash 與 bounded market fields。
7. 「執行透明度」顯示 as-of、snapshot、usage、model/tool versions、warnings 與
   degraded issues；不顯示 prompt、raw worker envelope 或 artifact path。
8. Research terminal 後會重新讀取 paper projection，避免研究結論與帳戶面板停在
   不同時間點。

命令列保留在畫面底部供進階操作，按 `/` 可從任何位置跳回：

| 輸入 | 動作 |
|---|---|
| `AAPL` | 載入報價與走勢 |
| `AAPL 5m` | 指定週期，可用 `1m` `5m` `15m` `1h` `1d` |
| `ADD <代號>` | 加入關注清單，上限 12 檔 |
| `DROP <代號>` | 從關注清單移除 |
| `RESEARCH <代號>` | 以 live snapshot 啟動 bounded durable research workflow |
| `REFRESH` | 重新讀取目前代號與關注清單 |
| `HELP` 或 `F1` | 開啟命令說明 |
| `↑` `↓` | 瀏覽命令紀錄 |
| `Esc` | 清除命令列或關閉說明 |

工作區狀態（代號、週期、關注清單）保存在 URL hash，因此重新整理或加入書籤都會
還原同一個版面。研究 run id 不寫入 URL 或 browser storage；重新整理後由 owner-scoped
history 重新開啟 run，進行中的 run 會恢復 SSE，不把敏感狀態存到 browser storage。

分頁可見且沒有進行中請求時，GUI 每 30 秒 bounded 更新主行情與 watchlist；窄版不會
autofocus。每個面板左緣有 provenance rail：灰色是尚未查詢、綠色是 backend 判定
available、紫色是 degraded／unknown、紅色是外部失敗、琥珀色是讀取中。Browser
不得用固定秒數把 backend 的品質升級。

## 已驗證行為

- 只監聽 `127.0.0.1`，Host 與 direct peer 都必須是 loopback。
- Browser → GUI 沒有人類登入或 OIDC；本機 loopback 是這一段的 transport boundary。
  短效 RS256 service identity 保護 GUI → OpenBB／Kronos sidecars，不能解讀成 public
  GUI auth。
- 拒絕 forwarded identity 與 live trading；唯一 canonical workflow mutation 是
  same-origin＋process-memory intent 保護的 research POST。模型設定 PUT／DELETE 使用
  相同保護，但只更改本次 process-memory route／secret，不能指定 owner、account、
  mode、target 或 order。
- Model route 只接受 DNS/IP pinned 公開 HTTPS，或 local／development 下 exact
  `127.0.0.1`／`localhost` loopback HTTP；private／metadata address、redirect、proxy、
  `.netrc` 與 DNS rebinding 全部 fail closed。Provider 若回顯 exact API key，會在 parse
  與 artifact archive 前拒絕。
- `--with-research` 才注入 durable facade；未啟用時 research route 固定回 structured
  503。啟用後會先 materialize live `1d` snapshot，再建立 snapshot-bound research job；
  chart interval 不會被拿來冒充 Kronos daily evidence。
- Research mode 自動驗證 pinned 模型、啟動只綁 loopback 的 authenticated Kronos CPU
  worker。Raw worker response與 sample paths 留在 artifact store；Browser 只取得 bounded
  typed metrics，不取得 raw artifact ref。
- 模型未設定／未驗證時，前端 CTA disabled，research POST 也固定回 typed 503，不建立
  snapshot 或 job。Invalid structured output 或 provider outage 不會覆蓋前一個已驗證設定。
- 2026-07-29 Chromium product fixture：1680×1020、390×844 與 320×800 都沒有頁面
  水平溢位，console 0 errors／warnings；研究歷史重開、citation→evidence、
  transparency、typed paper／safety、collapsed command console 與 canvas 鍵盤導覽皆通過。
  窄版初始焦點維持 BODY，排序為市場摘要 → 研究 → 圖表 → paper／來源 → 系統側欄。
- 每次查詢呼叫真正的 OpenBB sidecar；sidecar 再以固定 `yfinance` provider 讀取資料。
- Core 使用短效 RS256 service credential；private key 只存在該 Python process 記憶體，
  掛載到 sidecar 的檔案只有 public JWKS。
- 回應保留 symbol、provider、feed type、interval、observed time、latest event time、
  served time、freshness、quality、cache hit、OHLCV、warnings 與完整精度 JSON；
  畫面才格式化為兩位小數。
- Provider empty、timeout、auth、invalid response 或 sidecar outage 都回 structured
  failure；不會改用 fixture、hard-coded quote 或 stale cache 偽裝成功。
- GUI 只載入同源本地 script，CSP 為 `default-src 'none'`＋`script-src 'self'`＋
  `connect-src 'self'`，沒有 inline script、eval、外部來源或 `data:` 來源。
- Paper 面板只讀 PostgreSQL canonical 投影；顯示 NAV、settled／reserved／available
  cash、positions、pending count、risk authority、global kill switch 與 projection hash。
  未啟用時明示不可用，不顯示示範數字。
- capabilities 的 OpenBB／PostgreSQL／Kronos／research 狀態每次 GET 都重新 probe；
  sidecar 或 worker 在啟動後死亡不會繼續顯示 frozen `ready`。
- Research start 另有每分鐘 3 次與 single-active 的成本 limiter；第 4 次回
  `429 rate_limited` 與 `Retry-After`，不只依賴前端 disabled state。
- Market provider 每個 process 每分鐘最多 30 次 outbound request；連續三次失敗後
  cooldown 15 秒。Watchlist 最多 12 檔且使用 4 workers，cache hit 仍會重算
  `served_at`、age、freshness 與 quality。

## 資料語意

| 欄位 | 語意 |
|---|---|
| `feed_type` | `end_of_day_historical` 或 `intraday_historical` |
| `interval` | `1m` `5m` `15m` `1h` `1d` |
| `is_real_time` | 永遠是 `false` |
| `observed_at` | Stonks Agent 開始本次 provider request 的 UTC 時間 |
| `served_at` | 本次 API delivery 的 UTC 時間；cache hit 仍會更新 |
| `latest_event_time` | 回應中最新一根有效 bar 的 UTC event time |
| `data_age_seconds` | request-start 與最新 bar event time 的差 |
| `freshness` | backend session-aware `current`／`market_closed`／`delayed`／`stale`／`unknown` |
| `quality` | `available`／`degraded`／`unknown`，warning 與原因另列 |
| `cache_hit` | 是否使用最長 20 秒的 process-local quote cache |
| `previous_close` / `change` / `change_percent` | 由前一根 bar 推導；無法完整推導時三者一起省略 |

因此「最新」表示 provider 在查詢當下可提供的最新資料，不是交易所即時 quote。
週末、休市日、provider publication lag 與 upstream outage 都可能讓 latest event 早於
查詢時間。日內 bar 的原始時間戳是交易所本地時間，adapter 依 `America/New_York`
轉為 UTC 後才進入 canonical 型別。

各週期可查詢的回看天數受上游限制：`1m` 最多 7 天、`5m`／`15m` 最多 59 天；超過會在
送出請求前被拒絕，而不是回傳會被誤讀為合法空集合的空回應。

免費來源不是無條件 allowlist。Active／需使用者 credential／display-restricted／paid／
prohibited 的逐項矩陣見[免費市場資料來源](../research/free-market-data-sources.md)。

## API

GUI 同時提供 read-only market JSON 與 bounded research contract，全部使用專案統一的
`success/status/data/error/metadata` envelope：

```text
GET /api/v1/market/bars?symbol=AAPL&interval=1d&lookback_days=180
GET /api/v1/market/quotes?symbols=AAPL,MSFT,NVDA
GET /api/v1/capabilities
GET /api/v1/market-data/latest?symbol=AAPL&lookback_days=30
GET /api/v1/settings/llm
PUT /api/v1/settings/llm
DELETE /api/v1/settings/llm
POST /api/v1/research/runs
GET /api/v1/research/runs?limit=10
GET /api/v1/research/runs/{run_id}
GET /api/v1/research/runs/{run_id}/evidence
GET /api/v1/research/runs/{run_id}/events
```

`symbol` 接受英數、`.` 與 `-`，輸入會正規化成大寫；`lookback_days` 必須介於 1 到
366；`symbols` 最多 12 檔且不可重複。GUI 沒有 target、order、execution、kill-switch
或 strategy mutation route。

## Provider 能力邊界

`price/quote`、`profile`、`fundamental/*` 與 `discovery/*` 沒有列入 sidecar allowlist。
2026-07-27 實測：Yahoo 這些端點需要 crumb，而 yfinance 取得 cookie 的來源主機
`fc.yahoo.com` 已無法解析，`1.5.1` 與最新 `1.5.2` 都回 HTTP 401。因此終端不提供公司
簡介、財報指標與漲跌幅排行，也不會以其他來源冒充；報價一律由 bar 序列誠實推導。

## 目前未組合

- GUI Kronos exchange calendar 的已驗證 session/holiday window 目前是 2026；超出範圍
  會回 `configuration_invalid`，不以單純 weekday 猜測交易日。
- Paper 面板為唯讀投影。Kronos 是 shadow／paper weight 0、三個 baseline 是 draft、
  opinion mapper disabled；沒有 genuine evaluation/promotion artifact 時只能 no-order，
  不會為展示閉環偽造成交。
- HK／TW 尚無可由 GUI 使用的真實 live provider。
- 沒有 public ingress、production OIDC、TLS、trusted proxy 或 distributed rate limit。
- 券商帳號與 live trading 不支援，且不能用設定值啟用。

## 故障判讀

- `OpenBB sidecar lifecycle failed`：檢查 Docker daemon、Compose v2、port 占用與 build
  網路；entrypoint 不會在 sidecar 不健康時啟動 GUI。
- `本機 paper 資料庫初始化失敗`：檢查 Docker、`127.0.0.1:55433` 占用與
  `.data/gui/postgres-password`；GUI 不會以假資料頂替。
- `data_unavailable`：symbol 無資料、provider outage 或回應為 empty；這是預期的
  fail-closed 狀態。
- `provider_auth_failed`：ephemeral service identity 與 sidecar JWKS 不一致；停止後重新
  啟動，不可繞過 auth。
- `provider_timeout`：外部 provider 超過 bounded timeout；系統不會無限 retry。
- `configuration_invalid`：缺少或拒絕 LLM route/model/API key；研究 job 已 fail closed。
- `Kronos CPU worker 啟動或模型完整性驗證失敗`：pinned model 缺檔／hash 不符、
  `17200` 被占用或 Docker runtime failure；GUI 不會改用 synthetic forecast。

## 獨立診斷腳本

以下腳本都不是啟動前置步驟，只在需要單獨定位某一段 runtime 時使用；它們的輸出
不能當成 GUI run artifact：

| 腳本 | 用途 |
|---|---|
| `scripts/verify_snapshot_runtime.py` | 只驗證 OpenBB → canonical daily snapshot 這一段 |
| `scripts/verify_kronos_runtime.py` | 只驗證 authenticated Kronos CPU worker 與 pinned model |
| `scripts/verify_gui_research_runtime.py` | 只驗證 GUI research facade 的 snapshot＋LLM＋forecast 組合 |

OpenBB 是 AGPL-3.0-only optional sidecar。重新散布 image 或服務時仍須依
[license policy](../legal/license-policy.md) 與 sidecar `SOURCE_OFFER.md` 履行義務。
