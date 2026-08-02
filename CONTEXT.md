# Stonks Agent 開發交接

更新日期：2026-08-02

這份文件只記錄「讀程式碼與 git log 看不出來」的東西：目前狀態、決策理由、踩過的坑。
專案規範見 `AGENTS.md`／`CLAUDE.md`，使用方式見 `README.md`，教訓見 `tasks/lessons.md`。

## 目前狀態

- 分支 `main`，工作樹 clean，與 `origin/main` 對齊。最近一輪是文件整理（本檔、README、
  todo 大幅精簡）。前一輪 P23 將 GUI 功能分支經 PR #12 以 merge commit `8a0c834` 併入
  `main`，遠端功能分支已刪除。
- CI 注意：`Hardened core Compose` job 會偶發在 `compose_build_core` 失敗。smoke runner
  刻意 capture 子行程輸出以防 secret 外洩，因此 CI log 只有 typed envelope 沒有 build
  細節。同一 commit rerun 即通過，本機 `scripts/smoke_core_deployment.py` 亦 success；
  遇到時先 rerun 判定 transient，再本機重現拿完整輸出。
- 版本：正式 immutable release 是 `v0.1.2`（不含 GUI）。目前工作樹是**未發布**的 `0.2.0`
  candidate，Local GUI 只在此。不得用 `v0.1.2` 的簽章或 runtime 證據替 `0.2.0` 背書。
- 成熟度 pre-alpha、paper-only。Default deployment 只有 health／readiness，尚未組合
  production business API 或常駐 dispatcher。
- 最近一次完整 gate（`scripts/verify.py --with-postgres`，fresh disposable DB）：
  824 files formatted、Mypy 396 files、2,774 passed／10 skipped、coverage 86.18%，
  schema／migration／security／license／dependency gates 全綠。

## 目前的能力邊界（容易誤判的部分）

- **行情**：唯一 active 來源是 OpenBB → yfinance。Yahoo 的 `price/quote`、`profile`、
  `fundamental/*`、`discovery/*` 實測全數 401（yfinance cookie 種子主機 `fc.yahoo.com`
  無法解析），因此維持不在 allowlist、不提供、也不換來源冒充。公司簡介、財報指標與
  漲跌幅排行未實作。
- **市場區域**：`domain/market_region.py` 是 market/MIC/exchange-timezone 單一來源，market
  由 symbol 後綴決定（`.TW`／`.TWO`→TW、`.HK`→HK、其餘 US；`BRK.B` 仍為 US）。US＋TW 行事曆
  已驗證，HK 只會 typed fail closed。TW 假日來自 TWSE 官方 OpenAPI。
- **Kronos**：真 CPU inference 已接進 GUI research artifact，但策略仍是 `shadow`、paper
  weight 0。畫面顯示真 forecast，但 alpha 為 typed `blocked`、paper 決策為 no-order。
  沒有 genuine evaluation／promotion artifact 前不得為了展示閉環放寬門檻。
- **LLM**：需要使用者自備 endpoint／model／key。缺設定時 research POST 回 typed 503，
  durable history 仍可讀。Secret 只存在 process memory。
- **GUI launcher**：只支援完整 source checkout（需要 repo 內 Compose 與 OpenBB
  corresponding-source build context）。standalone wheel、core image 與 `v0.1.2` 不支援。

## 已完成階段摘要

| 階段 | 內容 |
|---|---|
| P0 | in-memory fake／replay 全閉環與最小 security/reliability 基線 |
| P1 | PostgreSQL 0001–0017、PIT evidence/snapshot、repositories/UoW、durable job/outbox/inbox |
| P2 | research／LLM contracts、PIT context builder、model adapters、TradingAgents worker、ai-hedge-fund PEAD/event-study、report/render/delivery、research pipeline |
| P3 | strategy registry／evaluation／promotion、baselines、evaluation policy、opinion mapper、Kronos manifest＋worker＋evaluation、Qlib quant-lab worker、strategy API/CLI |
| P4 | paper trading domain、schema 0010–0014、portfolio construction、risk authorization、deterministic broker、ledger、cycle runner、monitoring、operator commands、read projections |
| P5 | external platform contracts、AI-Trader adapter、community policy、backtest contracts、Nautilus／LEAN sidecars、cross-engine parity、RD-Agent sandbox、`config/features.yaml` |
| P6 | security composition、secret refs、rate limit、telemetry、budgets/SLO、S3 artifact transport、hardened deployment、release bundle＋keyless signing、resilience drills、capacity report、formal `v0.1.2` closure |
| P7–P12 | Local GUI：OpenBB live path、Stonks Desk、fenced worker dispatcher、durable research、Kronos GUI 接入、`start.ps1` launcher、`clean_workspace.py` |
| P13–P16 | Kronos snapshot-bound artifact、citation laundering／tool timeout／ingress 修復、session-scoped LLM settings、backend-owned freshness/quality |
| P17–P19 | 死碼清理與重複實作收斂、`start.sh`＋`.env`＋`fetch_kronos_model.py` 降低上手門檻、台股接入、研究輸出重排 |
| P20–P23 | GUI 全面重設計（graphite dark evidence workbench）、UI/安全/死碼三路稽核、pre-push 完整驗證、PR #12 合併至 `main` |

各階段的 exact 測試數字、hash 與 CI run ID 見 git log 與
`docs/verification/p6-handoff-evidence.md`；不在本檔重複。

## 已確定架構決策

1. 自有 canonical contracts 與 orchestration authority，不讓 upstream internal types 跨 process。
2. Authority chain：Evidence/Artifact → Opinion/Signal → deterministic Target → Risk →
   Reservation → OrderIntent → Receipt/Fill → balanced Journal。
3. Stochastic LLM/Kronos output 先封存 artifact，deterministic replay 從 artifact 開始，
   不宣稱 fresh inference bit-identical。
4. Core runner 是 transaction owner；remote worker 無 DB credentials，late result 用 lease
   generation/nonce fencing。
5. 同帳戶序列化並先 reserve cash／sellable position，防並行雙花與超賣。
6. OpenBB、Kronos、TradingAgents、Qlib、RD-Agent、LEAN／Nautilus 各自獨立 lock/image，
   不進 core environment。Docker 相依刻意保留：OpenBB 是 AGPL-3.0-only，process 隔離是
   授權邊界而非效能選擇。
7. AI-Trader 只作 external community HTTP adapter，不提交 canonical order。
8. GUI 政策是「只允許同源本地 script」：CSP `default-src 'none'` 加全部 `'self'`，
   禁 inline／eval／外部 origin／`data:`；未引入 npm、node_modules 或打包器。

## 上游研究結論

`.research/upstreams/` 有 9 個 shallow snapshots（ai-hedge-fund、Dexter、TradingAgents、
Kronos、daily_stock_analysis、AI-Trader、OpenBB、Qlib、RD-Agent），只供閱讀、不進版控、
不得直接 import 或 vendor。固定 commits 與授權證據在 `docs/research/`。

- ai-hedge-fund：MIT，可選擇性移植（已移植 PEAD／event study）。
- Dexter：缺完整 MIT license text，禁止複製 source／prompt／assets。
- TradingAgents：Apache-2.0，可作 isolated research worker。
- AI-Trader：server 授權聲明矛盾，禁止複用程式碼。
- OpenBB：AGPL-3.0-only，只能作 optional sidecar，process boundary 不是法律豁免。

## 可重跑驗證

```powershell
uv sync --frozen
uv run python scripts/verify.py
$env:STONKS_TEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:55432/stonks_test'
uv run python scripts/verify.py --with-postgres
uv run stonks fake-cycle --symbol AAPL --as-of 2026-01-02T21:00:00Z --idempotency-key smoke
```

`verify.py` 涵蓋 format、lint、strict mypy、tests/coverage、schema drift、upstream/license
policy、secret scan 與每份 isolated lock 的 dependency audit；`--with-postgres` 另驗
migration drift 與真實 DB 整合。

關鍵 regression（改動相關區域前先讀）：

- `tests/e2e/test_fake_cycle.py`：next-session fill、balanced journal、replay、future
  evidence fail-closed、concurrent no-double-spend。
- `tests/application/test_execution_authority.py`：research/forecast 與未授權 principal
  無法觸發 `ExecutionPort`。
- `tests/application/test_fake_job_fencing.py`：duplicate result 不重複寫 event/outbox。
- `tests/entrypoints/test_quick_start_script.py`：launcher 的 `assert "X" not in source`
  是刻意的安全不變量，放寬時必須換成更精確的斷言，不能直接刪。

## 下一個代理的起點

1. 先讀 `AGENTS.md`、本檔、`tasks/lessons.md` 與 `docs/runbooks/local-gui.md`。
2. 不得移動或刪除任何 protected release tag，也不得弱化 required-reviewer、exact identity
   或五證據 closure gate。
3. Research principals 只能讀 canonical evidence／artifacts，不能取得 DB、queue、risk 或
   execution authority。
4. 需要 runtime artifact 的測試必須自建 scoped state，只清理自己建立的路徑；gitignored
   `.data`／`.research` 會在本機掩蓋 CI 才會爆的缺失。
5. 每輪任務完成同步精簡 `AGENTS.md`、`CLAUDE.md`、本檔與 `tasks/todo.md` review。
