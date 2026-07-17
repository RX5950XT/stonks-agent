# SLO、預算與告警操作

本文件定義`stonks-slo/1`的paper-only操作契約。Canonical設定在
`config/slo.yaml`，實際cost與latency門檻在`config/budgets.yaml`。SLO與
telemetry都是observer與fail-closed gate，沒有建立target、reservation、
order或改寫既有commit的authority。

## Correctness SLO

四項correctness invariant使用30天視窗、零error budget。任何一次已觀測違規
都立即進入`failed`並要求critical page：

| Invariant | 目標 | 指標 |
| --- | --- | --- |
| zero duplicate paper order | 30天違規事件為0 | `stonks_correctness_violations_total{invariant="duplicate_paper_order"}` |
| zero future evidence | 30天違規事件為0 | `stonks_correctness_violations_total{invariant="future_evidence"}` |
| 100% claim provenance | 每個canonical claim都有完整evidence refs，違規為0 | `stonks_correctness_violations_total{invariant="claim_provenance"}` |
| 100% replayable risk decision | 每個risk decision都有可重播的input/policy/result binding，違規為0 | `stonks_correctness_violations_total{invariant="risk_replayability"}` |

`violations_total`只用來即時偵測已觀測違規；單獨看到counter為0，不代表能證明
producer與Prometheus完整無缺。Canonical validation、DB constraint、immutable
audit chain與phase-gate tests仍是correctness證據。設定將missing data視為
`breach`；資料未知時不可把SLO標成pass。

### duplicate-paper-order

立即停止新的paper target、reservation與order。保留原始order intent、
idempotency key、execution receipt、fill與journal，不重送、不放大quantity，
也不以新order補償。確認帳戶mutation serialization與reservation fence後，
完成reconciliation；未釐清前維持`failed`。

### future-evidence

立即停止受影響的research與paper cycle，封存`as_of`、`available_at`、
snapshot及evidence binding。隔離受影響artifact，不可重寫舊artifact掩蓋違規；
以修正後的新run重新產生研究，未通過PIT audit前維持`failed`。

### claim-provenance

隔離缺少或不一致evidence refs的report/artifact，停止其後續signal與paper
流程。以immutable generation artifact與canonical evidence重建claim鏈；不得
補寫未經驗證的citation。完成全量claim audit後才可恢復。

### risk-replayability

停止受影響帳戶的新order，保存target、risk input、policy identity、decision與
hash binding。從immutable artifact重播並比對；任何缺欄位、hash drift或policy
identity不一致都維持`failed`，不能用fresh stochastic inference取代重播證據。

## Availability、latency 與 cost

所有比例都以30天rolling window評估；沒有資料時不得宣稱達標。

- API availability：`api/http_request`成功比例至少99%，error budget為1%。
- Paper-cycle availability：`worker/process`成功比例至少99%，error budget為1%。
- API latency：`api/http_request`的p95最多2秒，允許5% request超過門檻。
- Worker latency：`worker/process`的p95最多30秒，允許5% operation超過門檻。
- Research與paper-cycle latency：各自的budget usage ratio p95最多1，允許5%
  operation超過soft門檻。
- Research與paper-cycle cost：各自的budget usage ratio p95最多1，允許5%
  operation超過soft門檻。金額以`Decimal`計算，不從binary float推導。

`stonks_budget_usage_ratio{budget,scope,environment}`提供低基數histogram；
`stonks_budget_outcomes_total{budget,scope,outcome,environment}`只接受
`budget=cost|latency`、`scope=research|paper_cycle`及
`outcome=within|degraded|failed`。實際門檻由versioned budget policy決定，
usage ratio固定以各scope的degraded（soft）threshold為分母；ratio大於1即為
`degraded`，`failed`則由evaluator依該scope獨立hard threshold決定，不假設固定
hard/soft倍數。SLO不複製美元或秒數，以免兩份設定漂移。

### api-availability

先確認process health、rate limit、database與下游依賴。API不能安全處理時回傳
structured failure；不得以略過auth、PIT、risk或ledger gate換取availability。

### paper-cycle-availability

先確認worker lease、queue backlog、database與provider是否可用。若工作已取得
canonical結果，允許該observed commit完成；禁止啟動新target/reservation/order。
不得用縮短風控、略過資料或重送order來改善表面availability。

### api-request-latency

檢查rate limit、database、provider與下游timeout。超限後停止接受新的高成本
research/paper work；read-only查詢可維持fail-closed structured error。不得
延長order deadline後追單。

### worker-process-latency

檢查lease、queue、database與provider/model deadline。在目前canonical boundary
停止高成本工作；已過期lease或舊generation/nonce結果不得commit，也不得以新
order追補逾期cycle。

### research-latency-budget

保存已完成的immutable research artifact；停止新的model/tool工作與後續target。
若只是soft門檻，狀態為`degraded`；hard門檻或usage缺失/無效則為`failed`。

### paper-cycle-latency-budget

在目前canonical boundary停止；已觀測commit不可被telemetry改寫。未建立的
target、reservation與order一律不補建，不追單。

### research-cost-budget

停止新的LLM/model/tool支出，保留usage與artifact證據。不得改用未核准模型、
隱藏usage或拆分request規避門檻。

### paper-cycle-cost-budget

停止新的paper cycle成本與order建立。任何補償、retry或recovery都不得增加
quantity或追逐已移動價格。

## Error-budget burn policy

Correctness invariant沒有burn容忍：單次違規立即critical、`failed`。
Research/paper-cycle的cost與latency budget SLO使用以下固定政策：

| Policy | Window | Burn rate | Hold | 結果 |
| --- | ---: | ---: | ---: | --- |
| fast | 5分鐘 | 14.4 | 2分鐘 | critical、`failed` |
| slow | 1小時 | 6 | 15分鐘 | warning、`degraded` |
| exhausted | 30天 | 1 | 0 | critical、`failed` |

狀態只可由`within`升為`degraded`或`failed`，不可因下一筆較快或較便宜而自動
降級。Soft cost/latency門檻轉`degraded`，hard門檻、missing usage或invalid
usage轉`failed`。兩種狀態都禁止新target、reservation與order，且一律不追單；
已觀測到的canonical commit可以完成並保留稽核證據。

API/worker availability與absolute latency另以5分鐘recording window、5分鐘
hold發warning；30天error budget用於SLO報告，目前尚未接多視窗normalized burn
alert。不得把這項尚未完成的告警能力宣稱為已上線。

## Operator 處置

Alert labels只允許固定的`severity/route`與metric catalog labels，不得加入
account、order、symbol、user、URL、prompt或exception text。

1. critical走`critical_paper_operator`，warning走
   `warning_paper_operator`；兩者的receiver都是paper operator。
2. 先確認correctness gate或budget outcome，再停止新的高風險工作；不要先清除
   counter、重啟到遺失證據，或用retry追單。
3. 保存trace/correlation、run/job/artifact及ledger refs；通知內容只放bounded
   opaque reference，不放secret或raw identity。
4. 依上方對應anchor完成reconciliation與證據稽核；只有canonical gate重新通過
   才能由另一個operator流程恢復。

Routing目前是`policy_only`：設定表達「應該page」與receiver，但
`configured=false`，沒有delivery guarantee。部署者不能把本機Grafana可見
誤寫成page成功。

## 目前限制

- Observability stack是單機拓撲；Prometheus與Grafana使用非持久tmpfs，重啟後
  會失去SLO window與alert state。
- 尚未接上 paging backend，也沒有Alertmanager、on-call provider或通知送達
  驗證；目前只能在本機Prometheus/Grafana檢視。
- Trace送到nop sink，沒有持久trace storage；synthetic parent限制仍沿用
  observability runbook所述現況。
- Prometheus本身失效時無法評估`missing_data: breach`或送出告警，因此這套設定
  不是high-availability monitoring，也不能宣稱production paging已完成。
- SLO metric只能補強canonical invariant，不能取代PIT、idempotency、risk replay、
  ledger及audit-chain的domain/DB驗證。
