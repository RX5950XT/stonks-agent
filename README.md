# Stonks Agent

[![CI](https://github.com/RX5950XT/stonks-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/RX5950XT/stonks-agent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/RX5950XT/stonks-agent)](https://github.com/RX5950XT/stonks-agent/releases/tag/v0.1.2)
[![License](https://img.shields.io/github/license/RX5950XT/stonks-agent)](./LICENSE)

Stonks Agent 是 evidence-first、可稽核、可重播的投資研究與 paper trading
平台。它把 agent research、時間序列預測、市場資料、量化評估、回測引擎與模擬交易
整合在同一套 canonical contracts 後方，同時禁止 LLM 或外部平台繞過 deterministic
risk、reservation、execution 與 ledger。

> [!IMPORTANT]
> 專案唯一允許的 execution mode 是 `paper`。目前不支援 live trading，也不構成投資、
> 法律或財務建議。

## 目前完成狀態

原訂 P0-P6.11 repository implementation、公開倉庫與 `v0.1.2` formal release closure 已完成。
目前工作樹是含 Local GUI 的未發布 `0.2.0` candidate；immutable `v0.1.2` 不含 GUI。
這裡的「完成」是指程式碼、測試、供應鏈與發行 gate 已關閉，不代表
它已是可連接券商、直接實盤或可暴露於公網的 production 產品；目前成熟度仍是
`pre-alpha`。

| 範圍 | 狀態 | 代表意義 |
|---|---|---|
| Canonical research／paper flow | `implemented` | contracts、PostgreSQL、replay、risk、reservation、fill 與 balanced journal 已測試 |
| Stonks Desk 與美股行情（0.2.0 candidate） | `actual_runtime_verified` | 後端導向的 loopback AI 研究工作台；透過 isolated OpenBB／yfinance 取得 bars，另提供鍵盤可讀 OHLCV 表格、研究歷史、cited evidence 與即時 runtime health |
| Terminal paper 投資組合面板（0.2.0 candidate） | `actual_runtime_verified` | `--with-paper` 啟動本機 PostgreSQL；typed 唯讀顯示 NAV、cash／reservation、positions、risk、global kill switch 與 projection integrity |
| Terminal durable research（0.2.0 candidate） | `composed / external_llm_required` | `--with-research` 會 materialize daily snapshot、執行 fenced LLM＋Kronos job並以SSE顯示typed結果；LLM endpoint／model／key可直接在GUI設定並先做structured completion驗證 |
| Kronos CPU forecast | `gui_composed / shadow` | research mode 自動啟停 authenticated CPU worker；每次 run 封存 snapshot-bound raw response、3 paths 與 forecast，paper weight 0、不具下單 authority |
| Public `v0.1.2` release | `externally_verified` | protected tag、GHCR、keyless signatures、provenance、SBOM 與 immutable assets 已重驗 |
| Default Docker deployment | `implemented` | 單機 core／PostgreSQL health、migration、restart、outage 與 replay baseline 已驗證 |
| Optional integrations | `mixed` | 4 個 CI runtime actual、5 個缺部署憑證而 blocked、1 個 GPU profile unsupported |
| Production business API | `not_composed` | 六份 API contract 已存在，但 default deployment 尚未組合成 production business API |
| External production wiring | `unverified` | 真實 IdP、cloud secret manager、public TLS、distributed rate limit、remote telemetry 尚未完成 |
| Live trading | `unsupported` | 沒有開關可啟用；必須另立 RFC、權限與安全模型 |

正式 release 與各項未驗證邊界可在
[P6 handoff evidence](./docs/verification/p6-handoff-evidence.md) 逐項核對。

## 整合內容

Canonical flow 固定為：

```text
Evidence / ResearchArtifact
  -> AgentOpinion / AlphaSignal / ForecastSignal
  -> deterministic PortfolioTarget
  -> RiskDecision
  -> AccountReservation
  -> OrderIntent
  -> ExecutionReceipt / Fill
  -> balanced Journal
```

外部模型只能在前半段產生 evidence、opinion、forecast 或 evaluation；只有 core 能建立
target、通過 risk、保留資金、模擬成交並寫入 ledger。

| 專案／能力 | 整合方式 | 現況 |
|---|---|---|
| ai-hedge-fund | MIT selective port：PEAD 與 event study | 已實作，輸出維持 draft／research-only |
| Dexter | clean-room research orchestration concepts | 授權證據不足，不 vendor source／prompt／assets |
| TradingAgents | pinned isolated worker + typed adapter | 已實作；部署需可信 service identity |
| Kronos | pinned CPU／CUDA forecast worker | 已實作；需本機唯讀模型與對應 runtime |
| daily_stock_analysis | report schema、template 與 evidence-quality primitives | 已整合至自有 reporting contracts |
| AI-Trader | default-off external community HTTP adapter | 只收 untrusted evidence，不採用其 paper／copy execution |
| OpenBB | optional AGPL sidecar | actual runtime 已驗；必須履行 corresponding-source 義務 |
| Qlib | isolated quant-lab worker | 已實作；只允許 evaluation output |
| RD-Agent | ephemeral clean-room factor sandbox | actual runtime 已驗；generated code 不會自動 promote |
| NautilusTrader／LEAN | isolated backtest sidecars | actual runtime 與 cross-engine parity fixtures 已驗 |

完整 process、license 與 authority 邊界見
[整合架構藍圖](./docs/architecture/integration-blueprint.md)。

## 五分鐘開始使用

### 1. 前置需求

- Git。
- `uv`。
- Python 3.12；不支援 3.11 或 3.13。
- 只有啟動真實資料 GUI、執行 deployment smoke 或 optional sidecar 時才需要 Docker
  Engine／Docker Desktop 與 Compose v2。

### 2. 取得程式碼與依賴

使用最新開發文件：

```powershell
git clone https://github.com/RX5950XT/stonks-agent.git
cd stonks-agent
uv python install 3.12
uv sync --frozen --python 3.12
```

### 3. 啟動 Stonks Desk

```powershell
.\start.ps1 -Mode market
```

根目錄 `start.ps1` 會檢查 source checkout、`uv`、Docker Compose 與 Docker daemon，
同步 frozen dependencies，再建立只含 public key 的暫時 JWKS、build／啟動 isolated
OpenBB sidecar，並在
`http://127.0.0.1:8787` 開啟終端。GUI 預設讀取目前可驗證最快的 `1m` historical
bars，分頁可見時每 30 秒 bounded 更新；這是近即時 bar，不是交易所 tick。直接在頂端
輸入 `AAPL` 即可讀取報價與走勢，底部命令列只作進階入口，
`AAPL 5m` 切換週期（`1m` `5m` `15m` `1h` `1d`），`ADD NVDA` 加入關注清單，
`F1` 顯示全部命令。每個面板都顯示 provider、feed 語意、backend freshness／quality、
observed／served／latest event time、cache 狀態與資料年齡；loading 會隱藏舊報價。

加上 `--with-paper` 會另外啟動本機 PostgreSQL、執行 migration、建立 paper 帳戶，
並在終端唯讀顯示 canonical 投資組合投影：

```powershell
.\start.ps1 -Mode paper
```

要從 GUI 觸發 live snapshot 與 durable LLM research，直接執行：

```powershell
.\start.ps1
```

在「AI 研究」上方的「LLM 模型連線」輸入 base URL、Model ID 與 API key，按
「儲存並驗證」。驗證成功後，在頂端搜尋標的並按「開始 AI 研究」；`start.ps1` 會自動 build／啟動
OpenBB、PostgreSQL、research worker 與 Kronos CPU，不需另跑 Kronos verifier。
介面會依序呈現 snapshot、evidence、
AI 分析與報告進度，再顯示 confidence、claims＋evidence refs、反方觀點、風險、
actual Kronos model／revision／3-path return metrics、alpha eligibility 與 paper decision。
最近研究可重新開啟；每條 citation 可定位到本輪 snapshot 內實際引用的 evidence，
並顯示 as-of、usage、model/tool versions 與 degraded issues。Paper 區只讀呈現
canonical NAV、cash、reservation、positions、risk authority、global kill switch 與
projection hash；不存在的 NAV／risk 明示空狀態。
行情圖週期與研究資料互相獨立；Kronos research 固定使用 canonical `1d` snapshot。
Research mode 會隱含啟用 paper 投影；研究寫入是
唯一 canonical workflow mutation。模型設定的 `PUT`／`DELETE` 只更新本次 server
process 記憶體，不具交易權限；Browser 不能指定 owner、account、target 或 order。
這條路徑不會退回 replay fixture、hard-coded quote 或假成功。
目前 active 行情來源只有 actual runtime 通過的 OpenBB → yfinance；Alpaca、Finnhub、
Alpha Vantage、Twelve Data、Cboe 與付費來源的 credential／display-rights／禁止自動
擷取邊界，見[免費市場資料來源](./docs/research/free-market-data-sources.md)。

API key 不會寫入 HTML、browser storage、DB、artifact 或 log；送出後欄位立即清空，
重新啟動後需再次輸入。也可依[自訂 LLM 設定](./docs/runbooks/llm-configuration.md)
用環境變數作本次啟動的初始設定。

Kronos 是讀取 PIT OHLCV、產生多條未來價格路徑，再由 core 決定性映射成
`AlphaSignal` 的預測 worker，不是聊天模型，也沒有下單權限。目前 actual CPU inference
已接進 GUI research terminal artifact。策略仍是 shadow、paper weight 0；在 genuine
evaluation／promotion authority 存在前，畫面會顯示真 forecast，但 alpha 為 typed
`blocked`、最終 paper 決策為 no-order。`scripts/verify_kronos_runtime.py` 只保留為
獨立診斷，不是啟動前置步驟。

只檢查啟動條件與實際轉交命令、不啟動服務：

```powershell
.\start.ps1 -Mode research -Check
```

完整操作與安全邊界見 [Local GUI runbook](./docs/runbooks/local-gui.md)。

GUI launcher 必須從目前 `main` 的 source checkout 執行，因為它需要 repository 內的
Compose 與 OpenBB corresponding-source build context；standalone wheel、core image 與
immutable `v0.1.2` 都不提供這個 launcher runtime。

### 4. 清理可重建開發輸出

先查看 exact allowlist plan，不變更檔案：

```powershell
uv run --frozen python scripts/clean_workspace.py --dry-run
```

清除 cache、coverage、Playwright output 與不影響快速啟動的 isolated environments：

```powershell
uv run --frozen python scripts/clean_workspace.py --include-isolated-envs
```

工具固定保留 root／OpenBB／Kronos `.venv`、`.data` 模型／artifacts／GUI 狀態、
`.research` 上游證據、所有原始碼及 Docker images／volumes；不呼叫 `git clean` 或
system-wide Docker prune。

### 5. 跑完整離線 paper cycle

```powershell
uv run stonks fake-cycle `
  --symbol AAPL `
  --as-of 2026-01-02T21:00:00Z `
  --idempotency-key demo
```

這個命令不需要 API key、LLM、PostgreSQL、網路或 optional sidecar。它使用 deterministic
fixture 執行 evidence、signal、target、risk、reservation、next-session fill、balanced
journal、report 與 replay，最後輸出統一 JSON envelope。成功輸出應包含：

- `"success": true`
- `"run_status": "completed"`
- `"metadata": {"execution_mode": "paper"}`
- `run_id`、`fill_price`、`projection_hash` 與 report conclusion

這是可重播的整合示範，不是即時行情分析。

### 6. 查看 CLI

```powershell
uv run stonks --help
uv run stonks research --help
uv run stonks strategy --help
uv run stonks paper --help
uv run stonks-deploy --help
uv run stonks-worker --help
```

| Entry point | 用途 | 額外需求 |
|---|---|---|
| `stonks-gui serve` | loopback AI 研究工作台、美股日／日內 bars、圖表與推導報價 | source checkout；Docker；網路；OpenBB／yfinance |
| `stonks-gui serve --with-paper` | 同上，另加本機 PostgreSQL 與唯讀 paper 投資組合面板 | 同上；`127.0.0.1:55433` |
| `stonks-gui serve --with-research` | live snapshot、durable LLM research、SSE 與研究報告 | 同上；自訂 LLM endpoint／model／key |
| `stonks fake-cycle` | 離線完整 paper／replay demo | 無 |
| `stonks data` | 建立 canonical data snapshot request | 本機 development/test PostgreSQL |
| `stonks research` | 建立 research job、讀 verified run events | PostgreSQL、既有 snapshot、local scoped principal |
| `stonks report show` | 從 local artifact store 讀 rendered report | content hash；不需要 PostgreSQL |
| `stonks strategy` | 查詢 evaluation／audit、執行 reviewer transition | PostgreSQL、相符權限 |
| `stonks paper` | portfolio／NAV／risk 查詢與 audited kill-switch 操作 | PostgreSQL、operator scope |
| `stonks-deploy` | migration、health server 與 loopback probe | hardened deployment 設定 |
| `stonks-worker run` | 常駐 fenced dispatcher；exact 分派 snapshot／research job | PostgreSQL；未知 job fail closed |
| `stonks-worker claim-once` | 診斷用：只 claim 一個 fenced durable job | PostgreSQL |

DB-backed CLI 必須明確設定 `STONKS_ENVIRONMENT=local|development|test` 與
`STONKS_DATABASE_URL`；staging／production 不允許使用 local principal。Default Compose
不發布 PostgreSQL host port，因此它不是可直接拿來操作這些 CLI 的開發資料庫。

所有會修改狀態的 CLI 都會驗證 scope、CAS／fence 與 paper-only authority。請先從各 command
的 `--help` 查看 exact arguments；部署邊界見
[Core deployment runbook](./docs/runbooks/core-deployment.md)。

## 驗證專案

### 本機 repository gate

完整 gate 會執行 format check、Ruff、strict mypy、pytest／coverage、schema drift、
upstream policy、secret scan 與 dependency vulnerability audit：

```powershell
uv run python scripts/verify.py
```

沒有網路、只想略過 `pip-audit` 的資料庫查詢時：

```powershell
uv run python scripts/verify.py --skip-audit
```

### Docker deployment smoke

```powershell
uv run python scripts/smoke_core_deployment.py
```

它會自行建立 temporary secret files、乾淨 PostgreSQL volume 與 hardened containers，
驗證 migration、least privilege、restart、DB outage、readiness 與 durable replay，無論
成功或失敗都會清理本次資源。

> [!NOTE]
> Default container 目前只提供 `/healthz` 與 `/readyz`。它是 hardened deployment
> baseline，不是已組合完成的 public research／paper API server。

### Formal release

- [Immutable `v0.1.2` release](https://github.com/RX5950XT/stonks-agent/releases/tag/v0.1.2)
- GHCR：
  `ghcr.io/rx5950xt/stonks-agent@sha256:9c61a2d5dd59d07d30318b483a7a205ac8af394236662b45021574e42ff19976`
- Release archive SHA-256：
  `823dc70999557c770e7c1cd5c7857cf0d9e155147743435a5013a38a98b85434`

Release archive 是含 SBOM、licenses、corresponding source 與五份 Sigstore evidence 的
正式驗證 bundle；它是 GUI 之前的歷史版本。若要重現該 release，請在 `uv sync`
前執行 `git checkout v0.1.2`；若要使用 GUI，請保留目前 `main`。

## Optional integrations

所有 optional profile 預設關閉，且不參與 core readiness：

```powershell
uv run --frozen python -m pytest -q --no-cov `
  tests/config/test_optional_features.py `
  tests/security/test_optional_integrations.py `
  tests/security/test_service_runtime_manifests.py
```

Raw Compose render 會要求各 worker 的 OIDC issuer、audience、subject、client ID 與
JWKS path；缺少任一值就會 fail closed，因此不能把未設定環境的裸 `docker compose
config` 當成可用檢查。各 profile 仍需 exact image、service identity、model／source、
license、SBOM 與 CVE gate。部署環境的 render、啟動方式與目前 matrix 請依
[Optional integrations runbook](./docs/runbooks/optional-integrations.md)。

## 專案結構

| 路徑 | 內容 |
|---|---|
| `src/stonks_agent/` | canonical domain、application services、ports、adapters 與 entrypoints |
| `packages/contracts/` | frozen Pydantic wire contracts |
| `packages/service-auth/` | service identity 與 auth 共用元件 |
| `sidecars/`、`workers/` | optional heavy runtimes 與獨立 locks／images |
| `schemas/` | deterministic JSON Schema 與 OpenAPI snapshots |
| `config/` | typed features、budgets、SLO、release 與 security policies |
| `infra/` | default／optional Compose 與 observability manifests |
| `docs/` | architecture、API、runbooks、research、operations 與 evidence |
| `tests/` | unit、contract、property、integration、policy、security、E2E 與 resilience |
| `tasks/` | implementation plan、review 與 lessons |

`.research/upstreams/` 只供本機研究且不進版控；禁止從該目錄直接 import、vendor 或提交。

## 常見問題

### 為什麼 `fake-cycle` 沒有抓最新股價？

它刻意使用固定 fixture，目的是證明 deterministic paper／replay flow。即時 provider
接入必須先 materialize 成 point-in-time canonical evidence，不能讓外部 API 直接餵給
order flow。若只需要查看最新可取得的真實美股日資料，使用
`uv run --frozen stonks-gui serve`。

### GUI 是即時行情或完整自動交易系統嗎？

不是。終端提供真實 OpenBB／yfinance 日線與日內 bars，並由 bar 序列推導報價，但一律
標示 `is_real_time=false`；預設 `1m` 並依 XNAS session 由後端標示
current／market-closed／delayed／stale／unknown，不能解讀成交易所 real-time entitlement。
Yahoo 的 quote／profile／財報／排行端點目前需要 crumb 而
上游 cookie 主機已無法解析，因此這些能力不提供也不以其他來源冒充。加上
`--with-research` 後可 materialize canonical snapshot、常駐處理 research job 並顯示
報告；HK／TW live provider 與 production ingress 仍未完成。Kronos 尚是 shadow，
不會為展示閉環跳過 promotion、risk 或 reservation；券商帳號與 live trading 則刻意
不支援。

### 是否整合了所有免費資料來源？

沒有，也不會把「免費 endpoint」直接等同可合法顯示的產品來源。目前 active 來源只有
actual runtime 通過的 OpenBB → yfinance；其他來源若需要使用者 key、只允許
non-display、不是免費、或禁止 automated extraction，就維持未組合。完整矩陣見
[免費市場資料來源](./docs/research/free-market-data-sources.md)。

### 為什麼啟動 default Compose 後沒有 research API？

Default deployment 只組合 health／readiness 與 PostgreSQL。六份 business API factory
已有 contracts 與測試，但 production dependency composition、external IdP、public
TLS／proxy 與 distributed enforcement 尚未完成。

### 可以連券商實盤嗎？

不可以。Repository、release policy、contracts 與 runtime 都只允許 `paper`；live
trading 不是隱藏設定。

### Optional worker 顯示 blocked 代表壞掉嗎？

不一定。`blocked` 表示 CI 缺少該 profile 所需的可信 service identity、model 或其他
部署前置條件，因此 fail closed；它不能被標成 runtime passed。

## 文件

先從 [文件中心](./docs/README.md) 選擇需要的路徑：

- [架構決策](./docs/architecture/README.md)
- [API contracts](./docs/api/README.md)
- [Operator runbooks](./docs/runbooks/README.md)
- [Local GUI](./docs/runbooks/local-gui.md)
- [P6 驗證證據](./docs/verification/p6-handoff-evidence.md)
- [上游研究](./docs/research/README.md)
- [Wire schemas](./schemas/README.md)
- [開發交接](./CONTEXT.md)
- [實作與 release review](./tasks/todo.md)

## License

Core 使用 [Apache-2.0](./LICENSE)。Optional upstream 具有各自授權、source-offer 與資料
使用條款；詳見 [license policy](./docs/legal/license-policy.md) 與
[third-party notices](./THIRD_PARTY_NOTICES.md)。
