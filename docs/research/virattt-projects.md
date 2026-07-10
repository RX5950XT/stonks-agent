# virattt 專案研究：ai-hedge-fund 與 Dexter

研究日期：2026-07-10（Asia/Taipei）  
範圍：只研究與規劃，未實作整合程式碼。

## Snapshot 與結論

| 專案 | 本次分析 commit | 授權判定 | 近期狀態 |
|---|---|---|---|
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | [`3a18702c`](https://github.com/virattt/ai-hedge-fund/commit/3a18702cb25777fb4bdb4b2527a0c868bc8297f4)（2026-07-03） | **MIT，可重用**；實際有 [LICENSE text](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/LICENSE#L1-L21)，需保留 copyright/license notice。 | 2026-07-03 發布 [`v2026.7.3`](https://github.com/virattt/ai-hedge-fund/releases/tag/v2026.7.3)；截至研究日近 30 天 9 commits。約 61k stars、10.8k forks、49 open issues、100 open PR；**repo 無 CI workflow**。 |
| [Dexter](https://github.com/virattt/dexter) | [`bae66167`](https://github.com/virattt/dexter/commit/bae661670c3d77e909942777ac32ece21e8af35d)（2026-07-03） | **授權不完整，保守視為目前不可複製／散布**。README 雖宣稱 [MIT](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/README.md#L190-L192)，但 snapshot root 無 `LICENSE`、`package.json` 無 license，且 [GitHub API metadata](https://api.github.com/repos/virattt/dexter) 為 `license: null`。需等上游補 LICENSE text 或取得書面授權。 | 2026-06-15 發布 [`v1.0.0`](https://github.com/virattt/dexter/releases/tag/v1.0.0)；截至研究日近 30 天 10 commits。約 27.3k stars、3.4k forks、36 open issues、61 open PR；snapshot 的 [CI 成功](https://github.com/virattt/dexter/actions/runs/28658180043)。 |

直接結論：

1. **不要合併兩套 repo 成一個巨型 agent。** 最合理的邊界是「Dexter 類控制平面」負責對話、研究工具、記憶、subagent、排程與通道；「Python 量化核心」負責 point-in-time data、alpha signals、backtest、portfolio、risk、execution 與 ledger。
2. `ai-hedge-fund/v2` 最值得直接採用的是 `DataClient`、Pydantic data models、`AlphaModel → Signal` 契約、PEAD、event study；portfolio/risk/execution/validation 大多仍是 scaffold，必須另建。
3. Dexter 的 agent loop、tool registry、finance meta-tools、memory、cron 與 gateway 很有價值，但授權是目前的硬阻塞。授權釐清前只能研究架構並 clean-room 重寫，不能 vendor/copy source。
4. LLM 只能產生研究結果與 `AlphaSignal`；position sizing、risk veto、order/fill 一律由 deterministic code 掌控。這也符合 ai-hedge-fund 自己的新 [vision](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/VISION.md#L148-L159)。

## 1. ai-hedge-fund

### 定位、stack 與 workflow

上游明確把現有專案定位為 educational proof of concept，且目前「不實際下單」([README](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/README.md#L3-L31))。

- Python 3.11、Poetry、LangChain/LangGraph、Pydantic、pandas/NumPy/SciPy。
- Web：FastAPI + SQLAlchemy/Alembic/SQLite；React 18 + TypeScript + Vite + React Flow + Radix/shadcn 類元件。依賴證據見 [pyproject.toml](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/pyproject.toml#L14-L40) 與 [frontend package.json](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/app/frontend/package.json#L5-L53)。
- v1 workflow：`start → 19 個可選 analyst nodes（平行）→ risk manager → LLM portfolio manager → END`，實作見 [`create_workflow`](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/src/main.py#L100-L130)。CLI 回傳 decisions 與所有 analyst signals。
- v2 目標：`Data → AlphaModel/Signal → Portfolio → Risk → Execution → Ledger`，並統一 backtest/paper/live；但 `run_cycle`、fund、ledger、broker、scheduler 仍未完成，roadmap 有清楚的 [shipped/planned 對照](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/ROADMAP.md#L19-L31)。
- Web backend 提供 SSE hedge-fund/backtest、flow/run CRUD、model/Ollama/API-key routes；frontend 是視覺化 React Flow 編排器。這是一個可參考的 UI/API shell，不是 production trading control plane。

### 可重用模組與判定

| 模組 | 判定 | 理由／整合方式 |
|---|---|---|
| [`v2.data.protocol.DataClient`](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/data/protocol.py#L34-L77)、[`v2.data.models`](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/data/models.py) | **直接 import** | 結構化 typing、明訂 infra failure 必須 raise、financial metrics 必須用 filing date；適合作為 Python data adapter SPI。 |
| [`v2.models.Signal`](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/models.py#L14-L73)、[`AlphaModel`](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/signals/base.py#L29-L52) | **直接 import** | 是乾淨的 plugin surface；可把 Kronos、TradingAgents 與傳統 quant factor 轉成同一 `[-1,+1]` view。AI-Trader 社群內容先是 external evidence，不能直接升成 signal。 |
| [`PEADModel`](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/signals/pead.py#L25-L104) | **直接 import，研究用途** | 有 filing-date、freshness window、重複 filing priority 與 stale event filter；應補 universe、成本與 out-of-sample 驗證再升級。 |
| [`compute_car`](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/event_study/engine.py#L68-L135) 與 pure stats | **直接 import** | 可作 event research/evaluation service；已有 market model、CAR、t-test、bootstrap CI。 |
| [`v2.backtesting.BacktestEngine`](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/backtesting/engine.py#L41-L92) | **直接 import，但只作 baseline** | 固定美元部位、固定持有期、close fill；程式碼自己也稱 mechanics intentionally simple。不可當 production simulator。 |
| [`FDClient`](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/data/client.py#L24-L66) | **包 adapter 後重用** | fail-loud 與 point-in-time filtering 值得保留；但應置於共用 Data Gateway，補 provider routing、persistent cache、rate budget、provenance。 |
| v1 persona/fundamental/technical/valuation agents | **改寫** | 抽出 deterministic calculations 與 thesis prompt，改成無副作用的 `AlphaModel.predict()`；不要沿用 LangGraph shared mutable state 或直接輸出 orders。 |
| v1 risk manager、LLM portfolio manager | **改寫** | 公式可參考，但 portfolio decision 仍由 LLM 產生；需改成 deterministic target-weight optimizer + hard risk policies。 |
| FastAPI SSE、flow/run schema、React Flow editor | **改寫／參考** | UI interaction 與 run-event stream 可利用；資料庫、auth、secret handling、domain contracts 必須重做。 |
| `v2/features`、`validation`、`portfolio`、`risk`、`pipeline` | **只適合 roadmap 參考** | 現在大多只有 docstring；例如 [portfolio](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/portfolio/optimizer.py)、[risk](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/risk/manager.py)、[execution](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/pipeline/execution.py)、[validation](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/validation/__init__.py) 都沒有實作。 |

### Data、LLM 與啟動

- 主要 data source 是 **Financial Datasets API**：prices/OHLCV、financial metrics/line items、company facts、earnings、news、insider trades；v2 目前只實作 `FDClient`，roadmap 才規劃 alternative data adapters。
- LLM adapters：OpenAI/Azure OpenAI、Anthropic、Google、DeepSeek、Groq、xAI、Kimi/Moonshot、OpenRouter、GigaChat、Ollama；實際可用 model 還取決於 JSON model registry 與 API key。
- CLI：`poetry install`，再執行 `poetry run python src/main.py --ticker ...`；backtest 是 `src/backtester.py` 或 Poetry `backtester` entrypoint。官方指令見 [README](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/README.md#L100-L132)。
- Web：FastAPI `:8000` + Vite `:5173`，或 `app/run.*`。Docker Compose 目前主要包 CLI/backtester/Ollama profiles，見 [compose](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/docker/docker-compose.yml)。

### 驗證與限制

本機驗證：

- `poetry install --no-interaction --no-ansi`：成功。
- `poetry run pytest -q`：臺灣 Windows 預設 CP950 下為 **96 passed / 13 failed / 38 skipped**；13 failures 全部是 fixture 用 `open("r")` 未指定 encoding 而讀 UTF-8 JSON 失敗，來源見 [integration conftest](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/tests/backtesting/integration/conftest.py#L33-L102)。
- `PYTHONUTF8=1 poetry run pytest -q`：**109 passed / 38 skipped / 2 warnings**。38 skips 是需要 `FINANCIAL_DATASETS_API_KEY` 的 live tests；因此不代表 live API、LLM 或真實 market-data workflow 已通過。
- v2 public imports smoke test：成功。

主要風險：

1. v2 README 宣稱 vectorized backtester、transaction costs、CPCV/PBO 等能力，但實際 backtester 是固定持有／固定美元 sizing 且沒有成本模型；文件與程式碼有明顯落差 ([README claim](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/README.md#L3-L28)、[actual engine](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/v2/backtesting/engine.py#L161-L199))。
2. v1 與 v2 是兩條尚未統一的路徑；v2 明示 WIP、尚未整合主應用。point-in-time 正確性也仍標示 in progress。
3. v1 portfolio manager 讓 LLM 直接選 buy/sell/short/cover；即使有 quantity clamp，也不應接 broker。
4. Web backend 沒有 auth，且 API keys 以 plaintext SQLite `Text` 儲存（程式碼只註記「encrypted in production」），見 [model](https://github.com/virattt/ai-hedge-fund/blob/3a18702cb25777fb4bdb4b2527a0c868bc8297f4/app/backend/database/models.py#L101-L111)。只能視為 localhost demo。
5. 根 repo 沒有 CI workflow；Windows encoding 問題也顯示跨平台驗證不足。
6. 單一付費資料供應商、v1 process-local cache、缺少完整 data lineage；回測 reproducibility 與 rate/cost 管理尚不足。

## 2. Dexter

### 定位、stack 與 workflow

Dexter 是 terminal-first financial research agent。README 所稱 planning/self-reflection/self-validation 應理解為 **LLM 在 bounded tool loop 內的行為**，不是獨立、形式化的 planner/verifier engine ([README](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/README.md#L3-L41))。

- TypeScript 5.9、ESM、Bun；LangChain providers、Zod、`pi-tui`、Playwright、SQLite、Croner、Baileys WhatsApp、LangSmith。manifest 見 [package.json](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/package.json#L1-L58)。
- 主流程：CLI/controller 建 `Agent` → 載入 SOUL/rules/memory → 建 tool registry → LLM streaming/tool calls → read-only tools 可最多 10 concurrency → JSONL scratchpad → micro/full compaction → 無 tool call 即回 final；硬上限預設 10 iterations。核心見 [`Agent.run`](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/src/agent/agent.ts#L24-L282)。
- Tool registry 含 financials、market data、SEC filings、screener、web/search/browser、filesystem、skills、subagents、memory、cron、heartbeat、user question；依 provider key 動態加入 web/X search，見 [registry](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/src/tools/registry.ts#L47-L214)。
- 持久記憶：Markdown + SQLite vector index，支援 session transcript、hybrid retrieval、temporal decay、MMR；這適合 research/user memory，不是交易帳本。
- Automation：JSON cron store、heartbeat、長駐 gateway；WhatsApp 有 self-chat/allowlist/pairing/group policy。

### 可重用模組與判定

> 以下是**技術判定**；在授權補齊前，全部不得直接 copy/vendor。若採 clean-room 重寫，只能重現概念與公開介面行為。

| 模組 | 技術判定（授權解決後） | 理由／整合方式 |
|---|---|---|
| [`Agent.create/run`](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/src/agent/agent.ts#L77-L282)、tool registry/executor | **可直接 import，再加 policy layer** | 已支援 streaming、tool allowlist、parallel read tools、abort、compaction、subagent。應保留為 research orchestrator，不能取得 broker/order 權限。 |
| finance meta-tools：`get_financials`、`get_market_data`、`read_filings`、`stock_screener` | **可 import／改成 service tools** | 能把自然語言拆成底層 Financial Datasets calls，涵蓋 statements、metrics、earnings、segments、prices、crypto、news、insiders、13F、SEC filing sections。建議輸出 canonical artifacts，不直接把 display string 當 domain data。 |
| provider registry、search fallback、skills loader、subagent tool | **可直接 import** | 是低耦合的 orchestration infrastructure；需把 model IDs、cost/rate policy 外部化。 |
| `MemoryManager` + hybrid/MMR/decay | **改寫／包 adapter** | 可用於偏好、研究 memo、session recall；交易 positions、orders、fills、NAV 必須進 immutable/event-sourced ledger，不能放 Markdown memory。 |
| cron/heartbeat/gateway | **改寫** | 排程概念與 WhatsApp access control 可用；需加入 idempotency key、market calendar、distributed lock、delivery retry、audit log。 |
| TUI components、SOUL/persona、formatting | **只適合參考或 optional client** | 對核心交易價值較低，且 TUI 與 `pi-tui`/Bun 耦合；SOUL 是 prompt persona，不是策略模型。 |
| WhatsApp/Baileys | **optional adapter** | Baileys 目前鎖 `7.0.0-rc.9`；應隔離在 delivery plugin，避免通道變動拖垮核心。 |

### Data、LLM 與啟動

- Market/fundamental/filing data 同樣集中於 **Financial Datasets API**，有 filesystem cache；底層 base URL 與 API key 見 [finance API](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/src/tools/finance/api.ts#L1-L123)。
- Web search fallback：Exa → Perplexity → Tavily → LangSearch；可選 X API；Playwright/browser/web fetch 可讀動態網站。
- LLM：OpenAI、Anthropic、Google、xAI、Moonshot/Kimi、DeepSeek、OpenRouter、Ollama local/cloud，集中於 [provider registry](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/src/providers.ts#L21-L86)。
- CLI：`bun install && bun start`；dev `bun dev`。Evaluation 用 `bun run src/evals/run.ts`，LangSmith + LLM-as-judge。WhatsApp 用 `gateway:login`、`gateway`；官方流程見 [README](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/README.md#L82-L177)。
- 沒有 Dockerfile、HTTP API server 或 compiled library build；`package.json` 的 `main/bin` 直接指向 `src/index.tsx`，目前最適合作 Bun source package/sidecar，而非通用 npm SDK。

### 驗證與限制

本機驗證：

- `bun install --frozen-lockfile --ignore-scripts`：成功（與 upstream CI 相同，未下載 Chromium）。
- `bun run typecheck`：成功。
- `bun test`：**74 passed / 0 failed**。
- `Agent` 與 `getToolRegistry()` source import smoke test：成功，預設 registry 取得 17 tools。
- 未執行需要真實 LLM/data/LangSmith 的 evaluation，也未做 Playwright/WhatsApp live test。

主要風險：

1. **License 是第一優先 blocker**；README 一句 MIT 不等於完整 license grant。授權未補前不可直接整合原始碼。
2. README 的「self-validation」沒有 deterministic verifier gate。Agent 一旦回覆無 tool calls 就結束；formal eval 是另外手動執行的 LLM-as-judge，不在 CI。
3. 所謂 loop detection 是 soft warning、永不 block；只有 max iterations 是 hard stop，證據見 [scratchpad](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/src/agent/scratchpad.ts#L127-L175)。
4. Mutation policy 不完整：executor 只對 `write_file`/`edit_file` 要 approval；cron、heartbeat、memory update 等也會改狀態，卻未使用同一 approval gate。見 [tool executor](https://github.com/virattt/dexter/blob/bae661670c3d77e909942777ac32ece21e8af35d/src/agent/tool-executor.ts#L28-L154)。
5. 財務 meta-tools 內含額外 LLM planning/structured-output calls，會增加成本、延遲與 provider schema 相容風險；不宜放進每個 backtest tick。
6. Open web/filing/browser content 可能造成 prompt injection；所有外部內容必須被標為 untrusted data，工具權限與 order plane 完全隔離。
7. Memory、scratchpad、cron、sessions 都是 local `.dexter/` state；多 instance、backup、locking、PII retention、encryption 尚未成為 production contract。
8. 單元測試與 CI 健康，但未覆蓋真實 agent accuracy、provider matrix、browser runtime、live data 或 WhatsApp E2E。

## 3. 建議整合架構

```text
CLI / Web / WhatsApp
        │
        ▼
Research Control Plane（Dexter-inspired，TS/Bun）
  planner · tool policy · subagents · memory · cron · run events
        │                     │
        │                     └── Open web / filings / user research
        ▼
Versioned JSON API / queue（跨 TS ↔ Python，不共用內部 state）
        │
        ├── Market Data Gateway
        │     Financial Datasets · OpenBB · 其他 provider
        │     point-in-time · cache · provenance · rate/cost budget
        │
        ├── Research Workers
        │     TradingAgents · clean-room research tools
        │     只輸出 ResearchArtifact / AlphaSignal
        │
        └── Quant Core（Python）
              ai-hedge-fund AlphaModel · Kronos adapter · quant factors
              backtest/validation → portfolio → hard risk → execution
                                        │
                                        ▼
                         immutable ledger · paper broker · opt-in live broker
```

### 統一 contracts

| Contract | 最少欄位 | 原則 |
|---|---|---|
| `MarketDatum` | `instrument`, `event_time`, `published_at`, `as_of`, `source`, `payload_hash` | 嚴格 point-in-time；任何 infra error 不得偽裝成 empty data。 |
| `ResearchArtifact` | `run_id`, `question`, `claims`, `citations`, `source_times`, `model/tool_versions` | 人類可讀研究與證據；不可直接變 order。 |
| `AlphaSignal` | `model_id/version`, `instrument`, `as_of`, `horizon`, `value[-1,1]`, `confidence`, `reasoning`, `provenance` | 以 ai-hedge-fund `Signal` 為起點，補 horizon/confidence/version。 |
| `PortfolioTarget` | `weights`, `constraints`, `optimizer_version`, `input_signal_ids` | Deterministic、可重播。 |
| `RiskDecision` | `approved`, `clamped_target`, `limits`, `reasons`, `policy_version` | Risk 可 veto；LLM 不可 override。 |
| `OrderIntent/Fill` | idempotency key、broker account、qty/price/type、status、timestamps | Broker adapter 僅接受已核准 intent。 |
| `RunEvent/LedgerEntry` | `run_id`, sequence、type、payload hash、parent IDs | append-only，支援 audit/replay。 |

### 與其他專案的角色分工

- **OpenBB**：優先作 Data Gateway provider，而不是讓每個 agent 各自直接呼叫資料源。
- **Kronos**：包成 batch inference worker/`AlphaModel`，輸出有 horizon、calibration 與 model version 的 `AlphaSignal`。
- **TradingAgents**：當 bounded research/debate worker；輸出 `ResearchArtifact + AgentOpinion`，不保留 portfolio/execution authority。
- **AI-Trader**：只作 optional external control/community/paper API adapter；回覆與投票先轉成 untrusted `ExternalEvidence`，不得直接成為 signal 或 order。
- **daily_stock_analysis**：吸收 daily orchestration/report template/notification 能力；排程與資料查詢仍由共用 gateway/control plane 管理。
- **ai-hedge-fund**：採 v2 contracts、PEAD、event study 與 baseline backtester；persona agents 只移植成 alpha plugins。
- **Dexter**：授權解決後採 control-plane/tooling；授權未解決則 clean-room 實作同等介面。

### 執行順序與驗收門檻

1. **P0 — License + contracts**：先解 Dexter license；凍結上述 JSON Schema/Pydantic/Zod contracts，建立 provenance 與 point-in-time conformance tests。
2. **P1 — Read-only research**：整合 OpenBB/Financial Datasets gateway、Dexter 類 research tools、各 agent workers；只產研究與 signals。
3. **P2 — Quant lab**：接 Kronos/PEAD/factors；建立 transaction costs、walk-forward/CPCV/PBO、benchmark、data leakage tests。任何策略未過 validation gate 不得 paper trade。
4. **P3 — Paper fund**：完成 deterministic portfolio/risk、event-sourced ledger、market-calendar scheduler、idempotent simulated broker、kill switch；研究 control plane 只能提出 mandate/target request。
5. **P4 — Opt-in live**：broker credentials 進 secret manager；雙重 approval、account/position/notional limits、daily loss kill switch、reconciliation、alerts、human promotion gate 全部通過後才開啟。

整合的關鍵不是「收集最多 agents」，而是只保留一個 orchestration authority、一套 data truth、一個 signal contract，以及不可被 LLM 繞過的 portfolio/risk/execution boundary。
