# 上游專案研究索引

本目錄保存 2026-07-10 的可重現研究成果；實作前以這些結論決定授權與 process boundary。

- [`virattt-projects.md`](./virattt-projects.md)：`ai-hedge-fund` 與 `Dexter` 的架構、測試、可重用模組與 control-plane 邊界。
- [`trading-agent-platforms.md`](./trading-agent-platforms.md)：`TradingAgents` 與 `AI-Trader` 的 decision/control plane、API、風險與實測。
- [`data-model-reporting.md`](./data-model-reporting.md)：`Kronos`、`daily_stock_analysis`、`OpenBB` 的 forecast/data/reporting 整合。
- [`alternatives-and-licensing.md`](./alternatives-and-licensing.md)：Qlib、RD-Agent、NautilusTrader、LEAN 等替代案與授權分區。
- [`upstream-snapshot.md`](./upstream-snapshot.md)：本次 shallow clones 的固定 commit。
- [`verification.md`](./verification.md)：研究產物與本機 snapshots 的交叉驗證結果。
- [`free-market-data-sources.md`](./free-market-data-sources.md)：免費來源、時效、display
  rights 與 active／blocked composition 邊界。

`.research/upstreams/` 只供研究，不是本專案 source dependency，後續必須排除版控。任何上游整合都應使用 pinned dependency、adapter 或隔離 worker，不從研究目錄直接 import。
