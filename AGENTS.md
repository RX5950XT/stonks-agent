# Stonks Agent 專案規範

## 語言與工作方式

- 一律使用繁體中文（臺灣用語），技術名詞保留英文；回覆精簡、先講結果。
- CLI 優先，能直接完成就不要把操作丟回使用者。
- 非簡單任務先更新 `tasks/todo.md`；收到確認後持續做到 phase gate 通過，不以 partial status 當完成。
- 修改後自行執行相稱的 lint、typecheck、tests、security/license checks；失敗就修到通過。
- 使用者修正要整理成可避免重犯的規則，寫入 `tasks/lessons.md`。

## 技術與架構不變量

- 主核心使用 Python 3.12、`uv`、`src` layout、frozen Pydantic contracts、typed `Protocol` ports。
- 資料存取採 Repository Pattern；可替換 provider/model/policy/executor 採 Strategy/Adapter；外部邊界回傳 structured errors。
- Canonical flow：`Evidence/ResearchArtifact -> AgentOpinion/AlphaSignal/ForecastSignal -> deterministic PortfolioTarget -> RiskDecision -> AccountReservation -> OrderIntent -> ExecutionReceipt/Fill -> balanced Journal`。
- LLM、TradingAgents、Kronos、community feedback 都不能直接建立 target/order，不能 override risk。
- Stochastic inference 先封存 immutable output artifact；replay 從 artifact 開始，不宣稱 fresh re-inference bit-identical。
- S3 production transport 只接受 injected atomic credentials 與 official SigV4，固定 origin/bucket/prefix、DNS/IP pinning 且禁止 redirect 與 default credential chain；versioning/Object Lock preflight 不確定即 fail closed。
- Finalized artifact 的 current/history versions 永不由 GC 實體刪除；retention/legal hold 只增不減，restore 只處理 exact delete marker 或受信 version。
- Default deployment 只有 core/PostgreSQL；migration 必須 explicit one-shot 並使用獨立 owner credential，runtime 只用 `stonks_app` login。Core image 固定 non-root/read-only、structured secret-file DB config 與 exact schema readiness。
- Core job runner 是 DB/event/outbox transaction owner；remote worker 無 DB credentials，舊 generation/nonce 或過期 lease 的 result 不得 commit。
- 同帳戶 mutation 必須 serialized 並先 reservation；journal 每種 currency/commodity 的 debit/credit 必須平衡。
- Resilience drill 必須符合 frozen failure/telemetry catalog；unknown、partial、forbidden side effect、缺 evidence 或 unsafe recovery 一律不得算通過。Database restore 只能使用 digest-pinned image 與 fresh target，重驗 Alembic head、hash-chain、replay、append-only 及 source/target isolation，通過後也不得自動 promote。
- Worker crash、lease expiry、duplicate/stale result、dead-letter 及 ledger mismatch 必須 fail closed；dead-letter 不得自動追單，ledger drift 先 rollback 再啟動 kill switch，resume 只能在完整 audited reconciliation/replay 後人工執行。
- Capacity report 的 runtime resource evidence 只能代表 `probe_process` 並使用獨立 `probe_runtime_budget`；六組部署 process budgets 僅由 static manifests 固定，不得冒充實測或 production SLA。Capacity PostgreSQL 必須使用 fresh disposable database，canonical evidence 不以 DELETE 清理。
- TradingAgents、Kronos 與 Quant lab 同步重工作必須 offload event loop，per-process concurrency 固定為 1；滿載立即回 `429 worker_busy`，core 不得對 429 自動 retry 或追單。
- GUI research 只有在 `--with-research` 注入 durable facade 時可用；必須先 materialize live daily snapshot 再建立 snapshot-bound LLM＋Kronos job，chart interval 不得冒充 Kronos daily evidence。缺 LLM 設定、invalid output 或 provider/Kronos failure 一律 typed fail closed，不能 fallback fixture 或舊 forecast。
- 根目錄 `start.ps1` 是 local source-checkout 的薄 launcher；只轉交 market/paper/research canonical GUI 參數，`-Check` 不得要求、輸出或持久化 LLM secret，也不得啟動服務。Research mode 必須自動驗證並啟停 authenticated Kronos CPU；獨立 smoke 不得冒充 GUI run artifact。
- Workspace 清理只使用 `scripts/clean_workspace.py` 的 exact allowlist；必須保留原始碼、`.data`、`.research`、root/OpenBB/Kronos 環境與 Docker volumes，不得使用廣域 `git clean` 或 system-wide prune。
- Research claim 只能引用本輪工具實際 materialize 的 evidence；allowlist 或 metadata inventory 不等於已讀取內容。Tool timeout 必須由執行邊界以 monotonic deadline 強制，逾時結果不得進 canonical flow。
- Kronos GUI forecast 只能使用版本化、已驗證範圍內的 exchange calendar；缺 holiday/session authority 時 fail closed。Kronos 目前是 `shadow` 且 paper weight 為 0；沒有 genuine evaluation/promotion artifact 時只能顯示真 forecast 與 typed blocked alpha，paper cycle 只能 no-order，不得為展示閉環繞過 signal eligibility、risk 或 reservation。
- `execution_mode=paper` 是唯一允許模式；live trading 必須另立 RFC，不能用設定值偷偷啟用。

## 資料、研究與安全

- 所有歷史研究只可讀 `available_at <= as_of` 的 immutable evidence；未知 publication lag 預設不得進嚴格 backtest。
- GUI 或「最新資料」能力只有在 actual external runtime 通過時才可宣稱可用；必須顯示 provider、observed/event time、即時性與 quality，且 external failure 不得 fallback 到 fixture、hard-coded quote 或偽造 success。Provider 端點能力必須實測後才可納入 sidecar allowlist；上游失效的端點維持不提供，不得換來源冒充。
- 「免費資料源」採 curated registry；免費額度不代表 display／storage／redistribution rights。只有官方條款、credential、時效、rate limit、PIT 與 actual runtime 全數通過才能 active，GUI freshness 只由 backend session policy 判定。
- 本機 console 允許同源 script，但 CSP 必須維持 `default-src 'none'` 與全部 `'self'` 來源，禁止 inline script、eval、外部 origin、`data:` 來源與由字串產生 markup。
- GUI 主流程不得要求使用者先記憶命令；已組合的後端能力要有可見、語意明確、鍵盤可達的控制與完整 loading／empty／failed／degraded／succeeded 狀態，命令列只作進階入口。窄版不 autofocus，主要研究與安全決策必須早於次要行情／系統診斷。
- GUI 採低彩度 graphite dark evidence workbench；避免 dark-fintech 金色／霓虹模板、卡片海、glow、glass、裝飾性 kicker 與多色狀態噪音。資訊架構由 actual backend capabilities 反推；所有已組合功能在首屏總覽或一次點擊內可達並顯示 truthful state，沒有 route 的操作不得做假入口。Market state 固定寫入 query string，hash 只供 section navigation；quiet refresh 不得清空既有畫面或覆寫使用者輸入，所有 exit 都必須釋放 loading／`aria-busy`；section deep link 必須在 async render 後校正。
- GUI capability 必須同時合成 route contract 與 bounded live service；model `configured` 不等於 `verified`。Market label 必須遵守 canonical symbol suffix mapping，async section deep link 只能在 terminal market success／failure 後完成校正。
- Composite control 只允許 wrapper 的單一 `:focus-within` ring；全域 `:focus-visible` 保留清楚 outline，但不得與內層 input 疊成雙框。
- GUI durable read projection 必須 exact owner/account scoped；研究 history 只揭露 final claim 實際引用的 snapshot evidence，paper safety 維持唯讀。服務狀態以 bounded live probe 更新；昂貴 research start 同時只允許一筆且每分鐘最多三筆。Research POST 是唯一 canonical workflow mutation；model settings PUT／DELETE 只管理本次 process-memory 設定，不具 durable 或 trading authority。
- GUI model settings 只有在 local research composition 可用；API key 不得回傳、持久化、記錄或進入 artifact，browser secret field 必須關閉 autocomplete 並在 page exit 清除。新設定必須先用 pinned transport 完成 bounded structured completion 才原子啟用；worker 每筆 lease 讀取同一代 route＋secret snapshot，未設定或未驗證時 research POST fail closed。Loopback `/api/` browser request 必須拒絕 cross-site `Origin`／Fetch Metadata，且在昂貴 provider 呼叫前完成。
- 外部 news/web/filing/community/MCP/LLM 內容一律視為 untrusted data；tool 必須 allowlist、typed、read-only、scoped、audited。
- 所有外部輸入必須驗證；API envelope 統一為 `success/status/data/error/metadata`，分頁資訊放 `metadata`。
- 不硬編碼或提交 secrets；錯誤、log、event、report 不得洩漏 token、credentials、敏感 prompt/data。
- Durable 配置只保存 logical `SecretRef`；local GUI 允許 process-memory session secret 但不得寫入 canonical payload 或 browser storage。local/development/test 用 exact env strategy，staging/production 只接受 workload-identity cloud strategy 且不得 stale/env fallback。Consumer 每個 logical request 重新 resolve；canonical durable payload 遇 secret-shaped 資料直接拒絕，不靠 egress redaction 補救。
- Production human principal 必須由 server-side asymmetric OIDC/JWKS 驗證並做 exact ownership；不得信任 client actor/role。Service identity 須 exact 綁 issuer/audience/azp/permission/target/fence 且不得取得 human/operator/admin authority；local token 只限 loopback local/development/test。
- 所有 FastAPI app 必須使用中央 API security composition；rate limit 先做 direct-peer/credential admission 再做 verified principal，forwarded identity 預設拒絕。Dynamic outbound URL 必須 exact allowlist 並連線到已驗證 public pinned address；未接 distributed store 或 trusted proxy 不得宣稱 multi-replica enforcement。
- Worker/sidecar ingress 必須在 JWT crypto 前做 bounded direct-peer/credential admission，拒絕所有 forwarded identity；request body 同時限制 bytes、ASGI frames 與總 deadline，並以 `--no-proxy-headers` 保留 direct peer。Public lease/log 不得輸出 raw nonce；處理 lease 須覆蓋最長 handler budget，stale completion 只能進 append-only quarantine audit。
- Telemetry 只允許 frozen 低基數 catalog；trace/correlation 欄位與 canonical payload/hash 分離，observer 無權跳過、替換、吞掉或重播 canonical 結果。OTLP runtime 不得隱式吃 `OTEL_*`、proxy、`.netrc` 或 redirect；log/span/metric 不得含 secret、prompt 或 raw identity。
- Production cost/latency usage 必須由同一 monotonic clock 與 Decimal cost 形成 versioned budget decision；狀態只能 `within -> degraded -> failed`。Missing/invalid usage fail closed，`degraded/failed` 後不得建立新 target/reservation/order，`budget_exhausted` 不得 retry 追單。
- 高風險邊界 fail closed：stale/conflict/unknown data、invalid model output、risk/ledger mismatch、duplicate execution、license drift。

## 上游與授權

- `.research/upstreams/` 只供研究，禁止從該目錄直接 import、vendor 或提交。
- Dexter 與 AI-Trader 授權不完整；禁止複製 source、prompt、skills、assets、frontend/server。AI-Trader 只作 external community HTTP adapter，不能提交 canonical order。
- OpenBB 是 `AGPL-3.0-only`；只能依核准的 optional sidecar policy 接入，process boundary 不代表自動免除 AGPL 義務。
- MIT/Apache 程式碼若移植，保留 copyright/license/NOTICE 與來源 commit；資料與模型權利另行追蹤。
- Heavy upstream 各自使用獨立 lock/image；OpenBB、PyTorch、TradingAgents、Qlib、RD-Agent、Nautilus、LEAN 不得進 core lock。
- Locked dependency audit 必須覆蓋 core 及每個 isolated worker/sidecar project；只做 `uv lock --check` 不算漏洞稽核，local-build suffix 須另以 public package identity 查 advisory。
- Linux core 只能使用 source-built `psycopg-c` 與 system `libpq`；不得以 bundled `psycopg-binary` 隱藏 native dependency。正式 bundle 必含並重驗 OpenBB、Alpine 與 Python exact corresponding source、canonical SBOM/Grype/VEX 與所有 locks/notices。
- Release 必須先在 registry publication 前通過 unsigned candidate，再只對 registry exact digest 做 GitHub OIDC keyless signing/attestation；本機 unsigned 結果不得宣稱 signature 或 provenance。
- Canonical CycloneDX serial 必須 deterministic 綁定 exact image；formal final verifier 須獨立重驗五份 exact evidence，且 SBOM attestation predicate body 必須與 signed canonical SBOM 完全相同。
- Optional profile 若 blocked/unsupported 不得宣稱 runtime compatibility；本機 render 或 fail-closed probe 不能取代 GitHub workflow runtime report，缺外部證據維持 unverified。

## 程式與驗證標準

- 優先不可變資料；函數盡量小於 50 行、檔案小於 800 行、巢狀不超過 4 層。
- 不靜默吞例外，不以空 list/DataFrame/`None` 偽裝 infra failure。
- 新功能與 bug fix 採 TDD；核心目標 coverage 80% 以上，另做 PIT、property、contract、E2E、replay、security tests。
- 測試不得依賴 gitignored `.data`／`.research` runtime artifact；必要前置狀態由 test scope 建立，且只清理由該測試建立的路徑。
- Commit 格式：`<type>: <description>`，type 使用 `feat|fix|refactor|docs|test|chore|perf|ci`。
- 每次任務完成同步精簡 `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md` 與 `tasks/todo.md` review；README 只宣稱已驗證能力。
