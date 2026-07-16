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
