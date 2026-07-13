# Stonks Agent 實作計畫

> 狀態：執行中（P0、P1、P2 gate 已通過，P3.1–P3.7 已完成，P3.8 進行中）
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

- [ ] **P3.8 Kronos evaluation and promotion** — 以golden跨市場snapshots執行walk-forward、baseline、成本與calibration報告；建立`config/strategies/kronos.yaml`。（Depends：P3.4、P3.7；Complexity：XL；Risk：High）
  - 未達預先固定門檻時保持`shadow`/weight 0，不能為了整合而降低門檻。

- [ ] **P3.9 Qlib quant-lab worker** — 建立`workers/quant_lab/{pyproject.toml,uv.lock,Dockerfile,README.md,app.py,qlib_adapter.py}`與`tests/contracts/workers/test_qlib.py`。（Depends：P3.1、P3.4；Complexity：XL；Risk：High）
  - `QuantResearchJob`只收immutable snapshot、feature/label/universe/cost/split specs；回傳predictions/positions/metrics/artifact hashes/provenance。
  - 不依賴已暫停的官方dataset；由canonical snapshot converter供應資料。

- [ ] **P3.10 Forecast/signal/evaluation API and CLI** — 建立`entrypoints/api/routes/{strategies,signals,evaluations}.py`、`entrypoints/cli_commands/strategy.py`。（Depends：P3.2–P3.9；Complexity：L；Risk：Medium）
  - Promotion endpoint需`strategy_reviewer`權限，且不能建立live state。

### P3 Verification gate

- [x] Leakage/PIT/survivorship fixtures故意污染時evaluation必須失敗。（Depends：P3.4）
- [ ] Opinion mapper default disabled；未評估opinion、Kronos或PEAD signal權重必為0。（Depends：P3.5、P3.8）
- [ ] Kronos archived-artifact replay、CPU smoke、可用時GPU schema/tolerance smoke、model checksum與calendar/validity tests通過；fresh stochastic inference不作bit-identical gate。（Depends：P3.6–P3.8）
- [ ] Qlib deterministic job同snapshot/runtime可重播相同artifact hashes；任何stochastic model則重播封存output artifact，worker無core DB/execution credentials。（Depends：P3.9）
- [ ] Strategy promotion/suspend/retire均有immutable audit event與CAS conflict tests。（Depends：P3.2、P3.10）

### P3 Success criteria

- [ ] 每個可參與paper portfolio的signal都有strategy/evaluation/data/runtime provenance。
- [ ] Forecast與agent opinion只能經明確promotion path影響target。
- [ ] 重型ML/quant dependencies完全留在各自worker environment。

---

## P4 — Deterministic portfolio、risk、paper execution 與 ledger

### Outcome

把P0已驗證的in-memory閉環升級為唯一PostgreSQL-backed canonical paper fund；portfolio/risk/account reservation/execution/balanced journal全部deterministic、idempotent、可重播，且與LLM/AI-Trader/OpenBB權限隔離。

### Tasks

- [ ] **P4.1 Portfolio/risk/reservation/execution domain** — 將P0 primitives升級為`domain/{portfolio,risk,reservations,orders,fills,journal}.py`、`ports/{portfolio_policy,risk_policy,execution,ledger}.py`與property tests。（Depends：P3 gate；Complexity：XL；Risk：High）
  - Decimal、rounding、order state transitions、sequence/hash與accounting invariants明確。
  - `RiskDecision`綁account aggregate/portfolio sequence並有expiry；sequence變更必須重評。Risk approval本身不保留資金，必須建立reservation才可形成command。

- [ ] **P4.2 Trading persistence** — 新增`migrations/versions/0003_paper_trading.py`、repositories與DB permissions。（Depends：P4.1、P1.4；Complexity：XL；Risk：High）
  - 建立account aggregate、cash/position reservations與projections、risk decisions、order intents/events、fills、balanced journal transactions/postings、kill switch。
  - Order idempotency、event sequence與append-only由DB constraint保護。

- [ ] **P4.3 Deterministic portfolio baseline** — 建立`application/portfolio/build_target.py`、`config/policies/portfolio_v1.yaml`與golden/property tests。（Depends：P3.1、P4.1；Complexity：L；Risk：High）
  - Fixed ensemble weights、confidence calibration、deadband、shrinkage、turnover penalty、position bounds與stable ordering。
  - Missing signal不重新正規化造成過曝；輸出calculation hash與cost diagnostics。

- [ ] **P4.4 Hard risk gate** — 建立`application/risk/evaluate.py`、`config/policies/risk_v1.yaml`與boundary tests。（Depends：P4.1、P4.3；Complexity：XL；Risk：High）
  - 檢查data/signal freshness、cash、pending orders、single/sector/asset/gross/net、turnover、ADV、market session、drawdown/daily loss、kill switch。
  - Unknown/conflict/stale required state、unsupported asset/order或ledger mismatch一律reject。
  - Risk核准到`OrderIntent`建立必須在per-account serialized transaction/advisory lock內重新確認sequence並原子reserve cash/sellable position；所有open reservations納入available state。

- [ ] **P4.5 Reference paper broker** — 建立`adapters/execution/paper.py`、`domain/execution_model.py`、`config/execution/paper_v1.yaml`與golden fill tests。（Depends：P4.1、P4.4；Complexity：XL；Risk：High）
  - Market/limit/expiry/partial fill、fees/slippage/spread/volume participation語義版本化。
  - 只用command後第一個可交易bar；無future bar不製造fill。
  - 同idempotency key + same payload回同receipt；same key + different payload fail closed。
  - 每個accepted command必須帶有效reservation；fill/cancel/reject/expire按account序列化並原子consume/release。

- [ ] **P4.6 Balanced journal and projections** — 建立`application/ledger/{post,replay,reconcile}.py`、`adapters/postgres/ledger_repository.py`與property tests。（Depends：P4.2、P4.5；Complexity：XL；Risk：High）
  - 每筆transaction至少兩個postings；每種currency/commodity經Decimal quantization後debit/credit sum為零，明確使用cash/inventory/fee/PnL/clearing accounts。
  - Cash/positions/fees/P&L只由journal推導；daily replay hash與DB projection一致。不平、gap、unknown order state觸發rollback與global paper kill switch。

- [ ] **P4.7 End-to-end workflow state machine** — 完成`application/workflows/run_cycle.py`各transition/retry/cancel/dead-letter；建立`tests/e2e/test_paper_fund_cycle.py`。（Depends：P1.6、P2.11、P3.10、P4.3–P4.6；Complexity：XL；Risk：High）
  - Flow固定`Evidence -> Research/Opinion/Signal -> PortfolioTarget -> RiskDecision -> OrderIntent -> ExecutionReceipt -> Ledger -> Report`。
  - Execution retry先query idempotency receipt；crash injection不得重複下單。

- [ ] **P4.8 Outcome monitoring and reflection evidence** — 建立`application/monitoring/{mark_to_market,outcomes,reflection_context}.py`與tests。（Depends：P4.6、P2.8；Complexity：L；Risk：Medium）
  - 保存raw return、benchmark alpha、drawdown、fees、fills與outcome evidence；LLM reflection只是新ResearchArtifact，不改歷史decision。

- [ ] **P4.9 Kill-switch and operator use cases** — 建立`application/operations/{activate_kill_switch,reconcile,resume}.py`、CLI/API routes與audit tests。（Depends：P4.4–P4.7；Complexity：L；Risk：High）
  - 啟動後拒絕新commands並取消可取消pending orders；不刪ledger或隱藏fills。
  - Resume必須reconciliation通過且由`paper_operator`/`admin` audited action完成。

- [ ] **P4.10 Portfolio/report projections** — 更新`AnalysisReport`加入target/risk/order/fill/outcome refs；建立portfolio/NAV/risk CLI/API projections。（Depends：P4.7、P4.8；Complexity：M；Risk：Medium）

### P4 Verification gate

- [ ] Property tests覆蓋position/cash、open reservations、concurrent double-spend/oversell、risk bounds、Decimal rounding、order transition與每資產journal平衡。（Depends：P4.1–P4.6）
- [ ] 同snapshot/config重跑得到相同target/risk/order/fill/ledger hashes。（Depends：P4.7）
- [ ] Crash/retry/duplicate/concurrent-account tests無重複paper order/fill、雙花或超賣；late worker result無法commit。（Depends：P4.4–P4.7）
- [ ] Stale/conflict/unknown/kill-switch/ledger mismatch fixtures全部reject並留下reason/audit。（Depends：P4.4、P4.9）
- [ ] E2E small portfolio完成report、replay、reconciliation，且AI-Trader/LLM/Kronos無execution credential/path。（Depends：P4.7–P4.10）

### P4 Success criteria

- [ ] Paper fund從schedule/API到ledger/report形成可重播閉環。
- [ ] 零重複paper orders、零future-data fills、零LLM risk override。
- [ ] 所有portfolio與report projection可由immutable events/artifacts重建。

---

## P5 — Optional ecosystem integrations 與 advanced evaluation

### Outcome

在不改canonical semantics與paper-only邊界下，接入AI-Trader external community/control API、Nautilus/LEAN simulation backends與sandboxed RD-Agent strategy lab。所有integration可關閉、可替換、各自獨立lock/image。

### Tasks

- [ ] **P5.1 External platform contracts** — 擴充`packages/contracts/src/stonks_contracts/platform.py`、`ports/platform.py`與schemas；定義publish thesis、poll feedback、challenge/experiment與external evidence。（Depends：P4 gate；Complexity：M；Risk：High）
  - 不定義submit order/copy trade為canonical operation；remote positions只能是external evidence。

- [ ] **P5.2 AI-Trader public HTTP adapter** — 建立`adapters/platform/ai_trader.py`、`config/platforms/ai_trader.yaml`、runtime-schema cassettes與contract tests。（Depends：P0.3、P5.1；Complexity：L；Risk：High）
  - 只使用external control/community endpoints：publish去敏thesis、discussion/reply、challenge/team/experiment、heartbeat/events。
  - 不import/vendor/clean-room重做其server/frontend，不呼叫paper/copy execution作canonical order path。
  - Typed tolerant reader、heartbeat cursor/inbox dedup、scoped token、schema/authz anomaly kill switch。

- [ ] **P5.3 Community feedback policy** — 建立`application/research/community_feedback.py`、reputation/deadline/prompt-injection fixtures。（Depends：P5.2、P2.2；Complexity：L；Risk：High）
  - Feedback轉`ExternalEvidence`，policy只可ignore、降低confidence或建立new research job；不可直接升成signal/order。

- [ ] **P5.4 Backtest engine contract** — 擴充`BacktestJob/Result` schemas與`ports/backtest_engine.py`；建立canonical orders/fills/positions/calendar/cost parity suite。（Depends：P4.1、P4.6；Complexity：L；Risk：High）

- [ ] **P5.5 NautilusTrader adapter** — 建立`sidecars/nautilus/{pyproject.toml,uv.lock,Dockerfile,README.md,app.py}`與contract/replay tests。（Depends：P0.3、P5.4；Complexity：XL；Risk：High）
  - LGPL runtime/types不滲入core；記錄engine/runtime/license/version與完整fills。

- [ ] **P5.6 LEAN adapter** — 建立`sidecars/lean/{Dockerfile,README.md,appsettings.template.json,adapter/}`與job/result contract tests。（Depends：P0.3、P5.4；Complexity：XL；Risk：High）
  - C#/Docker保持external sidecar；calendar/corporate action/fees/slippage mapping明確。

- [ ] **P5.7 Cross-engine parity evaluation** — 建立`tests/parity/{paper_nautilus_lean.py,fixtures/}`與`application/evaluation/engine_parity.py`。（Depends：P4.5、P5.5、P5.6；Complexity：XL；Risk：High）
  - 差異超預設threshold時標engine-specific，不能把結果平均或宣稱等價。

- [ ] **P5.8 RD-Agent sandbox worker** — 建立`workers/quant_lab/rd_agent/{Dockerfile,uv.lock,README.md,sandbox_policy.yaml,adapter.py}`與escape/reproducibility tests。（Depends：P0.3、P3.4、P3.9；Complexity：XL；Risk：High）
  - Linux-only、ephemeral、read-only dataset、no core secrets、default no egress、CPU/RAM/time限制。
  - 只輸出draft source/artifacts/evaluation request；核心重新靜態掃描與完整evaluation，絕不auto-promote。

- [ ] **P5.9 Optional integration manifests and feature flags** — 建立`config/features.yaml`、`infra/compose.optional.yaml`、`docs/runbooks/optional-integrations.md`。（Depends：P5.2、P5.5、P5.6、P5.8；Complexity：M；Risk：Medium）
  - 所有optional integration default off；未配置不影響core readiness。
  - Freqtrade、FinRL、vectorbt只保留future RFC條目，不在本phase安裝。

### P5 Verification gate

- [ ] AI-Trader adapter測試無order/copy endpoint且不在execution dependency graph；duplicate heartbeat/events去重。（Depends：P5.2）
- [ ] Community prompt injection/reputation/deadline tests證明feedback不能直接成signal/order。（Depends：P5.3）
- [ ] Nautilus/LEAN parity fixtures產生可解釋差異與完整provenance；任一sidecar關閉時core仍通過。（Depends：P5.7）
- [ ] RD-Agent sandbox escape、network、resource、malicious candidate與non-reproducible artifact fixtures全部fail closed。（Depends：P5.8）
- [ ] 每個image有獨立lock、SBOM、license notice/source manifest，core lock無新增重型依賴。（Depends：P5.9）

### P5 Success criteria

- [ ] External community、quant lab與simulation engines能增加evidence/evaluation能力但無法改寫canonical control plane。
- [ ] Optional services可單獨部署、升級、停用，不改domain contracts或paper safety。
- [ ] 授權不清/strong-copyleft元件均維持正確external boundary與release流程。

---

## P6 — Security、observability、resilience 與 release hardening

### Outcome

把P0起即存在的permission checks、redaction、telemetry、idempotency/fencing與failure tests替換/擴充為production-grade OIDC、secret manager、exporters、fault drills、deployment與supply-chain gates；仍不開live trading。此phase不得成為前面各phase缺少基本security/reliability的藉口。

### Tasks

- [ ] **P6.1 Production OIDC/RBAC and service identities** — 將P0 local principal/permission port接到`adapters/auth/oidc.py`、`entrypoints/api/dependencies/auth.py`、`config/rbac.yaml`並擴充authn/authz tests。（Depends：P0.9、P4 gate；Complexity：XL；Risk：High）
  - Roles：viewer/researcher/strategy_reviewer/paper_operator/admin；worker/executor service accounts最小權限。
  - Object/target ownership與route-level authz完整，不能重現AI-Trader任意target agent問題。

- [ ] **P6.2 Secret provider and redaction** — 實作`ports/secret_provider.py`、`adapters/secrets/{env,cloud}.py`、structured log/event/report redaction tests。（Depends：P6.1；Complexity：L；Risk：High）
  - Local env只存named refs；正式deployment使用secret manager、rotation與scoped identities。

- [ ] **P6.3 API security controls** — 實作request size/rate limit、CORS allowlist、SSRF endpoint allowlist、XSS-safe rendering、cookie模式CSRF與structured error sanitization。（Depends：P6.1、P6.2；Complexity：L；Risk：High）
  - 建立`tests/security/`涵蓋auth bypass、IDOR、prompt injection、SSRF、XSS、CSRF、secret leakage。

- [ ] **P6.4 Production OpenTelemetry exporters and metrics** — 將P0 trace/log/metric ports接到`adapters/observability/{logging,tracing,metrics}.py`、`infra/observability/{otel-collector,prometheus,grafana}/`。（Depends：P0.9、P1.6、P4.7；Complexity：L；Risk：Medium）
  - Correlation IDs、provider/queue/worker/LLM/model/signal/risk/execution/reconciliation/delivery metrics完整。
  - Logs不得包含raw secrets、tokens、unredacted sensitive evidence或完整prompts。

- [ ] **P6.5 Alerts, budgets and SLOs** — 建立`config/{budgets,slo}.yaml`、`docs/operations/slo.md`、alert rules。（Depends：P6.4；Complexity：M；Risk：Medium）
  - Correctness SLO：zero duplicate paper order、zero future evidence、100% claim provenance、100% replayable risk decision。
  - Cost/latency budget超限轉degraded/failed，不追單。

- [ ] **P6.6 S3-compatible artifact adapter and retention** — 實作`adapters/artifacts/s3.py`、retention/encryption/GC use cases與integration tests。（Depends：P1.5、P6.2；Complexity：L；Risk：High）
  - Object finalize/hash、signed scoped URLs、orphan GC、legal/data retention與restore測試。

- [ ] **P6.7 Deployment manifests** — 建立`infra/compose.yaml`、core `Dockerfile`、worker/sidecar profiles、health/readiness probes與non-root/read-only filesystem settings。（Depends：P5.9、P6.1–P6.6；Complexity：XL；Risk：High）
  - Default profile只啟動core/PostgreSQL；optional services顯式profile，OpenBB source流程一併發布。
  - `execution_mode=live`在任何manifest/schema都不存在。

- [ ] **P6.8 Supply-chain release gates** — 建立`.github/workflows/{security,release}.yml`、`scripts/{generate_sbom,verify_release}.py`、container signing與license/CVE policies。（Depends：P0.3、P6.7；Complexity：L；Risk：High）
  - Critical CVE、license drift、missing notice/source、unlocked dependency、secret scan failure阻擋release。

- [ ] **P6.9 Failure-injection and disaster drills** — 建立`tests/resilience/`、`docs/runbooks/{provider-outage,worker-crash,db-restore,ledger-mismatch,kill-switch,dead-letter}.md`。（Depends：P6.4–P6.7；Complexity：XL；Risk：High）
  - 演練provider/LLM/model/sidecar outage、DB restart、lease expiry、duplicate event、artifact corruption、ledger mismatch與restore/replay。

- [ ] **P6.10 Performance and resource budgets** — 建立`tests/performance/`與`docs/operations/capacity.md`，量測API、queue、snapshot、research、forecast、paper cycle；設定per-process CPU/RAM/concurrency budget。（Depends：P6.4、P6.7；Complexity：L；Risk：Medium）
  - 重型workers不能飢餓risk/execution；LLM/forecast queues獨立限流。

- [ ] **P6.11 Final docs and handoff sync** — 完成`README.md`、`docs/architecture/` ADRs、`docs/api/`、`docs/runbooks/`、`THIRD_PARTY_NOTICES.md`，同步精簡`AGENTS.md`、`CLAUDE.md`、`CONTEXT.md`、`tasks/todo.md`、`tasks/lessons.md`。（Depends：P6.1–P6.10；Complexity：L；Risk：Medium）
  - 文件只列實際驗證能力與限制；所有command由CI/本機重跑確認。

### P6 Verification gate

- [ ] Full CI：frozen builds、lint/type/unit/contract/integration/E2E/security/resilience、SBOM/license/CVE/secret scans全部通過。（Depends：P6.8、P6.9）
- [ ] Default compose由乾淨環境啟動、migrate、readiness、fake/replay E2E、shutdown/restart/replay全部通過。（Depends：P6.7）
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
