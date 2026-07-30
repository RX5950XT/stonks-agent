# Stonks Agent 開發交接

更新日期：2026-07-30

## 目前狀態

- P17 為純清理輪：刪除從未被引用的 `ports/instrument_repository.py`、
  `ports/trading_calendar.py`（其職責已由 `domain/calendar.py`＋`market_freshness`
  與 `adapters/postgres/trading_mapping.py` 實作取代）、`domain/evaluation.metric_map`、
  `domain/journal.LedgerAccountKind`、`composition/runtime.utc_now`、
  `regional/base.RegionalMarketDataAdapter`、`api/gui_research.INTENT_HEADER`、
  `CapacityPolicy.process_budget_for`，以及 terminal.css 9 個未使用變數與
  `.notice-failed`／`.paper-rows`。重複實作收斂為三處單一來源：17 份 `_utc_now`
  →`domain/clock.utc_now`；兩個 LLM adapter 的 credential 解析→
  `adapters/llm/_http.resolve_api_credential`；Kronos／TradingAgents 的 worker
  HTTP failure 對映與 origin 驗證→`adapters/_worker_http.py`。行為分支零改動，
  net 約 −250 行。`ports/repository.py` 雖僅自身測試引用，因是既有 Repository
  Pattern 契約宣告且被 4 份 sidecar boundary denylist 參照而保留。
  P17 gate：2,438 passed／6 skipped、coverage 86.83%、ruff format＋check、
  strict mypy 393 files、schemas、upstream／secret policy 全綠；
  `start.ps1 -Check`、`stonks --help`、`stonks-gui serve --help` 實測正常。

- P16 將最新行情改為 backend-owned freshness／quality：XNAS 2026 calendar 輸出
  current／market-closed／delayed／stale／unknown，API 增加 served time、quality
  reasons 與 cache hit；cache delivery 會重算 age，warning／stale 不會被 Browser
  升級。GUI 預設主行情與 watchlist 使用 `1m`，可見分頁每 30 秒 bounded 更新，
  loading 先隱藏舊報價，主摘要明示「非 tick」。Provider request budget 為每分鐘
  30 次，連續三次失敗 cooldown 15 秒。2026-07-29 actual AAPL 1m runtime：
  `openbb:yfinance`、1,699 bars、event age 40 秒、current／available、
  `is_real_time=false`；Chromium 1440×1000／390×844／320×800 都無 overflow、
  0 console error，並觀察到 30 秒自動更新推進最新 bar。免費來源採 curated catalog；
  Alpaca／Finnhub
  需 user credential，Twelve Data Basic 是 non-display，Alpha Vantage free quote
  是 EOD，Cboe 禁止 automated extraction，因此都未冒充 active source。P16 final
  gate 為 2,729 passed／7 skipped、coverage 86.05%、818 files formatted、Ruff、
  strict mypy 393 source files、schemas、Alembic、secret/upstream policy與 core＋
  8 份 isolated dependency audits 全綠，0 known vulnerabilities。另修正三個
  PostgreSQL CLI tests 將 SQLAlchemy redacted URL 當真密碼的既有 bug。
- P15 新增 local GUI session-scoped LLM settings：使用者可在 AI 研究面板輸入
  OpenAI-compatible base URL、Model ID、API key、價格與 request budgets，只有真
  structured completion 驗證成功才原子啟用。Secret 不回傳、不進 browser storage／DB／
  artifact／log，送出後欄位清空；worker 每筆 research lease 取得同一代 route＋secret
  snapshot。Public HTTPS 使用 DNS/IP pinning，local HTTP 只接受 exact loopback；
  provider exact key echo 在 parse／archive 前拒絕。模型未驗證時 GUI CTA disabled，
  direct research POST 也回 typed 503，durable history 仍可讀；完整 env seed 會在
  composition 啟動時自動 probe 以維持既有 launcher 相容。另修正 active-run gate
  必須追蹤到 terminal，並補齊 Kronos forecast metadata、paper NAV／position／risk／
  ledger 已有投影欄位。Chromium 1680×1020／390×844／320×800 為 0 overflow、
  0 console messages，鍵盤 Escape／focus 通過。P15 final gate 為2723 passed／7 skipped、
  coverage 86.04%、815 files formatted、Ruff、strict mypy 391 source files、schemas、
  Alembic、secret/upstream policy與core＋8份isolated dependency audit全綠，
  0 known vulnerabilities；本輪無使用者 LLM env，未宣稱 credentialed external success。
- P14 把已組合後端能力收斂成可操作產品面：owner-scoped recent research 可重新開啟或
  恢復 SSE，只顯示 final claims 實際引用的 PIT snapshot evidence；研究詳情新增 as-of、
  model/tool versions、usage、issues 與 warnings。Paper 面板改讀 typed portfolio／NAV／
  risk／global kill switch／projection integrity，空資料與 unavailable 分開呈現且全程唯讀。
  服務狀態改為 bounded live probes，research start 另有 single-active／每分鐘三筆成本
  gate；browser principal 移除 PAPER_OPERATOR，唯一 mutation 仍是 research POST。
  GUI 資訊架構改為研究與安全決策優先，加入 section navigation、鍵盤可讀 OHLCV、
  citation evidence explorer、collapsed expert console、ARIA／focus／reduced-motion。
  Chromium fixture 1680×1020、390×844、320×800 皆無 page overflow、console 0。
  P14 final gate 為 2699 passed／7 skipped、coverage 86.18%、808 files formatted、
  Ruff、strict mypy 386 source files、schemas、Alembic、secret/upstream policy及
  core＋8 份 isolated dependency audit 全綠，0 known vulnerabilities。
- P13 已把 Kronos 真正接進 GUI research：research facade 固定先 materialize canonical
  `1d` snapshot，preflight 帶入 exact manifest/content hash，production forecaster 以
  lease generation/nonce 與 ephemeral RS256 service identity 呼叫 CPU worker，raw response、
  3 seeded paths 與 `ForecastOutputArtifact` 一起進 `ResearchWorkerResult` 1.1 terminal
  artifact。GUI 顯示 actual model/revision、return probability、volatility、downside與
  quality；缺 genuine strategy evaluation 時 alpha 明示 blocked、weight 0、no-order。
- `start.ps1` 預設 research 現在會自動 build／ready／啟停 Kronos CPU，使用 unique
  Compose project與no-masquerade loopback bridge；停止時不刪模型、artifact或DB volume。
  `-KronosPort` 預設17200，`-Check`維持無副作用且Windows PowerShell 5.1／PowerShell 7
  均可解析。Actual production forecaster以pinned model完成3 paths，raw/path artifacts
  均已封存；Chromium 1680×1020與390×844均無水平溢位且0 console error。P13 final
  gate為2687 passed／7 skipped、coverage 86.33%、805 files format、Ruff、strict mypy
  385 source files、schemas、Alembic、secret/upstream policy與core＋8份isolated
  dependency audit全綠；外部真LLM success仍需使用者設定endpoint/model/key。
- P12 新增根目錄 `start.ps1` 薄 launcher，預設 research，另支援 market／paper；
  會檢查完整 source checkout、uv、Docker Compose／daemon 與 LLM env，API key 不接受
  argv、不讀檔、不輸出，`-Check` 只顯示 redacted canonical command。另新增
  exact-allowlist `scripts/clean_workspace.py`；首次 actual 清除 125 個 rebuildable
  targets／1,773,168,421 bytes，保留模型、artifacts、GUI DB、research evidence、
  root/OpenBB/Kronos env與Docker volumes。P12 final gate為2679 passed／7 skipped、
  coverage 86.68%、799 files format、Ruff、strict mypy 381 source files、schemas、
  Alembic 0018、secret/upstream policy及core＋8份isolated dependency audit全綠。
- Git 已初始化於 `main`；`PLAN-AUTH` 已成立，依 P0 → P6 連續實作。
- P9 已組合 env `SecretRef` OpenAI-compatible LLM、PIT-scoped read-only evidence tools、
  fenced worker dispatcher、live OpenBB snapshot materialization、durable research handler、
  GUI research POST／SSE 與 `--with-research` lifecycle。AAPL actual snapshot 為 30 筆
  evidence 且無 fixture fallback；缺 LLM 設定會進 typed `configuration_invalid`，
  不偽造成功。真 LLM success 尚待使用者提供 endpoint／model／key。
- Kronos pinned CPU runtime 已逐檔重驗模型並完成真 inference；目前 configuration 是
  `shadow`、paper weight 0。baseline 尚 draft、opinion mapper disabled，因此
  artifact-backed 九階段 handler 雖已實作，`paper_fund_cycle` 仍不得註冊成可成交
  runtime；合法終態維持 no-order，直到 genuine evaluation/promotion artifact 存在。
- P9 repository gate：2618 passed／7 skipped、coverage 86.64%、783 files format、
  Ruff、strict mypy 377 source files、schemas、Alembic drift、upstream／secret policy
  與 locked dependency audit 全綠，0 known vulnerabilities。Chromium 1680×1020 與
  390×844 皆無水平溢位且 console 0 errors／warnings；research failure 顯示 typed
  `configuration_invalid`。
- P10 已把 GUI 從命令列優先 terminal 重做為後端導向的 Stonks Desk：可見的 symbol
  search、interval controls、watchlist 與主要研究按鈕；研究面板呈現四階段進度、
  typed failure、confidence、claims＋evidence refs、counterarguments、risks、
  Kronos／paper decision 與折疊報告。Research 在 POST 前即 single-flight，late
  detail 以 serial fence 拒絕，terminal 後重新讀取 paper projection。
- P10 Chromium actual 使用真 OpenBB／PostgreSQL／research composition；1680×1020
  與 390×844 皆為 0 console errors／warnings、無水平溢位，窄版初始 `scrollY=0`
  且不 autofocus。直接搜尋 `MSFT` 成功換標的；缺 LLM 設定顯示
  `configuration_invalid`，沒有沿用舊 claims 或偽造 model／paper 結論。
- P10 完整 `verify.py --with-postgres` 為 2621 passed／7 skipped、coverage 86.64%、
  783 files format、Ruff、strict mypy 377 source files、schemas、Alembic drift、
  upstream policy、secret scan與 locked dependency audit 全綠，0 known vulnerabilities。
- P11 已修復研究 citation laundering、未執行的 tool timeout、raw lease nonce輸出、
  research late-result audit缺口、worker lease過短、worker/sidecar Slowloris與JWT前
  admission缺口。六個service surface現在使用bounded peer/credential fixed window、
  拒絕forwarded identity、body byte/frame/deadline cap與`--no-proxy-headers`；stale
  research result只進PostgreSQL append-only quarantine。
- 供應鏈已將Kronos CPU/CUDA升至PyTorch 2.13.0、相關setuptools升至83.0.0，並讓
  local/CI verify audit每份isolated lock。新版CPU image已用pinned 115MB weights完成
  actual ready/3-path forecast與shadow alpha mapping；仍為`paper_eligible=false`、weight 0。
  Docker Desktop對internal network不建立host publish，verifier只在smoke期間附加唯一、
  disabled-IP-masquerade bridge，production Compose仍維持internal network。
- P11 final gate：2671 passed／7 skipped、coverage 86.68%、796 files format、Ruff、
  strict mypy 381 source files、schemas current、Alembic 0018無drift、upstream/secret
  policy全綠；core及8份isolated runtime locks均為0 known vulnerabilities。
- 目前工作樹已推進為未發布 `0.2.0` candidate，包含 P7 Local GUI；formal immutable
  `v0.1.2` 是 GUI 之前的歷史 release，不能用其簽章或 runtime 證據替 0.2.0 背書。
- 第一次使用請從 `README.md` 的離線 `fake-cycle` 開始；`docs/README.md` 是統一文件
  索引。專案仍是 pre-alpha、paper-only；default deployment 只有 health/readiness，
  尚未組合 production business API 或常駐 dispatcher，也不支援 live trading。
- `uv run --frozen stonks-gui serve` 提供 Stonks Desk：直接操作的研究工作區、
  進階命令列、canvas K 線＋成交量、關注清單與 provenance rail。Sidecar allowlist 已加入 `interval`，
  實測可取 `1m`／`5m`／`15m`／`1h`／`1d` bars；報價由 bar 序列推導並固定
  `is_real_time=false`。加 `--with-paper` 會啟動具名 volume 的本機 PostgreSQL、執行
  migration、bootstrap `paper-local` 帳戶，並唯讀顯示 canonical portfolio／NAV 投影；
  未啟用時面板明示不可用且不顯示示範數字。`--with-research` 隱含啟用 paper，
  並只新增一條 same-origin、loopback-only research POST；其餘 route 維持 GET。
- GUI 政策由「零 JavaScript」改為「只允許同源本地 script」：CSP 為
  `default-src 'none'` 加全部 `'self'` 來源，禁 inline／eval／外部 origin／`data:`；
  未引入 npm、node_modules 或打包器，supply chain 與 SBOM 邊界不變。
- Yahoo 的 `price/quote`、`profile`、`fundamental/*`、`discovery/*` 實測全數 401
  （yfinance cookie 種子主機 `fc.yahoo.com` 無法解析，`1.5.1`／`1.5.2` 相同），因此
  維持不在 allowlist、不提供、也不換來源冒充；公司簡介、財報指標與漲跌幅排行未實作。
- OpenBB 日內 bar 是 naive 交易所本地時間，adapter 依 `America/New_York` 轉 UTC，
  並以交易所本地 session date 檢查請求範圍；已對照 Yahoo epoch 驗證（15:30 → 19:30Z）。
- GUI launcher 只支援完整 source checkout，因為 runtime 需要 repository 內 Compose
  與 OpenBB source build context；standalone wheel、core image 與 `v0.1.2` 不支援啟動。
- P8 actual-runtime gate：2026-07-27 以外部 `openbb:yfinance` 取得 `AAPL` 1d／`MSFT` 1d／
  `NVDA` 1h（133 根，最新 `2026-07-24T19:30:00Z`）／`AAPL` 5m（390 根）／`AAPL` 1m
  （1949 根）；不存在 symbol 回 typed `data_unavailable` 且無 fixture fallback。
  `--with-paper` 完成 migration 至 `0017` 並讀回 `paper-local` 的 USD 可用 100000.00、
  NAV `尚未估值`。Chromium 1680×1020 與窄視窗皆無水平溢位、console 0 errors，
  `/` 聚焦命令列、`F1`／`Esc` 開關說明、`ADD`／`DROP` 與失敗狀態皆通過。完整
  `verify.py --with-postgres` 為 2536 passed／7 skipped、coverage 87.39%，749 files
  format、Ruff、strict mypy 357 source files、schemas、Alembic 無 drift、upstream/
  license、secret 與 dependency audit 全綠，0 known vulnerabilities。
- P7 actual-runtime gate：2026-07-26以外部`openbb:yfinance`取得`AAPL`／`MSFT`各5根
  最新可用EOD bars（latest event 2026-07-24），不存在symbol為503且無fixture fallback；
  Chromium 1440/390/320、keyboard、success/error與無頁面overflow通過。完整gate為
  2247 passed／6 skipped／269 deselected、coverage 87.70%，748 files format、Ruff、
  strict mypy 356 source files、schemas、upstream/license、secret與dependency audit全綠。
  `0.2.0`仍是unsigned worktree candidate，沒有formal release證據。
- P0–P5 phase gates、P6.1–P6.11 repository implementation、本機 gates與GitHub外部驗證已完成；exact commit `5e9c2973b782cd1bd7274e6e6852cbe1df08a4f9`的CI run `30200612158`為13/13 jobs成功，Supply-chain run `30200612154`亦成功。Optional report重驗為4 actual、5 blocked、1 unsupported且0 canonical paper side effects。Repository已公開並配置active SemVer tag ruleset、required-reviewer `release` environment、tag-only deployment policy與immutable releases。
- Protected `v0.1.2` release run `30200908948`的六個jobs全數成功；GHCR exact digest為`sha256:9c61a2d5dd59d07d30318b483a7a205ac8af394236662b45021574e42ff19976`。Signed artifact `8631709866`已由fixed Cosign v3.0.6 canonical verifier重驗五份evidence，GitHub provenance/SBOM、registry signature/attestation、immutable Release及兩個release asset attestations亦獨立通過；正式archive與workflow artifact共208 files且hash-identical。Windows CP950重驗暴露的CLI UTF-8解碼問題已用explicit subprocess encoding修正並以同一bundle驗證。`v0.1.0`／`v0.1.1`失敗tag保持immutable且只作診斷證據。
- Release 後的 formal closure 文件提交 `e9095a2cecda4fbd0d22f5e0157bad2f2098fe26`
  另由 CI run `30202214474`（13/13）與 Supply-chain run `30202214473` 重驗成功；
  不與 release tag 所綁的 `5e9c297` publication evidence 混為同一版本。
- 2026-07-26 使用文件維護已將 README 改為可執行 onboarding，新增
  `docs/README.md`、全部 public docs local-link gate與repository metadata regression；
  離線 `fake-cycle`、CLI help、33 optional tests、10 docs/API tests及完整non-PostgreSQL
  gate均通過（2218 passed、6 skipped、coverage 87.85%）。Default Compose、optional
  identity與live-trading限制已在首屏與runbook明示。
- P6.8新增paper-only release contract、immutable CI/actions/scanners、canonical SBOM/license inventory、exact Grype DB/OpenVEX、keyless Cosign/GitHub attestations與pre-publish unsigned verification。Linux core改為source-built `psycopg-c`＋Alpine `libpq`；OpenBB、37-package Alpine及certifi/psycopg/psycopg-c source archives皆deterministic且由bundle verifier逐member重驗。GitHub release job可恢復既有draft；已發布後只重驗immutable release/assets，不重建publication。
- P6.9新增11項frozen resilience drill catalog、telemetry cross-contract與55項focused failure tests；provider/LLM/model/sidecar outage、artifact corruption、worker crash/lease/dead-letter、duplicate/stale result及ledger mismatch皆證明fail closed。Digest-pinned PostgreSQL actual drill使用fresh source/target、bounded custom dump與stdin restore，重驗Alembic `0017`、canonical replay/hash-chain/append-only/source marker；本次獨立量測RTO 0.719秒、RPO 0秒/0 lost events，只是single-host drill evidence，不是production SLA，restore不會自動promote。
- P6.10新增frozen `stonks-capacity/1` policy、六種workload×20 samples、independent report verifier、actual PostgreSQL queue/snapshot/research primitive、ASGI/forecast/full paper-cycle probes與least-privilege CI artifact。Runtime resource只量`probe_process`並對獨立4000m/2048MiB budget；六組部署budgets只做static manifest cross-check。TradingAgents/Kronos/Quant重工作offload event loop，gate固定1，滿載立即429且core不retry。
- P6.11完成architecture/ADR、API/OpenAPI、runbook與handoff evidence索引；notice closure把MIT selective port納入signed payload/runtime image。Final verifier獨立重驗image、manifest、report、provenance、SBOM五份evidence，要求exact repository/digest/workflow/ref/commit、deterministic image-bound CycloneDX serial及predicate body exact canonical。Optional matrix固定10 profiles與honest supported/blocked/unsupported狀態，blocked不得冒充runtime compatibility。
- P1/P3/P4/P6目前包含PostgreSQL 0001–0017、PIT evidence/snapshot、repositories/UoW、content-addressed artifacts、durable job/outbox/inbox與trace propagation、strategy registry、paper account/trading ledger、execution receipts、operator/artifact maintenance audit chains、immutable portfolio valuations與owner-scoped research/paper records。
- Human API principal只由server-side asymmetric OIDC/JWKS與frozen RBAC policy建立；central FastAPI dependency及application ownership checks拒絕forged actor/role與IDOR，local token/DB CLI只限明確loopback local/development/test。
- Remote worker/sidecar只接受exact issuer/audience/azp/permission/target/generation/nonce/deadline service credential；無DB credential、人類角色、operator/admin或paper authority，舊fence與錯誤target fail closed。
- S3 production transport只接受injected atomic credentials，以official SigV4、DNS/IP pinning、no redirect及exact origin/bucket/prefix執行；finalize採object-first/manifest-last，WORM/legal hold只增不減，GC永不實體刪除任何historical finalized artifact，restore只處理exact delete marker/version。
- Secret config只保存logical refs；local/development/test使用exact env strategy，staging/production只接受workload-identity cloud client且不做stale/env fallback。OpenAI、Anthropic、Financial Datasets與AI-Trader每個logical request重新resolve，retry固定同version、下次request取得rotation。
- Structured error/log/report在sink前使用bounded immutable-copy sanitizer；canonical run event/job/outbox/last_error在JSONB bind前拒絕secret-shaped payload並整筆rollback，不靠API egress redaction掩蓋DB洩漏。
- 所有FastAPI app共用typed security composition：body byte/frame cap、body前edge/credential admission、body後verified-principal limiter、exact CORS、security headers、forwarded identity拒絕與structured errors。Cookie模式顯式opt-in並強制same-origin/double-submit CSRF；webhook以exact URL/public DNS/TCP pin防SSRF與redirect pivot。
- P6.4新增frozen W3C trace/correlation、低基數metric/span catalog、redacting log correlation與exact OTel SDK/OTLP runtime。五個API、job/outbox/inbox、queue/worker與provider/model/signal/risk/execution/reconciliation/delivery可延續trace；observer failure、skip、偽造、吞錯或duplicate callback都不能改變canonical outcome。
- Pinned Collector/Prometheus/Grafana使用internal backend與loopback ingress、non-root/read-only/cap-drop/resource limits、external Grafana secrets及provisioned dashboard；真實runtime smoke已從core送出OTLP trace/metrics並在collector canonical endpoint驗證。Trace sink目前nop、狀態tmpfs，未接remote backend/multi-host TLS；synthetic carrier span尚未回綁SDK child span ID。
- P6.5新增strict `config/{budgets,slo}.yaml` loaders、immutable operational budget decision與low-cardinality SLO metrics。Cost只接受Decimal、latency只接受同一monotonic clock讀值，狀態維持`within→degraded→failed`單調；missing/invalid usage fail closed。Research與paper cycle在外部／canonical stage前重驗，soft/hard超限後不再建立target/reservation/order，PostgreSQL將`budget_exhausted`視為非重試terminal，不能追單。
- Prometheus新增correctness、API/worker availability、p95 latency、normalized 5m/1h/30d budget burn、hard outcome與soft usage alerts；pinned `promtool` config/rules/fixtures及三容器Collector→Prometheus exact-label smoke已通過。Correctness series會以0初始化以偵測缺失，但counter為0仍不能單獨證明100% coverage；目前只有policy routing、tmpfs狀態且無Alertmanager/paging送達。
- Job/snapshot/outbox的claim、deadline、lease與commit timestamps使用transaction內PostgreSQL clock；generation/nonce、caller clock drift、cross-run retry與完整audit graph皆有真實PostgreSQL測試。
- Reconciliation成功決策封存雙側raw/normalized hashes、metric/value、threshold與decision；conflict維持0 artifact writes並留下hash-chained failure event/outbox。
- Financial Datasets與OpenBB已驗證read-only observation contracts與共用daily query；OpenBB另有actual local GUI observation path，canonical materialization仍只宣稱replay source。`stonks-worker`只提供claim-once，不宣稱常駐dispatcher。
- Optional OpenBB sidecar已實測exact GET allowlist、exact service OIDC/source identity、frozen 64-package lock、SBOM/license policy、4個upstream sdists、AGPL source archive與non-root/read-only runtime。
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
- P2.12新增`ResearchPipelineCommand/Result`與application pipeline gate；同一PIT context先驗deterministic artifact與TradingAgents opinion的run/as-of/evidence scope，再把兩者ID注入structured report attribution、完成三channel render與file delivery。每次succeeded/degraded/failed結果皆封存public-safe immutable audit；provider/deterministic/report outage fail，TradingAgents outage degrade，任何contract均無target/order。Durable full-cycle transition/commit已由P4.7完成；常駐production dispatcher仍未宣稱完成。
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
- P4.1新增canonical portfolio/risk/reservation/order/fill/journal domain與typed policy/execution/ledger ports。Risk綁target/account/portfolio sequence與expiry；reservation原子推進account sequence，order必須exact等於authorized delta，command另要求open reservation與current post-reservation sequence。Reservation/order/journal events具closed state、monotonic sequence與hash；journal每種commodity在explicit quantum下exact平衡。
- P4.2新增0010 paper trading schema、SQLAlchemy mappings、typed repository port與PostgreSQL repository/UoW。Account CAS與matching hash-chained event、cash/position reservation projection、order idempotency/event chain、fill與balanced journal皆在同transaction；DB拒絕orphan event、無audit sequence update、append-only mutation與不平journal。App只有scoped insert/update，reader唯讀，worker無trading grants；corrupt persisted payload structured fail closed。
- P4.3新增frozen portfolio construction inputs、runtime-checkable policy Strategy、versioned fixed-weight policy與deterministic builder。NAV只接受單一base currency及exact quantity/currency quantum；只讀paper-eligible、calibrated、unexpired且point-in-time registry/evaluation binding一致的signal。Score不對缺少權重重新正規化，依序套deadband、shrinkage、current-weight turnover penalty與long-only bound，再以固定Decimal規則向下量化quantity；target保存stable calculation hash、turnover與cost diagnostics。
- P4.4新增frozen risk state/policy、19個stable hard checks、atomic risk authorization use case與multi-instrument reservation/order batch。Target/account/ledger、signal/evaluation、mark/session、kill switch、cash/position/open reservation reconciliation、pending order、ADV、single/sector/asset/gross/net exposure、turnover、drawdown/daily loss皆重驗；rejected decision可稽核但不產order。Approved path在同一UoW重讀account、保存target/decision、以一次CAS推進所有projections並建立全部reservations/orders；任一不足或drift整批rollback。
- P4.5新增frozen execution bar/request/policy/outcome、deterministic next-bar broker、core-owned execution UoW與0011 append-only durable receipt。Broker嚴格要求bar `opens_at > issued_at`且`available_at <= as_of`，market/limit、DAY/GTC/IOC、expiry、spread/slippage/impact、fees與volume participation/partial fill皆由content-hash policy固定；pending不製造fill。Persistence在account row lock內重驗intent/reservation/sequence、原子append order/reservation events、fills與receipt並consume/release projection；concurrent duplicate重播同receipt，payload/projection drift rollback。
- P4.6新增immutable account opening snapshot、versioned average-cost ledger policy、deterministic fill journal、replay/reconciliation use cases、PostgreSQL generic ledger projections與0012 migration。BUY/SELL以cash/inventory value與units/fee/realized PnL/clearing accounts逐commodity exact平衡；opening position因缺basis而禁止SELL。Execution現在於同一account-serialized UoW寫fill、journal、settled cash/position、ledger head、receipt與event，deferred DB guard拒絕任一fill/journal缺邊。Receipt replay重驗完整graph；gap、tamper、projection/order-state drift先rollback，再由獨立transaction啟動singleton global kill switch。
- P4.7新增frozen canonical cycle stage/reference/state/result contracts、core runner與PostgreSQL paper-cycle store。Stage prefix固定為evidence、research/opinion、signal、target、risk、order、receipt、ledger、report；每一checkpoint保存stably sorted ID/hash refs及state hash，加入既有run-event/outbox hash chain。所有load/checkpoint/retry/dead-letter/complete都以DB clock重驗job generation/nonce/owner/lease/deadline與run input hash；cancel有version CAS與actor/reason audit。完整state封存content-addressed result artifact。Execution receipt commit後、checkpoint前crash再以新generation重領，會先重播既有receipt並完成ledger/reconciliation，實測無duplicate fill/journal/receipt。
- P4.8新增frozen PIT mark/valuation/outcome/reflection contracts與三個monitoring use cases。Mark-to-market只接受settled ledger、單一base currency與每個open position的exact available mark；NAV、fees、realized PnL、position values及hash可deterministic replay。Outcome exact綁approved historical decision/target、strict valuation path、benchmark與fill refs，fee delta必須等於fills總費用，再封存為derived EvidenceItem。Reflection request只allow該outcome evidence，candidate必須是新`ResearchArtifact`且引用它，不能改寫歷史交易物件。
- P4.9新增frozen operator command/state/action contracts、role-gated activate/reconcile/resume use cases、PostgreSQL operations repository、0013 append-only hash-chain audit與paper CLI/API。Global/account activation使用switch version CAS，於同一transaction終止pending orders並釋放reservations；既有fills/journals永不刪除，新execution由ledger authority拒絕。Resume先鎖定scope/account並完成exact journal replay reconciliation；drift寫入`resume_rejected`並保持active，manual reconciliation drift則audited後啟動global switch。Action sequence/previous hash/head CAS由deferred DB triggers防止竄改。
- P4.10新增`ReportReference`與五組core-controlled target/risk/order/fill/outcome refs、content-hashed portfolio/risk read models、ledger-bound NAV recording、0014 append-only valuation與read-only paper projection CLI/API。Portfolio重驗account event chain並明示settled/reserved/available；NAV只接受current ledger sequence/hash/projection exact match，ledger一移動即拒絕stale snapshot；risk projection揭露latest decision/current binding但沒有order authority。真實小型portfolio完成pre/post NAV、fill/journal、outcome evidence、report refs、JSON replay與reconciliation。
- P5.1新增9個clean-room external platform schemas與runtime-checkable `PlatformPort`。Publication必須是public redacted thesis並綁exact artifact/hash/evidence/deadline；feedback page固定cursor、dedup、stable order與PIT；challenge/experiment均research-only。所有remote publication、feedback、position/outcome與activity response一律`untrusted_content=true`、`remote_authority=evidence_only`，port不存在order/copy/risk/DB/queue能力。
- P5.2新增default-off AI-Trader HTTP adapter與config/cassettes。Adapter只允許exact `https://api.ai4trade.ai`的strategy/discussion/reply、heartbeat、challenge與experiment routes；禁止redirect與automatic POST retry，以bounded canonical JSON、scoped Bearer token、typed tolerant response、raw artifact archive及injected inbox保護邊界。Schema/authz/redirect/body anomaly會停用instance；heartbeat使用opaque cursor並對event ID/payload hash duplicate/conflict fail closed。Live OpenAPI因DNS無法解析而未驗證，現有contract只綁固定snapshot `d03ff6c`的最小runtime shapes，不作current production保證。
- P5.3新增frozen/hash-bound community policy、command與decision。Policy只在publication window closed後接受exact platform/subject與PIT evidence；duplicate、future、scope drift及remote reputation與core policy snapshot不一致皆fail closed。同作者只計一次core-trusted reputation，support不加confidence，late/unknown reputation忽略，prompt-injection quarantine。Threshold只可選ignore、confidence haircut或經enqueue-only `JobEnqueuePort`建立固定safe question的research-only job；queued payload不含remote原文，module無signal/portfolio/risk/order/execution dependency。
- P5.4新增14個frozen canonical backtest schemas、runtime-checkable `BacktestEnginePort`與core validation boundary。Job固定content-addressed strategy/dataset、PIT calendar/session/bar、instrument/currency quantum、opening cash/positions、simulation-only orders與deterministic cost model。Result exact綁runtime/fence/input hashes與deadline；core重驗第一個可成交next bar、market/limit adverse price、volume cap、fees/slippage、outcomes及cash/position projection。Port無paper risk/reservation/broker/ledger或heavy runtime authority。
- P5.5新增default-off NautilusTrader `1.230.0` sidecar、獨立lock/image、bounded authenticated HTTP adapter與真實engine replay。Canonical scheduler固定open-cross market/limit、DAY/GTC/IOC、calendar session、shared participation cap與engine-neutral outcomes；Nautilus只產生scheduled-child native order/fill events，trade ID與除隨機event ID外的raw payload hash綁入fill provenance。Core再次重驗P5.4 economics/projection；runtime/source/image identity、deadline、work/child/request/concurrency bounds皆fail closed。Container使用internal network、non-root/read-only、cap-drop與resource limits，無core module或DB/provider/queue/broker credentials；LGPL/GPL/source replacement、SBOM與CVE gate已驗證。
- P5.6新增default-off QuantConnect LEAN `17917` / commit `c22774e` sidecar、獨立Python/NuGet locks、bounded authenticated HTTP adapter、固定C# algorithm與真實Launcher replay。Canonical scheduler擁有TIF/session/shared-volume/cost/projection，LEAN只回scheduled-child authority-free trace；core再次重建P5.4 result。Runtime/source/license/modification/image identity exact綁定，每job fresh process受deadline/work/trace bounds；internal、non-root、read-only container無core或paper credentials。Exact source隨image提供，NuGet transitive gate、Syft SBOM與Grype驗證0 High/Critical。
- P5.7新增frozen/content-hash parity policy/request/report與reference-baseline evaluator。所有engine先各自通過P5.4 exact job/result validation，再比較order/fill/cash/position/fee/warning的bounded hashes；canonical threshold固定0，warnings可有bounded threshold。Report保存runtime/image/job/result/semantic/fill-provenance hashes並明示只涵蓋fixture的adapter-normalized semantics；不含raw warning、engine選擇、平均、promotion、target/order或paper authority。Disabled/failed/late/tampered engine直接structured failure，不產生可能被誤讀為equivalent的report。
- P5.8新增clean-room RD-Agent `factor-expression-v1` sandbox。Frozen contracts綁定stochastic generation artifacts、archived source、label-free PIT dataset、sandbox/runtime/fence與one-shot receipts；default-deny AST只允許單一pure `compute(rows)`。可信launcher必須在兩個不同fresh containers取得exact canonical bytes，core再重掃、重建signals並呼叫P3.4完整evaluation；worker與aggregate都沒有target/order/risk/ledger/registry/promotion authority。Pinned MIT source只作provenance archive；Python 3.12.13/Alpine image為network none、UID/GID 65532、read-only、cap-drop/NNP/AppArmor與resource bounded，並移除未用tar/XML/HTML/compression/webbrowser/Windows asyncio/SQLite/system pip capabilities。Final runtime hash為`592710a3...b9fe`、digest為`sha256:62c9003c...08e5e`；593-component SBOM/27 packages、exact OpenVEX Grype與actual escape/network/CPU/output/reproducibility smoke全通過。
- P5.9新增`config/features.yaml`與frozen typed loader，固定11個integration的kind、exact profile、config path、environment allowlist、network、output scope、readiness/execution denial與supply-chain policy。缺檔回全關閉，malformed/unknown/live-authority或boundary drift fail closed。`infra/compose.optional.yaml`沒有default-active/core/database/broker service，10個explicit profiles皆可零credential render；Nautilus新增hardened Compose manifest，LEAN/RD-Agent/Kronos以safe render placeholder保留runtime-side fail-closed identity/model驗證。Freqtrade、FinRL、vectorbt僅為future RFC，沒有image/profile/dependency；runbook與Linux CI固定操作及governance gate。
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

## P0-P6 可重跑證據

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
- P4.1 focused domain/ports為23 passed、新模組branch coverage 83%；P0 execution/fake-cycle regression為20 passed。完整non-PostgreSQL gate為843 passed、190 deselected、coverage 88.14%，362 files format、ruff、mypy 217 source files、67 schemas、upstream/license、secret與locked dependency audit全通過。
- P4.2 focused PostgreSQL migration/repository為35 passed，trading repository branch coverage 84%。完整PostgreSQL gate為1048 passed、coverage 88.44%，370 files format、ruff、mypy 222 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P4.3 focused golden/property/boundary為16 passed，builder branch coverage 97%、construction contracts 92%。完整PostgreSQL gate為1064 passed、coverage 88.55%，376 files format、ruff、mypy 225 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P4.4 focused risk/authorization/portfolio/PostgreSQL regression為53 passed；risk evaluator branch coverage 94%、authorization 84%、trading repository 85%。完整PostgreSQL gate為1091 passed、coverage 88.60%，389 files format、ruff、mypy 233 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P4.5 focused application/golden/PostgreSQL為31 passed，四個execution核心模組合計branch coverage 81%。完整PostgreSQL gate為1122 passed、coverage 88.27%，400 files format、ruff、mypy 238 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P4.6 focused ledger/execution/PostgreSQL為53 passed，五個核心模組合計branch coverage 80.98%、reconciliation 95%。完整PostgreSQL gate為1144 passed、coverage 87.92%，412 files format、ruff、mypy 243 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P4.7 focused domain/application/PostgreSQL/E2E為21 passed，三個workflow核心模組合計branch coverage 85.57%、runner 91%。完整PostgreSQL gate為1157 passed、coverage 87.78%，419 files format、ruff、mypy 246 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P4.8 focused monitoring為11 passed，domain/application四個新模組branch coverage 82.27%。完整PostgreSQL gate為1168 passed、coverage 87.64%，429 files format、ruff、mypy 251 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P4.9 focused domain/application/API/PostgreSQL為23 passed，新模組branch coverage 82.45%；migration/operator/execution/ledger regression為48 passed。完整PostgreSQL gate為1191 passed、coverage 87.47%，445 files format、ruff、mypy 261 source files、67 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P4.10 focused contracts/projections/API/PostgreSQL/E2E為22 passed，新模組branch coverage 80.59%；P4 safety matrix為215 passed。完整P4 PostgreSQL gate為1206 passed、coverage 87.32%，459 files format、ruff、mypy 268 source files、68 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P5.1 focused contracts/authority/port為23 passed，external platform contract branch coverage 92%。完整PostgreSQL gate為1223 passed、coverage 87.37%，463 files format、ruff、mypy 271 source files、77 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P5.2 focused platform/adapter/config/security為44 passed，兩個adapter模組合計branch coverage 83.70%。完整PostgreSQL gate為1244 passed、coverage 87.27%，469 files format、ruff、mypy 274 source files、77 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P5.3 focused platform/community/security為42 passed，community policy branch coverage 90.28%。完整PostgreSQL gate為1254 passed、coverage 87.31%，472 files format、ruff、mypy 275 source files、77 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P5.4 focused contracts/security/P4 execution regression為107 passed，新backtest模組branch coverage 84.59%。完整PostgreSQL gate為1267 passed、coverage 87.24%，479 files format、ruff、mypy 279 source files、91 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P5.5 focused core/contract/security regression為47 passed，actual Nautilus wheel為19 passed；新root模組branch coverage 84%、Nautilus engine 92%。Hardened image在internal network完成authenticated HTTP replay、runtime/image/source/license與authority-isolation smoke；65-component SBOM與sidecar lock audit無已知CVE。完整PostgreSQL gate為1281 passed、coverage 87.25%，491 files format、ruff、mypy 280 source files、91 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P5.6 final hardened image經P5.7首根bar regression修正後，runtime hash為`ca04cdf4...34087`、digest為`sha256:a8fa4479...d3857`；4,754-component CycloneDX/166 packages，Grype仍為0 Critical/High。
- P5.7 focused core/backtest/parity/security為33 passed、LEAN sidecar為22 passed；真實internal-network matrix涵蓋7組MARKET/LIMIT、BUY/SELL、DAY/GTC/IOC、partial/shared-volume/multi-session/halted fixtures，每個engine各重播2次，共28次HTTP執行且semantic exact match、native fill provenance各自stable。完整PostgreSQL gate為1313 passed、coverage 87.32%，511 files format、ruff、mypy 282 source files、91 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P5.8 focused root為31 passed、獨立worker為28 passed；actual image runtime/Compose、source/license hashes、593-component SBOM、OpenVEX Grype、escape/network/rootfs/socket/CPU/output/reproducibility smoke全通過。完整PostgreSQL gate為1344 passed、coverage 87.41%，529 files format、ruff、mypy 285 source files、106 schemas、Alembic無drift、upstream/license、secret、core與worker locked dependency audit全通過。
- P5.9 focused catalog/security為14 passed；zero-default與10個explicit Compose profiles逐一render通過。完整non-PostgreSQL gate為1119 passed、239 deselected、coverage 87.64%；完整PostgreSQL P5 gate為1358 passed、coverage 87.43%，532 files format、ruff、mypy 286 source files、106 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。
- P6.1完整單程序PostgreSQL gate為1581 passed、3 skipped、coverage 87.45%；564 files format、Ruff、strict mypy 301 source files、106 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。所有optional manifests可用ephemeral asymmetric issuer/JWKS render；OpenBB hardened live smoke、service ingress matrix與key rotation runbook均已驗證。尚未宣稱連接真實外部IdP，跨host TLS/mTLS留待P6.7 deployment gate。
- P6.2 focused為187 passed；完整non-PostgreSQL gate為1404 passed、3 skipped、244 deselected、coverage 87.66%。完整PostgreSQL gate為1648 passed、3 skipped、coverage 87.46%；582 files format、Ruff、strict mypy 309 source files、106 schemas、Alembic無drift、upstream/license、secret scan、actionlint與locked dependency audit全通過。Cloud strategy以injected fake workload client驗證rotation/outage，尚未連接真實cloud secret manager，不宣稱live integration。
- P6.3 focused為199 passed；完整non-PostgreSQL gate為1497 passed、3 skipped、244 deselected、coverage 87.42%。完整PostgreSQL gate為1741 passed、3 skipped、coverage 87.26%；593 files format、Ruff、strict mypy 316 source files、106 schemas、Alembic無drift、upstream/license、secret scan、actionlint與locked dependency audit全通過。Rate limit仍為單process store，trusted proxy/distributed enforcement、DNS resolver lifetime/timeout pin與HSTS留待後續deployment gate。
- P6.4 focused telemetry/API/durable/infra matrix與真實三容器OTLP smoke全通過；完整non-PostgreSQL gate為1620 passed、3 skipped、258 deselected、coverage 87.75%，完整PostgreSQL gate為1878 passed、3 skipped、coverage 87.48%。617 files format、Ruff、strict mypy 323 source files、106 schemas、Alembic無drift、upstream/license、secret scan、actionlint、frozen lock與locked dependency audit全通過。
- P6.5 focused budget/SLO/config/canonical flow/Prometheus rules與真實三容器smoke全通過；完整non-PostgreSQL gate為1679 passed、3 skipped、259 deselected、coverage 87.85%，完整PostgreSQL gate為1938 passed、3 skipped、coverage 87.57%。630 files format、Ruff、strict mypy 329 source files、106 schemas、Alembic無drift、upstream/license、secret scan、actionlint、frozen lock與locked dependency audit全通過。
- P6.6 focused artifact/config/security/infra matrix為138 passed，PostgreSQL migration/audit為34 passed；digest-pinned SeaweedFS完成真實SigV4、conditional finalize、checksum/SSE metadata roundtrip與presigned GET smoke。完整non-PostgreSQL gate為1817 passed、3 skipped、267 deselected、coverage 87.69%，完整PostgreSQL gate為2084 passed、3 skipped、coverage 87.45%；660 files format、Ruff、strict mypy 343 source files、106 schemas、Alembic無drift、upstream/license、secret scan、frozen lock與locked dependency audit全通過。尚未連真實cloud IAM/KMS/Object Lock，不宣稱各S3 vendor完整相容。
- P6.7新增digest-pinned/non-root core image、default core/PostgreSQL Compose、explicit migration、strict secret-file DB settings、exact-head health/readiness與Linux CI。真實clean-volume smoke涵蓋migration冪等、least-privilege SCRAM runtime role、deterministic fake、persisted workflow replay、core/DB restart、DB outage、read-only/cap-drop與secret scan。Focused為71 passed、PostgreSQL role/migration為1 passed；完整non-PostgreSQL gate為1888 passed、3 skipped、268 deselected、coverage 87.54%，完整PostgreSQL gate為2156 passed、3 skipped、coverage 87.37%；672 files format、Ruff、strict mypy 346 source files、106 schemas、Alembic無drift、upstream/license、secret與locked dependency audit全通過。Core image目前只提供deployment health/readiness，不宣稱business API composition、常駐dispatcher、public TLS/mTLS、external IdP或跨host orchestration/network policy。
- P6.8新增closed release bundle、canonical SBOM/license、exact Grype DB/OpenVEX、pre-publish unsigned gate與protected-tag keyless signing/attestation workflow。Linux core以source-built `psycopg-c`＋system `libpq`取代bundled binary；OpenBB、Alpine 37 packages/27 origins/244 files與三個Python sdists皆有deterministic corresponding-source closure。Actual unsigned bundle為192 artifacts/136,809,165 bytes，core inventory 97 packages/865 components，0個未抑制High/Critical。Focused為99 passed；完整non-PostgreSQL gate為2004 passed、6 skipped、268 deselected、coverage 87.52%，完整PostgreSQL gate為2272 passed、6 skipped、coverage 87.36%；699 files format、Ruff、strict mypy 346 source files、106 schemas、Alembic無drift、upstream/license、secret、locks與dependency audit全通過。正式OIDC signature/provenance只由protected release workflow產生，本機不宣稱已簽章。
- P6.9 focused resilience/restore為55 passed；完整non-PostgreSQL gate為2059 passed、6 skipped、268 deselected、coverage 87.65%，完整PostgreSQL gate為2327 passed、6 skipped、coverage 87.47%；713 files format、Ruff、strict mypy 348 source files、106 schemas、Alembic無drift、upstream/license、secret、locks與dependency audit全通過。
- P6.10 focused capacity/worker/policy為119 passed，actual PostgreSQL performance為1 passed；root 120-sample report 24,439 bytes，六種workload全通過且敏感字串掃描為0。完整non-PostgreSQL gate為2145 passed、6 skipped、269 deselected、coverage 87.85%，完整PostgreSQL gate為2413 passed、7 skipped、coverage 87.64%；727 files format、Ruff、strict mypy 350 source files與probe scripts、106 schemas、Alembic無drift、upstream/license、secret、actionlint、locks與dependency audit全通過。
- P6.11 formal closure已由CI run `30200612158`、Supply-chain run `30200612154`與release run `30200908948`完成。Unsigned artifact `8631582545`為134,629,231 bytes；signed artifact `8631709866`包含208 files與五份Sigstore evidence，canonical verifier回傳`evidence_count=5`、`status=passed`。正式image、GitHub attestations、immutable release及assets均綁定exact tag/commit並已獨立重驗。

## 下一個代理的起點

1. 先閱讀 `AGENTS.md`、本檔、`tasks/todo.md` P7 與 local GUI runbook。
2. Public repository、外部CI、unsigned supply-chain、bounded optional evidence與formal `v0.1.2` release均已驗證；不得移動或刪除任何protected release tag，也不得弱化required-reviewer、exact identity或五證據closure gate。
3. Research principals只能讀canonical evidence/artifacts，不能取得DB、queue、risk或execution authority。
4. `.research/` 只供閱讀且不進版控；不得 vendor/import Dexter、AI-Trader 或 OpenBB 至 core。
5. 每個 phase 完成後同步精簡 `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md` 與 todo review。
