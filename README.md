# Stonks Agent

Stonks Agent 是 evidence-first、可稽核、可重播的投資研究與 paper trading 平台。P0 Foundation 與 P1 Canonical Data Hub 已通過 phase gate；P2 Research control plane 已完成 bounded research、structured LLM、TradingAgents worker/core adapter、draft PEAD/event-study、evidence/report integrity與deterministic multi-channel rendering，後續工作依[實作計畫](./tasks/todo.md)持續開發。

目前唯一 execution mode 是 `paper`，不支援 real-money trading。

## 已驗證能力

- Python 3.12 + `uv` workspace、frozen lock、ruff、mypy、pytest 與 80% coverage gate。
- 版本化 frozen Pydantic wire contracts 與 deterministic JSON Schema snapshots。
- 完整 in-memory paper cycle：evidence → signal → target → risk → cash reservation → next-session fill → balanced journal → report → replay。
- Idempotency、同帳戶並行防雙花、job generation/nonce fencing、late-result quarantine。
- PostgreSQL PIT evidence/snapshot、content-addressed artifacts、Repository/UoW、durable job/outbox/inbox與transaction-owned audit events。
- DB-authoritative lease/deadline/not-before fencing；caller clock漂移、duplicate/stale result與tampered retry graph皆fail closed。
- US/HK/TW replay fixtures與canonical snapshot materialization；雙來源reconciliation trace可由immutable artifact離線重驗。
- Financial Datasets與OpenBB read-only observation adapters共用daily query shape；optional OpenBB sidecar使用獨立lock/image、exact route allowlist、SBOM與AGPL corresponding source。
- Evidence-scoped `ResearchRequest/ResearchArtifact/AgentOpinion`、usage budget、structured LLM contracts，以及 deny-by-default read-only tool authorization；tool result會核對scope、identity、hash與byte limit。
- Clean-room bounded research orchestration：PIT artifact context、untrusted content isolation、structured planning/final turns、pre-authorized parallel read tools、budget/deadline hard-stop與deterministic artifact mapping。
- Frozen model allowlist與offline fake、OpenAI-compatible、Anthropic structured-output adapters；exact raw response先封存，JSON Schema、model identity、deadline、token/cost/cache pricing、bounded retry/repair與secret redaction均fail closed。
- Pinned TradingAgents v0.3.1 isolated worker：PIT canonical evidence facade、profile-per-process、serialized global config、internal model proxy、`AnalysisBundle/AgentOpinion`-only authority boundary，以及獨立lock/image/Apache notice。
- TradingAgents core adapter只傳signed artifact refs；fixed-origin HTTP、generation/nonce/result hash、nested context與schema drift全部fail closed。只有core DB transaction可一起註冊artifact metadata、append event/outbox並ack，stale result只進quarantine audit。
- ai-hedge-fund MIT selective port：PIT PEAD filing清理與pure-Python/Decimal event study（OLS、abnormal returns、CAR、Student t-test、seeded bootstrap）。策略固定`draft`、confidence 0，通過正式evaluation前不可成為paper target。
- Versioned analysis context assembler：只讀canonical repository一次，依capability組裝evidence blocks；PIT、sensitivity、license與redistribution scope fail closed，missing/stale/conflict/fallback/estimated/partial/fetch_failed不會被扁平化成假success。
- Structured `AnalysisReport` JSON truth：factual claim必須有citation與derived quality；stale/estimated/conflict只能是qualified，hypothesis不得偽裝evidenced fact。LLM只填closed draft，outlook、claim IDs、evidence union與paper/research guardrails由core決定。
- Sandboxed fixed Jinja templates從同一`AnalysisReport`重建full/brief Markdown與email HTML；Markdown/HTML escaping、quality qualifiers、language labels、channel byte caps與content-addressed rendering hashes均deterministic。
- Local RBAC、process capability/egress deny、secret redaction、統一 API envelope 與 telemetry ports。
- License/upstream policy、secret scan、locked dependency CVE audit，以及 Windows/Linux CI。

## Quick start

```powershell
uv sync --frozen
uv run python scripts/verify.py
uv run stonks fake-cycle --symbol AAPL --as-of 2026-01-02T21:00:00Z --idempotency-key demo
```

`fake-cycle` 完全離線，不需要 provider key、LLM、PostgreSQL 或 optional sidecar。

P1 的canonical ingestion已以replay source完整驗證。Financial Datasets與OpenBB目前是contract-tested observation adapters，尚未宣稱已接成production canonical materialization source；OpenAI-compatible與Anthropic adapters目前以官方wire contract及mock transport驗證，尚未使用真實credentials做live smoke；TradingAgents worker與core HTTP/job completion contract已驗證，但尚未提供常駐research dispatcher或production artifact capability signer；`stonks-worker claim-once`也不是常駐dispatcher。

## 核心文件

- [整合架構藍圖](./docs/architecture/integration-blueprint.md)
- [實作計畫](./tasks/todo.md)
- [上游研究索引](./docs/research/README.md)
- [研究一致性驗證](./docs/research/verification.md)
- [開發交接](./CONTEXT.md)

## 安全邊界

- 只有本地 canonical paper executor 可建立模擬交易；AI-Trader 只供 community/outcome observation。
- LLM、TradingAgents、Kronos 只產 evidence/opinion/signal，不能直接下單或覆寫 risk。
- Live trading 不在目前授權範圍，後續必須另立 RFC。
- 所有輸出僅供研究與模擬，不構成投資或法律建議。
