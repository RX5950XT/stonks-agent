# Stonks Agent 開發交接

更新日期：2026-07-13

## 目前狀態

- Git 已初始化於 `main`；`PLAN-AUTH` 已成立，依 P0 → P6 連續實作。
- P0 Foundation、P1 Canonical Data Hub、P2 Research control plane與P3 strategy/forecast/evaluation phase gates已完成；下一目標為P4.1 portfolio/risk/reservation/execution domain。
- P1/P3目前包含PostgreSQL 0001–0009、PIT evidence/snapshot、repositories/UoW、content-addressed artifacts、durable job/outbox/inbox、strategy registry、provider policy、US/HK/TW replay、snapshot API/CLI與canonical completion。
- Job/snapshot/outbox的claim、deadline、lease與commit timestamps使用transaction內PostgreSQL clock；generation/nonce、caller clock drift、cross-run retry與完整audit graph皆有真實PostgreSQL測試。
- Reconciliation成功決策封存雙側raw/normalized hashes、metric/value、threshold與decision；conflict維持0 artifact writes並留下hash-chained failure event/outbox。
- Financial Datasets與OpenBB已驗證read-only observation contracts與共用daily query；canonical materialization目前只宣稱replay source。`stonks-worker`只提供claim-once，不宣稱常駐dispatcher。
- Optional OpenBB sidecar已實測exact GET allowlist、frozen 64-package lock、SBOM/license policy、4個upstream sdists、AGPL source archive與non-root/read-only runtime。
- P2.1新增frozen evidence-scoped research/LLM contracts、immutable usage accounting、runtime-checkable research/LLM/tool ports與deny-by-default tool authorization；principal/profile/policy、instrument/evidence scope、typed args、timeout/output cap、audit redaction及result identity/hash/bytes皆fail closed。
- P2.2新增read-only PIT context builder、typed planning/final turn loop、pre-authorized parallel read tools與deterministic artifact builder；external content永遠維持untrusted，uncited claim降為hypothesis，budget/deadline/model/tool/scope錯誤皆hard-stop。
- P2.3新增frozen model policy、offline fake、OpenAI-compatible Chat Completions與Anthropic Messages adapters；固定HTTPS origin/endpoint、exact raw response artifact-first、local JSON Schema validation、bounded transient retry/invalid-output repair、deadline與cache-aware token/cost accounting均fail closed。
- P2.4新增pinned TradingAgents獨立worker；所有上游data tools改為request-scoped PIT canonical evidence facade，profile-per-process並serialize global config，唯一輸出為`AnalysisBundle/AgentOpinion`。獨立138-package lock、Apache notice與hardened image已驗證，heavy runtime未進core lock。
- P2.5改用shared signed-artifact wire contracts；core fixed-origin adapter驗證profile、artifact origin/expiry、generation/nonce、result hash與nested research context。worker只經fixed internal artifact service取內容並核對SHA-256；canonical completion由core PostgreSQL transaction一起寫artifact metadata/event/outbox/job ack，DB拒絕的stale result只進隔離audit port。
- P2.6選擇性重寫ai-hedge-fund MIT PEAD/event-study：PEAD只接受proven PIT filing、依report period dedup且排除future/unknown/stale/retrospective event；event study不用NumPy/SciPy，以Decimal/pure Python提供market-model OLS、CAR、Student t-test與seeded bootstrap。輸出永遠是draft `AlphaSignal`、confidence 0，無target/order authority。
- P2.7新增versioned immutable `AnalysisContext`與read-only evidence assembler；單次canonical query後依capability建立quality block，驗證subject/as-of、unique IDs、sensitivity/license/redistribution policy與block-ref exact coverage。DSA的available/missing/not_supported/fallback/stale/estimated/partial/fetch_failed vocabulary以既有自有`DataQualityStatus`吸收，另保留canonical conflict。
- P2.8擴充`AnalysisReport`為claim-linked JSON truth；structured draft只有outlook/score/confidence/claims等research欄位，core deterministic注入claim IDs、citation union、guardrails、model/prompt/policy與raw generation artifact refs。available evidence才可observed；fallback/estimated/stale/partial/missing/fetch_failed/conflict一律qualified，hypothesis不得帶fact metadata。
- P2.9新增sandboxed fixed-template Jinja adapter與clean full/brief Markdown、email HTML templates；所有輸出只讀同一`AnalysisReport`，autoescape/Markdown escape、quality qualifier、zh-TW/en labels、subject/brief truncation、channel byte caps、artifact metadata與render hash皆固定。未複製DSA template片段，因此無新增上游notice義務。
- P2.10新增artifact-backed delivery request/command/receipt contracts與fenced outbox consumer；console/file預設可用，email/webhook未配置時產生明確`skipped` receipt。所有adapter重驗SHA-256與idempotency identity；file限制fixed root、atomic replace並拒絕覆寫不同內容，webhook限制fixed HTTPS、no redirect、chunk idempotency key與bounded retry，錯誤只回public-safe code。
- P2.11新增paper-only `ResearchRunRequest`、atomic PostgreSQL run/job/snapshot link、verified run-event reader、queue-only API與CLI。SSE支援`Last-Event-ID`且只投影通過完整hash-chain驗證的canonical events；payload先secret redaction。Report API/CLI只讀source/license/sensitivity/template metadata符合renderer contract的artifact，任意LLM raw/prompt artifact不得經此能力讀取。
- P2.12新增`ResearchPipelineCommand/Result`與application pipeline gate；同一PIT context先驗deterministic artifact與TradingAgents opinion的run/as-of/evidence scope，再把兩者ID注入structured report attribution、完成三channel render與file delivery。每次succeeded/degraded/failed結果皆封存public-safe immutable audit；provider/deterministic/report outage fail，TradingAgents outage degrade，任何contract均無target/order。此為application-level gate，production常駐dispatcher與durable transition/commit仍由P4.7負責。
- P3.1新增immutable `StrategyManifest/StrategyRegistryEntry/EvaluationRequest/EvaluationReport/AlphaSignal/ForecastRequest/ForecastOutputArtifact`與runtime-checkable ports。Promotion graph不含live；evaluation與signal綁定exact manifest/data/runtime/policy hashes。任何unregistered、uncalibrated、stale、expired、non-paper-eligible或binding mismatch signal皆deterministic回零權重；stochastic forecast缺raw/path artifact即fail closed。
- P3.2新增0009 strategy registry/evaluation/audit tables、Postgres repository與UoW wiring。Registration idempotency、evaluation snapshot/artifact/hash binding、CAS promotion與hash-chain reader皆structured fail closed；DB triggers另行限制graph、version+1、DB clock、append-only rows與deferred matching audit，adapter被繞過也不能無audit commit。App update只限state/evaluation/version columns，worker無strategy grants。
- P3.3新增last-value、5-bar simple moving-average與5-bar OLS index-trend baselines；共用frozen manifest loader與PIT `BaselineSeries`，拒絕duplicate/future/unavailable/non-positive prices與不足lookback。所有統計採Decimal 12位quantization，輸出draft/research-only `ForecastSignal`，同輸入signal與payload hash deterministic。
- P3.4新增versioned content-hash evaluation policy、PIT/leakage/survivorship audit、purged walk-forward/embargo、bounded combinatorial PBO、cost sensitivity、performance metrics、calibration與promotion report。績效只讀walk-forward test union，不把training rows混入；9種mandatory checks各自保存pass/fail reason。污染資料直接Failure且不產report，合法但未達門檻者產`passed=false` rejected evidence。
- P3.5新增default-disabled content-hash opinion mapper policy與deterministic mapper。只有exact policy/manifest/runtime/evaluation binding、`paper_eligible`、passed/unexpired mapper evaluation與calibrated opinion同時成立才產`AlphaSignal`；rating只映射固定±0.5/0，不接受unknown或quantity-like字串。Signal保存本次current snapshot，evaluation則可來自不同historical snapshot，兩者不再被錯誤要求相同。
- P3.6新增Kronos-small/Tokenizer-base pinned manifest、source/model/tokenizer SHA-256驗證、local-only warm-once loader與exact runtime preflight。CPU/CUDA各自鎖定PyTorch 2.12.1與獨立image；compose為internal/read-only/non-root/cap-drop，環境拒絕DB/provider/broker/queue/HF token/cache/proxy credentials。四個實際權重檔已重算hash，CPU與RTX 3070 Ti CUDA inference均通過；canonical calendar/path/artifact/signal mapping尚由P3.7完成。
- P3.7新增closed Kronos wire contracts、calendar-aware canonical builder、逐seed path-retaining worker route與artifact-first core adapter。Future 1d timestamps只由exchange calendar產生；missing/estimated volume降級quality。Raw envelope與lease-secret-free replay artifact先封存，再驗fence/runtime/model/OHLCV/length/extreme jump並以Decimal metrics映射`ForecastSignal`；fresh stochastic inference不作bit-identical宣稱。CPU與RTX 3070 Ti CUDA以final exact runtime hash完成2-path route smoke，另保存16-path aggregate tolerance evidence。
- P3.8新增archived-only Kronos evaluation snapshot/record、US/HK/TW與三baseline identity fence、content hash與artifact-ref binding，以及evaluated forecast-to-alpha mapper。Committed strategy exact綁CPU runtime/model/tokenizer、feature/label/universe/cost/split/mapping hashes與production policy，deployment固定`shadow`、paper weight 0。768筆golden完成4 splits/252 OOS，baseline/cost/calibration未達原門檻而`passed=false`，沒有為整合放寬threshold；只有passed/calibrated/unexpired且exact-bound report可產shadow Alpha，global eligibility仍回零權重。
- P3.9新增15個shared Qlib job/result schemas、canonical `BarSeries` snapshot converter、fixed Qlib OLS adapter與isolated quant-lab worker。Source commit/archive hash、worker source/lock及Python/NumPy/Pandas/scikit-learn versions皆綁runtime identity；HTTP route實際重播同job得到相同prediction/position/metrics/model hashes。Worker只有research-only output，無promotion/target/order/DB authority；image為UID 65532、read-only、cap-drop/internal network，獨立lock audit 0 vulnerabilities，heavy dependencies未進core。
- P3.10新增typed strategy registry/UoW ports、reviewer-only strategy transition與read-only strategy/evaluation/audit/signal eligibility API/CLI。Actor由authenticated principal產生，body bounded且預設deny；live/order-shaped輸入、forged actor、stale CAS與evaluation/registry/audit binding drift皆fail closed。真實PostgreSQL驗證promotion/suspend/retire audit sequence與API/CLI共用CAS。
- 自有 core 採 Apache-2.0，唯一 execution mode 是 `paper`；live trading 必須另立 RFC。

## 已完成的研究

- `.research/upstreams/` 有 9 個 shallow snapshots：ai-hedge-fund、Dexter、TradingAgents、Kronos、daily_stock_analysis、AI-Trader、OpenBB、Qlib、RD-Agent。
- 固定 commits、授權與測試證據在 `docs/research/`；`verification.md` 最終為 PASS。
- 研究目錄只供閱讀，後續 `.gitignore` 必須排除；不能從其中直接 import 或提交。

關鍵實測結果：

- ai-hedge-fund：UTF-8 模式 109 passed / 38 live skipped；Windows CP950 會造成 13 個 fixture encoding failures。
- Dexter：typecheck 通過，74 tests 通過；但缺完整 MIT license text。
- TradingAgents：559 passed / 2 skipped、ruff 通過；Apache-2.0，可作 isolated research worker。
- AI-Trader：補齊缺漏依賴後 backend 123 tests 通過；原 requirements 無解、frontend Windows postbuild 失敗且有 8 個 audit vulnerabilities；server 授權聲明矛盾，禁止複用程式碼。
- OpenBB：AGPL-3.0-only；只能作 optional sidecar，且 process boundary 不是法律豁免。

## 已確定架構決策

1. 自有 canonical contracts 與 orchestration authority，不讓 upstream internal types 跨 process。
2. Canonical authority chain：Evidence/Artifact → Opinion/Signal → deterministic Target → Risk → Reservation → OrderIntent → Receipt/Fill → balanced Journal。
3. P0 就完成 in-memory fake/replay 全閉環與最小 security/reliability；P4 再升級為 PostgreSQL-backed canonical paper fund。
4. Stochastic LLM/Kronos output 先封存；deterministic replay 從 artifact 開始。
5. Core runner 是 transaction owner；remote workers 無 DB credentials，late result 用 lease generation/nonce fencing。
6. 同帳戶序列化並 reserve cash/sellable position，防並行雙花/超賣。
7. TradingAgents production/paper/backtest 只讀 allowed canonical evidence，預設 egress deny，避免 current news 污染歷史回測。
8. AI-Trader 只作 external community HTTP adapter；不提交 canonical paper/copy order。
9. OpenBB、Kronos、TradingAgents、Qlib、RD-Agent、LEAN/Nautilus 各自獨立 lock/image，不進 core environment。

## P0 / P1 / P2 / P3 可重跑證據

```powershell
uv sync --frozen
uv run python scripts/verify.py
$env:STONKS_TEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:55432/stonks_test'
uv run python scripts/verify.py --with-postgres
uv run stonks fake-cycle --symbol AAPL --as-of 2026-01-02T21:00:00Z --idempotency-key smoke-p0
```

- `scripts/verify.py` 執行format、lint、typecheck、完整tests/coverage、schema drift、upstream/license policy、secret scan與locked runtime dependency audit；`--with-postgres`另驗migration drift與真實DB整合。
- `tests/e2e/test_fake_cycle.py` 證明 next-session fill、balanced journal、replay、future evidence fail-closed 與 concurrent no-double-spend。
- `tests/application/test_execution_authority.py` 證明 research/forecast 與 unauthorized principal 無法觸發 `ExecutionPort`。
- `tests/application/test_fake_job_fencing.py` 證明 duplicate result 不重複寫 event/outbox，stale generation/nonce 只能隔離。
- P2.1 focused tests為22 passed、branch coverage 92%；完整`verify.py`為486 passed、171 PostgreSQL tests deselected、branch coverage 87.50%，119 source files mypy與所有security/license gates通過。
- P2.2後完整`verify.py`為500 passed、171 PostgreSQL tests deselected、branch coverage 87.62%；focused research tests為31 passed、application/adapters branch coverage 88%。
- P2.3後完整`verify.py`為550 passed、171 PostgreSQL tests deselected、branch coverage 88.08%；focused LLM contract/security tests為50 passed、branch coverage 92.55%，Mypy檢查134 source files。OpenAI/Anthropic只做official-wire mock contract，尚未做credentialed live smoke。
- P2.4後完整`verify.py`為570 passed、171 PostgreSQL tests deselected、core branch coverage 88.08%；focused worker tests為20 passed、branch coverage 95.77%。worker image已在UID 65532、read-only、cap-drop ALL、network none下通過health；model proxy outage回structured 503，不產生偽造success/order。
- P2.5後non-PostgreSQL gate為584 passed、172 deselected、coverage 88.18%；完整PostgreSQL gate為756 passed、coverage 88.59%，Alembic無drift。focused worker為21 passed/84.48%（含contracts），core HTTP/runner為13 tests；worker lock audit無已知CVE，image `stonks-tradingagents-worker:p2.5`在UID 65532、read-only、cap-drop ALL、no-new-privileges下health通過。
- P2.6後完整non-PostgreSQL gate為598 passed、172 deselected、coverage 88.27%；focused PEAD/event-study為14 passed、branch coverage 90.74%。PIT、after-close、duplicate filing/day、freshness、retrospective filter、golden、seed replay與MIT notice gates皆通過，core dependency未增加。
- P2.7後完整non-PostgreSQL gate為606 passed、172 deselected、coverage 88.23%；focused assembler為8 passed、branch coverage 84.35%。read-once、deterministic hash、PIT/repository scope、policy exclusions、missing/stale/conflict與infra failure tests皆通過。
- P2.8後完整non-PostgreSQL gate為613 passed、172 deselected、coverage 88.27%；focused generator/integrity為7 passed、branch coverage 90%。citation/quality/certainty、numeric bounds、prompt injection isolation、execution language、identity mismatch、model outage與bounded repair tests皆通過；43 schemas current。
- P2.9後完整non-PostgreSQL gate為619 passed、172 deselected、coverage 88.28%；focused renderer為6 passed、branch coverage 90%。三channel golden、stable replay hash、escaping、stale/conflict qualifier、多語、long subject、byte cap與startup template checks通過；Jinja2 3.1.6 locked audit無已知CVE。
- P2.10後完整non-PostgreSQL gate為630 passed、172 deselected、coverage 88.28%；focused delivery為11 passed、branch coverage 85.93%。outbox fence、artifact hash、UTF-8 byte chunk、idempotency、fixed-root no-overwrite、optional channel skip、webhook retry/no-redirect與redacted failures皆通過；locked audit無已知CVE。
- P2.11後non-PostgreSQL gate為640 passed、176 deselected、coverage 88.10%；完整PostgreSQL gate為816 passed、coverage 88.52%，Alembic無drift。focused API/CLI/SSE/report reader為12 passed、branch coverage 82.20%；另有4個真實PostgreSQL tests覆蓋atomic submit、PIT snapshot、idempotency、event chain與CLI enqueue，locked audit無已知CVE。
- P2.12後non-PostgreSQL gate為642 passed、176 deselected、coverage 88.12%；完整PostgreSQL gate為818 passed、coverage 88.54%，Alembic無drift。focused pipeline為2 E2E、branch coverage 87.16%，涵蓋snapshot→dual research→report/render/file delivery與provider/deterministic/TradingAgents/LLM outage audit；locked audit無已知CVE。
- P3.1 focused contracts/property tests為32 passed、新模組branch coverage 89.53%；完整non-PostgreSQL gate為667 passed、176 deselected、coverage 88.19%，291 files format、ruff、mypy 175 source files、43 schemas、upstream/license、secret與locked dependency audit全通過。
- P3.2 focused repository/domain為29 passed、branch coverage 84.27%；完整PostgreSQL gate為854 passed、coverage 88.42%，294 files format、ruff、mypy 176 source files、43 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P3.3 focused baseline/golden為7 passed、branch coverage 89.58%；完整non-PostgreSQL gate為674 passed、187 deselected、coverage 88.16%，300 files format、ruff、mypy 181 source files、43 schemas、upstream/license、secret與locked dependency audit全通過。
- P3.4 focused evaluation/domain為42 passed、branch coverage 90.91%；完整non-PostgreSQL gate為696 passed、187 deselected、coverage 88.33%，313 files format、ruff、mypy 189 source files、43 schemas、upstream/license、secret與locked dependency audit全通過。
- P3.5 focused mapper/domain為30 passed、mapper branch coverage 87.50%；完整non-PostgreSQL gate為706 passed、187 deselected、coverage 88.32%，317 files format、ruff、mypy 191 source files、43 schemas、upstream/license、secret與locked dependency audit全通過。
- P3.6 focused worker為26 passed；完整non-PostgreSQL gate為732 passed、187 deselected、coverage 88.32%，323 files format、ruff、core 191 source files與worker 6 files mypy、43 schemas、upstream/license、secret與core locked dependency audit全通過。實際115 MB model files SHA-256相符；CPU `2.12.1+cpu`與RTX 3070 Ti `2.12.1+cu129` images皆UID 65532並完成warm，GPU另完成32→2 bars六欄inference。OSV `torch 2.12.1`為0 vulnerabilities，兩個Linux images dependency audit均無已知CVE。
- P3.7 focused contracts/worker/adapter/domain/schema為72 passed；Kronos core HTTP adapter單模組coverage 86%。完整non-PostgreSQL gate為772 passed、187 deselected、coverage 88.31%，328 files format、ruff、core 195 source files與worker mypy、52 schemas、upstream/license、secret與core locked dependency audit全通過。實際CPU/CUDA canonical route各保留explicit seeded paths，final runtime hashes為`c3542191...dfa866`與`6a2ed7db...c6a7223`。
- P3.8 focused evaluation/mapper為15 passed、兩個新模組合計branch coverage 89.76%；P3 regression為137 passed。完整non-PostgreSQL gate為787 passed、187 deselected、coverage 88.34%，332 files format、ruff、mypy 197 source files、52 schemas、upstream/license、secret與locked dependency audit全通過。
- P3.9 focused contracts/converter/worker為24 passed、四個新模組合計branch coverage 86.70%；真實Qlib image build、health與duplicate HTTP job replay皆通過。完整non-PostgreSQL gate為811 passed、187 deselected、coverage 88.52%，341 files format、ruff、core 200 source files與worker 4 files mypy、67 schemas、upstream/license、secret、core與worker locked dependency audit全通過。
- P3.10 focused API/CLI為13 passed、branch coverage 87.14%；完整non-PostgreSQL gate為820 passed、190 deselected、coverage 88.56%。完整PostgreSQL P3 gate為1010 passed、coverage 88.73%，349 files format、ruff、mypy 207 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。

## 下一個代理的起點

1. 先閱讀 `AGENTS.md`、本檔、`tasks/todo.md` P4 與架構藍圖。
2. 保持 TDD；從P4.1 portfolio/risk/reservation/execution domain開始，先固定Decimal、sequence/hash、order transition、reservation與balanced journal invariants，再實作production code。
3. Research principals只能讀canonical evidence/artifacts，不能取得DB、queue、risk或execution authority。
4. `.research/` 只供閱讀且不進版控；不得 vendor/import Dexter、AI-Trader 或 OpenBB 至 core。
5. 每個 phase 完成後同步精簡 `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md` 與 todo review。
