# Lessons

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
