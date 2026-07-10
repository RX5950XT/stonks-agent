# Stonks Agent

Stonks Agent 是 evidence-first、可稽核、可重播的投資研究與 paper trading 平台。P0 基礎已完成並通過驗證；P1–P6 依[實作計畫](./tasks/todo.md)持續開發。

目前唯一 execution mode 是 `paper`，不支援 real-money trading。

## 已驗證能力

- Python 3.12 + `uv` workspace、frozen lock、ruff、mypy、pytest 與 80% coverage gate。
- 版本化 frozen Pydantic wire contracts 與 deterministic JSON Schema snapshots。
- 完整 in-memory paper cycle：evidence → signal → target → risk → cash reservation → next-session fill → balanced journal → report → replay。
- Idempotency、同帳戶並行防雙花、job generation/nonce fencing、late-result quarantine。
- Local RBAC、process capability/egress deny、secret redaction、統一 API envelope 與 telemetry ports。
- License/upstream policy、secret scan、locked dependency CVE audit，以及 Windows/Linux CI。

## Quick start

```powershell
uv sync --frozen
uv run python scripts/verify.py
uv run stonks fake-cycle --symbol AAPL --as-of 2026-01-02T21:00:00Z --idempotency-key demo
```

`fake-cycle` 完全離線，不需要 provider key、LLM、PostgreSQL 或 optional sidecar。

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
