# Stonks Agent

Stonks Agent 是 evidence-first、可稽核、可重播的投資研究與 paper trading 平台。P0 Foundation、P1 Canonical Data Hub、P2 Research control plane 與 P3 strategy/forecast/evaluation 已通過 phase gate，後續工作依[實作計畫](./tasks/todo.md)持續開發。

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
- Artifact-backed delivery ports以fenced outbox lease驅動console/file/email/webhook；每個adapter重驗content hash與idempotency identity，file固定root且拒絕覆寫不同artifact，webhook固定HTTPS URL、禁止redirect並做bounded retry。未配置email/webhook只留下`skipped` receipt，不偽裝已送達。
- Queue-only research API/CLI以PostgreSQL transaction原子建立run/job/snapshot link；API request thread不執行長任務。Canonical run events先驗完整hash chain，再以可重接的SSE `Last-Event-ID`投影並redact secrets；report read只接受renderer產生且metadata完整的typed artifact，拒絕任意raw prompt/model artifact。
- Canonical research pipeline gate把同一PIT context的deterministic artifact與TradingAgents opinion納入report attribution，再完成structured report、三channel rendering與file delivery；每次結果封存不含secret/error message的immutable audit artifact。Provider/deterministic/report outage為`failed`，TradingAgents outage為`degraded`且可產有限制說明的report，所有result contract都沒有target/order authority。
- Strategy/signal/evaluation domain固定paper-only promotion graph與exact strategy/data/runtime/policy/evaluation provenance；未註冊、未校準、stale、expired或hash binding不符的alpha一律回零權重。Forecast與strategy-lab ports只接immutable snapshot/artifact inputs，stochastic output必須先封存raw output與sampled paths。
- PostgreSQL strategy registry以CAS version序列化promotion，evaluation report與audit chain append-only；DB trigger本身驗promotion graph、exact evaluation binding、DB clock與每次mutation的matching audit event。`stonks_app`只有scoped columns權限，heavy worker role沒有strategy table權限。
- Strategy/evaluation API與`stonks strategy` CLI提供read-only registry、evaluation、audit查詢及reviewer-only transition；actor由authenticated principal產生，live/order-shaped輸入、stale CAS與forged actor皆fail closed。Signal eligibility只接受exact strategy/evaluation/data/runtime provenance，缺失或binding不符固定回零權重。
- Deterministic last-value、simple moving-average與OLS linear baselines共用PIT ordered-bar contract、frozen draft manifests與exact Decimal golden；輸出是research-only `ForecastSignal`，同輸入可重播相同payload hash，供Kronos/opinion/complex strategy在同dataset/cost evaluation下比較。
- Point-in-time evaluation engine先拒絕future feature/label、unknown publication lag與survivorship污染，再以purged walk-forward/embargo的out-of-sample observations計算CPCV/PBO、fees/slippage/turnover sensitivity、baseline alpha、drawdown與probability calibration。每項promotion check獨立、policy為content hash，同strategy/snapshot/runtime/policy可重播相同evaluation hash。
- Opinion-to-alpha mapper預設停用；啟用時仍要求mapper manifest parameters exact綁定policy hash、strategy為`paper_eligible`、evaluation passed且未過期、opinion confidence已校準。Bullish/neutral/bearish只映射成固定signed research value，unknown rating或任何quantity/order-shaped欄位fail closed。
- Pinned Kronos-small/Tokenizer-base isolated worker以本機唯讀model root做file size/SHA-256/symlink/untracked-entry驗證並在startup warm一次，禁止request-time download。CPU與CUDA使用獨立PyTorch 2.12.1 locks/images；non-root/read-only/internal-network runtime無DB/provider/broker/execution credentials。Canonical request由core以PIT `BarSeries`與exchange calendar產生，worker逐explicit seed以`sample_count=1`保留raw paths；core依序封存raw response與lease-secret-free path artifact，再驗fence/runtime/OHLCV/length/extreme jump並映射research-only `ForecastSignal`。Artifact replay、CPU與RTX 3070 Ti CUDA exact runtime route及16-path aggregate tolerance皆以實際115 MB權重通過。
- Kronos evaluation只從archived forecasts建立content-hashed PIT dataset，固定要求US/HK/TW、三個deterministic baselines、同一runtime/model/tokenizer identity與production evaluation policy。Golden的768筆跨市場資料完成4個purged walk-forward splits與252筆OOS；baseline、cost與calibration未達原門檻時保留`passed=false`，不調低threshold。Exact evaluated forecast可映射成versioned shadow `AlphaSignal`，但目前策略設定固定`shadow`且eligibility weight 0。
- Pinned Qlib quant-lab worker只接受canonical snapshot converter產生的immutable feature/label/universe/cost/split artifact，固定執行`DataHandlerLP -> DatasetH -> LinearModel(OLS)`，回傳research-only predictions/positions/metrics與content hashes。Qlib source archive、Python/NumPy/Pandas/scikit-learn與worker source/lock皆綁定runtime identity；同job真實HTTP route重播得到相同四組artifact hashes。獨立image為UID 65532、read-only、cap-drop/internal network，無DB/queue/provider/execution credentials；worker lock audit為0 vulnerabilities，Qlib與NumPy/Pandas未進core lock。
- Local RBAC、process capability/egress deny、secret redaction、統一 API envelope 與 telemetry ports。
- License/upstream policy、secret scan、locked dependency CVE audit，以及 Windows/Linux CI。

## Quick start

```powershell
uv sync --frozen
uv run python scripts/verify.py
uv run stonks fake-cycle --symbol AAPL --as-of 2026-01-02T21:00:00Z --idempotency-key demo
```

`fake-cycle` 完全離線，不需要 provider key、LLM、PostgreSQL 或 optional sidecar。

P1 的canonical ingestion已以replay source完整驗證。Financial Datasets與OpenBB目前是contract-tested observation adapters，尚未宣稱已接成production canonical materialization source；OpenAI-compatible與Anthropic adapters目前以官方wire contract及mock transport驗證，尚未使用真實credentials做live smoke；TradingAgents worker與core HTTP/job completion contract已驗證，但尚未提供production artifact capability signer。Qlib quant-lab已驗證isolated research route，strategy/evaluation API與CLI已通過P3 gate。Research API目前只建立`research_pipeline` job，application-level pipeline已通過P2 gate，但常駐dispatcher與durable全流程transition/commit wiring仍屬P4.7；`stonks-worker claim-once`也不是常駐dispatcher。

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
