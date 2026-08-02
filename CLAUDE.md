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
- S3 production transport只接受injected atomic credentials與official SigV4，固定origin/bucket/prefix、DNS/IP pinning且禁止redirect/default credential chain；versioning/Object Lock preflight不確定即fail closed。
- Finalized artifact的current/history versions永不由GC實體刪除；retention/legal hold只增不減，restore只處理exact delete marker或受信version。
- Default deployment只有core/PostgreSQL；migration必須explicit one-shot並使用獨立owner credential，runtime只用`stonks_app` login。Core image固定non-root/read-only、structured secret-file DB config與exact schema readiness。
- Core job runner 是 DB/event/outbox transaction owner；remote worker 無 DB credentials，舊 generation/nonce 或過期 lease 的 result 不得 commit。
- 同帳戶 mutation 必須 serialized 並先 reservation；journal 每種 currency/commodity 的 debit/credit 必須平衡。
- Resilience drill 必須符合 frozen failure/telemetry catalog；unknown、partial、forbidden side effect、缺 evidence 或 unsafe recovery 一律不得算通過。Database restore只能使用digest-pinned image與fresh target，重驗Alembic head、hash-chain、replay、append-only及source/target isolation，通過後也不得自動promote。
- Worker crash、lease expiry、duplicate/stale result、dead-letter及ledger mismatch必須fail closed；dead-letter不得自動追單，ledger drift先rollback再啟動kill switch，resume只能在完整audited reconciliation/replay後人工執行。
- Capacity report的runtime resource evidence只能代表`probe_process`並使用獨立`probe_runtime_budget`；六組部署process budgets僅由static manifests固定，不得冒充實測或production SLA。Capacity PostgreSQL必須使用fresh disposable database，canonical evidence不以DELETE清理。
- TradingAgents、Kronos與Quant lab同步重工作必須offload event loop，per-process concurrency固定為1；滿載立即回`429 worker_busy`，core不得對429自動retry或追單。
- GUI research 只有在`--with-research`注入durable facade時可用；必須先materialize live daily snapshot再建立snapshot-bound LLM＋Kronos job，chart interval不得冒充Kronos daily evidence。缺LLM設定、invalid output或provider/Kronos failure一律typed fail closed，不能fallback fixture或舊forecast。
- 根目錄`start.ps1`是local source-checkout的薄launcher；只轉交market/paper/research canonical GUI參數，`-Check`不得要求、輸出或持久化LLM secret，也不得啟動服務。Research mode必須自動驗證並啟停authenticated Kronos CPU；獨立smoke不得冒充GUI run artifact。
- Workspace清理只使用`scripts/clean_workspace.py`的exact allowlist；必須保留原始碼、`.data`、`.research`、root/OpenBB/Kronos環境與Docker volumes，不得使用廣域`git clean`或system-wide prune。
- Research claim 只能引用本輪工具實際 materialize 的 evidence；allowlist或metadata inventory 不等於已讀取內容。Tool timeout 必須由執行邊界以 monotonic deadline 強制，逾時結果不得進 canonical flow。
- Kronos GUI forecast只能使用版本化、已驗證範圍內的exchange calendar；缺holiday/session authority時fail closed。Kronos目前是`shadow`且paper weight為0；沒有genuine evaluation/promotion artifact時只能顯示真forecast與typed blocked alpha，paper cycle只能no-order，不得為展示閉環繞過signal eligibility、risk或reservation。
- `execution_mode=paper` 是唯一允許模式；live trading 必須另立 RFC，不能用設定值偷偷啟用。

## 資料、研究與安全

- 所有歷史研究只可讀 `available_at <= as_of` 的 immutable evidence；未知 publication lag 預設不得進嚴格 backtest。
- GUI 或「最新資料」能力只有在 actual external runtime 通過時才可宣稱可用；必須顯示 provider、observed/event time、即時性與 quality，且 external failure 不得 fallback 到 fixture、hard-coded quote 或偽造 success。Provider 端點能力必須實測後才可納入 sidecar allowlist；上游失效的端點維持不提供，不得換來源冒充。
- 「免費資料源」採 curated registry；免費額度不代表 display／storage／redistribution rights。只有官方條款、credential、時效、rate limit、PIT 與 actual runtime 全數通過才能 active，GUI freshness 只由 backend session policy判定。
- 本機 console 允許同源 script，但 CSP 必須維持 `default-src 'none'` 與全部 `'self'` 來源，禁止 inline script、eval、外部 origin、`data:` 來源與由字串產生 markup。
- GUI 主流程不得要求使用者先記憶命令；已組合的後端能力要有可見、語意明確、鍵盤可達的控制與完整 loading／empty／failed／degraded／succeeded 狀態，命令列只作進階入口。窄版不 autofocus，主要研究與安全決策必須早於次要行情／系統診斷。
- GUI 採低彩度 graphite dark evidence workbench；避免 dark-fintech 金色／霓虹模板、卡片海、glow、glass、裝飾性 kicker 與多色狀態噪音。資訊架構由 actual backend capabilities 反推；所有已組合功能在首屏總覽或一次點擊內可達並顯示 truthful state，沒有 route 的操作不得做假入口。Market state 固定寫入 query string，hash 只供 section navigation；quiet refresh 不得清空既有畫面或覆寫使用者輸入，所有 exit 都必須釋放 loading／`aria-busy`；section deep link 必須在 async render 後校正。
- GUI capability 必須同時合成 route contract 與 bounded live service；model `configured` 不等於 `verified`。Market label 必須遵守 canonical symbol suffix mapping，async section deep link 只能在 terminal market success／failure 後完成校正。
- Composite control 只允許 wrapper 的單一 `:focus-within` ring；全域 `:focus-visible` 保留清楚 outline，但不得與內層 input 疊成雙框。
- GUI durable read projection 必須 exact owner/account scoped；研究 history 只揭露 final claim 實際引用的 snapshot evidence，paper safety 維持唯讀。服務狀態以 bounded live probe 更新；昂貴 research start 同時只允許一筆且每分鐘最多三筆。Research POST是唯一canonical workflow mutation；model settings PUT／DELETE只管理本次process-memory設定，不具durable或trading authority。
- GUI model settings只有在local research composition可用；API key不得回傳、持久化、記錄或進入artifact，browser secret field 必須關閉 autocomplete 並在 page exit 清除。新設定必須先用pinned transport完成bounded structured completion才原子啟用；worker每筆lease讀取同一代route＋secret snapshot，未設定或未驗證時research POST fail closed。Loopback `/api/` browser request 必須拒絕 cross-site `Origin`／Fetch Metadata，且在昂貴 provider 呼叫前完成。
- 外部 news/web/filing/community/MCP/LLM 內容一律視為 untrusted data；tool 必須 allowlist、typed、read-only、scoped、audited。
- 所有外部輸入必須驗證；API envelope 統一為 `success/status/data/error/metadata`，分頁資訊放 `metadata`。
- 不硬編碼或提交 secrets；錯誤、log、event、report 不得洩漏 token、credentials、敏感 prompt/data。
- Durable配置只保存logical `SecretRef`；local GUI允許process-memory session secret但不得寫入canonical payload或browser storage。local/development/test用exact env strategy，staging/production只接受workload-identity cloud strategy且不得stale/env fallback。Consumer每個logical request重新resolve；canonical durable payload遇secret-shaped資料直接拒絕，不靠egress redaction補救。
- Production human principal 必須由server-side asymmetric OIDC/JWKS驗證並做exact ownership；不得信任client actor/role。Service identity須exact綁issuer/audience/azp/permission/target/fence且不得取得human/operator/admin authority；local token只限loopback local/development/test。
- 所有FastAPI app必須使用中央API security composition；rate limit先做direct-peer/credential admission再做verified principal，forwarded identity預設拒絕。Dynamic outbound URL必須exact allowlist並連線到已驗證public pinned address；未接distributed store/trusted proxy不得宣稱multi-replica enforcement。
- Worker/sidecar ingress 必須在JWT crypto前做bounded direct-peer/credential admission，拒絕所有forwarded identity；request body同時限制bytes、ASGI frames與總deadline，並以`--no-proxy-headers`保留direct peer。Public lease/log不得輸出raw nonce；處理lease須覆蓋最長handler budget，stale completion只能進append-only quarantine audit。
- Telemetry只允許frozen低基數catalog；trace/correlation欄位與canonical payload/hash分離，observer無權跳過、替換、吞掉或重播canonical結果。OTLP runtime不得隱式吃`OTEL_*`、proxy、`.netrc`或redirect；log/span/metric不得含secret、prompt或raw identity。
- Production cost/latency usage必須由同一monotonic clock與Decimal cost形成versioned budget decision；狀態只能`within -> degraded -> failed`。Missing/invalid usage fail closed，`degraded/failed`後不得建立新target/reservation/order，`budget_exhausted`不得retry追單。
- 高風險邊界 fail closed：stale/conflict/unknown data、invalid model output、risk/ledger mismatch、duplicate execution、license drift。

## 上游與授權

- `.research/upstreams/` 只供研究，禁止從該目錄直接 import、vendor 或提交。
- Dexter 與 AI-Trader 授權不完整；禁止複製 source、prompt、skills、assets、frontend/server。AI-Trader 只作 external community HTTP adapter，不能提交 canonical order。
- OpenBB 是 `AGPL-3.0-only`；只能依核准的 optional sidecar policy 接入，process boundary 不代表自動免除 AGPL 義務。
- MIT/Apache 程式碼若移植，保留 copyright/license/NOTICE 與來源 commit；資料與模型權利另行追蹤。
- Heavy upstream 各自使用獨立 lock/image；OpenBB、PyTorch、TradingAgents、Qlib、RD-Agent、Nautilus、LEAN 不得進 core lock。
- Locked dependency audit 必須覆蓋core及每個isolated worker/sidecar project；只做`uv lock --check`不算漏洞稽核，local-build suffix須另以public package identity查advisory。
- Linux core只能使用source-built `psycopg-c`與system `libpq`；不得以bundled `psycopg-binary`隱藏native dependency。正式bundle必含並重驗OpenBB、Alpine與Python exact corresponding source、canonical SBOM/Grype/VEX與所有locks/notices。
- Release必須先在registry publication前通過unsigned candidate，再只對registry exact digest做GitHub OIDC keyless signing/attestation；本機unsigned結果不得宣稱signature或provenance。
- Canonical CycloneDX serial必須deterministic綁定exact image；formal final verifier須獨立重驗五份exact evidence，且SBOM attestation predicate body必須與signed canonical SBOM完全相同。
- Optional profile若blocked/unsupported不得宣稱runtime compatibility；本機render或fail-closed probe不能取代GitHub workflow runtime report，缺外部證據維持unverified。

## 程式與驗證標準

- 優先不可變資料；函數盡量小於 50 行、檔案小於 800 行、巢狀不超過 4 層。
- 不靜默吞例外，不以空 list/DataFrame/`None` 偽裝 infra failure。
- 新功能與 bug fix 採 TDD；核心目標 coverage 80% 以上，另做 PIT、property、contract、E2E、replay、security tests。
- 測試不得依賴 gitignored `.data`／`.research` runtime artifact；必要前置狀態由 test scope 建立，且只清理由該測試建立的路徑。
- Commit 格式：`<type>: <description>`，type 使用 `feat|fix|refactor|docs|test|chore|perf|ci`。
- 每次任務完成同步精簡 `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md` 與 `tasks/todo.md` review；README 只宣稱已驗證能力。
