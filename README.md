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
這裡的「完成」是指程式碼、測試、供應鏈與發行 gate 已關閉，不代表
它已是可連接券商、直接實盤或可暴露於公網的 production 產品；目前成熟度仍是
`pre-alpha`。

| 範圍 | 狀態 | 代表意義 |
|---|---|---|
| Canonical research／paper flow | `implemented` | contracts、PostgreSQL、replay、risk、reservation、fill 與 balanced journal 已測試 |
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
- 只有執行 deployment smoke 或 optional sidecar 時才需要 Docker
  Engine／Docker Desktop 與 Compose v2。

### 2. 取得程式碼與依賴

使用最新開發文件：

```powershell
git clone https://github.com/RX5950XT/stonks-agent.git
cd stonks-agent
uv python install 3.12
uv sync --frozen --python 3.12
```

若要重現 formal verified release，請在 `uv sync` 前固定 tag：

```powershell
git checkout v0.1.2
```

### 3. 跑完整離線 paper cycle

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

### 4. 查看 CLI

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
| `stonks fake-cycle` | 離線完整 paper／replay demo | 無 |
| `stonks data` | 建立 canonical data snapshot request | 本機 development/test PostgreSQL |
| `stonks research` | 建立 research job、讀 verified run events | PostgreSQL、既有 snapshot、local scoped principal |
| `stonks report show` | 從 local artifact store 讀 rendered report | content hash；不需要 PostgreSQL |
| `stonks strategy` | 查詢 evaluation／audit、執行 reviewer transition | PostgreSQL、相符權限 |
| `stonks paper` | portfolio／NAV／risk 查詢與 audited kill-switch 操作 | PostgreSQL、operator scope |
| `stonks-deploy` | migration、health server 與 loopback probe | hardened deployment 設定 |
| `stonks-worker claim-once` | claim 一個 fenced durable job | PostgreSQL；不是常駐 dispatcher |

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
正式驗證 bundle；日常開發仍建議使用 Git checkout 加 frozen lock。

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
order flow。

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
- [P6 驗證證據](./docs/verification/p6-handoff-evidence.md)
- [上游研究](./docs/research/README.md)
- [Wire schemas](./schemas/README.md)
- [開發交接](./CONTEXT.md)
- [實作與 release review](./tasks/todo.md)

## License

Core 使用 [Apache-2.0](./LICENSE)。Optional upstream 具有各自授權、source-offer 與資料
使用條款；詳見 [license policy](./docs/legal/license-policy.md) 與
[third-party notices](./THIRD_PARTY_NOTICES.md)。
