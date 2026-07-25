# ADR-0002：Process、dependency 與 license isolation

- 狀態：Accepted
- 日期：2026-07-22
- 決策範圍：heavy upstream、wire contracts、licensing

## Context

OpenBB、TradingAgents、Kronos、Qlib、RD-Agent、NautilusTrader 與 LEAN 的 runtime、
native dependency 與授權條件不同。把它們塞入 core lock/process 會放大供應鏈、記憶體、
global state、授權與故障半徑；process boundary 本身也不會免除 copyleft 義務。

## Decision

Core 維持 Python 3.12 與獨立 frozen lock。Heavy upstream 各自使用獨立 lock、image、
SBOM、NOTICE/source closure 與 resource budget；跨程序只交換版本化 JSON
Schema/OpenAPI。Remote worker 不取得 core DB credentials 或 portfolio/execution ports。

- TradingAgents、Kronos、Qlib/RD-Agent、NautilusTrader 與 LEAN 保持 isolated runtime。
- OpenBB 僅能依核准的 optional AGPL sidecar policy 接入；發布者仍須履行 AGPL。
- Dexter 與 AI-Trader 授權不完整，不複製 source、prompt、skills、assets、frontend 或
  server。AI-Trader 只允許 default-off external community HTTP adapter。
- 移植 MIT/Apache code 必須保留 copyright/license/NOTICE 與來源 commit；模型、資料
  與 provider ToS 另外追蹤。
- 同步 heavy worker offload event loop，per-process concurrency 固定為 1；滿載立即
  回 `429 worker_busy`，core 不對 429 自動 retry。

## Consequences

- Core lock 不吸收 PyTorch、OpenBB、TradingAgents、Qlib、RD-Agent、Nautilus 或 LEAN。
- JSON contract 需要 tolerant reader、versioning、size/time/idempotency 與 authz 驗證。
- Process boundary 降低 runtime coupling，但不等於 security、network 或 license 豁免。
- Optional service 缺席不得影響 default core readiness 與 paper safety。

## Repository evidence

[整合藍圖](../integration-blueprint.md)、[runbook index](../../runbooks/README.md) 與
[P6 evidence index](../../verification/p6-handoff-evidence.md) 記錄可核驗範圍。
