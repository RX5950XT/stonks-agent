# Stonks Agent

[![CI](https://github.com/RX5950XT/stonks-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/RX5950XT/stonks-agent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/RX5950XT/stonks-agent)](https://github.com/RX5950XT/stonks-agent/releases/tag/v0.1.2)
[![License](https://img.shields.io/github/license/RX5950XT/stonks-agent)](./LICENSE)

Stonks Agent 是一套 evidence-first、可稽核、可重播的投資研究與 paper trading 平台。
它把 AI 研究、時間序列預測、市場資料、量化評估、回測與模擬交易整合在同一組 canonical
contracts 後面，並且不允許 LLM 或任何外部平台繞過 deterministic 的風控、資金保留、
成交與帳務。

> [!IMPORTANT]
> 唯一允許的 execution mode 是 `paper`。專案不支援 live trading，也不構成投資、法律或
> 財務建議。目前成熟度為 `pre-alpha`。

## 它是什麼、不是什麼

**是**：一個本機執行的研究工作台。使用者輸入標的，系統取得真實市場資料、產生 AI 研究
報告與 Kronos 價格預測，並把每一句結論綁回實際引用的證據；模擬交易全程唯讀可稽核。

**不是**：即時報價終端、自動交易機器人，也不是可直接連券商或暴露於公網的 production
服務。它取得的是近即時的歷史 bar，所有回傳都標示 `is_real_time=false`。

## 目前狀態

原訂 P0-P6.11 repository implementation、公開倉庫與 `v0.1.2` formal release closure 已完成，
但該 release 不含 GUI。目前工作樹是尚未發布的 `0.2.0` candidate，Local GUI 只存在於此。
「完成」指程式碼、測試、供應鏈與發行 gate 已關閉，不代表它是可連接券商或可暴露於公網的
production 產品。

| 範圍 | 狀態 | 說明 |
|---|---|---|
| Canonical research／paper flow | `implemented` | contracts、PostgreSQL、replay、風控、資金保留、成交與平衡帳務均已測試 |
| Stonks Desk 與美股／台股行情 | `actual_runtime_verified` | 本機 loopback 研究工作台；經 isolated OpenBB／yfinance 取得 US 與 TW bars，提供 OHLCV 表格、研究歷史與 runtime health |
| Desk paper 投資組合面板 | `actual_runtime_verified` | 唯讀顯示 NAV、現金／保留、部位、風控與 kill switch |
| Desk durable research | `composed / external_llm_required` | 需要使用者自備 LLM endpoint／model／key |
| Kronos CPU forecast | `gui_composed / shadow` | 每次 run 封存 snapshot-bound artifact；paper weight 0，不具下單權限 |
| Public `v0.1.2` release | `externally_verified` | protected tag、GHCR、keyless signatures、provenance、SBOM 皆已重驗 |
| Default Docker deployment | `implemented` | 單機 core／PostgreSQL 的 health、migration、restart、outage 與 replay 已驗證 |
| Optional integrations | `mixed` | 4 個 CI runtime 實測通過、5 個缺部署憑證而 blocked、1 個 GPU profile unsupported |
| Production business API | `not_composed` | 六份 API contract 已存在，但 default deployment 尚未組合成 production business API |
| External production wiring | `unverified` | 真實 IdP、cloud secret manager、public TLS、distributed rate limit 尚未完成 |
| Live trading | `unsupported` | 沒有任何開關可啟用 |

逐項證據見 [P6 handoff evidence](./docs/verification/p6-handoff-evidence.md)。

## Canonical flow

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

外部模型只能在前半段產生證據、意見、預測與評估。只有 core 能建立 target、通過風控、
保留資金、模擬成交並寫入帳本。

## 快速開始

### 前置需求

- Git、`uv`、Python 3.12（不支援 3.11／3.13）。
- Docker Engine／Desktop 與 Compose v2：只有啟動 GUI、執行 deployment smoke 或 optional
  sidecar 時才需要。

### 安裝

```powershell
git clone https://github.com/RX5950XT/stonks-agent.git
cd stonks-agent
uv python install 3.12
uv sync --frozen --python 3.12
```

### 啟動 Stonks Desk

根目錄的 `start.ps1`（Windows）與 `start.sh`（Linux／macOS）是等價的薄 launcher，參數、
檢查順序與 `-Check`／`--check` 輸出完全相同。三種模式：

```powershell
.\start.ps1 -Mode market     # 只有行情
.\start.ps1 -Mode paper      # 行情 + 本機 PostgreSQL 與唯讀投資組合
.\start.ps1                  # 預設 research：完整 AI 研究 + Kronos
```

Launcher 會檢查 source checkout、`uv`、Docker，同步 frozen dependencies，建立只含 public
key 的暫時 JWKS，啟動 isolated OpenBB sidecar，最後在 `http://127.0.0.1:8787` 開啟介面。

介面預設讀取 `1m` historical bars，分頁可見時每 30 秒 bounded 更新。在頂端輸入 `AAPL`
即可讀取報價與走勢；底部命令列只是進階入口，`AAPL 5m` 切換週期（`1m` `5m` `15m` `1h`
`1d`）、`ADD NVDA` 加入關注清單、`F1` 顯示全部命令。每個面板都會標示 provider、資料
時效、品質、observed／served／latest event time 與快取狀態。

只檢查啟動條件、不實際啟動服務：

```powershell
.\start.ps1 -Mode research -Check
```

### 啟用 AI 研究

Research mode 需要兩項前置：Kronos 權重與一組 LLM 設定。

權重由 one-shot 腳本取得（worker runtime 本身禁止下載）：

```powershell
uv run --frozen python scripts/fetch_kronos_model.py
```

腳本只抓 `workers/kronos/model-manifest.json` 記載的 exact repository／revision，逐檔比對
size 與 SHA-256，不符即刪檔並以非零 exit code 停止，結果寫入 `.data/models/kronos/`。
重跑會驗證既有檔案而不重新下載。缺檔時 launcher 會以 exit code 2 停止，不會退化成假的
forecast。

LLM 設定可直接在介面的「LLM 模型連線」輸入 base URL、Model ID 與 API key，按「儲存並
驗證」後系統會先做一次 structured completion 才啟用。若不想每次重開都重打，可將
`.env.example` 複製為根目錄 `.env`（已 gitignored）並填入值；launcher 會在啟動前載入並
注入子行程環境。`.env` 只接受 `STONKS_*` 鍵，出現其他鍵一律拒絕啟動。

API key 不會寫入 HTML、browser storage、資料庫、artifact 或 log，送出後欄位立即清空。
其他設定方式見 [自訂 LLM 設定](./docs/runbooks/llm-configuration.md)。

驗證完成後即可搜尋標的並按「開始 AI 研究」。介面會依序顯示 snapshot、evidence、AI 分析
與報告進度，接著呈現信心度、claims 與 evidence refs、反方觀點、風險、Kronos 的實際
model／revision 與三路徑報酬指標、alpha 資格與 paper 決策。每條 citation 都可定位到本輪
snapshot 內實際引用的證據。

Kronos 是讀取 PIT OHLCV、產生多條未來價格路徑，再由 core 決定性映射成 `AlphaSignal` 的
預測 worker，不是聊天模型，也沒有下單權限。目前策略維持 `shadow`、paper weight 0，因此
畫面會顯示真實 forecast，但 alpha 為 typed `blocked`、最終 paper 決策為 no-order。

目前唯一 active 的行情來源是實測通過的 OpenBB → yfinance。其他來源的授權與限制見
[免費市場資料來源](./docs/research/free-market-data-sources.md)。完整操作與安全邊界見
[Local GUI runbook](./docs/runbooks/local-gui.md)。

> [!NOTE]
> Launcher 必須從 `main` 的完整 source checkout 執行，因為它需要 repository 內的 Compose
> 與 OpenBB corresponding-source build context。standalone wheel、core image 與 `v0.1.2`
> 都不提供這個 runtime。

### 離線 paper cycle

不需要 API key、LLM、PostgreSQL、網路或任何 sidecar：

```powershell
uv run stonks fake-cycle `
  --symbol AAPL `
  --as-of 2026-01-02T21:00:00Z `
  --idempotency-key demo
```

它以 deterministic fixture 跑完 evidence、signal、target、風控、資金保留、次一 session
成交、平衡帳務、報告與 replay，輸出統一 JSON envelope，應包含 `"success": true`、
`"run_status": "completed"`、`"metadata": {"execution_mode": "paper"}` 以及 `run_id`、
`fill_price`、`projection_hash` 與報告結論。這是可重播的整合示範，不是即時行情分析。

### 清理可重建輸出

```powershell
uv run --frozen python scripts/clean_workspace.py --dry-run              # 先看 plan
uv run --frozen python scripts/clean_workspace.py --include-isolated-envs
```

工具使用 exact allowlist，固定保留原始碼、root／OpenBB／Kronos `.venv`、`.data` 模型與
GUI 狀態、`.research` 證據以及 Docker images／volumes，不呼叫 `git clean` 或 system-wide
prune。

## CLI

```powershell
uv run stonks --help
uv run stonks-deploy --help
uv run stonks-worker --help
```

| Entry point | 用途 | 額外需求 |
|---|---|---|
| `stonks-gui serve` | 本機研究工作台、美股與台股日／日內 bars | source checkout、Docker、網路 |
| `stonks-gui serve --with-paper` | 同上，另加本機 PostgreSQL 與唯讀 paper 面板 | 同上；`127.0.0.1:55433` |
| `stonks-gui serve --with-research` | live snapshot、durable LLM research、SSE 與報告 | 同上；自備 LLM endpoint／model／key |
| `stonks fake-cycle` | 離線完整 paper／replay demo | 無 |
| `stonks data` | 建立 canonical data snapshot request | 本機 development/test PostgreSQL |
| `stonks research` | 建立 research job、讀 verified run events | PostgreSQL、既有 snapshot |
| `stonks report show` | 從 local artifact store 讀 rendered report | content hash |
| `stonks strategy` | 查詢 evaluation／audit、執行 reviewer transition | PostgreSQL、相符權限 |
| `stonks paper` | portfolio／NAV／risk 查詢與 audited kill-switch | PostgreSQL、operator scope |
| `stonks-deploy` | migration、health server 與 loopback probe | hardened deployment 設定 |
| `stonks-worker run` | 常駐 fenced dispatcher | PostgreSQL；未知 job fail closed |
| `stonks-worker claim-once` | 診斷用：只 claim 一個 durable job | PostgreSQL |

需要資料庫的 CLI 必須明確設定 `STONKS_ENVIRONMENT=local|development|test` 與
`STONKS_DATABASE_URL`；staging／production 不允許使用 local principal。Default Compose 不
發布 PostgreSQL host port，因此它不是可直接給這些 CLI 使用的開發資料庫。所有會改變狀態
的 CLI 都會驗證 scope、CAS／fence 與 paper-only authority。

## 驗證

### 本機 repository gate

執行 format check、Ruff、strict mypy、pytest／coverage、schema drift、upstream policy、
secret scan 與 dependency vulnerability audit：

```powershell
uv run python scripts/verify.py
uv run python scripts/verify.py --skip-audit   # 無網路時略過漏洞資料庫查詢
```

### Docker deployment smoke

```powershell
uv run python scripts/smoke_core_deployment.py
```

它會自行建立臨時 secret files、乾淨的 PostgreSQL volume 與 hardened containers，驗證
migration、least privilege、restart、DB outage、readiness 與 durable replay，無論成功或
失敗都會清理本次資源。

> [!NOTE]
> Default container 目前只提供 `/healthz` 與 `/readyz`，它是 hardened deployment baseline，
> 不是已組合完成的 public research／paper API server。

### Optional integrations

所有 optional profile 預設關閉，且不參與 core readiness：

```powershell
uv run --frozen python -m pytest -q --no-cov `
  tests/config/test_optional_features.py `
  tests/security/test_optional_integrations.py `
  tests/security/test_service_runtime_manifests.py
```

Raw Compose render 會要求各 worker 的 OIDC issuer、audience、subject、client ID 與 JWKS
path，缺任一值即 fail closed，因此裸 `docker compose config` 不能當成可用性檢查。詳見
[Optional integrations runbook](./docs/runbooks/optional-integrations.md)。

### Formal release

- [Immutable `v0.1.2` release](https://github.com/RX5950XT/stonks-agent/releases/tag/v0.1.2)
- GHCR digest：
  `sha256:9c61a2d5dd59d07d30318b483a7a205ac8af394236662b45021574e42ff19976`
- Release archive SHA-256：
  `823dc70999557c770e7c1cd5c7857cf0d9e155147743435a5013a38a98b85434`

Release archive 是含 SBOM、licenses、corresponding source 與五份 Sigstore evidence 的驗證
bundle。重現該 release 須在 `uv sync` 前 `git checkout v0.1.2`；要使用 GUI 則保留 `main`。

## 上游整合

| 專案 | 整合方式 | 現況 |
|---|---|---|
| ai-hedge-fund | MIT selective port：PEAD 與 event study | 已實作，輸出維持 research-only |
| Dexter | clean-room 概念參考 | 授權證據不足，不 vendor source／prompt／assets |
| TradingAgents | pinned isolated worker + typed adapter | 已實作；部署需可信 service identity |
| Kronos | pinned CPU／CUDA forecast worker | 已實作；需本機唯讀模型 |
| daily_stock_analysis | report schema 與 evidence-quality primitives | 已整合至自有 reporting contracts |
| AI-Trader | default-off external community HTTP adapter | 只收 untrusted evidence |
| OpenBB | optional AGPL sidecar | 已驗；必須履行 corresponding-source 義務 |
| Qlib | isolated quant-lab worker | 已實作；只允許 evaluation output |
| RD-Agent | ephemeral clean-room factor sandbox | 已驗；generated code 不會自動 promote |
| NautilusTrader／LEAN | isolated backtest sidecars | 已驗，含 cross-engine parity fixtures |

完整 process、license 與 authority 邊界見
[整合架構藍圖](./docs/architecture/integration-blueprint.md)。

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

`.research/upstreams/` 只供本機研究且不進版控，禁止從該目錄直接 import、vendor 或提交。

## 常見問題

**為什麼 `fake-cycle` 沒有抓最新股價？**
它刻意使用固定 fixture，用途是證明 deterministic paper／replay flow。即時 provider 必須
先 materialize 成 point-in-time canonical evidence，不能讓外部 API 直接餵給 order flow。
需要真實資料時改用 `uv run --frozen stonks-gui serve`。

**GUI 是即時行情或自動交易系統嗎？**
都不是。它提供真實的 OpenBB／yfinance 日線與日內 bars，並由 bar 序列推導報價，但一律
標示 `is_real_time=false`，由後端依交易所 session 標示 current／market-closed／delayed／
stale／unknown。Yahoo 的 quote／profile／財報／排行端點目前需要 crumb 而上游 cookie 主機
已無法解析，因此這些能力維持不提供，也不換來源冒充。

**是否整合了所有免費資料來源？**
沒有。免費額度不等於可合法顯示的產品來源。需要使用者 key、只允許 non-display、並非免費
或禁止 automated extraction 的來源都維持未組合。

**為什麼啟動 default Compose 後沒有 research API？**
Default deployment 只組合 health／readiness 與 PostgreSQL。六份 business API 已有 contracts
與測試，但 production dependency composition、external IdP 與 public TLS 尚未完成。

**可以連券商實盤嗎？**
不可以。Repository、release policy、contracts 與 runtime 都只允許 `paper`。

**Optional worker 顯示 blocked 代表壞掉嗎？**
不一定。`blocked` 表示 CI 缺少該 profile 所需的可信 service identity、模型或其他部署前置
條件，因此 fail closed；它不能被標記成 runtime passed。

## 文件

- [文件中心](./docs/README.md)
- [架構決策](./docs/architecture/README.md)
- [API contracts](./docs/api/README.md)
- [Operator runbooks](./docs/runbooks/README.md)
- [Local GUI](./docs/runbooks/local-gui.md)
- [P6 驗證證據](./docs/verification/p6-handoff-evidence.md)
- [上游研究](./docs/research/README.md)
- [開發交接](./CONTEXT.md)

## License

Core 使用 [Apache-2.0](./LICENSE)。Optional upstream 具有各自授權、source-offer 與資料使用
條款，詳見 [license policy](./docs/legal/license-policy.md) 與
[third-party notices](./THIRD_PARTY_NOTICES.md)。
