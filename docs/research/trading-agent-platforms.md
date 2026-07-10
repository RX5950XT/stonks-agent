# TradingAgents × AI-Trader 整合研究

> 研究日期：2026-07-10（Asia/Taipei）  
> 範圍：只研究與規劃，未實作整合程式碼。  
> 結論以指定 snapshot 的官方 repo、官方文件與本機原始碼為準。

## 1. 結論先行

這兩個專案不是互斥方案，而是上下兩層：

- **TradingAgents** 適合當「研究／決策引擎」：它以 LangGraph 編排市場、情緒、新聞、基本面、bull/bear debate、Trader、三方風險辯論與 Portfolio Manager，最後輸出投資評等和報告。
- **AI-Trader** 適合當「agent-native 平台／控制面」：它提供 agent 註冊、signals、heartbeat/WebSocket、paper portfolio、copy trading、challenge、team mission、experiment、leaderboard 與研究資料匯出；它不是另一套內建 LLM 分析 graph。
- 最合理的組合是 **TradingAgents 只產生 `AnalysisBundle/AgentOpinion`，由自有 signal fusion 與 portfolio optimizer 生成 `PortfolioTarget`，再經 deterministic risk layer 形成 `OrderIntent`**；本地 paper executor 是唯一交易落點。AI-Trader HTTP adapter只發布去敏 thesis、讀取回饋與外部結果，不提交 canonical paper orders。不要讓 LLM 輸出直接打交易端點。
- **TradingAgents 可在 Apache-2.0 條件下直接 import／fork。AI-Trader 目前不可直接複用程式碼**：README 雖有 MIT badge，但 snapshot 沒有 `LICENSE`，GitHub API 也回報 `license: null`，且 `service/README.md` 明稱 server 為 proprietary。授權未釐清前只能使用公開 API，或 clean-room 重寫其模式。

建議產品邊界：

```text
Trigger / Scheduler / AI-Trader heartbeat
                  │
                  ▼
          Unified Orchestrator
                  │
       ┌──────────┴──────────┐
       ▼                     ▼
Market-data ports     TradingAgents adapter
                             │
                             ▼
                  AnalysisBundle / AgentOpinion
                             │
                             ▼
          Signal Fusion / Deterministic PortfolioTarget
                             │
                             ▼
             Deterministic Risk & Policy Gate
                             │
             ┌───────────────┴──────────────┐
             ▼                              ▼
        Local Paper Executor         AI-Trader adapter
             │                       thesis/feedback only
             └───────────────┬──────────────┘
                             ▼
              Event store / Memory / Evaluation
```

## 2. 研究 snapshot 與活躍度

| 專案 | Snapshot | 最近提交 | Release / 活躍度 | 授權 |
|---|---|---|---|---|
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | `01477f9afb7a47b849ed4c9259d3a9a4738d9fda` | 2026-07-05，`chore: release v0.3.1` | v0.3.1 於 2026-07-05 發布；251 commits；查詢時約 92,094 stars / 17,796 forks | **Apache-2.0**，repo 有完整 `LICENSE`，GitHub 可辨識 |
| [HKUDS/AI-Trader](https://github.com/HKUDS/AI-Trader) | `d03ff6c056b32ced735adf7c19ed8175adb1c8df` | 2026-06-11，merge PR #255 | 無 GitHub release；388 commits；查詢時約 20,660 stars / 3,167 forks | **不完整／不可視為 MIT**：badge 聲稱 MIT，但 `LICENSE` 不存在，GitHub `license: null`，server 文件又稱 proprietary |

判讀：兩者近期都仍活躍；TradingAgents 有明確版本與 CI，作為 library 的成熟度顯著較高。AI-Trader 更新密集但沒有 release、CI 或可重現的 dependency lock，較像持續運作中的服務原始碼 snapshot。

## 3. 能力對照

| 面向 | TradingAgents | AI-Trader | 整合定位 |
|---|---|---|---|
| 核心定位 | 多角色 LLM 財務研究與決策 graph | 外部 AI agents 的社群、paper trading、copy trading 與實驗平台 | 前者是 decision plane，後者是 control/community plane |
| Agent topology | 程式內固定角色與 LangGraph routes | agent 在平台外運行；平台保存 agents、signals、tasks、teams 與互動網路 | 不共用 graph；以 canonical events 串接 |
| 交易能力 | 只產生文字決策／報告，沒有 order、broker 或 portfolio ledger | 有模擬 cash/positions/fees、自動 copy、Polymarket settlement；沒有實際 broker connector | 必須新增自有 local paper execution/ledger；AI-Trader只作external community與outcome observation adapter |
| Debate | bull ↔ bear，再由 Research Manager 裁決；aggressive → conservative → neutral risk debate | discussion/reply、team submission/vote、challenge cooperation；不是自動 LLM debate | 可把 AI-Trader 社群回饋當外部 evidence，不直接混入 graph state |
| Memory | append-only Markdown decision log、事後報酬與 LLM reflection；SQLite graph checkpoint | PostgreSQL/SQLite 保存 signals、events、messages、tasks、positions、metrics；無 cognitive reflection memory | 統一重寫為 durable event + outcome memory；保留 TA memory 相容 adapter |
| Risk | 三位 LLM risk agents + Portfolio Manager；屬語意評估 | paper ledger 的 cash、quantity、fees、market-hours 與 challenge drawdown/position rules | 真正下單前另建 deterministic pre-trade risk gate |
| 市場資料 | Yahoo Finance / Alpha Vantage、FRED、Polymarket、StockTwits、Reddit | Alpha Vantage + yfinance（美股）、Hyperliquid（crypto）、Polymarket Gamma/CLOB、選配 Adanos | 透過 DataProvider port 統一 symbol、時間、provenance |
| 實際市場支援 | Yahoo Finance 可覆蓋的全球股票/ETF、crypto、forex/futures aliases；graph 的 asset mode 為 `stock`/`crypto` | 程式實際只接受 `us-stock`、`crypto`、`polymarket` | 不採信 AI-Trader README 的 forex/options/futures 廣告敘述 |
| LLM | 多 provider、deep/quick model 分層 | 平台 agents 自帶模型；server 只選配 OpenRouter 生成簡短 market-intel summary | LLM gateway 應放在 orchestrator/analysis worker，不放交易服務 |
| Public interface | Python package、CLI、Docker | REST、WebSocket、Markdown skills、研究匯出 | 各自包 adapter，不讓 upstream 型別滲入 domain |

## 4. TradingAgents 深入盤點

### 4.1 Tech stack 與啟動方式

- Python `>=3.10`，核心為 LangGraph / LangChain。
- Pydantic structured output、pandas、stockstats、yfinance、requests。
- CLI：Typer、Rich、Questionary；entry point 是 `tradingagents = cli.main:app`。
- Persistence：LangGraph SQLite checkpointer，以及 Markdown decision log。
- Docker：Python 3.12 multi-stage image；Compose 可選 Ollama profile。
- 開發品質：pytest、ruff；官方 CI 跑 Python 3.10/3.11/3.12/3.13、clean-install smoke 與全 repo lint。

官方啟動介面：

```bash
pip install .
tradingagents
# 或
python -m cli.main
```

```python
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

config = DEFAULT_CONFIG.copy()
engine = TradingAgentsGraph(config=config)
final_state, decision = engine.propagate("NVDA", "2026-01-15")
```

### 4.2 Agent topology / workflow

實際 graph 是循序流程，而不是四位 analyst 平行執行：

1. 依 `selected_analysts` 建 execution plan；預設次序為 Market → Sentiment → News → Fundamentals。
2. 每位 analyst 可在自身 node ↔ ToolNode 間反覆 tool-call；完成後清除 messages，再進下一位。
3. Bull Researcher 與 Bear Researcher交替辯論，回合數由 `max_debate_rounds` 控制。
4. Research Manager 用 deep model 產生 `ResearchPlan`。
5. Trader 用 quick model產生 `TraderProposal`。
6. Aggressive → Conservative → Neutral risk analysts 輪流辯論，回合數由 `max_risk_discuss_rounds` 控制。
7. Portfolio Manager 用 deep model產生 `PortfolioDecision`，graph 結束。

`AgentState` 的核心資料：instrument/date、四份 analyst reports、investment debate、investment plan、trader plan、risk debate、final decision、past context。

### 4.3 Public interfaces 與可重用模組

穩定度由高至低：

1. **主要 package API**
   - `TradingAgentsGraph(selected_analysts, debug, config, callbacks)`
   - `propagate(company_name, trade_date, asset_type="stock") -> (final_state, decision)`
   - `save_reports(final_state, ticker, save_path=None)`
   - `resolve_instrument_context(...)`
   - `process_signal(...)`
2. **Canonical config**
   - `DEFAULT_CONFIG`：LLM、debate depth、checkpoint、語言、vendor chain、benchmark、cache/results/memory paths。
   - `TRADINGAGENTS_*` env overrides；型別錯誤會 fail fast。
3. **Structured contracts**
   - `ResearchPlan(recommendation, rationale, strategic_actions)`
   - `TraderProposal(action, reasoning, entry_price, stop_loss, position_sizing)`
   - `PortfolioDecision(rating, executive_summary, investment_thesis, price_target, time_horizon)`
   - `SentimentReport(overall_band, overall_score, confidence, narrative)`
4. **Data/tool layer**
   - `get_stock_data`、`get_indicators`、`get_verified_market_snapshot`
   - `get_fundamentals`、財務三表、news、insider、FRED macro、Polymarket probabilities。
   - `route_to_vendor()` 支援顯式 ordered fallback，不會偷偷切換未配置 vendor。
5. **Persistence helpers**
   - `TradingMemoryLog` 與 checkpoint helpers 可參考，但不建議成為跨服務的 canonical store。

注意：`TradingAgentsGraph.__init__()` 會呼叫 process-global `set_config()`；多租戶或同 process 不同 config 可能互相污染。第一階段應以獨立 worker process 隔離每一種 runtime profile。

### 4.4 Data sources、markets、LLM providers

資料來源：

- yfinance：預設 OHLCV、technical indicators、fundamentals、news、insider；支援 exchange suffix。
- Alpha Vantage：上述類別的替代 vendor。
- FRED：rates、inflation、labor、growth 等 macro series。
- Polymarket：前瞻事件概率。
- StockTwits、Reddit：sentiment analyst 直接抓取；ticker news 是第三個 sentiment input。
- verified market snapshot：在 market analyst 最終報告前以 deterministic snapshot 錨定精確價格／指標。

markets：README 表示可使用 Yahoo Finance 覆蓋的市場，包括美股、港股、日股、英股、印度、加拿大、澳洲、中國 A 股與 crypto。程式另把常見 broker aliases 正規化成 Yahoo symbols，包括 forex、金屬、能源、index CFDs；但 graph 只有 `stock` 與 `crypto` 兩種 semantic mode，且沒有交易所執行。

LLM providers：

- Native：OpenAI、Anthropic、Google、Azure OpenAI、AWS Bedrock。
- OpenAI-compatible registry：xAI、DeepSeek、Qwen（global/CN）、GLM（global/CN）、MiniMax（global/CN）、OpenRouter、Mistral、Kimi/Moonshot、Groq、NVIDIA NIM、Ollama，以及任意自訂 OpenAI-compatible endpoint。
- 分成 `deep_think_llm` 與 `quick_think_llm`，另有 temperature、retry、provider-specific reasoning effort。

### 4.5 Memory / debate / risk / trading loop

Memory：

- 每次成功分析把 final decision 以 pending entry append 到 `~/.tradingagents/memory/trading_memory.md`。
- 下一次分析同 ticker 時，抓取舊決策後約 5 個交易日的 raw return 與相對 benchmark alpha，讓 quick LLM 產生 2–4 句 reflection。
- 新 run 注入最近 5 筆同 ticker 完整經驗與 3 筆 cross-ticker lessons。
- checkpoint 是 opt-in；以 ticker + date + graph shape 建 thread ID，成功後清除。

Debate：bull/bear 與三種 risk stance 都是交替對話，router 依 speaker label 與 counter 決定下一 node。它有研究價值，但 token cost/latency 會隨回合線性增加。

Risk：Portfolio Manager 有五級 `Buy / Overweight / Hold / Underweight / Sell`，Trader 有三級 `Buy / Hold / Sell`。這是 LLM judgement，不知道真實 cash、position、exposure、margin、compliance 或 broker limits。

Trading loop：`propagate()` 只回傳 state 與 rating string，寫 JSON/Markdown logs；原始碼沒有 broker order、exchange fill 或 portfolio accounting。README 的「simulated exchange execution」不應當成已實作能力。

### 4.6 已知風險

- 研究工具聲明明確；LLM sampling、live news/social data 與 provider model 更新都使結果不可完全重現。
- historical analysis 仍會混入「現在」的 social/news，不能直接當嚴格 point-in-time backtest。
- analyst 全部循序，單次決策有高延遲與高 token cost。
- LLM risk debate 不是 deterministic risk control。
- process-global data config、多個 graph 同 process 的隔離不足。
- Markdown memory 沒有跨 process transaction/locking，不適合併發服務。
- Data tools 多數回傳 human-readable strings；跨引擎整合前要保留原始數值、時間與 provenance。
- dependency 採寬鬆下限而非 lock；今天安裝測試通過，不代表未來 resolver 結果可重現。
- `backtrader`、`redis` 是 runtime dependencies，但目前主程式未實際 import，顯示 package 還有可瘦身空間。

## 5. AI-Trader 深入盤點

### 5.1 正確定位與 tech stack

目前 repo 是 **agent-native trading platform/API/skills**，不是舊式「專案內啟動一群 LLM agents」的研究 framework。

- Backend：Python、FastAPI、Pydantic、Uvicorn。
- Database：PostgreSQL（production）或 SQLite（local fixture/quick start）；以自製 adapter 把 SQLite SQL 改寫為 psycopg SQL。
- Cache/coordination：選配 Redis；API process 預設只跑 HTTP，另有 `worker.py` 跑 background loops。
- Market/data：requests/aiohttp、Alpha Vantage、yfinance、Hyperliquid、Polymarket、選配 Adanos。
- LLM：只有 market-intel 的短摘要可選配 OpenRouter；平台本身不決定每個 agent 用哪個模型。
- Frontend：React 18、TypeScript、Vite、React Router、Recharts、ethers。
- Research：pandas、NumPy、SciPy、statsmodels、NetworkX、Matplotlib；可做 A/B、DiD、regression、HTE、bootstrap CI、FDR 與互動網路分析。

### 5.2 External-agent topology / workflow

AI-Trader 的 agent 在服務之外運行，典型循環是：

1. 外部 agent 讀取 `skills/ai4trade/SKILL.md`，以 `/api/claw/agents/selfRegister` 註冊並取得 bearer token。
2. 每 30–60 秒 POST heartbeat，取得 unread messages 與 pending tasks；或用帶 token 的 WebSocket。
3. agent 讀 signal feed、market intel、price、positions、challenge/team mission 狀態。
4. agent 發布三類內容：`strategy`、`discussion`、`operation/realtime`。
5. realtime operation 更新自己的 paper cash/position；所有 active followers 以同一 absolute quantity 嘗試自動 copy。
6. service 保存 event、quality score、reward、profit/position history；worker 更新價格、settlement、leaderboard、team/challenge 與研究 metrics。

平台內的「collective intelligence」實作是 reply/mention/follow、team submissions/votes、challenge/team mission，而不是平台自動建立 bull/bear/risk LLM roles。

### 5.3 Public interfaces

最有價值的 public surface：

- **Agent/Auth**：self-register、login、me、points、wallet-signed token/password recovery。
- **Notifications**：`POST /api/claw/agents/heartbeat`、`wss://.../ws/notify/{agent_id}?token=...`、messages/tasks。
- **Signals**：feed/grouped/provider-specific、strategy、discussion、reply/accept、realtime operation。
- **Paper portfolio / copy**：positions、price、follow/unfollow、subscribers。
- **Challenges / teams**：individual/team portfolios、trades、submissions、votes、leaderboards、settlement。
- **Team missions**：team formation、roles、messages、submissions、contribution scoring。
- **Experiments / rewards**：variant assignment、notifications/tasks、behavior events、metrics。
- **Market intel**：overview、news、macro signals、ETF flows、featured stock analysis。
- **Research export**：schema 與 anonymized CSV/JSON datasets。
- **Skills**：`ai4trade`、`heartbeat`、`copytrade`、`tradesync`、`market-intel`、`polymarket` Markdown contracts。

`docs/api/openapi.yaml` 只描述早期的 registration、marketplace、signals、follow 與 positions 子集；原始碼實際 routes 遠多於 spec。整合時不能只 codegen 現有 OpenAPI，應由 runtime FastAPI schema 與 contract tests 反推完整 client。

### 5.4 Data sources、markets、LLM providers

實際 price path：

- `us-stock`：優先 Alpha Vantage；未配置、rate-limit 或無資料時回退 yfinance。
- `crypto`：Hyperliquid public info API，current L2 midpoint 或 1-minute candle。
- `polymarket`：Gamma resolve + CLOB orderbook，並有自動 settlement loop。
- market intel：Alpha Vantage news、選配 Adanos sentiment、技術規則與選配 OpenRouter summary。

**程式的 `SUPPORTED_MARKETS` 只有 `us-stock`、`crypto`、`polymarket`。** Binance、Coinbase、Kraken、OKX、Hyperliquid 等字串只是 normalize 成 `crypto` 的 aliases；repo 沒有這些 broker 的 authenticated order connectors。README 宣稱 Stocks/Crypto/Forex/Options/Futures 與「compatible with Binance/Coinbase/IBKR」不能解讀為已實作 execution coverage。

LLM：AI-Trader 不提供通用 LLM provider layer。外部 agents 自行決定模型；server 只有 `OPENROUTER_API_KEY` + `OPENROUTER_MODEL` 的 optional dashboard-summary call，失敗會回 deterministic fallback summary。

### 5.5 Memory / debate / risk / trading loop

Memory：PostgreSQL/SQLite 是 operational/event memory，涵蓋 signals、replies、messages、tasks、positions、profit history、experiments、rewards、network edges、team/challenge records；沒有 episodic retrieval、embedding 或 outcome reflection prompt。

Debate：discussion/replies、accepted replies、team thesis/proposal/review、approve/reject/revise votes可當多 agent 協作基礎；但平台沒有自動 debate turn-taking 或 consensus engine。

Risk / execution：

- paper trade 驗證 market、action、finite/positive quantity/price、上限、market hours、cash 與可賣部位。
- 收取 fee、更新 cash/positions、對 Polymarket 禁止 short/cover。
- challenge scoring可計 return、max drawdown、risk-adjusted/final score，並依 position/drawdown rules disqualify。
- follow 後會直接以 leader 的 **相同 absolute quantity** copy；只在 follower cash/position 不足時跳過。
- 沒有 real-money broker order、idempotency key、per-follower sizing policy 或完整 pre-trade exposure limit。

### 5.6 啟動、部署與測試現況

README 只說明 DB selection；實際服務需分開運行：

```bash
pip install -r service/requirements.txt
python service/server/main.py
python service/server/worker.py

cd service/frontend
npm ci
npm run build
```

production 建議 PostgreSQL + Redis，API/worker 分離。SQLite 只適合 local quick start。repo 沒有 Dockerfile、Compose、GitHub Actions，也沒有完整公開 production runbook；`docs/local-ops/production-branch.md` 反而要求服務跑在不推送的 private local branch。

現況有可重現性問題：

- `service/requirements.txt` 宣告 `openrouter>=1.0.0`，研究當日 package index 最高只有 `0.11.18`，原生安裝 resolver 直接失敗。
- tests 使用 FastAPI TestClient，但 requirements 漏 `httpx2`；`EmailStr` 也需要但漏 `email-validator`。
- 補裝 `openrouter==0.11.18`、`httpx2`、`email-validator` 後，backend 123 tests 全過。
- frontend `tsc && vite build` 成功產出；Windows 最後因 POSIX-only `chmod -R a+rX dist` 而讓 npm script 回傳失敗。
- npm audit 回報 8 個 vulnerabilities（1 low、5 moderate、2 high），且 main chunk 約 713 kB minified。

### 5.7 已知風險

1. **授權阻斷**：缺 `LICENSE` 且 proprietary/MIT 訊息衝突；不可 vendor、copy 或 import server/frontend code。
2. **dependency 不可重現**：原始 requirements 無解、缺 test/runtime extras、沒有 lock。
3. **文件 drift**：OpenAPI 遠少於 runtime routes；README 市場／broker claim 高於程式能力。
4. **認證安全**：agent/user bearer tokens 以明文存在 DB；password 使用 salted single SHA-256，而非 Argon2id/scrypt/bcrypt；正式整合應重寫。
5. **授權邊界**：`/api/claw/messages` 與 `/api/claw/tasks` 只驗證 caller 有 token，卻接受任意 target `agent_id`；需在採用前做完整 authz audit。
6. **交易重試風險**：realtime write 沒有 client idempotency key，timeout/retry 可重複建倉與 copy。
7. **copy sizing 風險**：相同 absolute quantity 不考慮 follower NAV、risk budget 或允許滑價；個別失敗多處被靜默略過。
8. **多 worker notification**：WebSocket connections 存在單 process memory；多 Uvicorn workers 沒有完整跨 process delivery coordinator。
9. **資料庫演進**：大量 schema create/ALTER 在 `init_database()` 執行，沒有版本化 migration/rollback；Postgres 依 SQL rewrite adapter。
10. **寬泛 exception swallowing**：部分 WebSocket、copy、cache、worker 路徑 fail-open，observability 與告警不足。
11. **paper != live**：目前 ledger 與 public market-data pricing 不能直接升級成 real-money execution。

## 6. 一致 orchestration layer 設計

### 6.1 Domain contracts

禁止讓 `AgentState`、AI-Trader DB rows 或 upstream markdown 成為跨模組 contract。自有 domain 至少定義：

```text
AnalysisRequest
  run_id, strategy_id, instrument, as_of, horizon, account_ref,
  analyst_set, data_policy, model_policy

AnalysisBundle
  reports[], debate_transcripts[], research_plan, trader_proposal,
  portfolio_opinion, source_refs[], model_usage, warnings[]

AgentOpinion / AlphaSignal
  opinion_or_signal_id, instrument, direction/value, confidence,
  horizon, thesis, evidence_refs[], producer/model/prompt versions

PortfolioTarget
  target_id, account_ref, target_weights, constraints,
  optimizer_version, input_signal_ids[]

RiskDecision
  approved, rejected_reasons[], normalized_size, limits_snapshot,
  required_approvals[], expires_at

OrderIntent / ExecutionReceipt
  idempotency_key, venue, account_ref, order_type, qty/notional,
  status, fills[], fees, external_refs[]

PlatformEvent
  event_id, run_id, type, occurred_at, producer, schema_version, payload
```

關鍵原則：

- `Overweight/Underweight` 是 target-position 語意，不能硬映射成 buy/sell；必須結合現有部位與 policy 算 delta。
- 每次外部 side effect 都要 `idempotency_key`、outbox 與 audit event。
- instrument 使用 canonical ID + venue aliases，禁止各 upstream 自行猜 ticker。
- data point 都要 `as_of`、`observed_at`、vendor、adjustment 與 point-in-time flag。

### 6.2 Workflow state machine

```text
scheduled
  -> gathering_data
  -> analyzing
  -> proposed
  -> risk_rejected | awaiting_approval | approved
  -> executing
  -> executed | execution_failed
  -> published
  -> monitoring
  -> resolved / reflected
```

- workflow state 存在 PostgreSQL，不依賴 LangGraph checkpoint 當全平台 transaction log。
- TradingAgents checkpoint 只處理單次 analysis worker resume。
- AI-Trader heartbeat/task 轉成 `PlatformEvent`，由 orchestrator consumer 驅動；不在 HTTP request thread 長時間跑 LLM graph。
- analysis、risk、execution、publication 分別有 retry policy；execution retry 必須以 idempotency 查詢先前結果。

### 6.3 Adapter 邊界

**TradingAgents adapter**

```text
analyze(AnalysisRequest) -> AnalysisBundle
```

- 在隔離 worker 建 `TradingAgentsGraph`，固定 snapshot/version/config。
- 把 `final_state` 與 typed schemas 正規化；markdown 只留 presentation/audit，不拿 regex 當唯一 contract。
- callbacks 蒐集 model/tool latency、tokens、errors。
- Production、paper 與 backtest worker一律使用共用 canonical evidence tool facade，只能讀 `allowed_evidence_ids`，並禁止任意 network egress。上游 data tools 僅可在明確標記為非 point-in-time 的互動研究 sandbox 評估，不能進可比較績效或策略 promotion 路徑。

**AI-Trader adapter**

```text
register/login
poll_events / subscribe_events
publish_strategy / publish_discussion
get_remote_positions / get_remote_outcomes / get_price_as_external_evidence
join_challenge / submit_team_proposal / vote
export_research_events
```

- 只寫自有 typed HTTP client，不 import AI-Trader code。
- 先以 runtime schema + contract tests 鎖定實際 response；對不完整 OpenAPI 採 tolerant reader。
- heartbeat cursor、message IDs 與本地 inbox 去重，避免「server 已標 read、client 尚未 commit」造成遺失。
- Adapter不提供 canonical order submission；remote paper/copy結果只可作外部觀測 evidence，不能取代本地 ledger。

### 6.4 可選 community feedback loop

1. TradingAgents 產生 initial `AnalysisBundle`。
2. Orchestrator 將 thesis 發成 AI-Trader `strategy` 或 team `trade_proposal`。
3. 在有 deadline 的 observation window 收 replies/votes，而不是無限等待。
4. 將外部意見轉成有作者、時間、reputation、引用來源的 `ExternalEvidence[]`。
5. deterministic policy 決定是否重跑 analysis 或只調整 confidence；不可把未信任的 reply 原文直接升成 tool/system prompt。
6. risk gate 通過後才建立 execution command；結果再發布 `realtime` signal。

## 7. 複用決策表

| 元件 | 決策 | 理由 / 作法 |
|---|---|---|
| `TradingAgentsGraph` | **直接 import（固定版本）** | Apache-2.0、主要 public API、測試完整；外包一層 adapter 並隔離 process-global config |
| `DEFAULT_CONFIG`、LLM client factory | **直接 import + adapter** | provider coverage 廣；secrets/model routing仍由自有 config service 管理 |
| `ResearchPlan` / `TraderProposal` / `PortfolioDecision` / `SentimentReport` | **直接 import 或鏡射 canonical DTO** | Pydantic contract 有價值；跨服務 wire contract 應由自有 schema 控制版本 |
| LangGraph bull/bear/risk topology | **先直接使用，後續按成本調整** | 已可運行；先收 telemetry 再決定平行 analysts 或減少 rounds |
| TradingAgents data tools | **套 DataProvider adapter** | 可快速用，但目前多回傳 strings、有 global config；需補 raw data/provenance |
| TradingAgents Markdown memory | **相容 adapter，核心重寫** | 單機研究好用；平台應用 event store + outcome store，必要時生成相同 prompt context |
| TradingAgents LLM risk verdict | **只參考，不能當交易控制** | 缺帳戶／法遵／曝險狀態且非 deterministic |
| TradingAgents broker/exchange | **重寫** | 實際不存在 |
| AI-Trader REST/heartbeat/WebSocket | **HTTP adapter** | 能直接利用 live platform；授權不清時不複用其程式碼 |
| AI-Trader Skills 文件 | **介面參考／連結，不複製** | 適合設計 agent onboarding 與 task routing；受授權問題約束 |
| AI-Trader paper trading/copy service | **只讀外部結果，不採 execution path** | 可把remote positions/outcomes轉成external evidence；不提交canonical orders、不跟單，也不以其ledger取代本地balanced ledger |
| AI-Trader challenges/team missions/experiments | **API adapter + 模式參考** | 很適合 evaluation/collaboration plane；不要耦合其 DB schema |
| AI-Trader database adapter/schema init | **重寫** | 單檔 init/ALTER 與 SQL rewrite 不適合正式 migration |
| AI-Trader auth/token/password | **重寫** | 改用成熟 IdP/OIDC、短效 access token、rotating refresh token、Argon2id、hashed tokens |
| AI-Trader frontend | **只參考** | license 阻斷、bundle 大、dependency vulnerabilities；以自有 UI 消費 canonical APIs |
| AI-Trader research export/metrics | **模式參考，clean-room 實作** | schema/統計方法值得保留，但 production data contract 應自有化 |

## 8. 建議整合順序（只規劃）

### Phase 0：法務與 contracts

- 固定 TradingAgents `v0.3.1` / commit，保存 Apache attribution。
- 向 HKUDS 取得明確 license；未取得前設 `NO_VENDOR_AI_TRADER_CODE` gate。
- 定義 canonical schemas、instrument mapping、event envelope、idempotency 與 threat model。

### Phase 1：安全的 paper-analysis vertical slice

- 建 TradingAgents isolated worker adapter。
- 讓 worker 只讀 immutable canonical evidence，production/backtest profile禁止任意 data egress。
- 建 deterministic `AgentOpinion/AlphaSignal -> PortfolioTarget -> RiskDecision -> OrderIntent`，預設只允許本地 paper executor。
- 同時建立 durable job/outbox、account reservation/serialized aggregate、portfolio-level risk、balanced ledger、kill switch、reconciliation與audit；這些不是後補的production polish。
- 端到端跑單一 instrument：analysis → risk → local paper trade → position → outcome event。

### Phase 2：memory、evaluation、community

- 建 outcome/reflection store，將 realized return、alpha、drawdown、fills回寫 run。
- 建 AI-Trader typed HTTP client，只做 thesis publication、feedback/challenge/team/experiment/research export與外部 outcome observation；不提交 canonical paper order。
- 加 community feedback window、prompt-injection filtering、reply reputation weighting。

### Phase 3：production hardening與規模化

- 擴充既有durable queue/outbox、account reservation、rate limit、circuit breaker、provider budgets與故障演練。
- 強化既有portfolio risk、kill switch、reconciliation、audit trails與RBAC；不能把這些延後到本phase才第一次實作。
- 本計畫仍不新增live broker adapters；real-money trading必須另立RFC與授權／安全審查。

## 9. 驗證紀錄

本機 shallow clones：

- `.research/upstreams/TradingAgents` → `01477f9afb7a47b849ed4c9259d3a9a4738d9fda`
- `.research/upstreams/AI-Trader` → `d03ff6c056b32ced735adf7c19ed8175adb1c8df`

實測：

| 專案 | 驗證 | 結果 |
|---|---|---|
| TradingAgents | `uv pip install -e ".[dev]"` | 成功 |
| TradingAgents | `pytest -q` | **559 passed、2 skipped、69 subtests passed**；skips 為未裝 Bedrock extra 與未提供 DeepSeek live key |
| TradingAgents | `ruff check .` | **All checks passed** |
| AI-Trader | 原始 `uv pip install -r service/requirements.txt` | **失敗**：`openrouter>=1.0.0` 無可解析版本 |
| AI-Trader | 補 `openrouter==0.11.18`、`httpx2`、`email-validator` 後 `pytest -q service/server/tests` | **123 passed** |
| AI-Trader frontend | `npm ci`、`tsc && vite build` | compile/bundle 成功；postbuild 的 Windows `chmod` 失敗；audit 為 8 vulnerabilities |
| Upstream worktrees | `git status --short --ignored` | 僅 `.venv`、cache、node_modules、dist 等 ignored research artifacts；未改 upstream tracked files |

## 10. 關鍵證據

TradingAgents（固定 snapshot）：

- [README / public API / markets / persistence](https://github.com/TauricResearch/TradingAgents/blob/01477f9afb7a47b849ed4c9259d3a9a4738d9fda/README.md)
- [Apache-2.0 LICENSE](https://github.com/TauricResearch/TradingAgents/blob/01477f9afb7a47b849ed4c9259d3a9a4738d9fda/LICENSE)
- [Package dependencies and CLI](https://github.com/TauricResearch/TradingAgents/blob/01477f9afb7a47b849ed4c9259d3a9a4738d9fda/pyproject.toml)
- [LangGraph topology](https://github.com/TauricResearch/TradingAgents/blob/01477f9afb7a47b849ed4c9259d3a9a4738d9fda/tradingagents/graph/setup.py)
- [`TradingAgentsGraph` public orchestration API](https://github.com/TauricResearch/TradingAgents/blob/01477f9afb7a47b849ed4c9259d3a9a4738d9fda/tradingagents/graph/trading_graph.py)
- [Structured schemas](https://github.com/TauricResearch/TradingAgents/blob/01477f9afb7a47b849ed4c9259d3a9a4738d9fda/tradingagents/agents/schemas.py)
- [Data vendor routing](https://github.com/TauricResearch/TradingAgents/blob/01477f9afb7a47b849ed4c9259d3a9a4738d9fda/tradingagents/dataflows/interface.py)
- [Decision/reflection memory](https://github.com/TauricResearch/TradingAgents/blob/01477f9afb7a47b849ed4c9259d3a9a4738d9fda/tradingagents/agents/utils/memory.py)
- [Official CI](https://github.com/TauricResearch/TradingAgents/blob/01477f9afb7a47b849ed4c9259d3a9a4738d9fda/.github/workflows/ci.yml)

AI-Trader（固定 snapshot）：

- [README / current agent-native platform positioning](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/README.md)
- [不存在的 LICENSE 連結](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/LICENSE)
- [Server 文件的 proprietary 宣告](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/README.md)
- [不完整的 server requirements](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/requirements.txt)
- [實際 market allowlist](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/server/routes_shared.py)
- [Agent auth / heartbeat / WebSocket](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/server/routes_agent.py)
- [Signal、paper execution 與 auto-copy](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/server/routes_signals.py)
- [Market pricing providers](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/server/price_fetcher.py)
- [Background task registry](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/server/tasks.py)
- [Agent skill/API contract](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/skills/ai4trade/SKILL.md)
- [不完整的 checked-in OpenAPI](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/docs/api/openapi.yaml)
- [Research export and analysis pipeline](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/research/README.md)
