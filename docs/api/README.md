# API index

目前 repository 匯出七份 OpenAPI 3.1 snapshots。六份既有 business／health factory
維持 reference contracts；新增 GUI snapshot 是已完成 actual runtime 驗證的 loopback
local surface。它們不是單一 production 部署入口，default deployment health app 仍未
組合成 production business API。六份 business/health snapshot 未內嵌 OpenAPI
security scheme 不代表匿名存取：其 runtime 仍由 injected authenticator、route
permission、exact ownership 與中央 API security composition fail closed。GUI 是明確
例外：Browser/JSON route 沒有人類 auth，只接受 loopback direct peer/Host；research
POST 與 session-only model settings PUT／DELETE另要求same-origin與process-memory intent。
未注入對應 facade 時固定回 structured 503。短效 RS256 identity 只用於 GUI →
OpenBB／Kronos sidecar；既有 GUI → OpenBB sidecar authority boundary 維持不變。

## Exact surfaces

| Snapshot | Title | Exact paths |
|---|---|---|
| [data.openapi.json](../../schemas/openapi/v1/data.openapi.json) | `Stonks Agent Data API` | `/v1/data/snapshots` |
| [deployment.openapi.json](../../schemas/openapi/v1/deployment.openapi.json) | `Stonks Agent Deployment Health` | `/healthz`；`/readyz` |
| [gui.openapi.json](../../schemas/openapi/v1/gui.openapi.json) | `Stonks Terminal` | `/api/v1/capabilities`；`/api/v1/market/bars`；`/api/v1/market/quotes`；`/api/v1/market-data/latest`；`/api/v1/instrument/overview`；`/api/v1/settings/llm`；`/api/v1/research/runs`；`/api/v1/research/runs/{run_id}`；`/api/v1/research/runs/{run_id}/events`；`/api/v1/research/runs/{run_id}/evidence` |
| [paper-operations.openapi.json](../../schemas/openapi/v1/paper-operations.openapi.json) | `Stonks Agent Paper Operations API` | `/v1/paper/kill-switches/activate`；`/v1/paper/kill-switches/resume`；`/v1/paper/kill-switches/{scope}`；`/v1/paper/operator-actions`；`/v1/paper/reconciliation` |
| [paper-projections.openapi.json](../../schemas/openapi/v1/paper-projections.openapi.json) | `Stonks Agent Paper Projection API` | `/v1/paper/accounts/{account_id}/nav`；`/v1/paper/accounts/{account_id}/portfolio`；`/v1/paper/accounts/{account_id}/risk` |
| [research.openapi.json](../../schemas/openapi/v1/research.openapi.json) | `Stonks Agent Research API` | `/v1/reports/{content_hash}`；`/v1/research/runs`；`/v1/research/runs/{run_id}/events` |
| [strategies.openapi.json](../../schemas/openapi/v1/strategies.openapi.json) | `Stonks Agent Strategy API` | `/v1/evaluations/{report_id}`；`/v1/signals/eligibility`；`/v1/strategies/{strategy_id}/versions/{strategy_version}`；`/v1/strategies/{strategy_id}/versions/{strategy_version}/events`；`/v1/strategies/{strategy_id}/versions/{strategy_version}/transitions` |

六份既有 factory 的 `info.version` 維持 `0.1.0`；新增 GUI snapshot 是未發布
`0.2.0`。API contract version 與 immutable product release `v0.1.2` 是不同版本軸，
而該 release 不含 GUI。Snapshots 由
[`scripts/export_openapi.py`](../../scripts/export_openapi.py) 產生，contract tests
執行 drift-check；詳細 wire schema index 見 [schemas README](../../schemas/README.md)。

## Authentication、RBAC 與 ownership

- Deployment healthz、readyz 是無 human principal 的 bounded health surface；
  readiness 不代表 business APIs 已組合或 external dependency 全部可用。
- Local GUI JSON route 只接受 loopback direct peer／Host；唯一 workflow mutation 是
  bounded research command。Model settings PUT／DELETE只管理本次process-memory
  route／secret，不是 workflow／trading mutation；Browser 不可指定 owner、account、
  execution mode、target 或 order。
  只有 `--with-research` 會注入 durable workflow facade；否則固定回 503。History、
  detail、events 與 evidence read 都 exact owner scoped，evidence 只含 final claims
  實際引用的 snapshot-safe projection。市場讀取仍以 ephemeral service identity 呼叫
  OpenBB，provider failure 不會 fallback 到 fixture。
- Data snapshot 與 research run command 需要 `run_research`。
- Projection、report、run events、strategy/evaluation read 與 signal eligibility 需要
  `read`，並在有 owner/account scope 時執行 exact ownership。
- Strategy transition 需要 `review_strategy`；kill switch、reconciliation、resume 與
  operator audit 需要 `operate_paper`。
- Production human principal 只接受 server-side asymmetric OIDC/JWKS validation；local
  bearer 僅限 loopback local/development/test。Forwarded identity 預設拒絕。

## Envelope 與 SSE

JSON boundary 固定 `success/status/data/error/metadata`；pagination 放 `metadata`，錯誤
使用 bounded structured code/message，不回 stack、credential 或 raw identity。

Research events 使用 `text/event-stream`，event sequence 形成 `id`，事件類型形成
`event`，`data` 仍是相同 JSON envelope。Client 以 `Last-Event-ID` 續讀；server 只讀
canonical append-only events，不能把 SSE 當 job dispatcher 或 mutation channel。

## Authority 與部署限制

所有交易 surface 都是 paper-only。API、LLM、worker、forecast 或 external community
route 都不能直接跳過 deterministic target/risk/reservation/order/ledger flow。目前六個
business/health factories 只各自有 contract、security 與 E2E/integration tests；local
GUI 是獨立 loopback runtime。Default Compose 僅啟動 deployment health/readiness app，
未組合成 production business API，也沒有 trusted proxy/distributed rate-limit、
public TLS/HSTS 或跨主機 mTLS 的驗證證據。
