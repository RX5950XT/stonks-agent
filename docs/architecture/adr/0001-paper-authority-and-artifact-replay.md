# ADR-0001：Paper authority 與 artifact replay

- 狀態：Accepted
- 日期：2026-07-22
- 決策範圍：canonical trading authority、stochastic inference、replay

## Context

LLM、TradingAgents、Kronos、quant model 與 community feedback 都會產生非確定性或
外部輸入。若它們能直接建立 target/order 或覆寫 risk，paper ledger、replay 與
audit 就不再有單一權威；若 replay 重新執行 stochastic inference，也不能合理宣稱
bit-identical。

## Decision

唯一 canonical flow 是：

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

只有 core job runner 擁有 DB/event/outbox transaction authority。Remote workers 無
DB credential，且 generation、nonce、lease 任一 stale 即不得 commit。LLM、模型與
community 只能產生 typed artifact/signal，不能跳過 deterministic portfolio/risk，
也不能直接建立 order。

Stochastic inference 完成後先封存 immutable output artifact。Replay 從該 artifact
開始重建 deterministic control plane；保證的是 artifact 之後的 canonical 結果與
hash-chain 可驗證，不宣稱 fresh re-inference bit-identical。

`execution_mode=paper` 是唯一模式。任何 live trading 都必須另立 RFC、broker
reconciliation、安全與法律 gate，不能以設定值啟用。

## Consequences

- 同帳戶 mutation 先 reservation 並 serialized；journal 按每種 currency/commodity
  平衡。
- Late/duplicate worker result、budget exhausted、ledger mismatch 與 risk unknown
  全部 fail closed。
- Replay evidence 可稽核，但不能證明模型 provider 在未來仍產生相同 token/path。
- External paper/copy platform 的 position/outcome 只能轉為 external evidence。

## Repository evidence

[整合藍圖](../integration-blueprint.md)、[API index](../../api/README.md) 與
[P6 evidence index](../../verification/p6-handoff-evidence.md) 提供目前實作／驗證入口。
