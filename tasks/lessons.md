# Lessons

## 2026-08-02

- CI hermeticity 不能由本機 clean worktree 推論；gitignored `.data`／`.research` 仍會掩蓋缺少
  prerequisite 的測試。需要 runtime artifact 的 test 必須自行建立 scoped state，只清理由它建立的路徑，
  並另保留 clean checkout 的 fail-closed regression。
- Migration 新增 head 後，所有 frozen runtime revision 都必須由 regression 對照 Alembic single head；
  只測設定檔內容會讓 capacity 等獨立 gate 在執行期才發現 drift。
- Capability route 存在不等於 runtime ready；GUI 必須同時合成 route contract、live service state
  與 model `api_key_configured && verified`，不得把 `configured` 翻成「已驗證」。
- Async deep-link 不能由 loading placeholder event 宣告完成；只允許真實 market success 或 typed
  terminal failure 觸發最後一次 layout 校正。
- UI market label 不得自行只判斷 `.TW`；必須跟 canonical suffix mapping 對齊 `.TW`、`.TWO`、
  `.HK` 與 US fallback，成功／失敗畫面都使用同一 helper。
- Secret field 在 `pagehide` 不只清值，也要恢復 `type=password` 與 reveal control state，避免
  BFCache 返回後讓下一次輸入以明文顯示。

## 2026-08-01

- Quiet refresh 的每個 success／failure／preserve-last-data exit 都必須集中釋放 loading 與
  `aria-busy`；只保留上一筆 quote 不代表狀態機已完成，永久 busy 是獨立 bug。
- Loopback 不等於 same-origin；昂貴 browser GET 仍可被跨站觸發，必須在 provider 前拒絕
  cross-site `Origin`／Fetch Metadata。Secret input 同時要關閉 autocomplete 並在 page exit 清除。
- Direct hash 目標若位於 async capability render 下方，初次 layout 完成後必須重新校正；
  320px viewport 不可再疊 `html min-width: 20rem` 與 scrollbar，固定底部 navigation 要預留 footer
  空間，所有互動 target 至少 44px。
- Dead-code 清理只刪除有靜態與 runtime 證據的 consumer-free chain；外部 API schema 欄位即使
  前端未讀取也不是安全刪除對象，應保留並記錄相容性理由。
- 全域 `:focus-visible` 不可再疊加 composite control 的 `:focus-within` box-shadow；搜尋列、
  secret input 與命令列只能有一個清楚的 focus indicator，避免內層輸入框出現第二圈粗框。
- 使用者否定的是廉價的 dark-fintech 模板感，不代表偏好亮色；theme 偏好與視覺品質是兩個
  維度。這個產品固定採低彩度 graphite dark mode，靠資訊密度、分隔線與單一 cobalt accent
  建立層級，不用金色、霓虹、glow、glass 或卡片海。
- 介面資訊架構必須從實際 backend capability 反推；每個已組合功能要在首屏功能總覽或
  一次點擊內可見，並顯示 truthful ready／blocked／degraded 狀態。沒有 backend route 的
  buy/sell、kill-switch mutation、cancel run 或 Kronos promotion 不得畫假按鈕。
- 使用者明確指出現有 GUI「看起來就是 AI 垃圾」時，不能把 dark fintech、金色 CTA、
  uppercase tracked kicker、rounded card grid、狀態側條換色後再交付；要先改資訊架構與
  component vocabulary，採真正 task-first 的 product workbench，並以實際桌面／窄版畫面
  通過 anti-AI-slop review 才能稱為重新設計。
- 完全重設計仍要保留 provider freshness、typed failure、paper-only、secret 與 deterministic
  authority 邊界；視覺重寫不是刪掉可信度資訊，也不能用新框架擴大 CSP 或供應鏈範圍。

## 2026-07-28

- 內建安全掃描工作區若需要使用者按鈕啟動，不能把互動式 setup 當成唯一執行路徑，
  更不能把工具逾時轉嫁給使用者；應立即改用 repo 原生 CLI、既有 security gates 與
  source-to-sink 稽核自主完成，只有缺少不可推導的產品決策時才停下詢問。
- Typed contracts、worker service 與完整單元測試都存在，不代表功能已可使用；每個外部能力
  都必須有 composition root、durable dispatcher、實際入口與 external runtime gate，
  否則 README／GUI 必須明示「未組合」，不能把「程式寫好了」當成整合完成。
- 研究或 forecast 成功不等於可交易。建立 `PortfolioTarget` 前必須重驗 strategy registry、
  evaluation validity 與 `paper_eligible`；shadow weight 0、draft baseline或 disabled mapper
  都不能為了展示閉環而臨時升級、偽造 signal 或製造 paper fill。
- API、SSE 與命令列都接好仍不等於 GUI 好用；主要任務要有直接操作入口，研究結論、
  evidence、risk 與 paper 狀態依決策順序呈現，命令列只保留為 power-user 路徑。
  窄版必須重新排序主要內容且禁止 boot autofocus，不能只把桌面卡片垂直堆疊。
- 長時間 research UI 必須在第一個 `await` 前鎖定 single-flight，用 run serial 阻止舊
  detail 覆蓋新 run，terminal 後重讀 paper projection；否則即使後端 fenced，
  前端仍可能重複建立工作或顯示跨 run／跨時間點的不一致結果。

## 2026-07-10

- 子代理從 live list 消失或出現 usage limit 時，先檢查已落盤 artifacts 與最後訊息；只續跑缺失的同一子任務，不從頭重做，也不能因子代理中止讓主任務停住。
- 平行研究完成後必須做 cross-report authority scan；「research worker、community adapter、signal、target、order」若混用，會讓 LLM 或外部平台意外跨過 deterministic risk/execution boundary。
- README badge 或一句 license 宣稱不等於完整授權證據；必須檢查實際 license text、package metadata與互相矛盾的子目錄聲明。
- Security、idempotency、account reservation、balanced journal、late-result fencing 必須在第一個 vertical slice 出現，不能當成最後一階段的 polish。
- LLM/Kronos 等 stochastic output 的 reproducibility 要靠封存 artifact 後重播，不可要求 fresh inference bit-identical。

## 2026-07-12

- 使用者提供新的 `AGENTS.md` 指令時，立即以最新版本取代舊規範；phase gate、TDD、paper-only、文件同步與外部 authority 邊界不得沿用已被取代的例外。
- 單元測試通過不代表 durable workflow 已整合；provider reconciliation、lease fencing、terminal transition與license source流程都要有同一條 canonical E2E 或實際 runtime 證據。
- DB-backed lease、deadline、not-before與commit timestamp不能信任caller傳入的`now`；transaction內只取一次DB clock，並以stale/future caller測試證明無法繞過。
- Provider observation contract通過不等於已接成canonical materialization或worker dispatcher；README、todo與handoff必須分開宣稱，default fallback的所有adapter也要接受同一份canonical query shape。

## 2026-07-16

- 使用者再次提供完整 `AGENTS.md` replacement 時，必須立即以新規範為準，更新執行計畫並把修正模式落到 lessons；複雜工作要平行委派單一職責子代理，但主任務不能等待失效子代理而停住。
- Service ingress 不能只驗證共用 bearer secret；必須先驗證短效 asymmetric OIDC service identity，再依解析後 canonical job ID 做 exact-target authorization，health/legal source routes才可明示匿名。
- Secret provider failure要保留`CONFIGURATION_INVALID`與`DATA_UNAVAILABLE`的語意差異；不可把rotation backend outage誤報為靜態設定錯誤，其他未知錯誤才轉generic internal failure。
- Security regression需要測PEM/token形狀時，不可把完整credential literal直接寫進source而繞過scanner；應在測試執行期組合fixture，讓runtime redaction與repository secret scan同時維持fail closed。

## 2026-07-17

- `.gitignore`若用negation重新納入被忽略目錄，必須在該目錄層級再次忽略`__pycache__/`與`*.pyc`；commit前同時檢查staged file list，避免生成物被誤納。
- Request body只限制bytes仍可被無限零長ASGI frames拖住；byte cap與frame cap必須一起做，且昂貴auth必須放在body前的credential/direct-peer admission之後。
- 應用程式不採信forwarded header不代表direct peer可靠；在trusted proxy拓樸與header清洗契約完成前，必須直接拒絕forwarded identity並避免宣稱multi-replica ingress enforcement。
- Observability callback必須視為無authority的旁路：即使它不呼叫、偽造回傳、吞掉exception或重複呼叫，canonical動作仍只能執行一次且保留原result/exception。
- Async telemetry latency必須包住完整`await`，且不得把`CancelledError`當一般export failure後重新dispatch；root sampling、flush與shutdown也要有實際訊號測試。
- OTel SDK constructor仍可能隱式讀`OTEL_*`、proxy、`.netrc`、redirect與resource env；exact config要逐項覆寫或拒絕，不能只傳endpoint就宣稱設定已固定。
- Docker internal network在Docker Desktop可能讓宣告的host port實際不建立binding；observability smoke必須從host送真實OTLP並讀回metric，不能只相信container health或Compose render。
- Budget usage ratio必須以各scope的soft/degraded threshold正規化，ratio大於1才直接代表degraded；hard threshold可能不是固定倍數，應由typed decision與`outcome=failed`告警表達。
- Zero-tolerance violation counter必須先建立四個固定series，否則`absent_over_time`會把健康但未發生事件誤判為缺失；zero初始化只證明metric liveness，不能取代canonical validation、DB constraint與immutable audit。
- Paper cycle的budget exhaustion必須是非重試terminal transition；只在應用層停止當次call仍可能被queue retry成追單，需以真實PostgreSQL狀態測試證明不會重新排程。

## 2026-07-18

- Strict environment allowlist要包含同一entrypoint實際消費的固定鍵；只做unit loader測試可能出現「runtime需要、loader卻拒絕」的自我矛盾，必須用真實Compose migration抓整合漂移。
- Readiness的client timeout必須大於內部DB connect timeout；否則DB outage雖會正確產生503，外層probe卻只看見timeout，造成不必要的unhealthy與無法辨識故障。
- PostgreSQL `CREATE/ALTER ROLE ... PASSWORD`不接受bind parameter；不可退回raw password literal。應先用libpq產生SCRAM verifier，再只把verifier寫入DDL，並以真實PostgreSQL登入測試驗證。

## 2026-07-26

- 使用者明示 `AGENTS.md` replacement 時，先逐條比對目前落盤版本並立即同步 `AGENTS.md`／`CLAUDE.md`；即使內容已一致，也要把新增或重申的不變量納入完成稽核，不能沿用較早的對話版本。
- 使用者追問「事情做完了嗎」時，必須明確區分repository公開、GHCR image存在與formal release closure；只要signature、attestation、immutable release或獨立驗證仍缺一項，就只能回報未完成並持續收尾。
- Cosign major-version行為不能由舊CLI記憶推定：v3 image bundle是DSSE attestation，saved bundle要用`verify-blob-attestation`綁exact digest/predicate驗證，並確認同一bundle確實附加至registry後再用`verify`重驗。
- 使用者要求「真實功能不是玩具」時，contract、mock、fixture 或 configured manifest 不能當成功證據；能力宣稱必須綁定 actual external runtime，並明列資料時效、fallback、持久化與 composition 缺口。

## 2026-07-27

- Provider 能力必須逐端點實測後才可規劃：OpenBB sidecar 容器內有 26 條路由，但 Yahoo 的
  `quote`／`profile`／`fundamental`／`discovery` 全數 401，因為 yfinance 取 cookie 的
  `fc.yahoo.com` 已無法解析（`1.5.1` 與 `1.5.2` 皆同）。升級套件前先讀上游原始碼確認
  修復點，否則會白做一輪版本升級與 lock/SBOM 重建。
- 「安全」政策若擋住產品本身的目的（零 JavaScript 讓終端只能是表單），要改的是政策的
  表述而不是產品：把「沒有 script」換成可驗證的「只允許同源 script、CSP 全 `'self'`、
  禁 inline/eval/`data:`/字串產生 markup」，安全性沒有下降而能力解鎖。
- 外部時間戳沒有時區時不能直接當 UTC：OpenBB 的日內 bar 是 naive 交易所本地時間，
  必須先對照上游 epoch 驗證換算（15:30 對應 19:30Z）再綁 `America/New_York`，
  並用交易所本地日期而非 UTC 日期做請求範圍檢查。
- 部分推導欄位要嘛全有要嘛全無：previous close 為 0 時只給絕對變化而略過百分比，
  會讓畫面看起來像一筆完整比較。無法完整推導就三個欄位一起省略。
- GUI server 在啟動時把 asset 讀進記憶體，改 CSS/JS 後必須重啟才會生效；瀏覽器端
  `ignoreCache` 重新載入不會讓舊 process 吐出新檔案，否則會誤判成版面 bug。
- 用 hash-only 的網址導覽不會重新載入文件，頁面仍綁在舊 server 的回應上；驗證新
  composition 必須真的 reload 或開新分頁。
- Evidence allowlist只代表可存取範圍，不代表模型真的看過內容；claim citation必須綁
  本輪tool實際materialize的IDs，inventory metadata不能冒充evidence reading。
- Tool contract宣告`timeout_ms`仍不等於已限制執行；執行器必須用monotonic deadline
  強制中止canonical等待，逾時thread結果不得再被採納。
- Worker處理lease要以最長handler/model budget設定安全下限，public payload與repr不得
  暴露attempt nonce；commit conflict要保存secret-free append-only quarantine evidence。
- Worker/sidecar不能只做body byte cap；JWT前需要bounded peer/credential admission，
  forwarded headers fail closed，body另需frame與total deadline cap，Uvicorn必須關閉
  proxy-header採信。
- Dependency gate必須audit每份isolated runtime lock；`uv lock --check`只證明lock新鮮，
  local-build版本還要用public package identity查advisory，不能只掃core lock。
- Docker Desktop的internal network可能保留HostConfig卻不建立實際port publish；local
  verifier可附加唯一且停用IP masquerade的短命bridge，完成後刪除，不能弱化production
  internal network來讓smoke通過。

## 2026-07-29

- 專業產品不能只把最新接線功能放上一張卡片；每輪 GUI 工作都要先做
  backend capability → visible journey matrix，區分「已 composition 可安全操作」、
  「只能 read-only 投影」、「需要高權限不能放進 local GUI」與「只有 contract 尚未可用」。
  已有後端資料若缺 history、evidence drill-down、risk/integrity、loading/empty/error
  與可見 navigation，測試全綠仍不算 product closure。
- 可獨立通過 actual runtime 的模型不等於已接進產品主流程；使用文件與 GUI 必須分開標示
  「worker 可驗證」與「本次 run 有 snapshot-bound artifact」，不能用固定摘要讓使用者
  誤以為 Kronos 已參與研究或 paper 決策。
- 本機產品若需要一長串 CLI 才能啟動，根目錄應提供受測的薄 launcher，精確轉交 canonical
  entrypoint；secret 只從 process environment 讀取，preflight 不得輸出值或產生副作用。
- 清理 dirty worktree 前要先把內容分成 source changes、可重建 cache、昂貴 runtime env、
  模型／資料庫狀態與研究證據；禁止用廣域 `git clean`／Docker prune。先驗證 exact path
  位於 workspace，再只刪明確可重建項，才能避免把未提交實作或 external evidence 當垃圾。
- 使用者要求「Kronos 接入 GUI／直接按 start」時，不能只把 worker readiness 或獨立
  verifier 包進 launcher；done definition 必須包含同一次 snapshot-bound run 的 durable
  forecast artifact、typed GUI projection，以及 launcher-owned start／ready／cleanup。
- Windows 根目錄 launcher 若要同時支援 Windows PowerShell 5.1 與 PowerShell 7，無 BOM
  的 `.ps1` 必須維持 ASCII；否則 5.1 會以 ANSI 誤解 UTF-8 中文字串，甚至吞掉後續
  function definition。Regression 必須實際用兩個 host 執行 `-Check`，不能只測 `pwsh`。
- 把 API key 搬進 GUI 不能只加 password input：route 必須使用 pinned transport 防
  private／metadata SSRF與DNS rebinding，provider exact key echo要在parse與artifact
  archive前拒絕；secret不得回傳、持久化或留在browser storage。
- Capability composed 不等於模型可用；新的研究必須同時要求 runtime ready 與本次
  session structured completion verified，前端 disabled 之外，POST 邊界也要 fail closed。
  Durable history 則應維持可讀，避免設定問題阻斷既有研究。
- Single-flight gate 不能在 queue submit 成功後立即釋放；必須記住 active run ID，
  讀到 terminal state 才開放下一筆，讀取失敗時維持 fail closed。
- 「整合所有免費資料」不是遍歷 endpoint：免費額度不代表允許 automated access、
  display、storage 或 redistribution。先建立 curated legal/runtime catalog，只有完成
  credential、entitlement、PIT、rate limit 與 actual smoke 的來源才能 active。
- 行情 freshness 不能由 Browser 用固定秒數猜測；backend 必須依 exchange session、
  interval、event time 與 verified calendar輸出 typed state。Cache hit 要重算 served
  age，loading 要先隱藏舊 symbol，近即時 historical bar 仍須明示 `非 tick`。
- SQLAlchemy `str(engine.url)` 會把密碼渲染為 `***`；需要把同一測試連線交給 CLI 時
  必須使用 `render_as_string(hide_password=False)`，且不得把結果寫入 assertion、
  log 或錯誤訊息。
- GUI paper 預設 port `55433` 可能被本機 Codex／ChatGPT 程序占用；先分開檢查 host
  port owner 與 Docker binding，再改用 `55434`，不要為了啟動刪除 paper volume。

## 2026-08-28 GUI 簡化與圖表互動

- 模型表單只保留使用者必要輸入，預設值由 backend contract 提供；不要在前端複製 budget 欄位。
- 自然語言先轉成既有 allowlist command；沒有 typed planner、permission、audit 就不把 LLM 接到命令執行。
- Canvas 要能左右移動，必須搭配可視資料視窗、pointer capture、touch/wheel 與鍵盤 fallback；只加 overflow CSS 不夠。

## 2026-08-29 Docker 精準清理

- Docker 顯得很肥時先分開看 containers、images、volumes、networks、build cache；本次主要占用是可重建的 14.71 GB build cache，不是資料庫。
- 只刪已退出 container、空 network、未被專案引用的 image 與指定 builder cache；保留 active services、pinned runtime／稽核 images、模型與 PostgreSQL volume，避免使用 system-wide prune。

## 2026-08-30 GUI 單欄排版

- 16:9 dashboard 若外層同時使用兩組雙欄 grid，會形成難讀的 2×2；主面板改單欄，只有
  表單與資料欄位等局部內容保留 grid。
- figure 若把固定高度給同時包含 canvas 與說明文字，說明會疊到圖表；讓 canvas 使用獨立
  stage，輔助文字放在 stage 外並單獨量測。

## 2026-08-30 行情來源與研究聊天室

- 同一份 runtime readiness 之外再放一組頂端工作狀態會造成重複；操作入口應和研究上下文
  放在一起，讓中文命令與 `RESEARCH` 共享既有 event／allowlist。
- Provider fallback 只能接已建立 typed adapter、固定順序、timeout／quota／failure mapping
  的真實來源；付費來源未有 key 或未完成 actual runtime 時保持未啟用，不能用 replay／fixture
  補成成功。
- OHLCV provider DTO 與 GUI canonical contract 的 Decimal 欄位要在 adapter 邊界明確轉型，
  尤其 `volume` 不能把外部整數直接送進 strict Decimal contract。
- 聊天室已和研究區合併；提示文字只留必要操作與安全限制，避免同一件事重複說明。
- 預覽必須清楚區分 fake UI fixture 與 actual OpenBB runtime；不能用兩根測試 bar 冒充完整行情。
- yfinance 可能在有效歷史回應混入單筆 non-finite OHLCV；adapter 應保留有效 bar、記錄 typed warning，全部無效才 fail closed。

## 2026-08-30 K 線週期與拖曳

- K 線聚合週期與 lookback 範圍是兩個不同選擇；UI 範圍必須依 provider 與 canonical bar 上限動態收斂，不能只新增按鈕。
- OpenBB 新週期要同步更新 core `BarInterval`、adapter literal、sidecar surface allowlist 與 provider manifest；只改 core 會在實際 sidecar 400 時才暴露。
- Canvas 拖曳要限制 primary pointer／左鍵、使用 pointer capture、清楚 clamp 起點，沒有隱藏資料時不顯示可拖游標；發佈前需用真實資料拖到較早日期確認畫面真的改變。

## 2026-08-30 標的儀錶板

- 少量週線／月線不能把 bar 中心硬拉到整個畫布寬度；有限 slot 置中可保留時間順序，也不會讓 K 線看起來散掉。
- 隱藏資料品質欄位只代表 UI 收斂，不能刪掉後端 provenance、freshness 與 fail-closed 判定；來源狀態由系統知道即可。
- AI 研究對話不能取代結構化基本面；沒有通過實際 provider、權利與時效驗證前，財報／估值應顯示缺口，不做假資料或假入口。

## 2026-08-30 年線與長時間範圍

- provider 沒有實測 `1Y` 時，年線應用已驗證的月線在 core 聚合，不能只把 UI 選項送給上游。
- YTD、5 年、10 年與全部必須同時落到 URL、API 邊界與實際 provider 日期；只加前端按鈕會造成空白圖。
- 全部日線需要同步調整 bounded response bytes 與 bar 數，並以真實歷史資料確認，不得用無限上限換功能。

## 2026-08-30 免費資料源與 Agent

- SEC accession number 是 `0000320193-26-000001` 這種 20 字元格式；只檢查 18 字元會讓申報紀錄靜默消失。
- TWSE 季別字串清理後要 `.strip()`；模型欄位的安全文字驗證會拒絕尾端空白，不能只在畫面層修。
- OpenBB upstream 列出 provider 不等於本機已安裝、端點已驗證或資料可顯示；每個來源都要分開記錄 runtime、PIT、rate limit 與權利。
- Agent 要積極取資料時，先把官方回應封存成同一份 snapshot，再給 audited read-only specialized tools；不能為了「多抓」開任意網路或讓 LLM 直接碰 order plane。

## 2026-08-31 標的儀錶板歷史資料

- Dashboard 內部資訊不能再堆成單欄長頁；桌面用少量局部欄位分組，申報與歷史表跨欄，窄版才收斂。
- SEC company facts 同一指標會混有季度、累計與年度期間；顯示期間與 `published_at`，保存 bounded history，不把它們假裝成同一種季度序列。
- TWSE 公開 OpenAPI 的財報端點目前只提供最新彙總列；沒有歷史列時要如實顯示 1 筆，不可改抓被封鎖的 MOPS HTML 來冒充可靠來源。
- 同日 TWSE 民國日期要以台灣日曆日期和 `as_of` 比較，不能直接把日期當 UTC 午夜而誤判成未來資料。

## 2026-08-31 自選股與側欄導航

- 移除 UI 上限要追到 frontend、API contract、OpenAPI、docs 與 tests，不能只刪一個常數。
- 沒有固定檔數不等於無界輸入；要保留 query length、去重、rate limit 與 concurrency 邊界。
- 刪除導航後要一併清掉 active state、hash/deep-link listener 與 CSS，避免留下孤兒程式碼。

## 2026-08-31 全專案死碼掃描

- Vulture 對 `locals()`、Protocol 參數與 FastAPI／Typer decorator 會報誤判；刪除前必須做全 repo caller 反查。
- 舊型別只有測試引用時，仍要確認沒有 public export 或安全邊界契約；確認被現行契約取代後才刪除，provenance 等仍被使用的模組要保留。

## 2026-08-31 README 與 PR 整合

- 根 README 只保留目前能重跑、能實測的入口；歷次驗證數字與工作紀錄放交接／證據文件，避免 README 過期。
- Dependabot PR 要先看 required checks 與變更範圍；全綠才合併，舊且失敗的 PR 不用為了清單好看而硬併。
- 多個 Dependabot PR 要逐個以最新 `main` rebase，合併時重新讀取完整 `headRefOid`；不能拿舊的短 SHA 或舊基底重試。
- 舊 Dependabot PR 的跨平台、Docker、sidecar 或 release check 失敗，先以最新 `main` rebase 再重跑；本次 PR #7 的四個失敗在 rebase 後全數消失，不能直接把過期 merge base 當成程式根因。
- 合併依賴 PR 後要立刻跑 `uv lock --check`；frozen lock 真正更新時，須修正新版工具報出的型別錯誤並同步所有由來源 hash 綁定的 runtime identity 與 fixture。
- 官方來源 archive 的單次網路逾時不能直接當成資料不存在；在固定次數重試後仍失敗才 fail closed，並保留完整錯誤邊界。
