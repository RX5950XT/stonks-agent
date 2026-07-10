# Stonks Agent 開發交接

更新日期：2026-07-11

## 目前狀態

- Git 已初始化於 `main`；`PLAN-AUTH` 已成立，依 P0 → P6 連續實作。
- P0 Foundation、contracts、fake/replay、paper-only/security/CI 已完成；完整 gate 為 100+ tests、branch coverage >90%、ruff、mypy、schema、license、secret 與 dependency audit 全通過。
- P1.1 instrument/calendar/time domain、P1.3 PostgreSQL 0001 schema與P1.5 memory/local content-addressed artifact stores已完成。
- PostgreSQL 17實測 upgrade/downgrade/re-upgrade、Alembic drift、artifact FK、strict-PIT check、append-only trigger與least-privilege grants皆通過；Linux CI已有真實PostgreSQL service job。
- P1.4 repositories/UoW與P1.6 durable queue/outbox已完成：rollback、run CAS、`SKIP LOCKED`、not-before/deadline、lease reclaim、generation/nonce fencing、dead letter、atomic result/event/outbox/ack、outbox retry與worker CLI均有真實PostgreSQL測試。
- P1.2 provenance/data-quality已完成；available、legitimate-empty、not-supported、config-missing、quota、stale、partial、conflict與fetch-failed皆為互斥typed state，strict-PIT與unsafe source provenance fail closed。
- P1.7 provider policy已完成：US/HK/TW capability allowlist、ordered fallback、freshness/quota metadata、stale/partial opt-in、reconciliation threshold與typed failure states皆有測試。
- 下一個實作目標是 P1 replay、Financial Datasets/OpenBB/regional adapters與ingestion API/CLI。
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

## P0 可重跑證據

```powershell
uv sync --frozen
uv run python scripts/verify.py
uv run stonks fake-cycle --symbol AAPL --as-of 2026-01-02T21:00:00Z --idempotency-key smoke-p0
```

- `scripts/verify.py` 執行 lint、typecheck、完整 tests/coverage、schema drift、upstream/license policy、secret scan 與 locked runtime dependency audit。
- `tests/e2e/test_fake_cycle.py` 證明 next-session fill、balanced journal、replay、future evidence fail-closed 與 concurrent no-double-spend。
- `tests/application/test_execution_authority.py` 證明 research/forecast 與 unauthorized principal 無法觸發 `ExecutionPort`。
- `tests/application/test_fake_job_fencing.py` 證明 duplicate result 不重複寫 event/outbox，stale generation/nonce 只能隔離。

## 下一個代理的起點

1. 先閱讀 `AGENTS.md`、本檔、`tasks/todo.md` P1 與架構藍圖。
2. 保持 TDD；先寫 P1 domain/property/integration failures，再實作。
3. PostgreSQL migration、lease/outbox 需以真實 PostgreSQL 驗證；不可用 SQLite 冒充。
4. `.research/` 只供閱讀且不進版控；不得 vendor/import Dexter、AI-Trader 或 OpenBB 至 core。
5. 每個 phase 完成後同步精簡 `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md` 與 todo review。
