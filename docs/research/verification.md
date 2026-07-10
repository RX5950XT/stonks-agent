# 研究產物一致性驗證

> 驗證日期：2026-07-10（Asia/Taipei）。本次只做本機、唯讀診斷；未重新執行上游完整測試，也未清理研究環境。

## 驗證結論

**PASS（有低風險警告）。** 9 個本機 snapshot、授權證據、關鍵路徑與 tracked worktree 都可重現；`AI-Trader` 角色、TradingAgents 的 point-in-time data boundary 與 signal-to-order authority 在最終報告 snapshot 已對齊。所有 GitHub `blob/tree` 證據均已固定 commit；殘留問題僅是各研究報告保留不同層級的 schema 命名／欄位摘要。

| 項目 | 結果 | 摘要 |
|---|---|---|
| Snapshot identity | **PASS** | 9/9 `HEAD` 與 `upstream-snapshot.md` 完整 commit 相同；commit time、remote 與報告宣告的 branch 亦一致。 |
| License evidence | **PASS** | 7 個有根目錄 license 的專案內容符合報告；`dexter`、`AI-Trader` 缺檔也與報告一致。 |
| Local/link integrity | **PASS** | README 相對連結與 5 個具名本機 snapshot path 均存在；82 個 GitHub `blob/tree` 證據全部固定 commit，local/web-only 證據皆可重現。 |
| Upstream tracked state | **PASS** | 9/9 worktree、index 均無 tracked 變更；研究測試未污染 tracked files。 |
| Cross-report semantics | **PASS（有警告）** | 無 material authority conflict；仍需統一 `ResearchArtifact/AnalysisBundle`、`AgentOpinion/AlphaSignal` 與 execution event 欄位。 |

## Snapshot 與授權證據

| Upstream | 實際 HEAD | License / 缺失證據 | Tracked state |
|---|---|---|---|
| `ai-hedge-fund` | `3a18702cb25777fb4bdb4b2527a0c868bc8297f4` | 根目錄 MIT | clean |
| `dexter` | `bae661670c3d77e909942777ac32ece21e8af35d` | 無 `LICENSE`；README 只有 MIT 宣稱 | clean |
| `TradingAgents` | `01477f9afb7a47b849ed4c9259d3a9a4738d9fda` | 根目錄 Apache-2.0 | clean |
| `Kronos` | `67b630e67f6a18c9e9be918d9b4337c960db1e9a` | 根目錄 MIT | clean |
| `daily_stock_analysis` | `aa513135d67425d2484cdc9c643402c0f4c3ae07` | 根目錄 MIT | clean |
| `AI-Trader` | `d03ff6c056b32ced735adf7c19ed8175adb1c8df` | 無 `LICENSE`；根 README 是 MIT badge，`service/README.md` 稱 proprietary | clean |
| `OpenBB` | `1c74893140292944e71ff5cdd9536edf12f05483` | 根 LICENSE 為 AGPL v3；package metadata 為 `AGPL-3.0-only` | clean |
| `Qlib` | `d5379c520f66a39953bad76234a7019a72796fd0` | 根目錄 MIT | clean |
| `RD-Agent` | `4f9ecb005881cddc08df0124a2e894c018007679` | 根目錄 MIT | clean |

9 個 repository 都是 shallow clone；`Qlib`、`RD-Agent` 另使用 `blob:none` partial-clone filter，與研究用途相容。

## 連結與路徑檢查

- `README.md` 的 6 個相對 Markdown links 在本檔建立後皆存在。
- 報告列出的 `.research/upstreams/Kronos`、`daily_stock_analysis`、`OpenBB`、`TradingAgents`、`AI-Trader` 均存在。
- 共掃描 82 個 GitHub `blob/tree` link occurrences，全部使用 40 字元 commit。81 個可映射至本機 clones，其中 80 個路徑可由 Git object database 解析；唯一不可解析者是報告刻意列出的 `AI-Trader/LICENSE` 不存在證據。
- `vectorbt/LICENSE.md` 是唯一 web-only evidence；GitHub 已確認 commit `bf7aff6d081fda1e9cd7dc0464d68f98309875a1`、檔案存在，內容明示 `Apache 2.0 with Commons Clause`，且 commit 日期為 2026-07-10。`upstream-snapshot.md` 已明記它不是本機 dependency。
- 除上述 web-only evidence 外，本次未全面爬取所有外部 HTTP links；結論主要保證固定 snapshot 與 Git object 一致。

## 跨報告一致性

- `AI-Trader` 在最終 snapshot 已一致定位為 optional external control/community HTTP adapter（paper outcomes只讀觀測）；缺 license 時不複用其程式碼，也不成為 orchestration authority 或 research worker。
- `AI-Trader` adapter不提供canonical paper/copy order submission；本地paper executor與balanced journal是唯一交易/accounting truth，remote positions/outcomes只轉成external evidence。
- `TradingAgents` 已一致限制為輸出 `AnalysisBundle/AgentOpinion`，後續由自有 signal fusion、deterministic `PortfolioTarget` 與 risk layer 形成 `OrderIntent`；符合其他報告「LLM 不直接下單」的邊界。
- Production/paper/backtest TradingAgents worker只讀`allowed_evidence_ids`與canonical tool facade，預設禁止任意network egress；current news/social只可進明示非PIT的互動research sandbox。
- `docs/architecture/integration-blueprint.md` 使用相同 authority flow，並明訂 `AgentOpinion` 必須先經版本化 mapper 才能成為 `AlphaSignal`；其 process matrix 也把 `AI-Trader` 限制為 canonical flow 外的 external adapter。
- Phase語意已收斂：P0先證明完整in-memory target/risk/reservation/paper-fill/balanced-journal/report/replay與最小security/reliability；P4再升級為PostgreSQL-backed canonical small-portfolio閉環，security/reliability不是延後到P6才新增。
- 各研究報告仍保留 upstream/source-specific vocabulary 與不同層級的欄位摘要，例如 `ResearchArtifact`／`AnalysisBundle` 及 execution event 拆分程度；這不是 authority conflict，因 blueprint 已提供 canonical 規則。實作時仍應只以單一 versioned schema package 與明確 mapping 為準，不從個別研究表格各自生成 contract。

## 其他低風險偏差

- OpenBB 在部分文字簡寫成 `AGPL-3.0`；正式 SBOM、notice 與 policy 應統一使用 package metadata 的 `AGPL-3.0-only`。
- `P0/P1` 在各報告有時代表 phase、有時代表能力優先級；整合規格應拆成 `phase` 與 `priority` 兩欄，避免誤讀。
- 各報告記載的 upstream 測試結果本次未重跑；本次只確認測試後沒有 tracked worktree/index 污染。

## 實際診斷命令類別

- Snapshot：`git rev-parse HEAD`、`git branch --show-current`、`git log -1 --format=...`、`git remote -v`、`git rev-parse --is-shallow-repository`。
- Worktree：`git status --porcelain=v1 --untracked-files=no`、`git diff --quiet`、`git diff --cached --quiet`。
- License：根目錄 license candidate inventory、license header 讀取、`git grep` package metadata／README license claims。
- Links：抽取 Markdown targets；相對路徑以 `Test-Path` 驗證；pinned GitHub `blob/tree` link 映射本機 clone 後以 `git cat-file -e <ref>:<path>` 驗證。
- Web-only evidence：開啟固定 commit 的 GitHub file/commit page，並以 GitHub API 讀取 commit SHA、日期與訊息。
- Cross-report：比對 full commit hashes、license 關鍵字、角色敘述與 canonical contract 名稱。
- Follow-up semantics：搜尋並確認無`submit_paper_trade`、`AI-Trader先當paper adapter`、`upstream data tools第一階段留在graph`或`double-entry-like`殘留；核對`AccountReservation`、transaction-owner/late-result fencing、archived stochastic artifact replay及P0/P4/P6 gates同時存在於blueprint與todo。
