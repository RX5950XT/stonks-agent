# TradingAgents isolated worker

這是固定於TradingAgents `01477f9a` / v0.3.1的獨立研究runtime。它不屬於core
lock，也沒有DB、broker、queue或直接provider credentials。每個paper、backtest、
production profile各自使用一個process，避免上游process-global config互相污染。

Worker只接受PIT合法且與`allowed_evidence_ids`完全相符的canonical evidence；
response只包含`AnalysisBundle`與`AgentOpinion`。上游Trader/Portfolio/risk文字都是
research opinion，不會形成`TradeIntent`、target或order。

```powershell
uv lock --check
uv sync --frozen
uv run pytest ../../tests/contracts/workers/test_tradingagents.py -q
docker compose -f ../../infra/compose.tradingagents.yaml config
```

容器必須使用read-only rootfs、non-root UID 65532、drop all capabilities與internal
network。模型與artifact access後續只可經核准proxy/facade；目前預設egress deny，
沒有真實provider credential live smoke。
