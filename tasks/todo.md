# Stonks Agent 實作計畫

> 狀態：執行中（P0–P5 gate、P6.1–P6.9 已通過，P6.10 進行中）
> Architecture source of truth：`docs/architecture/integration-blueprint.md`  
> 執行規則：本計畫確認一次後，依 P0 → P6 連續實作；phase gate 是驗證門檻，不是再次等待確認。只有 live trading、產品授權變更或新增高權限外部整合須另立 RFC。

## 標記說明

- Complexity：`S`（單一小模組）、`M`（數個模組）、`L`（跨層/跨程序）、`XL`（重大整合）。
- Risk：`Low`、`Medium`、`High`；High 必須在同一項目列出 fail-closed gate。
- `[ ]` 未開始、`[x]` 完成；只有驗證證據已落盤才能勾選。
- 每個 task 的 `Depends` 指必要前置；可在不衝突時平行處理同 phase 項目。
- 所有新增 Python 為 3.12，主核心使用 `uv`；optional workers/sidecars各自鎖定 environment。

## 一次性確認

- [x] **PLAN-AUTH** — 確認 `docs/architecture/integration-blueprint.md` 與本計畫，授權按 P0→P6 持續實作，不逐 phase 暫停。（Depends：None；Complexity：S；Risk：Low）

---

## P0 — Foundation、governance 與 canonical contracts

### Outcome

建立可安裝、可測試的 Python 3.12 + `uv` workspace、版本化 wire contracts、paper-only capability boundary，以及不連真實 provider/LLM/service 但語意完整的 in-memory fake/replay 閉環：evidence → signal → target → risk → account reservation → next-session paper fill → balanced journal → report → replay。最小 security、authorization、idempotency、late-result fencing與telemetry從本phase即為gate，不延後到P6。

### Tasks

- [x] **P0.1 Bootstrap uv workspace** — 建立 `pyproject.toml`、`uv.lock`、`.python-version`、`src/stonks_agent/__init__.py`、`tests/conftest.py`；鎖定core runtime與dev dependencies，設定`ruff`、`mypy`、`pytest`、coverage、Hypothesis。（Depends：PLAN-AUTH；Complexity：M；Risk：Low）
  - Core只含Pydantic 2、Typer、FastAPI、HTTPX、SQLAlchemy/Alembic、psycopg、structlog與OpenTelemetry primitives；禁止PyTorch/OpenBB/LangGraph/Qlib進主lock。
  - 驗證：`uv sync --frozen`、`uv run ruff check .`、`uv run mypy src packages`、`uv run pytest`。

- [x] **P0.2 Repository conventions and docs** — 建立`.gitignore`、`.editorconfig`、`README.md`、`AGENTS.md`、`CLAUDE.md`、`CONTEXT.md`、`tasks/lessons.md`，將繁中、CLI-first、驗證、paper-only與文件同步規則落盤。（Depends：P0.1；Complexity：S；Risk：Low）
  - `.research/`、`.data/`、model cache、secrets、coverage、DB、worker outputs全部排除版控。
  - README先描述安全範圍與quick start，不宣稱尚未完成能力。

- [x] **P0.3 License and upstream policy** — 建立`LICENSE`（一次性確認採Apache-2.0作自有core預設）、`THIRD_PARTY_NOTICES.md`、`docs/legal/license-policy.md`、`docs/legal/upstream-manifest.yaml`、`scripts/check_upstream_policy.py`。（Depends：PLAN-AUTH；Complexity：M；Risk：High）
  - Manifest固定研究snapshot、license、允許的adoption mode與禁止路徑。
  - CI gate明確設`NO_VENDOR_DEXTER_CODE`、`NO_VENDOR_AI_TRADER_CODE`與`NO_OPENBB_IMPORT_IN_CORE`。
  - Fail-closed：未知/漂移license、缺notice或OpenBB package進core dependency graph即失敗。

- [x] **P0.4 Contracts package skeleton** — 建立`packages/contracts/pyproject.toml`、`packages/contracts/src/stonks_contracts/{common,instrument,market_data,evidence,research,signal,portfolio,risk,execution,workflow,report}.py`與`packages/contracts/tests/`。（Depends：P0.1；Complexity：L；Risk：Medium）
  - Models為frozen Pydantic、`extra='forbid'`、timezone-aware UTC、Decimal字串、explicit schema version。
  - `research.py`明確定義`ResearchArtifact`、`AnalysisBundle`、`AgentOpinion`；`AgentOpinion`不得含order/qty/execution欄位。
  - `risk.py`/`execution.py`明確定義`AccountReservation`、`OrderIntent`、`ExecutionReceipt`、`JournalTransaction/Posting`；journal每種currency/commodity必須debit/credit平衡。
  - Canonical chain固定為`Evidence/ResearchArtifact -> AnalysisBundle/AgentOpinion/AlphaSignal/ForecastSignal -> PortfolioTarget -> RiskDecision -> OrderIntent -> ExecutionReceipt`。

- [x] **P0.5 Contract schema export and compatibility** — 建立`scripts/export_schemas.py`、`schemas/v1/*.json`、`schemas/README.md`、`tests/contracts/test_schema_snapshots.py`、`tests/contracts/test_round_trip.py`。（Depends：P0.4；Complexity：M；Risk：Medium）
  - JSON Schema排序與輸出deterministic；breaking change必須新major schema directory。
  - 驗證Pydantic↔JSON round-trip、unknown fields rejection、Decimal/time serialization、payload hash穩定。

- [x] **P0.6 Domain/application/ports skeleton** — 建立`src/stonks_agent/domain/{errors,ids,time,quality}.py`、`application/`, `ports/`, `adapters/`, `entrypoints/`, `config/`與對應`__init__.py`；加入dependency-direction tests。（Depends：P0.4；Complexity：M；Risk：Medium）
  - `domain`不得import`adapters`、FastAPI、SQLAlchemy或上游套件。
  - Ports使用typed `Protocol`與structured error unions，不回傳ambiguous `None`。

- [x] **P0.7 Validated configuration and paper-only boundary** — 建立`src/stonks_agent/config/settings.py`、`config/defaults.toml`、`config/policies/paper.yaml`、`tests/config/test_paper_only.py`。（Depends：P0.6；Complexity：M；Risk：High）
  - `execution_mode`只接受`paper`；unknown/live值在startup fail fast。
  - Secret欄位使用named refs，不可被序列化到logs/events/config snapshots。

- [x] **P0.8 Complete in-memory fake/replay vertical slice** — 建立`src/stonks_agent/adapters/fakes/`、`application/workflows/run_cycle.py`、`entrypoints/cli.py`、in-memory event/job/outbox/account repositories與`tests/e2e/test_fake_cycle.py`。（Depends：P0.5、P0.6、P0.7；Complexity：XL；Risk：High）
  - Flow必須完整經過deterministic target、hard risk、per-account serialized reservation、`OrderIntent`、command之後第一個可交易bar fill、reservation consume/release、balanced journal postings、report與projection replay；不得用已知同根close成交。
  - Core runner是唯一transaction owner；fake remote result也帶`attempt_generation + attempt_nonce`，舊lease/late result不得commit。
  - 同一frozen clock、IDs、archived artifacts與inputs連跑兩次，control-plane hashes相同且無duplicate side effects；stochastic inference不在P0重跑。

- [x] **P0.9 Security/reliability baseline** — 建立統一API envelope/error mapping、local principal + minimal permission checks、secret redaction、process capability/egress deny、trace context、structured logs、in-memory metrics、idempotency/inbox/outbox與crash/duplicate/concurrent-run tests。（Depends：P0.6–P0.8；Complexity：L；Risk：High）
  - 兩個同帳戶run並行時只能在serialized aggregate內建立reservation，不得雙花cash或超賣position。
  - Same idempotency key/different payload、nonce/generation mismatch、journal不平、unknown execution state與unauthorized call全部fail closed。

- [x] **P0.10 CI baseline** — 建立`.github/workflows/ci.yml`、`.github/dependabot.yml`、`scripts/verify.py`；CI執行frozen install、format check、lint、typecheck、unit/contract/E2E tests、license policy、secret scan、dependency audit。（Depends：P0.1、P0.3、P0.8、P0.9；Complexity：M；Risk：Medium）
  - Windows與Linux至少各一個Python 3.12 job，避免encoding/path drift。

### P0 Verification gate

- [x] `uv sync --frozen`可由乾淨cache重建，core dependency tree無PyTorch/OpenBB/LangGraph/Qlib。（Depends：P0.1、P0.10）
- [x] `uv run ruff check .`、`uv run mypy src packages`、`uv run pytest`全通過。（Depends：P0.10）
- [x] Exported schemas與snapshot無未審核diff；非法time/Decimal/extra field會被拒絕。（Depends：P0.5）
- [x] Fake E2E完成target/risk/reservation/next-session fill/balanced journal/report/replay；測試證明`AgentOpinion`、`ForecastSignal`無法直接呼叫`ExecutionPort`。（Depends：P0.8）
- [x] Concurrent-run、crash/duplicate、late result、idempotency conflict、unauthorized call與unbalanced journal fixtures全部fail closed。（Depends：P0.9）
- [x] License gate能阻擋Dexter/AI-Trader source與core OpenBB import。（Depends：P0.3、P0.10）

### P0 Success criteria

- [x] 新開發者只需Python 3.12 + `uv`即可完成core驗證。
- [x] 不啟動任何optional service也可跑語意完整的fake paper cycle並重建相同projection。
- [x] Contracts、paper-only、balanced journal、account reservation、security/reliability與CI成為後續phase不可繞過的基線。

---

## P1 — Canonical Data Hub、evidence 與 durable workflow

### Outcome

建立point-in-time資料/evidence真相、PostgreSQL state、content-addressed artifacts、durable jobs/outbox、explicit provider policy與optional OpenBB sidecar。

### Tasks

- [x] **P1.1 Instrument and time domain** — 實作`src/stonks_agent/domain/{instrument,calendar,market_data}.py`、`ports/instrument_repository.py`、`ports/trading_calendar.py`、`tests/domain/test_instrument.py`。（Depends：P0 gate；Complexity：L；Risk：High）
  - Instrument以UUID + MIC/currency/timezone為identity；provider symbols帶validity interval。
  - Calendar tests涵蓋休市、午休、DST、跨日session與symbol change。

- [x] **P1.2 Data quality/provenance domain** — 實作`src/stonks_agent/domain/{evidence,provenance,data_quality}.py`與property tests。（Depends：P0.4、P1.1；Complexity：L；Risk：High）
  - 完整區分`event_time/published_at/available_at/observed_at/as_of`。
  - `available_at > as_of`、unknown strict-PIT evidence、OHLC invariant失敗都fail closed。

- [x] **P1.3 PostgreSQL schema and migrations** — 建立`alembic.ini`、`migrations/env.py`、`migrations/versions/0001_core_data.py`、`src/stonks_agent/adapters/postgres/models/`。（Depends：P0.1、P1.2；Complexity：XL；Risk：High）
  - 建立instrument/alias、artifact/evidence/edge/snapshot、run/event/job/outbox/inbox/provider_health/usage_budget tables。
  - Append-only tables以DB trigger/permissions阻止update/delete；所有FK/index/unique idempotency constraints明確。

- [x] **P1.4 Repository and unit-of-work adapters** — 實作`adapters/postgres/{repositories,unit_of_work}.py`、`ports/{evidence_repository,workflow_store,unit_of_work}.py`與integration tests。（Depends：P1.3；Complexity：L；Risk：High）
  - Transaction內同時寫domain event、job/outbox；optimistic run version做CAS transition。
  - Tests涵蓋rollback、concurrent claim、duplicate idempotency、append-only violations。

- [x] **P1.5 Artifact stores** — 實作`ports/artifact_store.py`、`adapters/artifacts/{local,memory}.py`、`tests/integration/test_artifact_store.py`。（Depends：P1.2；Complexity：M；Risk：Medium）
  - SHA-256 content address、atomic finalize、size/media/license/sensitivity metadata與hash驗證。
  - `.data/artifacts/`不進版控；DB event只能引用已finalize artifact。

- [x] **P1.6 Durable queue/outbox** — 實作`domain/job.py`、`ports/queue.py`、`adapters/postgres/{job_queue,outbox}.py`、`entrypoints/worker.py`、`tests/integration/test_job_leases.py`。（Depends：P1.3、P1.4；Complexity：XL；Risk：High）
  - `SKIP LOCKED` lease、not-before/deadline、attempt/max、lease expiry、dead letter、idempotent ack。
  - Crash-after-result-before-ack測試不得產生重複event/side effect。

- [x] **P1.7 Provider policy engine** — 建立`domain/provider_policy.py`、`application/data/fetch_evidence.py`、`config/providers/*.yaml`與`tests/domain/test_provider_policy.py`。（Depends：P1.2、P1.4；Complexity：L；Risk：High）
  - 每個market/capability設定allowlist、fallback、freshness、quota、stale acceptance與reconciliation threshold。
  - Empty、legitimate empty、not-supported、quota、stale、partial、conflict皆為不同typed state。

- [x] **P1.8 Replay and canonical fixture adapter** — 建立`adapters/market_data/replay.py`、`tests/fixtures/market_data/manifest.yaml`、`tests/golden/`。（Depends：P1.5、P1.7；Complexity：M；Risk：Medium）
  - Golden set至少涵蓋US/HK/TW、daily/intraday、DST、拆股、股利、stale、partial、conflict。
  - Fixture保存source/time/hash但不含secret或無權重散布資料。

- [x] **P1.9 Financial Datasets HTTP adapter** — 建立`adapters/market_data/financial_datasets.py`、`tests/contracts/providers/test_financial_datasets.py`。（Depends：P1.7；Complexity：M；Risk：Medium）
  - 使用自有HTTP DTO，不import ai-hedge-fund/Dexter；明確rate budget、timeout與structured errors。
  - 無API key時adapter標`config_missing`並由policy跳過，不使離線tests失敗。
  - 已驗證的是read-only observation contract；轉成canonical raw/evidence artifact的production bridge尚未宣稱完成。

- [x] **P1.10 OpenBB REST adapter** — 建立`adapters/market_data/openbb_rest.py`、`tests/contracts/providers/test_openbb_rest.py`。（Depends：P1.7；Complexity：L；Risk：High）
  - 保存`provider/warnings/extra/id`，再正規化；fallback仍由本系統決定。
  - Endpoint/provider allowlist固定，request不可注入arbitrary URL/provider。
  - 與replay/Financial Datasets共用daily query shape；canonical materialization仍只使用已驗證的replay source。

- [x] **P1.11 Optional OpenBB sidecar** — 建立`sidecars/openbb/{pyproject.toml,uv.lock,Dockerfile,README.md,SOURCE_OFFER.md,provider-manifest.yaml}`與`infra/compose.openbb.yaml`。（Depends：P0.3、P1.10；Complexity：L；Risk：High）
  - Pin已發布OpenBB與最小providers，不跟`develop`；保存exact source、patch/build recipe、AGPL notice。
  - Core在sidecar未啟動時仍正常以replay/其他provider運作。

- [x] **P1.12 Regional adapter contract and initial mappings** — 建立`adapters/market_data/regional/base.py`、`config/instruments/{us,hk,tw}.yaml`與contract fixtures；只宣稱已有合法、穩定fixture與adapter contract的US/HK/TW能力，中國A股另待後續regional provider RFC。（Depends：P1.1、P1.7；Complexity：L；Risk：High）
  - 不把Yahoo suffix當完整市場支援；unsupported capability必須明示。
  - 新provider採HTTP adapter或獨立worker，不把DSA monolith拉入core。

- [x] **P1.13 Data ingestion API/CLI** — 建立`application/data/create_snapshot.py`、`entrypoints/api/routes/data.py`、`entrypoints/cli_commands/data.py`與E2E tests。（Depends：P1.4–P1.12；Complexity：L；Risk：Medium）
  - API只回job/snapshot/evidence refs；大payload走artifact store。

### P1 Verification gate

#### P1 gate audit closure（2026-07-11）

- [x] PIT property tests以多組`available_at/snapshot as_of/run as_of`證明future evidence與future snapshot皆由domain/DB fail closed。
- [x] Snapshot完成後可透過canonical evidence repository完整round-trip，不產生未知`EvidenceKind`。
- [x] Regional capability、provider route、fixture與adapter支援矩陣一致；未實作能力不得宣稱支援。
- [x] Snapshot ingestion實際走policy reconciliation；threshold、provider/endpoint authority與outage狀態有E2E證據。
- [x] OpenBB SBOM每個component皆有license metadata，license/source policy fail closed。
- [x] FastAPI validation、unknown field與request-size錯誤仍使用統一API envelope。
- [x] Outbox同owner重領也以generation/nonce fencing拒絕stale ack；artifact multi-instance finalize與exact grants另有整合測試。
- [x] Provider fetch後、artifact finalize前以fresh DB time再次驗lease fence；跨run canonical snapshot reuse的exact retry可完整重驗。
- [x] Job、snapshot failure/completion與outbox的claim/ack/nack皆以transaction內PostgreSQL time判斷not-before/deadline/lease；caller clock漂移不得讓late result或提前claim commit。
- [x] Generic job deadline/max-attempt terminal transition具hash-chained event/outbox、清除lease，completed/idempotent retry完整重驗audit與immutable identity。
- [x] API/auth/content-length與provider HTTP streaming遇惡意輸入仍回redacted structured error，並防compression bomb與總下載時間繞過。
- [x] Linked PIT authority不可事後修改；worker只具必要column-level UPDATE權限。
- [x] OpenBB sidecar runtime route採exact allowlist；CI重驗source/license hashes、live adapter smoke與sidecar lock CVE。
- [x] Snapshot request retry完整重驗run/job immutable identity；model-copy繞過的provider output在0 artifact writes前拒絕。
- [x] Reconciliation決策至少封存雙側content refs、metric/value、threshold與decision，能由immutable artifact離線重播。

- [x] PostgreSQL migration可upgrade/downgrade/re-upgrade；schema與grants測試通過。（Depends：P1.3）
- [x] 併發jobs、worker crash、outbox retry與duplicate inbox測試無重複事件。（Depends：P1.6）
- [x] PIT property tests證明run無法引用future evidence；DST/calendar/corporate-action golden tests通過。（Depends：P1.1、P1.2、P1.8）
- [x] Provider outage/empty/stale/conflict皆產生正確quality或`DataUnavailable`，不產生empty success。（Depends：P1.7–P1.12）
- [x] OpenBB sidecar SBOM/license/source流程完整，core lock與imports仍無OpenBB。（Depends：P1.11）

### P1 Success criteria

- [x] 相同query/as-of/policy可建立hash-identical snapshot manifest並離線重播。
- [x] 所有canonical datum/evidence均可追到raw artifact、provider、版本與時間語義。
- [x] 即使所有外部providers關閉，replay vertical slice與core測試仍完整通過。

---

## P2 — Research control plane、agent opinions、reporting 與 delivery

### Outcome

整合TradingAgents、ai-hedge-fund可用分析與DSA報告優點，同時clean-room建立Dexter-inspired bounded research orchestration；所有輸出停在`ResearchArtifact/AnalysisBundle/AgentOpinion`或經正式plugin產生的`AlphaSignal`，不觸碰order plane。

### Tasks

- [x] **P2.1 Research domain and tool policy** — 實作`domain/{research,tool_policy,usage_budget}.py`、`ports/{research_worker,llm,tool}.py`、`tests/domain/test_tool_policy.py`。（Depends：P1 gate；Complexity：L；Risk：High）
  - Tool具allowlist、typed args、instrument/evidence scope、read-only/mutation class、timeout、byte limit、redaction與audit。
  - Research principals無filesystem write/shell/secret/queue/execution ports。

- [x] **P2.2 Clean-room bounded research orchestrator** — 建立`application/research/{orchestrate,tool_loop,context_builder}.py`、`adapters/research/deterministic.py`與測試。（Depends：P2.1；Complexity：XL；Risk：High）
  - 只依公開概念重做planning/tool loop、bounded iterations、parallel read tools、budget與loop hard-stop；不複製Dexter source/prompt/assets。
  - 外部內容包成untrusted blocks；無citation claim標hypothesis。

- [x] **P2.3 LLM structured-output adapters** — 建立`adapters/llm/{fake,openai_compatible,anthropic}.py`、`config/models.yaml`、`tests/contracts/llm/`。（Depends：P2.1；Complexity：L；Risk：High）
  - Provider/model allowlist、schema validation、retry budget、token/cost accounting、secret redaction。
  - Invalid output bounded repair後仍失敗即structured error，不回free-form success。

- [x] **P2.4 TradingAgents isolated worker** — 建立`workers/tradingagents/{pyproject.toml,uv.lock,Dockerfile,README.md,app.py,adapter.py}`與`tests/contracts/workers/test_tradingagents.py`。（Depends：P0.3、P2.1；Complexity：XL；Risk：High）
  - Pin `01477f9a`/v0.3.1與Apache NOTICE；每種runtime profile獨立process，避免global config污染。
  - 唯一response為`AnalysisBundle + AgentOpinion`；上游Trader/Portfolio/risk debate文字不能命名`TradeIntent`或帶execution authority。
  - Production/paper/backtest profile只能讀`allowed_evidence_ids`對應的scoped artifacts，使用canonical tool facade且預設network egress deny；不得呼叫upstream current-news/social/data tools污染PIT run。
  - Callback記錄model/tool latency、tokens、warnings與source refs。

- [x] **P2.5 TradingAgents core adapter** — 建立`adapters/research/tradingagents_http.py`、`config/workers/tradingagents.yaml`與timeout/retry/schema-drift tests。（Depends：P2.4；Complexity：M；Risk：High）
  - Core只傳evidence refs/signed artifact URLs；worker不能自行寫DB。
  - Request/response帶lease generation、attempt nonce與artifact hash；core job runner驗證後才在單一transaction寫metadata/event/outbox並ack，late result只進隔離audit。

- [x] **P2.6 ai-hedge-fund alpha/event-study adoption** — 建立`strategies/{pead.py,manifest.yaml}`、`analytics/event_study.py`、`tests/golden/{pead,event_study}/`並更新notice。（Depends：P1.2、P0.3；Complexity：L；Risk：High）
  - 只移植MIT允許且有實作/測試的PEAD與pure stats；不採v1 LLM portfolio/risk或v2 scaffold。
  - Filing date/freshness/duplicate filing與PIT tests必須通過；未完成evaluation前strategy state=`draft`。

- [x] **P2.7 Analysis context/evidence assembler** — 建立`application/reporting/evidence_assembler.py`、`domain/analysis_context.py`與tests。（Depends：P1.2、P2.1；Complexity：M；Risk：Medium）
  - 吸收DSA quality vocabulary但使用自有versioned schema；assembler只讀canonical evidence，不自行抓資料。

- [x] **P2.8 Structured report generator and integrity policy** — 建立`application/reporting/{generate,integrity_policy}.py`、`domain/report.py`、`tests/reporting/test_integrity.py`。（Depends：P2.3、P2.7；Complexity：L；Risk：High）
  - `AnalysisReport` JSON為truth；每個claim/evidence ref完整，estimated/stale/conflict不可寫成確定事實。
  - LLM invalid JSON、missing citation、數值越界與decision guardrail均fail/retry bounded。

- [x] **P2.9 Jinja renderers and templates** — 建立`templates/{full.md.j2,brief.md.j2,email.html.j2}`、`adapters/reporting/jinja.py`、`tests/golden/reports/`。（Depends：P2.8、P0.3；Complexity：M；Risk：Medium）
  - 若移植DSA模板片段，保留MIT notice與來源commit；否則clean implementation。
  - Render snapshot涵蓋missing/stale/conflict、多語、long symbol/channel limit與escaping。

- [x] **P2.10 Delivery ports** — 建立`ports/delivery.py`、`adapters/delivery/{console,file,email,webhook}.py`、`application/reporting/deliver.py`與idempotency tests。（Depends：P1.6、P2.9；Complexity：L；Risk：High）
  - Chunking、rate limit、retry/outbox、receipt與redacted errors一致；console/file為default。
  - Email/webhook未配置時不阻擋報告產生。

- [x] **P2.11 Research/report API and CLI** — 建立`entrypoints/api/routes/{research,reports}.py`、`entrypoints/cli_commands/{research,report}.py`與SSE run-event projection。（Depends：P2.2–P2.10；Complexity：L；Risk：Medium）
  - API不直接執行長任務，只建立job並stream/read canonical events。

- [x] **P2.12 Canonical research pipeline gate** — 建立`application/research/pipeline.py`與snapshot→deterministic/TradingAgents→report/render/delivery E2E；每次result輸出immutable stage audit artifact。（Depends：P2.1–P2.11；Complexity：XL；Risk：High）
  - TradingAgents outage可degrade；deterministic/report failure須形成failed result；任何路徑都不得產生target/order或偽造success。
  - 此gate只關閉P2 research control plane；P4.7仍負責portfolio/risk/execution完整state machine。

### P2 Verification gate

- [x] Fake LLM、prompt-injection fixtures、tool scope/timeout/output-limit與budget exhaustion tests全部通過。（Depends：P2.1–P2.3）
- [x] TradingAgents pinned worker contract測試證明只回`AnalysisBundle/AgentOpinion`，且worker無execution/DB credentials、無任意data egress、無late-result commit能力。（Depends：P2.4、P2.5）
- [x] PEAD/event-study golden與PIT tests通過，notice完整。（Depends：P2.6）
- [x] 每個report claim都能解析到evidence；所有channel render可由同一report重建且hash穩定。（Depends：P2.7–P2.10）
- [x] Provider/LLM/TradingAgents outage時run能degrade/fail/report，不產生偽造success或order。（Depends：P2.11）

### P2 Success criteria

- [x] 單一instrument可從snapshot完成deterministic + TradingAgents research並產生可稽核report。
- [x] Agent opinion與community-like文字沒有任何直接execution path。
- [x] Dexter與AI-Trader source/prompt/assets未進repository；DSA/ai-hedge-fund採用均有notice。

---

## P3 — Forecast、alpha evaluation、Qlib 與 strategy promotion

### Outcome

建立模型/策略registry、嚴格point-in-time evaluation與promotion gates；Kronos/AgentOpinion只能在通過evaluation後以版本化`AlphaSignal`參與後續portfolio。

### Tasks

- [x] **P3.1 Strategy/signal/evaluation domain** — 實作`domain/{strategy,signal,evaluation}.py`、`ports/{forecast,strategy_lab}.py`與property tests。（Depends：P2 gate；Complexity：L；Risk：High）
  - Promotion state固定`draft -> evaluating -> rejected|shadow -> paper_eligible -> suspended|retired`。
  - 未註冊evaluation report、expired/stale/un-calibrated signal預設權重0。
  - [x] TDD：先固定promotion transition、provenance binding、PIT/expiry/calibration與零權重fail-closed properties。
  - [x] Forecast與strategy-lab ports只接受immutable artifact/snapshot inputs，回傳structured `Result`，不具target/order/risk authority。
  - [x] Focused + full gate通過後同步README/CONTEXT與本review並提交。

- [x] **P3.2 Strategy registry persistence** — 新增`migrations/versions/0009_strategy_registry.py`、`adapters/postgres/strategy_repository.py`與concurrency tests。（Depends：P3.1、P1.4；Complexity：L；Risk：High）
  - Artifact/runtime/data/evaluation hashes不可變；promotion用CAS與audit event。
  - [x] TDD：register/idempotency、exact evaluation binding、CAS race、DB clock、hash-chain audit與DB immutability/grant tests。
  - [x] Registry/evaluation/audit schema可downgrade/re-upgrade，Alembic metadata無drift；heavy worker role無strategy mutation權限。

- [x] **P3.3 Deterministic baselines** — 建立`strategies/baselines/{last_value,moving_average,linear}.py`、manifests與golden tests。（Depends：P3.1；Complexity：M；Risk：Medium）
  - Kronos、LLM opinions與complex models必須和相同dataset/cost下baselines比較。
  - [x] TDD：PIT/ordering/positive-price input invariants、manifest loader、三算法golden與deterministic replay hash。
  - [x] Baseline只產`ForecastSignal`，固定draft research authority；不得產target/order或自稱calibrated/paper eligible。

- [x] **P3.4 Evaluation engine** — 建立`application/evaluation/{walk_forward,leakage,costs,metrics,calibration,promotion}.py`與`tests/evaluation/`。（Depends：P3.1、P3.3；Complexity：XL；Risk：High）
  - 涵蓋historical universe、publication lag、purged splits/embargo、walk-forward、CPCV/PBO（適用時）、fees/slippage/turnover sensitivity、benchmark alpha、drawdown、calibration。
  - 同snapshot/strategy/runtime必須輸出相同evaluation hash。
  - [x] TDD：future feature/label、unknown publication lag、current-universe survivorship污染都必須structured fail closed。
  - [x] Purged walk-forward/embargo、bounded CPCV/PBO、cost sensitivity、benchmark/drawdown與probability calibration都有deterministic tests。
  - [x] Promotion report exact綁定strategy/snapshot/runtime/policy；同輸入不同report ID/time仍產相同evaluation hash。

- [x] **P3.5 Opinion-to-alpha policy** — 建立`application/signals/opinion_to_alpha.py`、`config/policies/opinion_mappers.yaml`與tests。（Depends：P3.1、P3.4；Complexity：L；Risk：High）
  - Default disabled；只有mapper本身有evaluation、opinion confidence有校準且strategy=`paper_eligible`時才產`AlphaSignal`。
  - 無法把rating字串或LLM quantity直接映射為order/target。
  - [x] TDD：disabled/unknown rating/uncalibrated/non-paper/failed-or-expired evaluation全部不產signal。
  - [x] Enabled mapper使用固定signed values與exact policy/strategy/evaluation/runtime/current-data provenance，輸出仍無target/order authority。

- [x] **P3.6 Kronos isolated worker environment** — 建立`workers/kronos/{pyproject.toml,uv.lock,Dockerfile,README.md,app.py,model_loader.py,adapter.py}`、pinned model manifest/checksums。（Depends：P0.3、P3.1；Complexity：XL；Risk：High）
  - Pin code、tokenizer、model revisions/hash；模型warm一次，禁止request-time任意下載。
  - CPU與CUDA profiles分開；核心不安裝torch。
  - [x] TDD：固定manifest schema、source archive SHA-256、模型/tokenizer revision與file SHA-256；缺檔、symlink、size/hash drift一律在load前fail closed。
  - [x] Warm-once loader只接受唯一本機唯讀model root；禁止repo ID、URL、`HF_HOME` cache fallback與request-time download，並對concurrent startup只載入一次。
  - [x] CPU/CUDA各自使用獨立locked environment/image target；runtime compose為internal network、read-only、non-root、cap-drop且無DB/provider/execution credentials。
  - [x] Contract/HTTP health與forecast preflight、core heavy-dependency isolation、license/notice/source provenance及container policy tests通過。

- [x] **P3.7 Kronos canonical input/output adapter** — 實作calendar-aware input、sample path retention、seed policy、OHLC/volume invariant與`ForecastSignal` mapping；建立`tests/golden/kronos/`。（Depends：P1.1、P1.2、P3.6；Complexity：XL；Risk：High）
  - Missing/estimated volume降低quality；future timestamps來自exchange calendar。
  - Invalid output、length mismatch、extreme jump、model revision mismatch不產signal。
  - 每次raw sampled paths與runtime/model metadata先封存為immutable artifact；deterministic replay從該artifact開始，不宣稱fresh stochastic re-inference可bit-identical。
  - [x] TDD：shared worker wire contracts固定lease fence、PIT ordered bars、calendar timestamps、model/tokenizer/runtime identity、seed/sample count與closed authority；unknown/duplicate/out-of-order/future欄位fail closed。
  - [x] Core request builder只接受canonical `BarSeries`與`ExchangeCalendar`；1d future bars跨週末/假日/DST取session close，missing/estimated volume留下可降級quality，不讓worker自行猜calendar。
  - [x] Worker逐seed執行`sample_count=1`並保留每條raw path，固定Python/NumPy/PyTorch/CUDA seeds；不使用上游會先平均掉paths的multi-sample輸出。
  - [x] Core HTTP adapter先封存raw response與sample paths artifacts，再驗fence/identity/length/timestamps/OHLCV/extreme jump並映射research-only `ForecastSignal`；invalid output只回structured failure。
  - [x] Golden archived-artifact replay、CPU/GPU tolerance、schema snapshots、core無torch與full gate通過後同步文件並提交。

- [x] **P3.8 Kronos evaluation and promotion** — 以golden跨市場snapshots執行walk-forward、baseline、成本與calibration報告；建立`config/strategies/kronos.yaml`。（Depends：P3.4、P3.7；Complexity：XL；Risk：High）
  - 未達預先固定門檻時保持`shadow`/weight 0，不能為了整合而降低門檻。

- [x] **P3.9 Qlib quant-lab worker** — 建立`workers/quant_lab/{pyproject.toml,uv.lock,Dockerfile,README.md,app.py,qlib_adapter.py}`與`tests/contracts/workers/test_qlib.py`。（Depends：P3.1、P3.4；Complexity：XL；Risk：High）
  - `QuantResearchJob`只收immutable snapshot、feature/label/universe/cost/split specs；回傳predictions/positions/metrics/artifact hashes/provenance。
  - 不依賴已暫停的官方dataset；由canonical snapshot converter供應資料。

- [x] **P3.10 Forecast/signal/evaluation API and CLI** — 建立`entrypoints/api/routes/{strategies,signals,evaluations}.py`、`entrypoints/cli_commands/strategy.py`。（Depends：P3.2–P3.9；Complexity：L；Risk：Medium）
  - Promotion endpoint需`strategy_reviewer`權限，且不能建立live state。

### P3 Verification gate

- [x] Leakage/PIT/survivorship fixtures故意污染時evaluation必須失敗。（Depends：P3.4）
- [x] Opinion mapper default disabled；未評估opinion、Kronos或PEAD signal權重必為0。（Depends：P3.5、P3.8）
- [x] Kronos archived-artifact replay、CPU smoke、可用時GPU schema/tolerance smoke、model checksum與calendar/validity tests通過；fresh stochastic inference不作bit-identical gate。（Depends：P3.6–P3.8）
- [x] Qlib deterministic job同snapshot/runtime可重播相同artifact hashes；任何stochastic model則重播封存output artifact，worker無core DB/execution credentials。（Depends：P3.9）
- [x] Strategy promotion/suspend/retire均有immutable audit event與CAS conflict tests。（Depends：P3.2、P3.10）

### P3 Success criteria

- [x] 每個可參與paper portfolio的signal都有strategy/evaluation/data/runtime provenance。
- [x] Forecast與agent opinion只能經明確promotion path影響target。
- [x] 重型ML/quant dependencies完全留在各自worker environment。

---

## P4 — Deterministic portfolio、risk、paper execution 與 ledger

### Outcome

把P0已驗證的in-memory閉環升級為唯一PostgreSQL-backed canonical paper fund；portfolio/risk/account reservation/execution/balanced journal全部deterministic、idempotent、可重播，且與LLM/AI-Trader/OpenBB權限隔離。

### Tasks

- [x] **P4.1 Portfolio/risk/reservation/execution domain** — 將P0 primitives升級為`domain/{portfolio,risk,reservations,orders,fills,journal}.py`、`ports/{portfolio_policy,risk_policy,execution,ledger}.py`與property tests。（Depends：P3 gate；Complexity：XL；Risk：High）
  - [x] Decimal、rounding、order state transitions、sequence/hash與accounting invariants明確。
  - [x] `RiskDecision`綁account aggregate/portfolio sequence並有expiry；sequence變更必須重評。Risk approval本身不保留資金，必須建立reservation才可形成command。

- [x] **P4.2 Trading persistence** — 新增`migrations/versions/0010_paper_trading.py`、repositories與DB permissions。（Depends：P4.1、P1.4；Complexity：XL；Risk：High）
  - [x] 建立account aggregate、cash/position reservations與projections、risk decisions、order intents/events、fills、balanced journal transactions/postings、kill switch。
  - [x] Order idempotency、event sequence與append-only由DB constraint保護。

- [x] **P4.3 Deterministic portfolio baseline** — 建立`application/portfolio/build_target.py`、`config/policies/portfolio_v1.yaml`與golden/property tests。（Depends：P3.1、P4.1；Complexity：L；Risk：High）
  - [x] Fixed ensemble weights、confidence calibration、deadband、shrinkage、turnover penalty、position bounds與stable ordering。
  - [x] Missing signal不重新正規化造成過曝；輸出calculation hash與cost diagnostics。

- [x] **P4.4 Hard risk gate** — 建立`application/risk/evaluate.py`、`config/policies/risk_v1.yaml`與boundary tests。（Depends：P4.1、P4.3；Complexity：XL；Risk：High）
  - [x] 檢查data/signal freshness、cash、pending orders、single/sector/asset/gross/net、turnover、ADV、market session、drawdown/daily loss、kill switch。
  - [x] Unknown/conflict/stale required state、unsupported asset/order或ledger mismatch一律reject。
  - [x] Risk核准到`OrderIntent`建立必須在per-account serialized transaction/advisory lock內重新確認sequence並原子reserve cash/sellable position；所有open reservations納入available state。

- [x] **P4.5 Reference paper broker** — 建立`adapters/execution/paper.py`、`domain/execution_model.py`、`config/execution/paper_v1.yaml`與golden fill tests。（Depends：P4.1、P4.4；Complexity：XL；Risk：High）
  - [x] Market/limit/expiry/partial fill、fees/slippage/spread/volume participation語義版本化。
  - [x] 只用command後第一個可交易bar；無future bar不製造fill。
  - [x] 同idempotency key + same payload回同receipt；same key + different payload fail closed。
  - [x] 每個accepted command必須帶有效reservation；fill/cancel/reject/expire按account序列化並原子consume/release。

- [x] **P4.6 Balanced journal and projections** — 建立`application/ledger/{post,replay,reconcile}.py`、`adapters/postgres/ledger_repository.py`與property tests。（Depends：P4.2、P4.5；Complexity：XL；Risk：High）
  - [x] 每筆transaction至少兩個postings；每種currency/commodity經Decimal quantization後debit/credit sum為零，明確使用cash/inventory/fee/PnL/clearing accounts。
  - [x] Cash/positions/fees/P&L只由journal推導；daily replay hash與DB projection一致。不平、gap、unknown order state觸發rollback與global paper kill switch。

- [x] **P4.7 End-to-end workflow state machine** — 完成`application/workflows/run_cycle.py`各transition/retry/cancel/dead-letter；建立`tests/e2e/test_paper_fund_cycle.py`。（Depends：P1.6、P2.11、P3.10、P4.3–P4.6；Complexity：XL；Risk：High）
  - [x] Flow固定`Evidence -> Research/Opinion/Signal -> PortfolioTarget -> RiskDecision -> OrderIntent -> ExecutionReceipt -> Ledger -> Report`。
  - [x] Execution retry先query idempotency receipt；crash injection不得重複下單。

- [x] **P4.8 Outcome monitoring and reflection evidence** — 建立`application/monitoring/{mark_to_market,outcomes,reflection_context}.py`與tests。（Depends：P4.6、P2.8；Complexity：L；Risk：Medium）
  - [x] 保存raw return、benchmark alpha、drawdown、fees、fills與outcome evidence；LLM reflection只是新ResearchArtifact，不改歷史decision。

- [x] **P4.9 Kill-switch and operator use cases** — 建立`application/operations/{activate_kill_switch,reconcile,resume}.py`、CLI/API routes與audit tests。（Depends：P4.4–P4.7；Complexity：L；Risk：High）
  - [x] 啟動後拒絕新commands並取消可取消pending orders；不刪ledger或隱藏fills。
  - [x] Resume必須reconciliation通過且由`paper_operator`/`admin` audited action完成。

- [x] **P4.10 Portfolio/report projections** — 更新`AnalysisReport`加入target/risk/order/fill/outcome refs；建立portfolio/NAV/risk CLI/API projections。（Depends：P4.7、P4.8；Complexity：M；Risk：Medium）

### P4 Verification gate

- [x] Property tests覆蓋position/cash、open reservations、concurrent double-spend/oversell、risk bounds、Decimal rounding、order transition與每資產journal平衡。（Depends：P4.1–P4.6）
- [x] 同snapshot/config重跑得到相同target/risk/order/fill/ledger hashes。（Depends：P4.7）
- [x] Crash/retry/duplicate/concurrent-account tests無重複paper order/fill、雙花或超賣；late worker result無法commit。（Depends：P4.4–P4.7）
- [x] Stale/conflict/unknown/kill-switch/ledger mismatch fixtures全部reject並留下reason/audit。（Depends：P4.4、P4.9）
- [x] E2E small portfolio完成report、replay、reconciliation，且AI-Trader/LLM/Kronos無execution credential/path。（Depends：P4.7–P4.10）

### P4 Success criteria

- [x] Paper fund從schedule/API到ledger/report形成可重播閉環。
- [x] 零重複paper orders、零future-data fills、零LLM risk override。
- [x] 所有portfolio與report projection可由immutable events/artifacts重建。

---

## P5 — Optional ecosystem integrations 與 advanced evaluation

### Outcome

在不改canonical semantics與paper-only邊界下，接入AI-Trader external community/control API、Nautilus/LEAN simulation backends與sandboxed RD-Agent strategy lab。所有integration可關閉、可替換、各自獨立lock/image。

### Tasks

- [x] **P5.1 External platform contracts** — 擴充`packages/contracts/src/stonks_contracts/platform.py`、`ports/platform.py`與schemas；定義publish thesis、poll feedback、challenge/experiment與external evidence。（Depends：P4 gate；Complexity：M；Risk：High）
  - 不定義submit order/copy trade為canonical operation；remote positions只能是external evidence。
  - [x] TDD：先固定public redaction、immutable artifact/hash、PIT、cursor/page dedup、untrusted/evidence-only與authority-free invariants。
  - [x] `PlatformPort`只提供publish/poll/challenge/experiment typed `Result`；不得依賴execution、DB或queue ports。
  - [x] 匯出versioned JSON schemas，focused/full gate通過後同步README/CONTEXT與本review並提交。

- [x] **P5.2 AI-Trader public HTTP adapter** — 建立`adapters/platform/ai_trader.py`、`config/platforms/ai_trader.yaml`、runtime-schema cassettes與contract tests。（Depends：P0.3、P5.1；Complexity：L；Risk：High）
  - 只使用external control/community endpoints：publish去敏thesis、discussion/reply、challenge/team/experiment、heartbeat/events。
  - 不import/vendor/clean-room重做其server/frontend，不呼叫paper/copy execution作canonical order path。
  - Typed tolerant reader、heartbeat cursor/inbox dedup、scoped token、schema/authz anomaly kill switch。
  - [x] TDD：固定exact HTTPS origin/endpoint allowlist、no redirect/no automatic POST retry、bounded JSON、token redaction與typed tolerant responses。
  - [x] Publication/feedback/challenge/experiment綁raw response artifact；heartbeat events具opaque cursor與注入式inbox dedup，duplicate/conflict fail closed。
  - [x] 以`d03ff6c` runtime shape建立最小clean-room cassettes；live OpenAPI無法解析時明確保留unverified狀態，不把snapshot推測成current production保證。

- [x] **P5.3 Community feedback policy** — 建立`application/research/community_feedback.py`、reputation/deadline/prompt-injection fixtures。（Depends：P5.2、P2.2；Complexity：L；Risk：High）
  - Feedback轉`ExternalEvidence`，policy只可ignore、降低confidence或建立new research job；不可直接升成signal/order。
  - [x] TDD：固定closed observation window、PIT/scope/dedup、reputation threshold、one-author-one-weight與deterministic confidence haircut。
  - [x] Prompt-injection/high-risk content只能被隔離；new research question/payload只含固定instruction、typed identifiers與evidence IDs，不含remote原文。
  - [x] 只有rerun action可經narrow typed `JobEnqueuePort`建立research-only job；ignore/haircut零side effect，輸出契約不存在signal/order/risk authority。

- [x] **P5.4 Backtest engine contract** — 擴充`BacktestJob/Result` schemas與`ports/backtest_engine.py`；建立canonical orders/fills/positions/calendar/cost parity suite。（Depends：P4.1、P4.6；Complexity：L；Risk：High）
  - [x] TDD：固定immutable dataset/calendar/session/bar、instrument quantum、initial cash/positions、simulation-only order與explicit cost-model hashes。
  - [x] Result exact綁job/runtime/generation/nonce/input hashes，並重驗next-bar fills、fees/slippage、order outcomes、cash/position reduction與stable artifact hash。
  - [x] `BacktestEnginePort`只回canonical result；core boundary將invalid/late/engine-specific output轉structured failure，reference/fake-engine parity與replay fixtures通過。

- [x] **P5.5 NautilusTrader adapter** — 建立`sidecars/nautilus/{pyproject.toml,uv.lock,Dockerfile,README.md,app.py}`與contract/replay tests。（Depends：P0.3、P5.4；Complexity：XL；Risk：High）
  - LGPL runtime/types不滲入core；記錄engine/runtime/license/version與完整fills。
  - [x] TDD：固定Nautilus `1.230.0` / commit `8160730c`、LGPL dynamic-library boundary、獨立lock/image、runtime hash與bounded HTTP envelope。
  - [x] Canonical mapper只接受`engine=nautilus` exact runtime；以synthetic open quote + scheduled order在真正`BacktestEngine`重播，保存每筆engine fill ID/raw hash並映射完整canonical outcomes/fills。
  - [x] Core仍重驗P5.4 next-bar/cost/projection；sidecar無DB/provider/queue/broker credentials，invalid/late/unsupported/runtime drift皆structured fail closed。
  - [x] Fake-runtime contract/replay、真實wheel smoke、container hardening、lock/CVE/license checks與完整phase gate通過後同步文件。

- [x] **P5.6 LEAN adapter** — 建立`sidecars/lean/{Dockerfile,README.md,appsettings.template.json,adapter/}`與job/result contract tests。（Depends：P0.3、P5.4；Complexity：XL；Risk：High）
  - C#/Docker保持external sidecar；calendar/corporate action/fees/slippage mapping明確。
  - [x] TDD：固定官方LEAN source/tag/commit、license/corresponding source、.NET/runtime image與獨立dependency lock/provenance。
  - [x] Canonical mapper只接受`engine=lean` exact runtime，真實engine replay只能回authority-free trace；core重新驗P5.4 schedule/economics/projection。
  - [x] Sidecar無DB/provider/queue/broker credentials；request/work/concurrency/deadline與process/resource bounds fail closed，default no egress。
  - [x] Fake-runtime contract/replay、真實engine/container smoke、SBOM/CVE/license/source checks與完整phase gate通過後同步文件。

- [x] **P5.7 Cross-engine parity evaluation** — 建立`tests/parity/`、`scripts/smoke_engine_parity.py`與`application/evaluation/engine_parity.py`。（Depends：P4.5、P5.5、P5.6；Complexity：XL；Risk：High）
  - 差異超預設threshold時標engine-specific，不能把結果平均或宣稱等價。
  - [x] P5.4 prerequisite regression：halted首個calendar opportunity會消耗IOC；極端cost/Decimal只產生validated failure；LEAN能在首根bar前提交GTC/IOC partial order。
  - [x] TDD：建立frozen/content-hash parity policy、validated job/result observation、pairwise delta與report contracts；reference為唯一baseline。
  - [x] 逐order/fill/cash/position對齊並解釋status、quantity、price、fee與projection差異；semantic/provenance分類stable且不平均結果。
  - [x] Reference/Nautilus/LEAN共用MARKET/LIMIT、DAY/GTC/IOC、BUY/SELL、partial/shared-volume與multi-session fixtures；真實sidecar replay保存exact runtime/image/result provenance。
  - [x] Sidecar disabled/failed回structured failure且不產生parity report；core無heavy runtime仍通過，完整phase gate後同步文件。

- [x] **P5.8 RD-Agent sandbox worker** — 建立`workers/quant_lab/rd_agent/{Dockerfile,uv.lock,README.md,sandbox_policy.yaml,adapter.py}`與escape/reproducibility tests。（Depends：P0.3、P3.4、P3.9；Complexity：XL；Risk：High）
  - Linux-only、ephemeral、read-only dataset、no core secrets、default no egress、CPU/RAM/time限制。
  - 只輸出draft source/artifacts/evaluation request；核心重新靜態掃描與完整evaluation，絕不auto-promote。
  - [x] TDD：新增frozen/hash-bound proposal、sandbox job/result、draft artifact/evaluation contracts與static-scan policy；禁止target/order/promotion authority。
  - [x] 實作固定entrypoint、AST allowlist、雙fresh-process replay、timeout/output/memory/process bounds與structured failures；dataset不暴露label且read-only。
  - [x] Core重新驗fence/runtime/source/policy、重跑相同static scan並以P3.4完整evaluation產report；即使passed也不變更registry state。
  - [x] Pinned RD-Agent MIT source/NOTICE/獨立lock/image與Linux actual escape/network/resource/non-reproducibility smoke、SBOM/CVE/license gates全通過。

- [x] **P5.9 Optional integration manifests and feature flags** — 建立`config/features.yaml`、`infra/compose.optional.yaml`、`docs/runbooks/optional-integrations.md`。（Depends：P5.2、P5.5、P5.6、P5.8；Complexity：M；Risk：Medium）
  - 所有optional integration default off；未配置不影響core readiness。
  - Freqtrade、FinRL、vectorbt只保留future RFC條目，不在本phase安裝。
  - [x] TDD：frozen typed catalog/flags、缺檔all-disabled fallback、malformed/unknown/live-authority fail closed。
  - [x] 所有可部署integration映射exact Compose profile；零default-active service、無core dependency/readiness coupling。
  - [x] 每個image catalog綁定獨立lock、notice、source/license、SBOM與CVE policy；future RFC不得有image/profile/dependency。
  - [x] Linux Compose render、runbook、CI、完整phase gate與文件同步全通過。

### P5 Verification gate

- [x] AI-Trader adapter測試無order/copy endpoint且不在execution dependency graph；duplicate heartbeat/events去重。（Depends：P5.2）
- [x] Community prompt injection/reputation/deadline tests證明feedback不能直接成signal/order。（Depends：P5.3）
- [x] Nautilus/LEAN parity fixtures產生可解釋差異與完整provenance；任一sidecar關閉時core仍通過。（Depends：P5.7）
- [x] RD-Agent sandbox escape、network、resource、malicious candidate與non-reproducible artifact fixtures全部fail closed。（Depends：P5.8）
- [x] 每個image有獨立lock、SBOM、license notice/source manifest，core lock無新增重型依賴。（Depends：P5.9）

### P5 Success criteria

- [x] External community、quant lab與simulation engines能增加evidence/evaluation能力但無法改寫canonical control plane。
- [x] Optional services可單獨部署、升級、停用，不改domain contracts或paper safety。
- [x] 授權不清/strong-copyleft元件均維持正確external boundary與release流程。

---

## P6 — Security、observability、resilience 與 release hardening

### Outcome

把P0起即存在的permission checks、redaction、telemetry、idempotency/fencing與failure tests替換/擴充為production-grade OIDC、secret manager、exporters、fault drills、deployment與supply-chain gates；仍不開live trading。此phase不得成為前面各phase缺少基本security/reliability的藉口。

### Tasks

- [x] **P6.1 Production OIDC/RBAC and service identities** — 將P0 local principal/permission port接到`adapters/auth/oidc.py`、`entrypoints/api/dependencies/auth.py`、`config/rbac.yaml`並擴充authn/authz tests。（Depends：P0.9、P4 gate；Complexity：XL；Risk：High）
  - Roles：viewer/researcher/strategy_reviewer/paper_operator/admin；worker/executor service accounts最小權限。
  - Object/target ownership與route-level authz完整，不能重現AI-Trader任意target agent問題。
  - [x] TDD：typed/frozen principal、human RBAC、service identity與target ownership policy；unknown role/claim/permission fail closed。
  - [x] Pinned asymmetric OIDC/JWKS驗證issuer/audience/azp/alg/kid/signature/exp/iat/nbf/jti與bounded claims；rotation/outage不得沿用錯誤key或洩漏token。
  - [x] `config/rbac.yaml`固定human/service最小權限與claim allowlist；local token仍只限loopback development，production default deny。
  - [x] Production auth composition依runtime environment只接受完整OIDC設定；缺mode/claim policy/JWKS設定時startup fail closed。
  - [x] Data/research/strategy/paper DB CLI只允許明確local/development/test環境，staging/production與未宣告環境皆不得建立DB連線或內建principal。
    - Review（2026-07-16）：新增runtime auth factory，非local環境只接受完整asymmetric OIDC/JWKS + RBAC policy設定；local預設deny-all，local token仍須explicit mode。四組DB CLI改為environment gate後才建立exact-target principal/DB connection，operator audit明示local admin；29個boundary tests、89個auth/CLI focused regression、5個真實PostgreSQL CLI tests全綠，新模組branch coverage 89.57%，Ruff、strict mypy、secret/upstream policy與locked dependency audit全通過。
  - [x] Central FastAPI dependency覆蓋所有routes，body/query/header不能偽造actor/role；401/403維持structured envelope。
  - [x] Account、strategy、evaluation、research run/report/snapshot target做application-level ownership/IDOR checks，admin bypass明示且可稽核。
  - [x] Worker/executor service identities不能取得operator/admin/reviewer或非assigned target authority；security matrix、full gate、文件同步全通過。

- [x] **P6.2 Secret provider and redaction** — 實作`ports/secret_provider.py`、`adapters/secrets/{env,cloud}.py`、structured log/event/report redaction tests。（Depends：P6.1；Complexity：L；Risk：High）
  - Local env只存named refs；正式deployment使用secret manager、rotation與scoped identities。
  - [x] TDD：transport-neutral frozen `SecretRef`、non-serializable `ResolvedSecret`與runtime-checkable provider port；raw value不得出現在repr/str/model dump/error。
  - [x] Exact-ref/purpose env與cloud strategies：env只限local/development/test；staging/production只接受workload-identity cloud client，missing/blank/disabled/stale/wrong-scope/outage無fallback且fail closed。
  - [x] OpenAI、Anthropic、Financial Datasets與AI-Trader在每個logical request解析一次secret；bounded retry固定同version，下個request取得rotation，provider failure發生在任何network/artifact write前。
  - [x] Pure bounded redaction policy涵蓋nested structures、credential URL/JWT/PEM/provider key、known value、exception與cycle/depth/size；原輸入不可修改，金融symbol/hash/UUID不得誤遮罩。
  - [x] Structured error/log/report在emit/store/render前sanitize；canonical job/event/outbox/inbox遇secret-shaped payload直接拒絕，不以API egress redaction掩蓋DB洩漏。
  - [x] Focused、full non-PostgreSQL/PostgreSQL、secret/dependency gates與文件同步全通過；未接真實cloud manager時不得宣稱live production integration。

- [x] **P6.3 API security controls** — 實作request size/rate limit、CORS allowlist、SSRF endpoint allowlist、XSS-safe rendering、cookie模式CSRF與structured error sanitization。（Depends：P6.1、P6.2；Complexity：L；Risk：High）
  - [x] 建立單一typed API security policy/composition；所有FastAPI app套用streaming request limit、bounded rate limit、exact CORS allowlist、安全response headers與fail-closed proxy identity。
  - [x] Rate limit key只信任已驗證principal或直接peer；拒絕偽造forwarded header，回傳一致envelope、`429`與bounded `Retry-After`，並提供deterministic clock/storage contract tests。
  - [x] 建立outbound endpoint allowlist/SSRF guard；只允許exact scheme/host/port/path，拒絕userinfo、fragment、redirect pivot、DNS rebinding、loopback/private/link-local/multicast/unspecified/reserved位址。
  - [x] HTML/Jinja維持autoescape並加CSP等headers；cookie auth opt-in時強制same-origin與double-submit CSRF，bearer-only模式拒絕ambient auth cookie。
  - [x] 安裝全域structured exception handlers；validation/HTTP/internal error先sanitize且不得回傳stack、raw body、secret、internal exception或敏感header。
  - [x] 建立`tests/security/`涵蓋auth bypass、IDOR、prompt injection、SSRF、XSS、CSRF、secret leakage、rate-limit bypass與oversize streaming body。
  - [x] 完成focused/full non-PostgreSQL/PostgreSQL、security/secret/dependency gates與文件同步；未接distributed rate-limit store或trusted reverse proxy時不得宣稱multi-replica production enforcement。

- [x] **P6.4 Production OpenTelemetry exporters and metrics** — 將P0 trace/log/metric ports接到`adapters/observability/{logging,tracing,metrics}.py`、`infra/observability/{otel-collector,prometheus,grafana}/`。（Depends：P0.9、P1.6、P4.7；Complexity：L；Risk：Medium）
  - Correlation IDs、provider/queue/worker/LLM/model/signal/risk/execution/reconciliation/delivery metrics完整。
  - Logs不得包含raw secrets、tokens、unredacted sensitive evidence或完整prompts。
  - [x] TDD固定frozen trace carrier、low-cardinality metric catalog與typed trace/metric ports；attribute/label allowlist拒絕raw ID、symbol、account、user、prompt、URL、exception與secret-shaped值。
  - [x] 實作OpenTelemetry SDK tracing/metrics/log correlation adapters與validated runtime config；local no-op、OTLP HTTP export、shutdown/flush、export failure不得影響canonical transaction。
  - [x] 五個FastAPI app加入request span/correlation middleware；只接受canonical W3C trace context，response回bounded correlation ID，validation/error/rate-limit paths亦可觀測且不洩漏輸入。
  - [x] Job/outbox/inbox與worker boundaries durable保存獨立trace context欄位並跨process延續；trace欄位不進canonical payload/content hash，舊generation/nonce/lease仍不得commit。
  - [x] 對provider、queue/worker、LLM/model、signal、risk、execution、reconciliation、delivery做manual instrumentation；固定operation/status/provider-kind等低基數dimensions與latency/error/counter/histogram。
  - [x] 建立hardened OTEL Collector、Prometheus、Grafana設定與provisioned dashboard；預設只bind loopback/internal network，無anonymous admin、無host metrics/raw logs，resource limits與health/readiness明示。
  - [x] 完成contract/API/PostgreSQL/E2E/collector smoke、cardinality/redaction/failure tests及full gates；未做real remote backend與multi-host TLS時不得宣稱production telemetry transport完成。

- [x] **P6.5 Alerts, budgets and SLOs** — 建立`config/{budgets,slo}.yaml`、`docs/operations/slo.md`、alert rules。（Depends：P6.4；Complexity：M；Risk：Medium）
  - Correctness SLO：zero duplicate paper order、zero future evidence、100% claim provenance、100% replayable risk decision。
  - Cost/latency budget超限轉degraded/failed，不追單。
  - [x] 先以schema/tests固定versioned SLO與budget設定；拒絕unknown field、重複metric、無界限值、未知action，以及未宣告correctness invariant。
  - [x] 建立immutable budget evaluation contract；使用monotonic elapsed time與Decimal cost，`within/degraded/failed`只可等級上升，缺失或無效usage fail closed。
  - [x] 將research/paper cycle的cost/latency hard-stop接到canonical flow；超限不得建立新target、reservation、order或補追訂單，既有commit結果不可被observer改寫。
  - [x] 為duplicate paper order、future evidence、claim provenance、risk replayability與availability/latency/cost建立低基數metrics及Prometheus recording/alert rules。
  - [x] 告警規則需通過`promtool`真實驗證與fixture tests；correctness違規立即page，budget burn/availability/latency採明確for/window/severity且無raw ID label。
  - [x] 完成SLO定義、error-budget policy、告警路由與runbook；明列目前單機/非持久Prometheus限制，不宣稱production paging backend已完成。
  - [x] 完成focused、full non-PostgreSQL/PostgreSQL、security/license/dependency gates與文件同步。

- [x] **P6.6 S3-compatible artifact adapter and retention** — 實作`adapters/artifacts/s3.py`、retention/encryption/GC use cases與integration tests。（Depends：P1.5、P6.2；Complexity：L；Risk：High）
  - Object finalize/hash、signed scoped URLs、orphan GC、legal/data retention與restore測試。
  - [x] 先以tests固定strict artifact-storage config、frozen encryption/retention/capability/GC/restore contracts與最小S3 dependency；拒絕ambient credentials/config、未知endpoint/prefix、無versioning/object-lock、無界size/TTL/retention與unsafe bypass。
  - [x] S3 finalize使用content-addressed exact keys、SHA-256 checksum與conditional/idempotent writes；object先驗證才發布immutable manifest，partial upload只成為不可引用orphan，concurrent metadata conflict fail closed。
  - [x] Read/head需重驗manifest、size、hash、encryption與retention；signed URL只允許單一finalized object、GET、短效TTL與固定bucket/key/origin，capability不得進log/event/DB。
  - [x] Retention policy依sensitivity套用明確SSE與WORM retain-until；legal hold只能經typed operator use case延長/啟用，不能靜默縮短、解除或使用governance bypass。
  - [x] Orphan GC只處理超過grace且未finalize／未註冊的exact prefix objects；canonical DB reference、legal hold、retention、unknown state或list/head/delete failure一律保留並留下structured audit。
  - [x] Restore只在versioned bucket上移除exact delete marker或讀回指定受信version，重驗hash/size/manifest後才成功；不能以覆寫新bytes偽裝restore。
  - [x] 完成fake failure matrix、真實digest-pinned S3-compatible integration smoke、PostgreSQL audit/retention integration、security/license/dependency與完整non-PostgreSQL/PostgreSQL gates。
  - [x] 同步artifact operations/runbook、README、`AGENTS.md`/`CLAUDE.md`、`CONTEXT.md`、lessons與本review；明列未連真實cloud KMS/IAM及不同S3 vendor相容範圍。

- [x] **P6.7 Deployment manifests** — 建立`infra/compose.yaml`、core `Dockerfile`、worker/sidecar profiles、health/readiness probes與non-root/read-only filesystem settings。（Depends：P5.9、P6.1–P6.6；Complexity：XL；Risk：High）
  - Default profile只啟動core/PostgreSQL；optional services顯式profile，OpenBB source流程一併發布。
  - `execution_mode=live`在任何manifest/schema都不存在。
  - [x] 先以policy/contract tests固定deployment settings、secret-file DB composition、liveness/readiness envelope與exact migration-head判定；拒絕raw DB URL/password、unknown environment/mode、unbounded timeout及schema drift。
  - [x] 建立digest-pinned Python 3.12 multi-stage core `Dockerfile`；只安裝frozen production lock，runtime為UID/GID 65532、read-only-compatible、無compiler/package manager cache、無heavy upstream dependencies，並保存OCI source/revision metadata。
  - [x] 建立typed `stonks-deploy` migrate/serve/probe commands；migration持有獨立one-shot authority，API只使用runtime credential，liveness不依賴DB、readiness需DB連線且`alembic_version` exact等於packaged single head。
  - [x] 建立`infra/compose.yaml`：runtime只有core/PostgreSQL，migration為同core image one-shot init job；DB不發布host port，core只綁loopback ingress，服務採non-root/read-only/cap-drop/NNP、tmpfs、resource/PID limits、healthchecks與bounded restart policy。
  - [x] 所有DB/password/OIDC/provider值只可由external secret file或明確allowlisted environment注入；Compose config/image history/log不得出現secret，core/migrator/worker不得取得不需要的provider、broker或live authority。
  - [x] 保持P5.9 optional catalog default-off；main與`compose.optional.yaml`合併後所有explicit profiles可render，缺少任何optional service不影響core readiness，OpenBB profile仍帶AGPL corresponding-source surface。
  - [x] 建立真實Compose smoke：乾淨volume啟動→migration→health/readiness→同image deterministic fake/replay→core restart→PostgreSQL restart→readiness恢復→migration idempotent→graceful shutdown；stale schema、DB outage與secret缺失皆fail closed。
  - [x] CI加入core image build與deployment smoke；完成focused、full non-PostgreSQL/PostgreSQL、security/license/dependency gates及文件同步，明列單host loopback/Compose與尚未驗證的external IdP、TLS/mTLS、orchestrator/network-policy邊界。

- [x] **P6.8 Supply-chain release gates** — 建立`.github/workflows/{security,release}.yml`、`scripts/{generate_sbom,verify_release}.py`、container signing與license/CVE policies。（Depends：P0.3、P6.7；Complexity：L；Risk：High）
  - Critical CVE、license drift、missing notice/source、unlocked dependency、secret scan failure阻擋release。
  - [x] 先以policy/contract tests固定versioned release policy、deterministic manifest、allowlisted bundle paths與bounded file/count/size limits；path traversal、symlink、case collision、unknown/duplicate entry及hash/size drift皆fail closed。
  - [x] Release bundle必須包含core與獨立runtime lockfiles、schemas/OpenAPI snapshots、SBOM、CVE/license/secret/upstream reports、`LICENSE`/`THIRD_PARTY_NOTICES.md`、OpenBB exact corresponding source/offer/manifests與verification report；缺件或未鎖依賴不得發布。
  - [x] `generate_sbom.py`只接受exact image digest與digest-pinned Syft，產生CycloneDX SBOM及deterministic normalized inventory；mutable tag、invalid/oversized output、identity mismatch與unknown license皆拒絕。
  - [x] `verify_release.py`重算所有hash/size、重驗CycloneDX、CVE severity、license/source/notice、lock與paper-only boundary；High/Critical CVE、secret finding、license drift或OpenBB source不完整皆fail closed。
  - [x] Security workflow以least privilege執行frozen lock、secret/upstream/license、dependency與digest-pinned image SBOM/CVE gates；actions與scanner images固定immutable SHA/digest。
  - [x] Release workflow只簽署registry回傳的exact image digest，使用GitHub OIDC keyless Cosign與build provenance/SBOM attestations；驗證固定issuer與repository workflow identity，PR/fork不得取得signing或package write authority。
  - [x] 本機unsigned candidate只能證明結構、內容與policy gates，不宣稱signature/provenance；正式release缺少可驗證signature/attestation bundle即失敗。
  - [x] 完成focused、真實core image SBOM/CVE/license/secret/release smoke、完整non-PostgreSQL/PostgreSQL gates與文件同步。

- [x] **P6.9 Failure-injection and disaster drills** — 建立`tests/resilience/`、`docs/runbooks/{provider-outage,worker-crash,db-restore,ledger-mismatch,kill-switch,dead-letter}.md`。（Depends：P6.4–P6.7；Complexity：XL；Risk：High）
  - 演練provider/LLM/model/sidecar outage、DB restart、lease expiry、duplicate event、artifact corruption、ledger mismatch與restore/replay。
  - [x] 先以frozen drill matrix與policy tests固定failure class、injection point、expected terminal/degraded state、forbidden side effect、audit/metric evidence與recovery precondition；unknown/partial result不得算通過。
  - [x] 建立`tests/resilience/`跨層fault fixtures，證明provider/LLM/model/sidecar outage不建立錯誤target/order，artifact corruption不能重播，duplicate/stale event與worker result不重複commit。
  - [x] 演練worker crash、lease expiry、generation/nonce fencing、retry exhaustion與dead-letter；receipt commit後crash仍只能有一筆fill/journal/receipt，dead-letter不得自動追單。
  - [x] 演練ledger mismatch、reconciliation failure與global/account kill switch；任何drift先rollback再audited fail-safe，resume必須完整replay通過且不能刪除既有fill/journal。
  - [x] 建立digest-pinned PostgreSQL實際backup/restore drill：DB restart、bounded dump、fresh target restore、Alembic head、row/hash-chain/replay與append-only constraints重驗；來源/目標混用或驗證失敗即丟棄restore。
  - [x] 建立六份operator runbook與machine-readable drill report；記錄RTO/RPO measurement但不在未完成P6.10負載基準前宣稱production SLA。
  - [x] 將resilience/restore smoke接入least-privilege CI，完成focused、完整non-PostgreSQL/PostgreSQL/security gates與文件同步。

- [ ] **P6.10 Performance and resource budgets** — 建立`tests/performance/`與`docs/operations/capacity.md`，量測API、queue、snapshot、research、forecast、paper cycle；設定per-process CPU/RAM/concurrency budget。（Depends：P6.4、P6.7；Complexity：L；Risk：Medium）
  - 重型workers不能飢餓risk/execution；LLM/forecast queues獨立限流。

- [ ] **P6.11 Final docs and handoff sync** — 完成`README.md`、`docs/architecture/` ADRs、`docs/api/`、`docs/runbooks/`、`THIRD_PARTY_NOTICES.md`，同步精簡`AGENTS.md`、`CLAUDE.md`、`CONTEXT.md`、`tasks/todo.md`、`tasks/lessons.md`。（Depends：P6.1–P6.10；Complexity：L；Risk：Medium）
  - 文件只列實際驗證能力與限制；所有command由CI/本機重跑確認。

### P6 Verification gate

- [ ] Full CI：frozen builds、lint/type/unit/contract/integration/E2E/security/resilience、SBOM/license/CVE/secret scans全部通過。（Depends：P6.8、P6.9）
- [x] Default compose由乾淨環境啟動、migrate、readiness、fake/replay E2E、shutdown/restart/replay全部通過。（Depends：P6.7）
- [ ] Optional profiles逐一smoke；缺任何optional service時core readiness與paper safety不受影響。（Depends：P5 gate、P6.7）
- [ ] Kill switch、ledger mismatch、duplicate execution、future evidence與auth bypass drills全部fail closed並告警。（Depends：P6.1–P6.9）
- [ ] Release bundle含lockfiles、schemas/OpenAPI、SBOM、signatures、notices、OpenBB對應source流程與驗證報告。（Depends：P6.8、P6.11）

### P6 Success criteria

- [ ] Staff-level review可由tests、traces、audit與replay證明paper platform正確性。
- [ ] 任一LLM/model/provider/optional ecosystem失效不會製造錯誤交易或破壞ledger。
- [ ] 發布物仍為paper-only，且所有第三方code/data/model授權與provenance可稽核。

---

## Cross-phase invariants

- [ ] Core dependency graph永遠不含OpenBB、PyTorch、TradingAgents、Qlib、RD-Agent、Nautilus或LEAN runtime packages。
- [ ] AI-Trader永遠只作external control/community adapter，不是research worker、executor或ledger。
- [ ] Canonical flow永遠使用`AgentOpinion/AlphaSignal/ForecastSignal -> PortfolioTarget -> RiskDecision -> OrderIntent -> ExecutionReceipt`，不引入模糊`TradeIntent`。
- [ ] 所有external side effect均有idempotency key、outbox、receipt與audit event。
- [ ] Core job runner永遠是DB/event/outbox transaction owner；remote workers無DB credentials，generation/nonce不符或lease失效的late results只能隔離，不能commit。
- [ ] 同帳戶mutation永遠經serialized aggregate與reservation；balanced journal每種currency/commodity的postings必須平衡。
- [ ] Stochastic LLM/Kronos重播永遠從封存immutable output artifact開始；不以fresh re-inference bit-identical作正確性宣稱。
- [ ] 所有歷史研究/evaluation只讀`available_at <= as_of` evidence。
- [ ] 所有修改完成後同步精簡`AGENTS.md`、`CLAUDE.md`、`CONTEXT.md`與本todo review；使用者修正另記`tasks/lessons.md`。

## Review（實作時持續維護）

### Phase review template

每個phase gate通過時，在下方新增一段，不以口頭宣告取代證據：

```markdown
### Pn Review — YYYY-MM-DD

- Scope completed:
- Files / migrations / contracts changed:
- Verification commands and exact results:
- Replay / security / license evidence:
- Deviations from blueprint and ADR links:
- Remaining risks accepted for next phase:
- `AGENTS.md` / `CLAUDE.md` / `CONTEXT.md` sync status:
```

### Final review checklist

- [ ] P0–P6所有mandatory tasks與gates均有可重跑證據；optional service若因外部授權/credential不可live測試，已有cassette/contract test及明確限制。
- [ ] `git diff`只含有意變更；generated/cache/model/secrets/research clones未被提交。
- [ ] 主要branch與最終行為差異已有E2E/replay證明，不只通過單元測試。
- [ ] 所有schema/migration能向前升級；rollback/recovery限制已寫入runbook。
- [ ] 所有報告claim、signal、risk、order、fill與outcome可沿provenance/audit chain追溯。
- [ ] Security、license、CVE、SBOM、secret、source-offer gates通過。
- [ ] 專案文件與實際commands/ports/defaults一致，且README只宣稱已驗證能力。
- [ ] 確認沒有live broker adapter、live credential schema或可繞過paper-only boundary的設定。

### Review log

### Planning Review — 2026-07-10

- Scope completed：指定7案與Qlib/RD-Agent的snapshot、license、架構、public interface、風險與本機測試研究；完成blueprint與P0–P6計畫。
- Verification：9/9 snapshot identity與tracked worktree clean；82個GitHub evidence refs固定commit；cross-report authority/PIT/execution semantics最終PASS，詳見`docs/research/verification.md`。
- Safety decisions：P0完整fake/replay閉環、account reservation、balanced journal、core transaction ownership/late-result fencing、stochastic artifact replay、security/reliability shift-left。
- Files synchronized：`README.md`、`AGENTS.md`、`CLAUDE.md`、`CONTEXT.md`、`tasks/lessons.md`與研究索引已更新。
- Runtime implementation：尚未開始；`PLAN-AUTH`等待一次性確認，不能把研究clone或規格文件誤報成可執行產品。

### Plan Authorization — 2026-07-10

- 使用者已明確要求「依照計畫完成」；`PLAN-AUTH`成立，從P0開始連續實作。
- 核心預設授權採Apache-2.0，唯一execution mode為paper；live trading仍需另立RFC。

### P0 Review — 2026-07-11

- Scope completed：Python 3.12/uv foundation、versioned contracts/schemas、paper-only boundary、完整in-memory fake/replay、security/reliability baseline與Windows/Linux CI。
- Files / contracts changed：新增core與contracts packages、39個v1 schema snapshots、license/upstream manifest、quality/security scripts、CI及103個unit/contract/E2E tests。
- Verification：`uv sync --frozen --no-cache`可在全新venv安裝68 packages；`scripts/verify.py`為103 passed、branch coverage 91.73%、ruff/mypy/schema/license/secret/CVE全通過；actionlint 1.7.12通過。
- Replay / security / license evidence：獨立同seed run的control/projection hash一致；future evidence、並行雙花、late result、idempotency conflict、unknown execution state、未授權/非command execution與unbalanced journal全部fail closed；upstream violation與secret findings皆為0。
- Runtime smoke：`stonks fake-cycle`回傳HTTP-style status 200、`execution_mode=paper`、next-session fill 101.00與可重播projection hash。
- Deviations：contracts使用workspace根`uv.lock`，不保留容易漂移的nested lock；P0未啟動PostgreSQL/provider/LLM/sidecar，符合phase boundary。
- Remaining P1 risks：真實PostgreSQL migration/lease併發、PIT calendar、provider state taxonomy與artifact atomicity尚待P1 gate驗證。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`規範無需改動且SHA-256一致。

### P1 Review — 2026-07-12

- Scope completed：PIT instrument/evidence、PostgreSQL 0001–0008、repositories/UoW、content-addressed artifacts、DB-clock durable job/outbox/inbox、provider policy、US/HK/TW replay fixtures、Financial Datasets/OpenBB observation adapters、optional AGPL OpenBB sidecar，以及snapshot request/API/CLI與canonical replay completion。
- Verification：`scripts/verify.py --with-postgres --skip-audit`為635 passed、branch coverage 87.90%；195 files format、ruff、mypy 113 source files、schema、upstream/license、secret scan與Alembic drift全通過。
- Replay / security：相同request產生hash-identical manifest；future evidence/run links、stale generation/nonce、caller clock drift、expired lease、duplicate completion、tampered retry graph、oversized/compressed HTTP與reconciliation conflict皆fail closed。雙來源決策封存雙側raw/normalized hashes、metric/value、threshold與decision；conflict維持0 artifact writes並寫immutable failure audit。
- OpenBB / license：sidecar static policy 8/8、48 policy tests、64-package frozen lock無已知CVE；重建image後live historical smoke回2 rows、source archive驗5類hash，runtime為UID 65532、read-only rootfs、cap-drop ALL、no-new-privileges，exact allowlist之外回404。
- Honest boundary：Financial Datasets與OpenBB目前只宣稱read-only observation contracts；production canonical materialization source已驗證的是replay。`stonks-worker`目前只claim lease，完整常駐dispatcher不列為已完成能力。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review、內容與SHA-256仍一致。

### P2 Progress Review — P2.1 — 2026-07-12

- Scope completed：frozen evidence-scoped research/LLM contracts、immutable multi-dimensional usage budget、runtime-checkable research/LLM/tool ports，以及read-only tool authorization/result validation。
- Security：research principal無ambient filesystem/process/network/secret/queue/execution capabilities；principal/profile/policy、tool allowlist、typed arguments、instrument/evidence scope、timeout/output cap、audit redaction、result call/hash/byte limit皆fail closed。
- Verification：P2.1 focused為22 passed、branch coverage 92%；完整`scripts/verify.py`為486 passed、171 PostgreSQL tests deselected、branch coverage 87.50%，205 files format、ruff、mypy 119 source files、schema、upstream/license、secret與locked dependency audit全通過。
- Phase status：P2 gate尚未完成；下一項為P2.2 clean-room bounded research orchestrator。P2.1無migration或DB行為變更，因此本次未重跑P1 PostgreSQL suite。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。

### P2 Progress Review — P2.2 — 2026-07-12

- Scope completed：clean-room context builder、structured planning/final turn parser、bounded tool loop、parallel read-only batch與deterministic artifact builder；未複製Dexter source/prompt/assets。
- Security：context只讀request allowlist內且`available_at <= as_of`的immutable artifacts並一律標untrusted；tool batch先完成request/policy雙層scope authorization與usage reservation才執行，LLM/tool exception、deadline、invalid schema、oversize、out-of-scope citation皆structured fail closed。
- Verification：P2 research focused為31 passed、application/adapters branch coverage 88%；完整`scripts/verify.py`為500 passed、171 PostgreSQL tests deselected、branch coverage 87.62%，216 files format、ruff、mypy 125 source files、schema、upstream/license、secret與locked dependency audit全通過；Barrier test證明兩個read tools確實平行啟動。
- Phase status：P2 gate尚未完成；下一項為P2.3 LLM structured-output adapters。P2.2無migration或DB行為變更，因此本次未重跑P1 PostgreSQL suite。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。

### P2 Progress Review — P2.3 — 2026-07-12

- Scope completed：frozen `models-v1` provider/model allowlist、domain-owned redacted `SecretRef`、offline deterministic fake、OpenAI-compatible Chat Completions與Anthropic Messages structured-output adapters，以及共用artifact-first/schema/accounting/HTTP security primitives。
- Provider evidence：2026-07-12依官方文件固定OpenAI `response_format.json_schema/strict`與`gpt-4o-mini-2024-07-18`（$0.15 input、$0.075 cached input、$0.60 output），Anthropic採GA `output_config.format`、`claude-haiku-4-5-20251001`（$1 input、$1.25 cache write、$0.10 cache read、$5 output）；provider價格仍屬可變外部政策，修改須更新allowlist與contract tests。
- Security / replay：caller不能指定origin/endpoint/provider model；remote只允許credential-free HTTPS config、identity encoding、no redirects、bounded request/response/deadline與narrow transient retry。每個200 stochastic response在provider envelope/schema解析前封存exact raw bytes；refusal、model mismatch、late/empty/compressed/oversize/malformed output、impossible token details、archive failure與repair exhaustion皆structured fail closed，錯誤不回顯secret/provider body。
- Cost / repair：所有成功或invalid-but-billed attempts累計immutable input/output/cache token、cost與elapsed usage；invalid JSON/schema與token truncation最多repair一次，refusal/permanent HTTP failure不重試；failure details保留safe usage供audit，絕不把free-form內容當成功。
- Verification：P2.3 focused為50 passed、branch coverage 92.55%；完整`scripts/verify.py`為550 passed、171 PostgreSQL tests deselected、branch coverage 88.08%，228 files format、ruff、mypy 134 source files、schema、upstream/license、secret與locked dependency audit全通過且無已知CVE。
- Honest boundary：remote adapters使用official-wire mock transport，未使用真實OpenAI/Anthropic credentials做live smoke；P2 gate尚未完成，下一項為P2.4 TradingAgents isolated worker。P2.3無migration或DB行為變更，因此未重跑P1 PostgreSQL suite。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。

### P2 Progress Review — P2.4 — 2026-07-12

- Scope completed：建立獨立`workers/tradingagents` runtime、HTTP app、frozen request/response/policy/telemetry contracts、canonical evidence facade、pinned source-archive lock、hardened Dockerfile、三個profile獨立process compose與Apache NOTICE；core不import worker或heavy runtime。
- Upstream / authority：TradingAgents固定v0.3.1 commit `01477f9afb7a47b849ed4c9259d3a9a4738d9fda`；所有market/fundamental/news/social/macro/prediction tools在graph建構前替換為該request的PIT canonical evidence，pending yfinance outcome resolution停用。Trader、Portfolio Manager與risk debate只正規化為`AnalysisBundle + AgentOpinion`，response schema無target/risk/order/qty/execution authority，缺confidence明示為0且uncalibrated。
- Isolation / security：paper/backtest/production各自一process並serialize graph run，避免process-global config污染；只允許fixed internal `http://model-proxy:8000/v1`，upstream vendor routes設為`canonical_facade` fail closed。request evidence IDs必須exact scope、unique且`available_at <= as_of`；body streaming byte cap、identity encoding、deadline、profile、source refs、symbol與output均驗證。worker環境拒絕DB/Postgres/broker/Redis/queue與direct provider keys。
- Packaging / license：worker lock解析138 packages（runtime 114、其餘dev），TradingAgents使用同commit GitHub source archive避免image需Git；worker venv實際安裝與`TradingAgentsGraph` import成功。`TRADINGAGENTS-APACHE-2.0-WORKER` notice已登錄，完整Apache-2.0 license進image；worker dependency audit除本地`stonks-contracts`無PyPI條目而skip外無已知CVE。
- Verification：focused contract/security為20 passed、worker branch coverage 95.77%；完整`scripts/verify.py`為570 passed、171 PostgreSQL tests deselected、core branch coverage 88.08%，235 files format、ruff、mypy 134 source files、schema、upstream/license、secret與core locked audit全通過。Docker image `stonks-tradingagents-worker:test`成功重建；UID 65532、read-only rootfs、cap-drop ALL、no-new-privileges、network none health smoke通過，無model-proxy時analyze明確回503 `runtime_failed`且不偽造success/order。
- Phase status：P2 gate尚未完成；下一項為P2.5 TradingAgents core HTTP adapter與lease generation/attempt nonce late-result fencing。P2.4無migration或DB行為變更，因此未重跑P1 PostgreSQL suite。
- 文件同步：`README.md`、`THIRD_PARTY_NOTICES.md`、legal manifest、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。

### P2 Progress Review — P2.5 — 2026-07-13

- Scope completed：新增4個versioned shared wire schemas、signed evidence capability、fixed-origin core HTTP adapter、worker artifact resolver與core-owned `process_tradingagents_lease`；request/response帶request/run/job、generation、nonce與deterministic result artifact hash，research payload仍只有`AnalysisBundle + AgentOpinion`。
- Security / fencing：core與worker各自固定worker/artifact origins，禁止redirect/compression/任意URL，限制request/response/artifact bytes、deadline與bounded transient retry；artifact capability path/hash/expiry、profile、exact evidence scope、nested run/instrument/as-of/horizon/citations、response schema與hash全部fail closed。worker仍拒絕DB/Postgres/queue/broker/direct provider keys且沒有completion port。
- Transaction / late result：只有core `PostgresJobQueue.complete`可在重新驗證DB clock、lease owner、generation/nonce/deadline後，同一transaction註冊TradingAgents artifact metadata、append hash-chained event/outbox、更新job並ack；stale conflict不寫canonical graph，只送`LateResultAuditPort`隔離紀錄。真實PostgreSQL integration test驗證metadata/event/outbox/ack原子完成。
- Verification：focused worker為21 passed、branch coverage 84.48%（含shared contracts），core HTTP/runner為13 tests；non-PostgreSQL完整gate為584 passed、172 deselected、coverage 88.18%，PostgreSQL完整gate為756 passed、coverage 88.59%，242 files format、ruff、mypy 138 source files、42 schemas、upstream/secret與Alembic drift全通過。worker dependency audit除本地`stonks-contracts`無PyPI條目而skip外無已知CVE；image `stonks-tradingagents-worker:p2.5`重建成功並以UID 65532、read-only、cap-drop ALL、no-new-privileges通過health smoke。
- Honest boundary：尚未提供常駐research dispatcher與production artifact capability signer；本項完成的是strict adapter、worker fetch boundary與transaction-owned completion contract，不宣稱production服務已部署。P2 gate尚未完成，下一項為P2.6 PEAD/event-study。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且SHA-256仍一致，規範無需變更。

### P2 Progress Review — P2.6 — 2026-07-13

- Scope completed：新增`PEADStrategy`、frozen PIT earnings event contract、draft strategy manifest、pure-Python/Decimal market-model event study、CAR、Student t-test、seeded percentile bootstrap與兩組deterministic golden fixtures；未引入NumPy、SciPy或上游application/data client。
- PIT / authority：PEAD只使用同instrument、proven且`available_at <= as_of`的quarterly BEAT/MISS；依report period deterministic dedup（8-K優先），排除future、unknown availability、超過4日freshness與filing lag >=45日的retrospective rows。所有PEAD輸出固定`promotion_state=draft`、confidence 0，只具`alpha_signal` authority，不能建立target/risk/order。
- Event-study integrity：caller必須提供canonical aligned returns與calendar-resolved event day；return availability不得早於trading day或晚於as-of，event return不得早於filing availability。duplicate day、future/after-close leakage、missing event day、短estimation window、non-finite/invalid stats皆fail closed；OLS、abnormal return、CAR與bootstrap replay有golden/seed證據。
- License：來源固定ai-hedge-fund commit `3a18702cb25777fb4bdb4b2527a0c868bc8297f4`；`AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY`已登錄manifest/THIRD_PARTY_NOTICES，完整Virat Singh copyright與MIT text已收錄。
- Verification：focused為14 passed、branch coverage 90.74%；完整non-PostgreSQL gate為598 passed、172 deselected、coverage 88.27%，248 files format、ruff、mypy 142 source files、schema、upstream/license與secret gates全通過。無dependency、migration或DB行為變更，因此未重跑P1 PostgreSQL suite。
- Honest boundary：PEAD尚未完成universe、cost、walk-forward/PBO與out-of-sample evaluation，明確維持draft且不可paper eligible。P2 gate尚未完成，下一項為P2.7 evidence assembler。
- 文件同步：`README.md`、`THIRD_PARTY_NOTICES.md`、legal manifest/notice、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。

### P2 Progress Review — P2.7 — 2026-07-13

- Scope completed：新增versioned frozen `AnalysisContextRequest/AnalysisContext/EvidenceRequirement/EvidenceBlock`與read-only assembler；每個request只執行一次`EvidenceRepository.query_available(subject, as_of)`，不含provider、web、LLM或其他fetch path。
- Quality / provenance：沿用自有`DataQualityStatus`承接DSA的available、missing、not_supported、fallback、stale、estimated、partial、fetch_failed詞彙並保留conflict；block保存completeness、evidence refs、provider/source、latest availability、warnings與missing reason，context保存exact canonical `EvidenceItem`和deterministic payload hash。
- PIT / policy：repository若回future as-of/availability、wrong subject或duplicate IDs即整體conflict；infra failure原樣傳遞，不以空context偽裝。sensitivity、license、redistribution不符者排除並形成明確limitation；同event time不同content hash保留雙側refs並標conflict，untrusted flag不會被清除。
- Contract integrity：requirements/capabilities/policies、block refs/sources與context evidence IDs皆unique；所有block refs必須exact cover context evidence，available block不可無evidence。missing/stale/conflict等只是輸入品質，不冒充analysis/job/delivery狀態。
- Verification：focused為8 passed、branch coverage 84.35%；完整non-PostgreSQL gate為606 passed、172 deselected、coverage 88.23%，252 files format、ruff、mypy 145 source files、schema、upstream/license與secret gates全通過。無dependency、migration或DB行為變更，因此未重跑P1 PostgreSQL suite。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P2 gate尚未完成，下一項為P2.8 report generator/integrity policy。

### P2 Progress Review — P2.8 — 2026-07-13

- Scope completed：新增closed `ReportDraft/DraftClaim/GenerateReportRequest`、`ReportClaim/ClaimCertainty` wire contract、structured report generator與deterministic integrity policy；`AnalysisReport`新增claims與exact raw generation artifact ref，schema snapshots更新為43個。
- JSON truth / citations：每個非hypothesis claim必須引用context內evidence且quality精確等於引用refs的最差block狀態；只有available可`observed`，其他狀態只能`qualified`。Hypothesis必須無citation/data quality；unknown ref、missing citation、quality/certainty mismatch皆`model_output_invalid`。
- Authority / provenance：LLM draft不含conclusion free text、guardrail、ID、rendering或order欄位；core從outlook enum建立conclusion，deterministic產生claim IDs/evidence union，固定注入research-only、paper-only與deterministic portfolio/risk guardrails，並保存generator/model/prompt/policy版本與raw output artifact ref。直接execution語言或extra order schema fail closed。
- Prompt / retry：safe messages只含subject/as-of、quality blocks、limitations與allowed IDs；raw evidence payload只放`untrusted_blocks`。score/confidence wire schema強制0..1字串；真實fake structured adapter測試證明invalid output最多repair一次，兩次無效即失敗且不產report。model failure/exception/identity mismatch皆安全失敗且不洩漏內容。
- Verification：focused為7 passed、branch coverage 90%；完整non-PostgreSQL gate為613 passed、172 deselected、coverage 88.27%，256 files format、ruff、mypy 148 source files、43 schemas、upstream/license與secret gates全通過。無dependency、migration或DB行為變更，因此未重跑P1 PostgreSQL suite。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P2 gate尚未完成，下一項為P2.9 Jinja renderers/templates。

### P2 Progress Review — P2.9 — 2026-07-13

- Scope completed：新增sandboxed fixed-template Jinja adapter、clean `full.md.j2`、`brief.md.j2`、`email.html.j2`與三份golden snapshots；`AnalysisReport.renderings`保存format、template version、content hash/ref，rendered bytes存content-addressed artifact store。
- Determinism / channels：三個channel都只從同一`AnalysisReport` JSON truth建構，不讀context、LLM或provider；rerender已帶renderings的report仍產生相同bytes/hashes。template version、format、report ID、media type與artifact metadata固定，channel產物可獨立重建驗證。
- Safety / UX：Jinja使用sandbox、StrictUndefined與固定startup-loaded paths；HTML autoescape、Markdown special-char escape、subject/brief deterministic truncation、zh-TW/en labels與observed/qualified/hypothesis + quality標籤都有測試。full Markdown 64KiB、brief 4KiB、email 128KiB上限在任何artifact write前檢查；unsupported language或missing template fail closed。
- Upstream / dependency：模板為clean implementation，未複製daily_stock_analysis片段，不新增其MIT notice；core新增輕量Jinja2 3.1.6並更新frozen lock，locked runtime audit無已知CVE。
- Verification：focused為6 passed、branch coverage 90%；完整non-PostgreSQL gate為619 passed、172 deselected、coverage 88.28%，259 files format、ruff、mypy 150 source files、43 schemas、upstream/license/secret與locked dependency audit全通過。無migration或DB行為變更，因此未重跑P1 PostgreSQL suite。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P2.1–P2.12與phase gate已完成；下一項為P3.1 forecast contracts。Production常駐dispatcher與durable全流程transition仍屬P4.7，不在P2 gate冒充完成。

### P3 Progress Review — P3.1 — 2026-07-13

- Scope completed：新增frozen strategy manifest/registry、fixed promotion graph、artifact-only evaluation request/report、provenance-complete alpha、artifact-first forecast request/output與runtime-checkable forecast/strategy-lab ports；wire promotion enum同步加入`suspended`且永遠沒有live state。
- Eligibility / authority：paper weight只有exact strategy/data/runtime/evaluation-policy/report binding、`paper_eligible`、passed evaluation、calibrated且fresh時才可使用confidence；unregistered、shadow/suspended、failed/expired evaluation、uncalibrated、stale/expired或hash mismatch一律deterministic weight 0。所有contracts拒絕target/order/quantity/risk override欄位。
- Replay / PIT：evaluation window與forecast input不得越過`as_of`；evaluation hash排除identity/time等非決定性欄位並正規化checks/metrics/baselines。Stochastic forecast在任何canonical mapping前必須有immutable raw output與sampled-path artifact，fresh inference不宣稱bit-identical。
- Verification：focused contracts/property tests為32 passed、新模組branch coverage 89.53%；完整non-PostgreSQL gate為667 passed、176 deselected、coverage 88.19%，291 files format、ruff、mypy 175 source files、43 schemas、upstream/license、secret與locked dependency audit全通過。P3.1無migration或DB行為變更，因此未重跑PostgreSQL suite。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P3 gate尚未完成；下一項為P3.2 PostgreSQL strategy registry persistence。

### P3 Progress Review — P3.2 — 2026-07-13

- Scope completed：新增Alembic 0009的strategy registry/evaluation/audit schema、SQLAlchemy mappings、`PostgresStrategyRepository`與UoW wiring；同strategy/version registration exact-idempotent，evaluation綁定canonical snapshot與finalized artifact。
- Transaction / authority：promotion使用`state + version` CAS且registry update與audit append同transaction；兩個並行paper state mutation只有一個成功。DB trigger固定allowlisted graph、version+1、exact manifest/runtime/evaluation binding與DB timestamp，deferred constraint要求每個mutation有同sequence/from/to/evaluation/timestamp audit；繞過adapter的無audit update無法commit。
- Immutability / grants：manifest/source/runtime/feature/label/universe/cost/split/parameter hashes與identity由trigger禁止修改；evaluation與audit append-only，reader重驗完整SHA-256 chain與registry projection。`stonks_app`只有registry state/evaluation/version/timestamp columns可update，`stonks_reader`唯讀，`stonks_worker`無strategy table privileges。
- Verification：focused repository/domain為29 passed、branch coverage 84.27%；完整PostgreSQL gate為854 passed、coverage 88.42%，294 files format、ruff、mypy 176 source files、43 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過；0009 downgrade/re-upgrade與metadata exact match通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P3 gate尚未完成；下一項為P3.3 deterministic baselines。

### P3 Progress Review — P3.3 — 2026-07-13

- Scope completed：新增closed baseline manifest loader、PIT `BaselineSeries`、共用Decimal statistics與last-value/simple moving-average/OLS linear三個策略；三份versioned YAML固定deterministic、draft與lookback/minimum observations。
- PIT / determinism：bars必須strictly ordered/unique、positive close且event/availability皆`<= as_of`；lookback不足或linear非正預測fail closed。forecast ID、expected/median return、volatility、downside/max-drawdown、dispersion皆由canonical input與12位Decimal規則決定；同輸入signal/payload hash相同。
- Authority / comparison：baseline只輸出`ForecastSignal`與`research_only_unevaluated` warning，沒有promotion/target/order/quantity/risk override欄位，也不宣稱calibrated。Kronos、opinion mapper與complex model後續必須在P3.4用相同dataset/cost/split contract對照這三個baseline。
- Verification：focused baseline/golden為7 passed、branch coverage 89.58%；完整non-PostgreSQL gate為674 passed、187 deselected、coverage 88.16%，300 files format、ruff、mypy 181 source files、43 schemas、upstream/license、secret與locked dependency audit全通過。無migration或DB行為變更，P3.2完整PostgreSQL gate仍為854 passed且Alembic無drift。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P3 gate尚未完成；下一項為P3.4 evaluation engine。

### P3 Progress Review — P3.4 — 2026-07-13

- Scope completed：新增closed evaluation observation/dataset/candidate/policy contracts、PIT/leakage audit、purged walk-forward、bounded CPCV/PBO、cost scenarios、performance metrics、calibration buckets與end-to-end promotion report；production thresholds固定於`config/policies/evaluation_v1.yaml`且policy hash由完整內容計算。
- PIT / survivorship：feature event/availability必須在prediction前，label/outcome必須在prediction後且於evaluation as-of前可得；publication lag必須proven，historical universe membership必須在prediction時已知且為真。任何future/unknown/current-survivor污染直接structured `INVALID_INPUT`且不產report。
- Evaluation integrity：purge+embargo形成明確train/test gap，績效只用所有walk-forward test rows的deduplicated union；CPCV在多candidate且4–8偶數groups時估PBO。Fees/slippage乘turnover做0.5x/1x/2x sensitivity，另算benchmark alpha、max drawdown、hit rate、Sharpe、Brier/ECE buckets。
- Promotion：point-in-time、leakage、survivorship、reproducibility、baseline、cost、drawdown、calibration、overfitting均為獨立mandatory checks。污染是Failure；合法但未過門檻是immutable `passed=false` report。Report exact綁定manifest/snapshot/data/runtime/policy，report ID/artifact/time不進evaluation hash，因此同輸入可deterministic replay。
- Verification：focused evaluation/domain為42 passed、branch coverage 90.91%；完整non-PostgreSQL gate為696 passed、187 deselected、coverage 88.33%，313 files format、ruff、mypy 189 source files、43 schemas、upstream/license、secret與locked dependency audit全通過。無migration或DB行為變更；P3.2 strategy repository PostgreSQL tests另做focused regression。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P3 gate尚未完成；下一項為P3.5 opinion-to-alpha policy。

### P3 Progress Review — P3.5 — 2026-07-13

- Scope completed：新增frozen/content-hash `OpinionToAlphaPolicy`、default-disabled YAML、closed command與deterministic mapper；policy固定bullish=0.5、neutral=0、bearish=-0.5及stale/expiry windows。
- Gates / provenance：disabled、unknown recommendation、uncalibrated confidence、non-`paper_eligible`、failed/expired/mismatched evaluation或policy/manifest parameters mismatch全部structured fail且不產signal。成功signal exact保存mapper strategy/evaluation/runtime、current data snapshot/hash、opinion raw artifact與evidence refs。
- Authority correction：mapper不讀LLM quantity且輸出schema拒絕quantity/target/order/risk override。P3.1 eligibility修正為current inference snapshot不必等於historical evaluation snapshot；仍要求evaluation ID/hash、manifest/runtime/policy exact binding，因此不放寬promotion或risk authority。
- Verification：focused mapper/domain為30 passed、mapper branch coverage 87.50%；完整non-PostgreSQL gate為706 passed、187 deselected、coverage 88.32%，317 files format、ruff、mypy 191 source files、43 schemas、upstream/license、secret與locked dependency audit全通過。無migration或DB行為變更。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P3 gate尚未完成；下一項為P3.6 Kronos isolated worker。

### P3 Progress Review — P3.6 — 2026-07-13

- Scope completed：新增Kronos-small/Tokenizer-base pinned manifest、file size/SHA-256驗證、symlink/untracked-entry拒絕、thread-safe warm-once loader、exact runtime preflight與bounded HTTP health/readiness；runtime只讀本機`/models`，不接受repo ID、URL、HF cache fallback或request-time download。
- Isolation / licensing：CPU與CUDA各有獨立PyTorch index、`pyproject.toml`/`uv.lock`與Docker target；compose採internal network、read-only、UID 65532、cap-drop ALL、no-new-privileges及唯讀model mount，無DB/provider/broker/queue/execution credentials。Kronos MIT source archive、license、model/tokenizer revisions與hash已加入notice/manifest，core lock仍無torch。
- Runtime evidence：四個實際Hugging Face檔案共約115 MB均重算SHA-256相符。CPU image以`torch 2.12.1+cpu` warm成功；CUDA image以`torch 2.12.1+cu129`在RTX 3070 Ti完成32→2 bars、OHLCV/amount六欄inference。兩者皆UID 65532；CPU約0.80 GiB、CUDA約11.95 GiB。
- Security / verification：原選2.11.0因OSV `GHSA-rrmf-rvhw-rf47`受影響而fail closed升至首個fixed 2.12.1；OSV為0 vulnerabilities，CPU/CUDA image內Linux dependency audit均無已知CVE（local `stonks-contracts`與帶build suffix的torch由PyPI scanner跳過，torch另以OSV標準identity補查）。focused為26 passed；完整non-PostgreSQL gate為732 passed、187 deselected、coverage 88.32%，323 files format、ruff、core/worker mypy、43 schemas、upstream/license、secret與core locked audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P3 gate尚未完成；下一項為P3.7 Kronos canonical input/output adapter。

### P3 Progress Review — P3.7 — 2026-07-13

- Scope completed：新增9個shared Kronos wire models、CPU/CUDA exact core configs、calendar-aware request builder、逐seed native worker forecast route、artifact-first fixed-origin HTTP adapter、deterministic mapper與`tests/golden/kronos/`。`ForecastRequest/ForecastOutputArtifact`另修正revision與artifact SHA-256為不同identity，不再錯誤互相比較。
- PIT / path retention：builder只接受matching canonical `BarSeries`/`ExchangeCalendar`，1d timestamps跨週末、假日與DST固定取session close。Worker固定Python/NumPy/PyTorch/CUDA seed，每seed以upstream `sample_count=1`序列執行並保留完整paths；per-seed重驗deadline，missing/estimated volume保留quality provenance。
- Artifact / replay / authority：core先封存raw envelope與不含lease nonce的replay-complete path artifact，再驗generation/nonce、request/result/runtime/model/tokenizer、timestamps、OHLCV、length與step jump。Mapping只產research-only `ForecastSignal`；invalid/late/drift output回structured failure，fresh stochastic inference不宣稱bit-identical。
- Runtime / tolerance：實際115 MB pinned weights在CPU與RTX 3070 Ti CUDA final source route各完成2 explicit seeds × 2 bars；另以16 paths保存aggregate tolerance golden。Final CPU/CUDA runtime hashes為`c3542191fc3a6137540219098a66be4f3f32c7c7203e52f44018eb4e66dfa866`與`6a2ed7db73e7a2b16c0b4eaa79f8c47fb6d505ce6fa81a5e3f68943e0c6a7223`。
- Verification：focused contracts/worker/adapter/domain/schema為72 passed，Kronos core adapter coverage 86%；完整non-PostgreSQL gate為772 passed、187 deselected、coverage 88.31%，328 files format、ruff、core 195 source files與worker mypy、52 schemas、upstream/license、secret與core locked dependency audit全通過。
- 文件同步：`README.md`、worker README、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P3 gate尚未完成；下一項為P3.8 Kronos evaluation and promotion。

### P3 Progress Review — P3.8 — 2026-07-13

- Scope completed：新增frozen Kronos evaluation record/snapshot、archived forecast → generic evaluation inputs、production promotion wrapper、exact strategy configuration與evaluated forecast → versioned `AlphaSignal` mapper；snapshot logical content hash同時綁定request data hash與artifact ref。
- PIT / comparison：每筆row保存forecast/raw/path refs、feature/outcome/label/universe availability、realized與benchmark prices、turnover及三個baseline predictions；整體強制strict order、US/HK/TW、同runtime/model/tokenizer與exact baseline identities。Future label、request/data/artifact/runtime drift、model-copy fence bypass、stale/conflict quality皆fail closed。
- Golden / authority：768筆跨市場archived forecasts依未修改的production policy完成4個purged walk-forward splits與252筆OOS。Candidate與baseline打平且cost/calibration不合格，因此golden固定`passed=false`；committed deployment仍為`shadow`/paper weight 0，不為整合降低threshold。只有passed、calibrated、unexpired、exact manifest/runtime/model/evaluation binding可產shadow Alpha，shared eligibility仍回weight 0，輸出無target/order/risk authority。
- Verification：focused為15 passed、兩個新模組合計branch coverage 89.76%；P3 contracts/worker/adapter/evaluation/signal regression為137 passed。完整non-PostgreSQL gate為787 passed、187 deselected、coverage 88.34%，332 files format、ruff、mypy 197 source files、52 schemas、upstream/license、secret與locked dependency audit全通過。無dependency、migration或DB行為變更。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P3 gate尚未完成；下一項為P3.9 Qlib quant-lab worker。

### P3 Progress Review — P3.9 — 2026-07-13

- Scope completed：新增15個frozen shared Qlib contracts、canonical `BarSeries`→immutable tabular artifact converter、closed Qlib OLS runtime、bounded HTTP worker、獨立76-package lock、hardened OCI image/compose、MIT NOTICE與67份schema snapshots。官方dataset未使用，core lock未加入Qlib/NumPy/Pandas。
- PIT / replay / authority：dataset逐row綁feature/label/universe availability與forward label，spec、snapshot、runtime、cost、split及generation/nonce皆exact fence；purge/embargo與deadline前後fail closed。Worker只允許`qlib_linear_ols`，拒絕任意module/class/expression/pickle/path，輸出固定research-only predictions/positions/metrics/artifact hashes，不能promotion、target、risk或order。
- Runtime / security：Qlib固定commit `d5379c52`與source archive SHA-256 `3aaefc2f...cb2276`，worker source/lock與Python 3.12.12、NumPy 2.2.6、Pandas 2.2.3、scikit-learn 1.7.2綁runtime hash `4219b706...107ca5a`。真實`DataHandlerLP -> DatasetH -> LinearModel(OLS)` HTTP route兩次重播四組artifact hashes完全相同；image為UID 65532、read-only、cap-drop/internal network且無DB/queue/provider/execution credentials。修補`filelock`/`requests`後獨立lock audit為0 vulnerabilities。
- Verification：focused contracts/converter/worker為24 passed、四個新模組合計branch coverage 86.70%；完整non-PostgreSQL gate為811 passed、187 deselected、coverage 88.52%，341 files format、ruff、core 200 source files與worker 4 files mypy、67 schemas、upstream/license、secret、core與worker locked dependency audit全通過。無migration或DB行為變更。
- 文件同步：`README.md`、worker README、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P3 gate尚未完成；下一項為P3.10 forecast/signal/evaluation API與CLI。

### P3 Progress Review — P3.10 / phase gate — 2026-07-13

- Scope completed：新增typed strategy registry/UoW ports與application use cases、read-only strategy/evaluation/audit查詢、signal eligibility、reviewer-only CAS transition API，以及`stonks strategy` show/events/evaluation/transition CLI。Promotion graph使用既有`PromotionState`，schema與CLI都不接受live或order-shaped欄位。
- Authorization / integrity：API預設deny、request body bounded、actor由authenticated principal產生；transition要求`strategy_reviewer`，成功才commit。Evaluation read重驗row、registry exact binding與current audit hash chain；signal缺strategy/evaluation或binding不符時deterministic weight 0，infra/conflict不偽裝成功。
- PostgreSQL gate：真實資料庫覆蓋evaluation integrity、promotion → suspend → retire audit sequence、stale CAS零新增event，以及API transition與CLI audit reader共用同一DB-authoritative CAS。Alembic check無drift。
- Verification：focused API/CLI為13 passed、branch coverage 87.14%；完整non-PostgreSQL gate為820 passed、190 deselected、coverage 88.56%。完整PostgreSQL P3 phase gate為1010 passed、coverage 88.73%，349 files format、ruff、mypy 207 source files、67 schemas、upstream/license、secret、locked dependency audit與Alembic全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。P3 gate完成；下一項為P4.1 portfolio/risk/reservation/execution domain。

### P4 Progress Review — P4.1 — 2026-07-13

- Scope completed：新增canonical account snapshot/portfolio target、risk decision、cash/position reservation、order intent/command/event、fill/receipt與balanced journal domain；新增portfolio/risk/canonical execution/ledger typed ports，同時保留P0 wire-compatible `ExecutionPort`。
- Sequence / authority：RiskDecision exact綁input/normalized target hash、account aggregate與portfolio sequence並有expiry；reservation建立前重驗sequence，成功後只推進一次account sequence。OrderIntent必須等於authorized target delta的instrument/side/quantity/quantum，ExecutionCommand還需open reservation、exact risk/reservation hashes與reservation後sequence，單獨risk approval不能形成command。
- State / accounting：reservation支援open/partial consume/consume/release/expire immutable hash events；order transitions為closed graph、monotonic cumulative fill與hash chain，valid-until採exclusive execution boundary。Fill receipt重驗account/instrument/side/quantity與event totals。Journal要求posting已依commodity quantum量化、stable ordering，且每種commodity debit/credit exact平衡，transaction sequence/hash chain可重驗。
- Fail closed：timezone-naive clock、extreme Decimal、hidden fractional amounts、stale sequence、over-consume/overfill、terminal transition、receipt identity drift、mixed quantum、unbalanced或tampered chain皆拒絕；所有mutation use case回structured `Result`。
- Verification：focused domain/ports為23 passed，新trading modules branch coverage 83%；P0 execution/fake-cycle regression為20 passed。完整non-PostgreSQL gate為843 passed、190 deselected、coverage 88.14%，362 files format、ruff、mypy 217 source files、67 schemas、upstream/license、secret與locked dependency audit全通過。無migration、DB或dependency變更。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。下一項為P4.2 trading persistence。

### P4 Progress Review — P4.2 — 2026-07-13

- Scope completed：新增0010的14個paper trading tables、version-pinned trigger/grant helper、SQLAlchemy mappings、frozen account state/event與reservation-order persistence result、runtime-checkable trading repository port、PostgreSQL repository與UoW wiring。
- Transaction / integrity：account aggregate以CAS推進且同transaction必須append matching hash-chained event；cash/position reservation projection、target/risk binding、order idempotency/event chain、fill與journal source binding皆fail closed。Savepoint只回滾局部conflict，commit仍由core UoW擁有；同帳戶並行reservation只有一個成功。
- DB authority / security：DB trigger拒絕orphan account event、無event account update、stale projection、reservation/order chain drift、append-only mutation與不完整或不平衡journal。`stonks_app`只有trading tables的select/insert及五個projection的column-scoped update，reader唯讀，worker無任何trading table權限；corrupt persisted payload回structured `CONFLICT`。
- Verification：focused PostgreSQL migration/repository為35 passed，trading repository branch coverage 84%、mapping 97%。完整PostgreSQL gate為1048 passed、coverage 88.44%，370 files format、ruff、mypy 222 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。下一項為P4.3 deterministic portfolio baseline。

### P4 Progress Review — P4.3 — 2026-07-13

- Scope completed：新增frozen `BuildTargetCommand`、mark/signal-candidate/policy contracts、runtime-checkable deterministic builder、content-hash `portfolio_v1.yaml`、golden fixture與property/boundary tests。Policy以strategy/version固定五組權重，總和必須exact為1，long-only不可由設定切換成live/short execution。
- Eligibility / PIT：每個signal都重驗paper-eligible registry、passed/calibrated/unexpired evaluation及exact manifest/runtime/report binding；signal、registry/evaluation或mark來自snapshot未來、重複strategy-instrument、未知權重、currency/quantity quantum drift、missing mark與zero NAV皆structured fail closed。
- Determinism / exposure：instrument與signal固定排序；score直接加總`fixed weight × alpha × calibrated confidence`，缺少權重保留為cash、不重新正規化。Deadband、shrinkage、current-weight turnover penalty、long-only position bound及quantity floor順序固定；target輸出actual rounded weight、input signal IDs、policy/calculation hashes、turnover與per-instrument cost/missing-weight diagnostics。
- Verification：focused golden/property/boundary為16 passed，builder branch coverage 97%、construction contracts 92%。完整PostgreSQL gate為1064 passed、coverage 88.55%，376 files format、ruff、mypy 225 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。下一項為P4.4 hard risk gate。

### P4 Progress Review — P4.4 — 2026-07-13

- Scope completed：新增frozen risk state/policy、19個stable hard checks、atomic risk authorization UoW、multi-instrument reservation/order batch與versioned `risk_v1.yaml`。Risk business rejection產immutable `RiskDecision`與sorted checks；contract/infra conflict維持structured failure。
- Checks / fail-closed：重驗target/account/portfolio/ledger sequence、signal/evaluation及mark freshness、session、global/account kill switch、cash/position/open reservation reconciliation、pending orders、ADV、single/sector/asset/gross/net exposure、turnover、drawdown與daily loss。Unknown/missing/future/stale/binding drift、unsupported asset/order皆拒絕。
- Transaction / reservation：approved path在同一UoW重讀account並重跑risk，保存target/decision後以一次account CAS推進所有cash/position projection sequence，原子建立全部reservations/orders。多buy成本按traded notional分攤並向上量化；sell exact reserve available position。任一不足、partial idempotency或concurrent sequence drift整批rollback。
- Verification：focused risk/authorization/portfolio/PostgreSQL regression為53 passed；risk evaluator branch coverage 94%、authorization 84%、trading repository 85%。完整PostgreSQL gate為1091 passed、coverage 88.60%，389 files format、ruff、mypy 233 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。下一項為P4.5 reference paper broker。

### P4 Progress Review — P4.5 — 2026-07-13

- Scope completed：新增frozen `PaperExecutionPolicy/ExecutionBar/PaperExecutionRequest/PaperExecutionOutcome`、deterministic reference broker、core-owned execution UoW、PostgreSQL execution store、0011 durable receipt、versioned `paper_v1.yaml`與golden fixtures。`PaperExecutionModelPort`只做pure simulation，DB transaction仍由core runner擁有。
- Fill semantics / PIT：只選`opens_at > issued_at`、`opens_at < valid_until`、`available_at <= as_of`的第一根sorted tradable bar，已知command當根永不成交。Market採adverse spread + base slippage + participation-scaled impact；limit依open price improvement或intrabar touch成交且不越limit。Volume cap、partial fill、DAY/GTC/IOC、expiry、fee bps/per-unit/minimum與所有Decimal quantization皆由content-hash policy固定；無future bar保持accepted pending，不偽造fill。
- Transaction / idempotency：application先重讀DB-authoritative intent、reservation、account、events/fills，再模擬並於account row lock內重驗sequence/prefix/hash。Order/reservation events、fills、reservation projection consume/release與append-only `paper_execution_receipt`同transaction寫入；same idempotency + exact command重播同record，different payload、concurrent drift、cash/position projection mismatch與incomplete chain全rollback。兩個並行duplicate commands實測只產一筆receipt/fill。
- Verification：focused application/golden/PostgreSQL為31 passed，四個execution核心模組合計branch coverage 81%。完整PostgreSQL gate為1122 passed、coverage 88.27%，400 files format、ruff、mypy 238 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且規範無需變更。下一項為P4.6 balanced journal and projections。

### P4 Progress Review — P4.6 — 2026-07-13

- Scope completed：新增immutable opening snapshot、versioned `ledger_v1.yaml`、deterministic fill journal、journal replay/reconciliation、generic ledger account projections、PostgreSQL repository與0012 migration。BUY/SELL依average cost建立cash/inventory value與units/fee/realized PnL/clearing postings；opening position無cost basis時SELL fail closed。
- Atomicity / graph：execution在單一account-serialized UoW完成fill、journal、settled cash/position、generic projection、ledger/account head/event與receipt。Deferred constraints雙向保證每筆fill恰有一筆journal且latest order state/cumulative fill一致；receipt replay重驗fills/journals/head/projection graph，concurrent duplicate只回authoritative record。
- Reconciliation / safety：opening + immutable journals可deterministic重建projection/hash；CAS同時綁account/ledger sequence與previous hash。Gap、tamper、unknown ledger account/order state、projection drift或unbalanced graph先rollback，再以獨立transaction啟動singleton global paper kill switch；active switch拒絕新execution。
- Verification：focused ledger/execution/PostgreSQL為53 passed，五個核心模組合計branch coverage 80.98%、reconciliation 95%。完整PostgreSQL gate為1144 passed、coverage 87.92%，412 files format、ruff、mypy 243 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P4.7 end-to-end workflow state machine。

### P4 Progress Review — P4.7 — 2026-07-13

- Scope completed：新增frozen canonical stage/reference/state/result contracts、typed handler/store ports、core `run_paper_fund_cycle`/cancel use cases與PostgreSQL paper-cycle store。Flow嚴格固定evidence → research/opinion → signal → target → risk → order → receipt → ledger → report；每階段只接受allowlisted canonical ref types、stable IDs/hashes與前綴state hash。
- Durable transitions / fencing：checkpoint、retry、dead-letter、cancel、complete全寫入既有run-event/outbox matching hash chain，不新增平行workflow authority。每次mutation在transaction內以PostgreSQL clock重驗job generation/nonce/owner/lease/deadline、payload hash與run input hash；concurrent同stage CAS只有一個commit，late generation不能讀寫。Cancel有version CAS及actor/reason audit；terminal result為content-addressed immutable artifact。
- Crash / idempotency：真實PostgreSQL E2E在reference broker已commit receipt/fill/journal後、workflow checkpoint前刻意crash；lease expiry後新generation重領，execution use case先query/驗證既有receipt與ledger graph再繼續。最終仍只有1 receipt、1 fill、1 journal，9 stage checkpoints + completion均有matching outbox；完整completion replay不重跑handler。
- Verification：focused domain/application/PostgreSQL/E2E為21 passed，三個workflow核心模組合計branch coverage 85.57%、runner 91%。完整PostgreSQL gate為1157 passed、coverage 87.78%，419 files format、ruff、mypy 246 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P4.8 outcome monitoring and reflection evidence。

### P4 Progress Review — P4.8 — 2026-07-13

- Scope completed：新增frozen `PointInTimeMark/PortfolioValuation/OutcomeEvidence/ReflectionContext` contracts，以及mark-to-market、outcome calculation/artifact persistence與reflection boundary use cases。NAV snapshot exact綁ledger sequence/hash/projection、base currency、currency quantum與每個open position mark；valuation/outcome/context都有stable content hash。
- PIT / reconciliation：mark必須`event_time <= available_at <= valuation as_of`，missing/extra/future/foreign mark、future ledger與cross-currency state皆fail closed。Outcome固定strict valuation path、approved historical decision/target與benchmark identity，計算12位Decimal raw/benchmark return、alpha與path max drawdown；cumulative ledger fee delta必須等於decision-bound fill refs總費用。
- Evidence / authority：完整outcome payload先以canonical JSON bytes封存content-addressed artifact，再建立derived `EvidenceItem`與完整source evidence lineage。Reflection只建立allowlist為該outcome evidence的新`ResearchRequest`；回傳必須是identity/scope/time完全一致且實際引用outcome的新`ResearchArtifact`，domain schema不含target/order/quantity/risk override，歷史decision hash保持不變。
- Verification：focused monitoring為11 passed，四個新模組branch coverage 82.27%。完整PostgreSQL gate為1168 passed、coverage 87.64%，429 files format、ruff、mypy 251 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。無migration或dependency變更。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P4.9 kill-switch and operator use cases。

### P4 Progress Review — P4.9 — 2026-07-14

- Scope completed：新增frozen `ActivateKillSwitchCommand/ReconcilePaperCommand/ResumePaperCommand`與state/action/result contracts、role-gated application use cases、PostgreSQL operations repository、0013 operator audit migration，以及paper status/actions/activate/reconcile/resume CLI/API。API actor只取authenticated principal，不接受body偽造。
- Safety / atomicity：global/account switch mutation以version CAS與row locks serialized；activation在同一transaction把仍可取消的pending order轉為cancelled、已逾期者轉為expired，並release/expire reservations與reserved cash/position。新execution由既有ledger authority fail closed；fills、journals與歷史event只讀保留。Concurrent同version activation實測只有一個winner。
- Reconciliation / audit：resume必須先在鎖定scope內對opening snapshot與immutable journal做exact replay，再解除switch；drift寫`resume_rejected`且維持active。Manual reconciliation drift寫`reconciliation_failed`並啟動global switch。Operator actions以global sequence、previous hash、action hash與audit-head CAS串成append-only chain，DB triggers拒絕update/delete、斷鏈或head drift。
- Verification：focused domain/application/API/PostgreSQL為23 passed，新模組branch coverage 82.45%；migration/operator/execution/ledger regression為48 passed。完整PostgreSQL gate為1191 passed、coverage 87.47%，445 files format、ruff、mypy 261 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P4.10 portfolio/report projections與P4 phase gate。

### P4 Progress Review — P4.10 / Phase Gate — 2026-07-14

- Scope completed：`AnalysisReport`新增五組stably sorted `ReportReference(ref_id, content_hash)`，由core request注入target/risk/order/fill/outcome refs，structured LLM draft仍`extra=forbid`且無新增authority。新增content-hashed portfolio/risk projections、ledger-bound NAV recording/read use cases、typed ports、PostgreSQL adapter、0014 immutable valuation與paper portfolio/nav/risk CLI/API。
- Projection integrity：portfolio先驗完整account event chain、target payload/row binding與latest order states，明示settled/reserved/available cash/position。NAV append前鎖account並重驗ledger sequence/hash/projection；read latest valuation再次與journal replay比對，ledger移動立即structured conflict。Risk重驗latest decision payload/row，`currently_authorized`只反映exact account/portfolio sequence與expiry，projection本身沒有order method/path。
- P4 closure：真實小型portfolio從pre-trade NAV經reference fill、balanced journal、post-trade NAV、outcome evidence到帶五類refs的report，JSON round-trip hash一致且ledger reconciliation matched。既有cycle crash/reclaim只產一筆receipt/fill/journal；TradingAgents/Kronos worker env deny DB/broker/queue secrets，LLM/research outputs與AI-Trader policy皆無execution path。
- Verification：focused contracts/projections/API/PostgreSQL/E2E為22 passed，新模組branch coverage 80.59%；P4 property/concurrency/crash/stale/authority/upstream safety matrix為215 passed。完整PostgreSQL gate為1206 passed、coverage 87.32%，459 files format、ruff、mypy 268 source files、68 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。P4 phase gate與success criteria全部通過，下一項為P5.1 external platform contracts。

### P5 Progress Review — P5.1–P5.2 — 2026-07-14

- Scope completed：新增9個clean-room external platform schemas、runtime-checkable `PlatformPort`、AI-Trader adapter-local frozen response contracts、default-off config與snapshot-bound cassettes。Adapter涵蓋去敏strategy/discussion/reply、challenge join/submit/vote、experiment enroll/observation/result與heartbeat events，所有remote輸出固定為untrusted、evidence-only artifact/evidence。
- Boundary / resilience：只允許exact HTTPS origin與allowlisted community/control endpoints；禁止order/copy/realtime/position/follow routes、redirect與automatic POST retry。Canonical bounded JSON、scoped token、raw response artifact、typed tolerant reader及injected event inbox皆有contract/security tests；schema/authz/body anomaly停用adapter，heartbeat duplicate忽略、same ID payload conflict fail closed。Adapter dependency graph沒有execution、risk、DB或queue authority。
- Verification：focused platform/adapter/config/security為44 passed，兩個adapter模組合計branch coverage 83.70%。完整PostgreSQL gate為1244 passed、coverage 87.27%，469 files format、ruff、mypy 274 source files、77 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- Limitation / docs：live `api.ai4trade.ai`與OpenAPI因DNS無法解析而未驗證；目前只保證固定AI-Trader snapshot `d03ff6c`最小runtime shapes與clean-room cassettes，不宣稱current production compatibility。`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P5.3 community feedback policy。

### P5 Progress Review — P5.3 — 2026-07-14

- Scope completed：新增frozen `CommunityFeedbackPolicy/Command/Decision`與`apply_community_feedback`，以及enqueue-only `JobEnqueuePort`。Feedback只能在closed observation window後產生ignore、deterministic confidence haircut或固定模板的research-only job；decision與完整core reputation policy各自content-hash binding。
- PIT / reputation / injection：platform、publication subject、event/evidence identity、available/observed/as-of與deadline皆exact驗證；duplicate、future、scope/reputation drift fail closed。Reputation只取stably ordered core policy，remote self-claim不能取得權重；同作者最多一票、support不提高confidence、late/unknown reputation忽略。Injection文字即使高reputation亦quarantine，queue payload只有固定safe question、typed identifiers與evidence IDs，不含remote原文。
- Authority / verification：policy只持有enqueue能力，無claim/complete、signal、portfolio、risk、order、execution dependency；ignore/haircut零queue side effect。Focused platform/community/security為42 passed，community module branch coverage 90.28%；完整PostgreSQL gate為1254 passed、coverage 87.31%，472 files format、ruff、mypy 275 source files、77 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P5.4 backtest engine contract。

### P5 Progress Review — P5.4 — 2026-07-14

- Scope completed：新增14個frozen canonical backtest schemas、runtime-checkable `BacktestEnginePort`與core `run_backtest` boundary。Job固定content-addressed strategy/dataset、PIT calendar/session/bar、instrument/currency quantum、opening cash/positions、simulation-only orders與deterministic cost model；reference、Nautilus與LEAN共用相同wire surface。
- Replay / economics：result exact綁request/run/job、generation/nonce、runtime、job/input/dataset/calendar/cost hashes與deadline。Core重建JSON後驗第一個可成交next bar，拒絕same-bar或skipped-bar lookahead，並重算market/limit adverse price、participation impact、fees/slippage、per-bar volume cap、order outcomes與cash/position projection；engine-specific fill IDs/refs不影響semantic hash。
- Authority / verification：port只有`run(job) -> Result[BacktestResult]`，contracts固定`execution_mode=backtest`與`simulation_only=true`，dependency/security tests證明無paper account、risk、reservation、broker、ledger或heavy engine runtime。Focused contracts/security/P4 execution regression為107 passed，新backtest模組branch coverage 84.59%；完整PostgreSQL gate為1267 passed、coverage 87.24%，479 files format、ruff、mypy 279 source files、91 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P5.5 NautilusTrader adapter。

### P5 Progress Review — P5.5 — 2026-07-14

- Scope completed：新增default-off NautilusTrader `1.230.0` sidecar、獨立frozen lock/image、canonical adapter、bounded authenticated HTTP service與真實`BacktestEngine` replay。Canonical scheduler以synthetic open quote固定open-cross market/limit、DAY/GTC/IOC、calendar session、aggregate participation cap與child budget；adapter保存engine trade ID/raw payload hash並重建完整canonical result。
- Determinism / fail-closed：runtime、adapter、contracts、lock、OCI image與reviewed source identity exact綁定；engine-specific IDs不影響semantic hash，固定clock重播則完整result hash一致。Core重新驗next-bar schedule、price/fee/slippage、outcomes、projection與deadline；halted IOC、aggregate adverse price、coarse quantum、extreme Decimal、missing/duplicate fill、unsupported timing與runtime drift皆structured fail closed。
- Isolation / license：sidecar無`stonks_agent`、DB、provider、queue、broker、risk、reservation或ledger依賴；HTTP要求internal bearer token並限制body、concurrency、orders、bars、order×bar work與schedule children。Hardened image以internal network、UID/GID 65532、read-only、cap-drop、no-new-privileges與CPU/RAM/PID limits完成真實HTTP smoke；Nautilus LGPL、Debian GPL、exact source sdist、replacement notice、65-component SBOM與獨立lock audit均通過。
- Verification：focused core/contract/security為47 passed，actual Nautilus wheel為19 passed；root新模組branch coverage 84%、engine 92%。完整PostgreSQL gate為1281 passed、coverage 87.25%，491 files format、ruff、mypy 280 source files、91 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`、sidecar README與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P5.6 LEAN adapter。

### P5 Progress Review — P5.6 — 2026-07-15

- Scope completed：新增default-off QuantConnect LEAN `17917` / commit `c22774e` external sidecar、獨立Python/NuGet locks、canonical adapter、bounded authenticated HTTP service、固定C# algorithm與真實Launcher replay。Canonical scheduler擁有TIF/session/shared volume/cost/projection；LEAN只接收scheduled fillable children，以zero native fee/slippage回傳authority-free trace，core再次重建並驗證完整P5.4 result。
- Determinism / isolation：runtime hash `b94e4594...a93001`與OCI image digest `sha256:8bbe6b8e...47340` exact綁定；同一HTTP job兩次重播的semantic hash與raw fill refs一致。每job fresh fixed-command process、sanitized env、no shell/stdin、deadline/trace/request/order/bar/work/child/concurrency bounds；container為internal network、UID/GID 65532、read-only、cap-drop、no-new-privileges與CPU/RAM/PID限制，無core module或DB/provider/queue/broker/risk/ledger credentials。
- Provenance / security：固定Apache-2.0 source archive與license hashes、原始archive及modification source隨image提供；移除vulnerable DotNetZip/NetMQ與未使用ServiceModel/WinHttp/System.Drawing chains，16份NuGet lock皆以`--locked-mode` restore並在build內掃transitive vulnerabilities。Syft CycloneDX為4,754 components/166 unique packages；Grype為0 Critical、0 High（145 Medium、24 Low、2 Negligible），Python與core locked audit皆0 known vulnerabilities。Debian runtime實驗因14 Critical/85 High fail closed撤回，最終使用pinned Ubuntu/.NET runtime。
- Verification：focused adapter/contracts/security/SBOM為22 passed，strict mypy 6 files、Ruff、actionlint、Compose、source/license/upstream policy與real hardened replay全通過。完整PostgreSQL gate為1296 passed、coverage 87.25%，502 files format、ruff、mypy 280 source files、91 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過；secret scanner另以TDD排除gitignored `.data` runtime/build cache。
- 文件同步：`README.md`、`CONTEXT.md`、sidecar README與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P5.7 cross-engine parity evaluation。

### P5 Progress Review — P5.7 — 2026-07-15

- Scope completed：新增frozen/content-hash `EngineParityPolicy/Request/Report`、reference-baseline evaluator、bounded per-dimension comparison與真實雙sidecar smoke matrix。每個candidate先經P5.4 `run_backtest` exact validation，再比較order outcomes、fill schedule/quantity/price/fee/slippage、cash、positions、total fee與warnings；canonical threshold固定0，report明示`fixture_canonical_semantics_only`與`adapter_normalized_not_native_matching`。
- Fail-closed / authority：runtime、image、job、result、semantic、warning及fill provenance只保存bounded counts/hashes，不傳raw warning；不含selected/preferred engine、average、promotion、target、order或paper authority。Disabled engine回`CAPABILITY_DENIED`，failed clock/engine、late或tampered result回既有structured failure，全部不產可能被誤讀為equivalent的report。Core dependency/security test證明無sidecar/heavy runtime、execution、risk、ledger或persistence import。
- Real replay / LEAN regression：7組fixture涵蓋MARKET/LIMIT、BUY/SELL、DAY/GTC/IOC、partial、shared-volume、multi-session、unfilled與halted；每個engine各重播2次，共28次authenticated HTTP execution，semantic exact match且各自native fill provenance stable。實測發現LEAN首根bar前scheduled event無data clock而遺失；以first-bar同open、volume=0的native-only bootstrap修正並新增GTC/IOC regression，最終runtime hash為`ca04cdf4d8cfe4e7f4dc4caf6deab622a21e77322b7d2956fb5b93a262834087`、image digest為`sha256:a8fa44799d6298dd343382d842665b2185fd3f7635ba0cab6435fd0a453d3857`。
- Verification：focused core/backtest/parity/security為33 passed、LEAN sidecar為22 passed，Ruff、strict mypy與actionlint全通過；最終LEAN image的Syft SBOM為4,754 components/166 packages，Grype為0 High/Critical。完整PostgreSQL gate為1313 passed、coverage 87.32%，511 files format、ruff、mypy 282 source files、91 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。CI新增internal-network雙image real replay並保存30日bounded provenance artifact。
- 文件同步：`README.md`、`CONTEXT.md`、LEAN README與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P5.8 RD-Agent sandbox worker。

### P5 Progress Review — P5.8 — 2026-07-15

- Scope completed：新增16個frozen/hash-bound RD proposal、label-free dataset、runtime/job/invocation、one-shot run、雙run aggregate與draft evaluation schemas，以及default-deny AST allowlist。只接受單一`compute(rows)` factor subset；stochastic generation provenance先封存，deterministic replay從archived source開始。Worker只回canonical predictions/draft artifacts，沒有target/order/risk/ledger/registry/promotion authority。
- Isolation / replay：可信launcher用相同job啟動兩個不同fresh containers；instance ID由實際container ID衍生，core要求policy/runtime/source/dataset/fence與canonical bytes完全一致，再重跑相同scanner、重建direction/exposure/turnover並呼叫P3.4完整evaluation。Candidate只見immutable label-free rows；fixed `python -I -S`、restricted builtins、clean env、no shell/stdin/log、RLIMIT與container CPU/RAM/PID/time/output bounds全部fail closed。
- Supply chain / actual runtime：pinned RD-Agent commit `4f9ecb0`、MIT archive/license hashes與獨立48-package dev lock；upstream source只以archive保存，不進PYTHONPATH也不執行。Final Alpine/Python 3.12.13 image為UID/GID 65532、network none、read-only、cap-drop/NNP/AppArmor/private IPC；移除tar/XML/HTML/compression/webbrowser/Windows asyncio/SQLite/system pip capabilities。593-component SBOM/27 packages無SQLite、pip或heavy runtime；10個Python High以exact `pkg:generic/python@3.12.13` OpenVEX標記vulnerable code not present，任何新High/Critical仍由Grype拒絕。
- Verification：focused root為31 passed、獨立worker為28 passed，Ruff、strict mypy、actionlint、Compose、pip-audit、schema、source/license與actual escape/network/CPU/output/reproducibility smoke全通過。Final runtime hash為`592710a3915da6fe45d8245d76d69c68263d31f9275df400b0b81285241bb9fe`，image digest為`sha256:62c9003c50793f35414a571e59ec4acacf7188649492624e75d200c146d08e5e`。完整PostgreSQL gate為1344 passed、coverage 87.41%，529 files format、ruff、mypy 285 source files、106 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`、worker README與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。下一項為P5.9 optional integration manifests and feature flags。

### P5 Progress Review — P5.9 / Phase Gate — 2026-07-15

- Scope completed：新增frozen typed optional feature catalog與flags，固定11個integration的kind、exact Compose profiles、config paths、environment allowlist、network、output scope、readiness/execution denial及supply-chain metadata。缺檔回全關閉；malformed、unknown、live mode、authority或boundary drift皆structured fail closed。Freqtrade、FinRL、vectorbt只有future RFC條目，無image/profile/dependency。
- Deployment / isolation：新增zero-default `compose.optional.yaml`與Nautilus hardened manifest；OpenBB、TradingAgents、Kronos CPU/CUDA、Qlib、Nautilus、LEAN、RD-Agent共10個explicit profiles可逐一render，aggregate沒有core/database/broker/default service或`depends_on`。Safe render placeholder不會繞過sidecar runtime identity/token/model hash驗證，RD-Agent仍是network-none one-shot sandbox。
- Supply chain / operations：catalog逐image綁定獨立lock、NOTICE、source identity/license、SBOM mode/ref與fail-on-High CVE policy；core lock掃描拒絕heavy runtimes。新增operator runbook與Linux CI，驗zero-default/所有profiles、typed boundary、future RFC及core dependency isolation；OpenBB AGPL corresponding source流程與AI-Trader no-copy boundary保持不變。
- Verification：focused catalog/security為14 passed；zero-default與10 profiles Compose render全通過。完整non-PostgreSQL gate為1119 passed、239 deselected、coverage 87.64%；完整PostgreSQL P5 gate為1358 passed、coverage 87.43%，532 files format、ruff、mypy 286 source files、106 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- 文件同步：`README.md`、`CONTEXT.md`、optional runbook與本review已更新；`AGENTS.md`/`CLAUDE.md`已review且現有規範已完整涵蓋，不需變更。P5 phase gate與success criteria全部通過，下一項為P6.1 production OIDC/RBAC and service identities。

### P6 Progress Review — P6.1 — 2026-07-16

- Scope completed：新增frozen human/service principal、`config/rbac.yaml`、asymmetric OIDC/JWKS verifier與runtime factory、central FastAPI dependency、owner-scoped migration 0015及application IDOR checks。Unknown role/claim/permission、forged actor/role、foreign account/strategy/evaluation/research run/report/snapshot、stale ownership與production local-token/CLI composition皆fail closed。
- Service identity / isolation：core-only RS256 issuer以exact issuer/audience/azp/permission/target/generation/nonce/deadline簽發短效credential；Kronos、TradingAgents、Qlib、Nautilus、LEAN、OpenBB ingress在載入heavy runtime前驗證bounded body與完整fence。Remote service無DB credential、人類角色、operator/admin/reviewer或paper authority；public JWKS與private key分離，rotation/outage/runbook、source identity及OpenBB AGPL corresponding source流程已固定。
- Verification：完整單程序PostgreSQL gate為1581 passed、3 skipped、coverage 87.45%；564 files format、Ruff、strict mypy 301 source files、106 schemas、Alembic無drift、upstream/license、secret scan與locked dependency audit全通過。Focused auth/service/ownership、ephemeral issuer/JWKS manifests、所有optional Compose profiles及OpenBB hardened live smoke皆通過。
- 文件同步：`README.md`、`CONTEXT.md`、OIDC rotation runbook與本review已更新；`AGENTS.md`/`CLAUDE.md`同步production identity不變量。尚未宣稱真實外部IdP或跨host TLS/mTLS；P6全域gate仍未完成，下一項為P6.2 secret provider and redaction。

### P6 Progress Review — P6.2 — 2026-07-17

- Scope completed：新增transport-neutral frozen `SecretRef/SecretAccessRequest/ResolvedSecret`、runtime-checkable provider port及env/cloud/factory strategies。Local/development/test只做exact logical ref/purpose→env lookup；staging/production只接受injected workload-identity client與exact resource binding。Missing、blank、control、oversize、disabled、expired、wrong-scope與outage皆無stale/env fallback，secret value不進repr/str/model dump/error。
- Rotation / consumers：OpenAI、Anthropic、Financial Datasets與AI-Trader constructor只保存provider與logical ref；每個logical request resolve一次，bounded HTTP retry與LLM repair固定同version，下一次request取得rotation。Provider failure在任何network、artifact或quota consumption前停止，保留`CONFIGURATION_INVALID`/`DATA_UNAVAILABLE`語意並使用public-safe訊息。
- Redaction / persistence：pure bounded sanitizer涵蓋nested mapping/sequence/set、Pydantic/dataclass、bytes/exception、known values、cycle/depth/item/string limits、credential URL/JWT/PEM/provider key；StructuredError、standard logging formatter、canonical report與Jinja artifacts均在sink前sanitize。Run event、job、outbox及last_error使用secret-free JSONB bind guard，偵測時不修改hash-bound payload、以無敏感SQL參數的safe exception整筆rollback；真實PostgreSQL測試證明run/event/outbox均0 writes。
- Verification / boundary：focused matrix為187 passed；完整non-PostgreSQL gate為1404 passed、3 skipped、244 deselected、coverage 87.66%。完整PostgreSQL gate為1648 passed、3 skipped、coverage 87.46%；582 files format、Ruff、strict mypy 309 source files、106 schemas、Alembic無drift、upstream/license、secret scan、actionlint與locked dependency audit全通過。尚未連接真實cloud secret manager，不宣稱live integration；下一項為P6.3 API security controls。

### P6 Progress Review — P6.3 — 2026-07-17

- Scope completed：五個FastAPI app改用單一typed security composition，統一body byte/frame cap、body前edge/credential admission、body後verified-principal rate limit、exact CORS、security headers、forwarded identity拒絕與structured exception envelope。Cookie auth維持explicit opt-in，要求canonical same-origin與double-submit CSRF；bearer-only模式拒絕ambient auth cookie。
- SSRF / rendering：新增exact scheme/host/port/path outbound guard、全public DNS集合驗證與httpcore pinned TCP transport；webhook拒絕userinfo/query/fragment、redirect、DNS rebind及所有非public位址。Jinja autoescape、Markdown escaping、CSP與redaction回歸均通過；custom webhook client只允許test注入。
- Adversarial closure：補上256-frame上限、pre-auth credential/direct-peer admission、principal二段quota、credential SHA-256 key、heap expiry、clock/store safe 503、denied preflight JSON與forwarded header fail-closed。最終唯讀security review未找到可重現blocker/high。
- Verification / boundary：focused matrix為199 passed；完整non-PostgreSQL gate為1497 passed、3 skipped、244 deselected、coverage 87.42%。完整PostgreSQL gate為1741 passed、3 skipped、coverage 87.26%；593 files format、Ruff、strict mypy 316 source files、106 schemas、Alembic無drift、upstream/license、secret scan、actionlint、frozen lock與locked dependency audit全通過。Rate limit仍為單process store；trusted proxy/distributed enforcement、DNS resolver lifetime/timeout pin與HSTS未宣稱完成，下一項為P6.4 production observability。

### P6 Progress Review — P6.4 — 2026-07-17

- Scope completed：新增frozen W3C `TraceCarrier/TraceContext/CorrelationContext`、四個canonical metrics、typed telemetry ports、contextvars、redacting log correlation、sync/async OperationRecorder及exact OTel SDK/OTLP runtime。Root trace預設sampled；endpoint、span/metric attributes、resource、sampler、limits、batch與lifecycle均bounded，ambient `OTEL_*`、proxy、`.netrc`、redirect、credential與resource override fail closed。
- Propagation / authority：五個FastAPI app在validation/error/rate-limit paths回傳bounded trace/request metadata；0016為job/outbox/inbox新增獨立nullable trace欄位並跨API→queue→worker→completion/delivery延續，不進payload hash且不改generation/nonce/lease。Queue/worker與provider、LLM/model、signal、risk、execution、reconciliation、delivery均manual instrument；recorder即使skip、replace、swallow、duplicate或前後失敗，也只能取得同一canonical result/exception。
- Infra / smoke：官方digest-pinned Collector、Prometheus、Grafana以internal backend＋loopback ingress、non-root、read-only、cap-drop/NNP、external secrets、resource limits與provisioned dashboard啟動。真實三容器smoke由core OTLP exporter送出trace/metrics，驗證host health、Grafana health及collector exact metric/label catalog；Grafana ambient plugin/update/news/live均關閉。
- Verification / boundary：完整non-PostgreSQL gate為1620 passed、3 skipped、258 deselected、coverage 87.75%；完整PostgreSQL gate為1878 passed、3 skipped、coverage 87.48%。617 files format、Ruff、strict mypy 323 source files、106 schemas、Alembic無drift、upstream/license、secret scan、actionlint、frozen lock與locked dependency audit全通過，三容器OTLP runtime smoke包含在final gates。Trace目前送到nop sink且狀態為tmpfs，未接真實remote backend或multi-host TLS/network policy；response/durable synthetic span尚未回綁SDK child span ID。下一項為P6.5 alerts、budgets與SLOs。

### P6 Progress Review — P6.5 — 2026-07-17

- Scope completed：新增strict versioned `config/{budgets,slo}.yaml`、frozen operational budget contracts與low-cardinality SLO metrics。Cost只接受Decimal字串、latency只接受同一monotonic clock讀值，`within→degraded→failed`只能等級上升；缺失、rollback、非有限或無效usage均fail closed。
- Fail-closed / no chase：research與paper cycle在外部及durable canonical stage前重驗budget；soft/hard超限後不再建立target、reservation或order。PostgreSQL將`budget_exhausted`記為非重試terminal，既有canonical commit不受observer改寫且不補追訂單。
- Alerts / honest boundary：新增correctness、API/worker availability、p95 latency、normalized 5m/1h/30d burn、hard outcome與soft usage rules，通過pinned `promtool`與真實Collector→Prometheus smoke。Correctness series以0初始化以偵測缺失，但counter為0不能單獨證明完整coverage；目前只有policy routing、tmpfs狀態且無Alertmanager/paging送達。
- Verification / boundary：focused budget/SLO/canonical/rules matrix與真實三容器smoke全通過；完整non-PostgreSQL gate為1679 passed、3 skipped、259 deselected、coverage 87.85%，完整PostgreSQL gate為1938 passed、3 skipped、coverage 87.57%。630 files format、Ruff、strict mypy 329 source files、106 schemas、Alembic無drift、upstream/license、secret scan、actionlint、frozen lock與locked dependency audit全通過。文件已同步；下一項為P6.6 S3-compatible artifact adapter與retention。

### P6 Progress Review — P6.6 — 2026-07-18

- Scope completed：新增strict artifact config、frozen encryption/retention/capability/maintenance contracts、S3 content-addressed adapter、official botocore SigV4/httpx transport、signed preflight、bounded XML parser、retention/legal-hold/orphan-GC/exact-version restore use cases及PostgreSQL 0017 append-only maintenance audit。Finalize固定object-first/manifest-last、SHA-256、conditional PUT、SSE與retain-until；presigned capability只允許單一finalized GET且不進序列化或durable sink。
- Safety / durability：production只接受injected atomic credentials，exact HTTPS origin/bucket/prefix/owner、DNS/IP pinning、no redirect及no ambient credential/proxy chain。Versioning/Object Lock無法由provider證明即fail closed；retention/legal hold只增不減，GC遇到任何historical finalized manifest、DB reference、hold/retention/unknown或provider failure均保留，restore只刪exact latest delete marker或驗證指定version，永不覆寫canonical bytes。
- Runtime / provenance：digest-pinned SeaweedFS 4.34以non-root、read-only、cap-drop/NNP、resource limits、loopback ingress及runtime-generated credentials完成真實SigV4、conditional finalize/retry、SHA-256/checksum/SSE metadata roundtrip、conflict與direct presigned GET smoke；Apache source/license identity與notice已固定。Emulator不證明真實cloud IAM、SSE-KMS、Object Lock或各S3 vendor完整相容性。
- Verification：focused artifact/config/security/infra為138 passed，PostgreSQL migration/audit為34 passed；完整non-PostgreSQL gate為1817 passed、3 skipped、267 deselected、coverage 87.69%，完整PostgreSQL gate為2084 passed、3 skipped、coverage 87.45%。660 files format、Ruff、strict mypy 343 source files、106 schemas、Alembic無drift、upstream/license、secret scan、frozen lock與locked dependency audit全通過。`README.md`、`AGENTS.md`/`CLAUDE.md`、`CONTEXT.md`、runbook與本review已同步；`tasks/lessons.md`經review無新的使用者修正可新增，下一項為P6.7 deployment manifests。

### P6 Progress Review — P6.7 — 2026-07-18

- Scope completed：新增strict deployment/DB role settings、deployment-only FastAPI control surface與typed `stonks-deploy serve/migrate/probe`。Liveness不依賴DB；readiness以bounded query要求exact single Alembic head。Raw/ambient DSN、password/libpq authority、unknown key、非paper mode、任意deployment root與schema drift皆fail closed，CLI錯誤只輸出public-safe envelope。
- Image / topology：root multi-stage `Dockerfile`固定uv 0.9.27與Python 3.12.13 digest，只安裝frozen production lock；runtime為UID/GID 65532且無uv/pip/compiler/tests/heavy upstream。Default Compose只有core/PostgreSQL，migration為explicit one-shot profile；PostgreSQL以UID70/SCRAM、private network、named volume執行，core只綁loopback。所有service為read-only、cap-drop/NNP、tmpfs及CPU/RAM/PID/restart bounded。
- Authority / replay：migrator使用owner secret並以advisory lock升級；runtime login先由libpq產生SCRAM verifier，只取得`stonks_app` membership，無superuser/DDL/role/database/replication authority。Committed smoke由乾淨volume執行migration三次、fake-cycle exact compare、persisted workflow write、core/DB restart、DB outage health200/ready503、full down/up、replay/verify succeeded version3、rootfs/image/secret檢查並必定cleanup；明示不是fresh stochastic re-inference。
- Optional / CI / honest boundary：main與P5.9 zero-default manifest合併後10個explicit profiles逐一render，core不依賴optional。Linux CI新增real build/deployment smoke。Core container目前只是deployment health/readiness surface，尚未組合五組business API或常駐dispatcher；public TLS/HSTS、trusted proxy/mTLS、external IdP、orchestrator、managed DB TLS/backup與跨主機network policy仍未宣稱完成。
- Verification：focused deployment/config/entrypoint/policy/smoke為71 passed，真實PostgreSQL role/migration為1 passed；clean image/Compose smoke全通過。完整non-PostgreSQL gate為1888 passed、3 skipped、268 deselected、coverage 87.54%，完整PostgreSQL gate為2156 passed、3 skipped、coverage 87.37%。672 files format、Ruff、strict mypy 346 source files、106 schemas、Alembic無drift、upstream/license、secret scan、frozen lock與locked dependency audit全通過。文件、規範、lessons與本review已同步；下一項為P6.8 supply-chain release gates。

### P6 Progress Review — P6.8 — 2026-07-22

- Scope completed：新增versioned paper-only release policy、closed manifest/bundle verifier、canonical CycloneDX SBOM/license inventory、exact Grype DB/OpenVEX、OpenAPI snapshots，以及least-privilege security/tag-only release workflows。所有GitHub Actions、Syft/Grype/Cosign versions皆immutable；release在registry publish前先驗unsigned candidate，再只簽registry exact digest並重驗GitHub OIDC workflow identity、manifest/report signatures與provenance/SBOM attestations。
- Source / legal closure：Linux core由bundled `psycopg-binary`改為source-built `psycopg-c`＋Alpine `libpq 18.4-r0`。OpenBB archive為26 members/421,673 bytes；Python archive封存certifi、psycopg、psycopg-c三個exact sdists；Alpine archive由實際37-package DB封存27 origins、244 files、133,074,248 source bytes。三者都以canonical tar/gzip、closed manifest及member hash/size/path重驗；OpenBB/Alpine兩次、Python三次獨立產生bytes相同。
- Image / vulnerability evidence：clean core image runtime證明`psycopg.pq`為C implementation、無`psycopg_binary`、system `libpq`與CPython license存在。Canonical inventory為97 packages、865 components，reviewed hash `b1584f2a...6bd4`；Grype DB v6.1.9掃描為17 Medium、2 Low、1 Negligible、0個未抑制High/Critical，10個Python High只由exact OpenVEX抑制。
- Bundle / verification：本機unsigned candidate含192 artifacts、136,809,165 bytes，manifest、paper identity、image report、locks、reports、SBOM/license、Grype DB/VEX、notices及三組source closure全部通過。Formal keyless path只由protected tag/release environment執行；本機沒有偽造OIDC、發布registry或宣稱signature/provenance已產生。
- Verification：focused release/source/policy matrix為99 passed，actionlint、Ruff、strict mypy與actual archive verification全通過。完整non-PostgreSQL gate為2004 passed、6 skipped、268 deselected、coverage 87.52%；完整PostgreSQL gate為2272 passed、6 skipped、coverage 87.36%，699 files format、Ruff、mypy 346 source files、106 schemas、Alembic無drift、upstream/license、secret、frozen lock與dependency audit全綠。`README.md`、`AGENTS.md`/`CLAUDE.md`、`CONTEXT.md`、legal/security/runbook與本review已同步；下一項為P6.9 failure injection與disaster drills。

### P6 Progress Review — P6.9 — 2026-07-22

- Scope completed：新增paper-only `stonks-resilience-drills/1` frozen catalog、11項failure class/injection point/terminal state/forbidden side effect/audit/metric/recovery contract，以及六份operator runbook。Metric evidence直接重驗既有低基數telemetry catalog；unknown、duplicate、partial、contract mismatch、forbidden side effect、缺evidence/precondition與unsafe recovery全數fail closed。
- Failure matrix：`tests/resilience/`證明provider/LLM/model/sidecar outage在target/order前停止；artifact hash corruption不可重播；duplicate result只commit一組event/outbox，payload drift拒絕；expired lease/stale generation+nonce結果進quarantine，current lease只commit一次；retry exhaustion進dead-letter且不得追單；ledger mismatch先rollback、另transaction啟global kill switch，projection修復後仍維持execution closed。
- Actual restore / security：digest-pinned PostgreSQL `17.10-alpine` drill使用fresh source/target、random loopback ports、temporary 0600 secret file、bounded custom dump與stdin restore。Preflight與exact labels確保只cleanup本次owned資源；source/target system IDs、Alembic `0017`、2-event canonical digest/replay、source marker、append-only update/delete及invalid-chain probes全通過，且無container/network殘留。本次獨立量測RTO 0.719秒、RPO 0秒/0 lost events，明示只為single-host drill measurement、不是production SLA，且restore永不自動promote。
- Verification：focused resilience/restore matrix為55 passed，actionlint、Ruff、format與strict mypy全通過；完整non-PostgreSQL gate為2059 passed、6 skipped、268 deselected、coverage 87.65%，完整PostgreSQL gate為2327 passed、6 skipped、coverage 87.47%。713 files format、Ruff、mypy 348 source files、106 schemas、Alembic無drift、upstream/license、secret、frozen lock與dependency audit全綠。`README.md`、`AGENTS.md`/`CLAUDE.md`、`CONTEXT.md`、runbooks與本review已同步；`tasks/lessons.md`無新的使用者修正可新增，下一項為P6.10 performance與resource budgets。
