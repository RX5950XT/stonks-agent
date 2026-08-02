# 文件整理與精簡（2026-08-02）

## 本輪執行

- [x] 重寫 `README.md`：第三人稱、去除重複與行話堆疊，只保留已驗證的能力宣稱。
- [x] 壓縮 `CONTEXT.md`：移除逐階段測試數字流水帳，保留狀態、能力邊界、架構決策與陷阱。
- [x] 精簡 `tasks/todo.md`：保留最近一輪詳情，歷史輪次收斂為摘要。
- [x] 對齊 `AGENTS.md` 與 `CLAUDE.md`（policy test 要求兩檔 byte-identical）。
- [x] 跑 docs policy gate 與相關 regression。
- [x] Commit 並推送 `main`，驗證 GitHub CI 與 Supply-chain 全綠。

## Review

- README 396 → 331 行，改為第三人稱、加入「它是什麼／不是什麼」段落，狀態表與 CLI 表
  保留，重複的 GUI 說明段落合併為單一「啟用 AI 研究」流程。`test_docs_handoff.py`
  綁定的 9 個 token 與全部 local link 均保留。
- CONTEXT 425 → 118 行。P0–P23 的逐階段測試數字、hash 與 CI run ID 已由 git log 與
  `docs/verification/p6-handoff-evidence.md` 記錄，不在交接文件重複；改為保留「讀 code
  看不出來」的能力邊界、決策理由與 regression 指路。
- todo 201 → 34 行；`AGENTS.md`／`CLAUDE.md` 只做中英文間距與 run-on 句可讀性整理，
  不變量逐條保留、兩檔維持 byte-identical。`docs/README.md` 的過時「P7 todo」標籤已更新。
- 驗證：`scripts/verify.py --skip-audit` → `[verify] all gates passed`；Ruff、strict mypy
  396 files、2,486 passed／6 skipped、coverage 86.86%、schemas current、upstream 0 violation、
  secret scan 0 finding；`tests/policy/` 188 passed／3 skipped。
- 推送：commit `c359b9c` 已進 `origin/main`。首次 CI 的 `Hardened core Compose` 在
  `compose_build_core` 失敗，diff 未觸及 Dockerfile／lock；同 commit rerun 後 CI 全綠、
  Supply-chain security 亦 success，本機 `scripts/smoke_core_deployment.py` 回
  `runtime_hardening/persistence_replay: verified`，判定為 transient build failure。

---

## 歷史輪次摘要

較早輪次的完整 review 見 git log；以下只保留結論。

| 輪次 | 結果 |
|---|---|
| GUI 功能分支合併至 main（2026-08-02） | PR #12 以 merge commit `8a0c834` 併入 `main`。初次 CI 的 5 個 failure 來自兩個根因：clean runner 缺 Kronos model directory、capacity revision 停在 `0017`；兩者以 regression 修正，未弱化 launcher fail-closed。修正後 14/14 checks 全綠。 |
| Pre-push 完整驗證與發布（2026-08-02） | 三個唯讀子代理完成 code／安全／docs 獨立審查；修正 truthful capability（`configured` ≠ `verified`）、async deep-link、market label suffix mapping 與 secret lifecycle。完整 PostgreSQL gate 2,772 passed／coverage 86.18%。 |
| 前端品質／安全／死碼稽核（2026-08-01） | 修正 loading 永久 busy、deep link、degraded state、320px footer、44px touch target；loopback `/api/` 在 provider 呼叫前拒絕 cross-site `Origin`／Fetch Metadata；移除未引用的 freshness rail chain 與 DOM binding。 |
| GUI 完全重設計（2026-08-01） | 移除 dark-fintech 金色／霓虹模板與卡片海，改為低彩度 graphite dark evidence workbench。首屏 capability map 由 actual backend 反推，market state 從 hash 移至 query string，quiet refresh 不再清空畫面。 |
| 降低上手門檻（2026-07-30） | Phase A：`fetch_kronos_model.py`、`start.sh`、`.env` 載入。Phase B：台股接入（`market_region.py`、TWSE 官方行事曆、per-market 時區）。Phase C：研究輸出重排，`blocked alpha`／`no-order` 降為合規狀態列。 |
