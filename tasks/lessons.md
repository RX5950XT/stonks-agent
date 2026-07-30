# Lessons

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
