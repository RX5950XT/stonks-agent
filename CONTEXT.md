# Stonks Agent 開發交接

更新日期：2026-07-12

## 目前狀態

- Git 已初始化於 `main`；`PLAN-AUTH` 已成立，依 P0 → P6 連續實作。
- P0 Foundation 與 P1 Canonical Data Hub phase gates已完成；P2.1–P2.9 bounded research、structured LLM、TradingAgents worker/core adapter、draft PEAD/event-study、evidence/report integrity與Jinja renderers已完成，下一目標為P2.10 delivery ports。
- P1包含PostgreSQL 0001–0008、PIT evidence/snapshot、repositories/UoW、content-addressed artifacts、durable job/outbox/inbox、provider policy、US/HK/TW replay、snapshot API/CLI與canonical completion。
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

## P0 / P1 / P2.1–P2.9 可重跑證據

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

## 下一個代理的起點

1. 先閱讀 `AGENTS.md`、本檔、`tasks/todo.md` P2 與架構藍圖。
2. 保持 TDD；從P2.10 delivery ports開始，console/file預設可用，email/webhook未配置不阻擋報告；所有side effect須idempotency、receipt與redacted error。
3. Research principals只能讀canonical evidence/artifacts，不能取得DB、queue、risk或execution authority。
4. `.research/` 只供閱讀且不進版控；不得 vendor/import Dexter、AI-Trader 或 OpenBB 至 core。
5. 每個 phase 完成後同步精簡 `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md` 與 todo review。
