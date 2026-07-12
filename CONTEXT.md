# Stonks Agent 開發交接

更新日期：2026-07-12

## 目前狀態

- Git 已初始化於 `main`；`PLAN-AUTH` 已成立，依 P0 → P6 連續實作。
- P0 Foundation 與 P1 Canonical Data Hub phase gates已完成；P2.1–P2.2 research contracts與bounded orchestrator已完成，下一目標為P2.3 LLM structured-output adapters。
- P1包含PostgreSQL 0001–0008、PIT evidence/snapshot、repositories/UoW、content-addressed artifacts、durable job/outbox/inbox、provider policy、US/HK/TW replay、snapshot API/CLI與canonical completion。
- Job/snapshot/outbox的claim、deadline、lease與commit timestamps使用transaction內PostgreSQL clock；generation/nonce、caller clock drift、cross-run retry與完整audit graph皆有真實PostgreSQL測試。
- Reconciliation成功決策封存雙側raw/normalized hashes、metric/value、threshold與decision；conflict維持0 artifact writes並留下hash-chained failure event/outbox。
- Financial Datasets與OpenBB已驗證read-only observation contracts與共用daily query；canonical materialization目前只宣稱replay source。`stonks-worker`只提供claim-once，不宣稱常駐dispatcher。
- Optional OpenBB sidecar已實測exact GET allowlist、frozen 64-package lock、SBOM/license policy、4個upstream sdists、AGPL source archive與non-root/read-only runtime。
- P2.1新增frozen evidence-scoped research/LLM contracts、immutable usage accounting、runtime-checkable research/LLM/tool ports與deny-by-default tool authorization；principal/profile/policy、instrument/evidence scope、typed args、timeout/output cap、audit redaction及result identity/hash/bytes皆fail closed。
- P2.2新增read-only PIT context builder、typed planning/final turn loop、pre-authorized parallel read tools與deterministic artifact builder；external content永遠維持untrusted，uncited claim降為hypothesis，budget/deadline/model/tool/scope錯誤皆hard-stop。
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

## P0 / P1 / P2.1–P2.2 可重跑證據

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

## 下一個代理的起點

1. 先閱讀 `AGENTS.md`、本檔、`tasks/todo.md` P2 與架構藍圖。
2. 保持 TDD；從P2.3 LLM structured-output adapters開始，再接isolated TradingAgents worker。
3. Research principals只能讀canonical evidence/artifacts，不能取得DB、queue、risk或execution authority。
4. `.research/` 只供閱讀且不進版控；不得 vendor/import Dexter、AI-Trader 或 OpenBB 至 core。
5. 每個 phase 完成後同步精簡 `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md` 與 todo review。
