# API index

目前 repository 匯出六份 OpenAPI 3.1 snapshots。它們是六個獨立 FastAPI factory 的
contract snapshots，不是單一部署入口；default deployment health app 尚未組合成
production business API。snapshot 未內嵌 OpenAPI security scheme 不代表匿名存取：
business app runtime 仍由 injected authenticator、route permission、exact ownership 與
中央 API security composition fail closed。

## Exact surfaces

| Snapshot | Title | Exact paths |
|---|---|---|
| [data.openapi.json](../../schemas/openapi/v1/data.openapi.json) | `Stonks Agent Data API` | `/v1/data/snapshots` |
| [deployment.openapi.json](../../schemas/openapi/v1/deployment.openapi.json) | `Stonks Agent Deployment Health` | `/healthz`；`/readyz` |
| [paper-operations.openapi.json](../../schemas/openapi/v1/paper-operations.openapi.json) | `Stonks Agent Paper Operations API` | `/v1/paper/kill-switches/activate`；`/v1/paper/kill-switches/resume`；`/v1/paper/kill-switches/{scope}`；`/v1/paper/operator-actions`；`/v1/paper/reconciliation` |
| [paper-projections.openapi.json](../../schemas/openapi/v1/paper-projections.openapi.json) | `Stonks Agent Paper Projection API` | `/v1/paper/accounts/{account_id}/nav`；`/v1/paper/accounts/{account_id}/portfolio`；`/v1/paper/accounts/{account_id}/risk` |
| [research.openapi.json](../../schemas/openapi/v1/research.openapi.json) | `Stonks Agent Research API` | `/v1/reports/{content_hash}`；`/v1/research/runs`；`/v1/research/runs/{run_id}/events` |
| [strategies.openapi.json](../../schemas/openapi/v1/strategies.openapi.json) | `Stonks Agent Strategy API` | `/v1/evaluations/{report_id}`；`/v1/signals/eligibility`；`/v1/strategies/{strategy_id}/versions/{strategy_version}`；`/v1/strategies/{strategy_id}/versions/{strategy_version}/events`；`/v1/strategies/{strategy_id}/versions/{strategy_version}/transitions` |

六份 `info.version` 均為 `0.1.0`；這是 API contract snapshot version，與產品 release
SemVer `v0.1.2` 是不同版本軸。Snapshots 由
[`scripts/export_schemas.py`](../../scripts/export_schemas.py) 產生與 drift-check；詳細 wire
schema index 見 [schemas README](../../schemas/README.md)。

## Authentication、RBAC 與 ownership

- Deployment healthz、readyz 是無 human principal 的 bounded health surface；
  readiness 不代表 business APIs 已組合或 external dependency 全部可用。
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
business/health factories 只各自有 contract、security 與 E2E/integration tests；default
Compose 僅啟動 deployment health/readiness app，未組合成 production business API，
也沒有 trusted proxy/distributed rate-limit、public TLS/HSTS 或跨主機 mTLS 的驗證證據。
