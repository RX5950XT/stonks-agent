# Provider outage drill

本 runbook 只處理 paper-only research/data/provider 故障；provider、LLM、model、
sidecar 永遠無權直接建立 target/order 或覆寫 risk。

## 觸發與隔離

- 對 timeout、quota、auth、invalid schema、stale/conflict 與 legitimate empty 保留不同
  structured state；infra failure 不得轉成空資料或成功。
- 停止受影響 capability 的新工作，保留已封存 artifact、job fence、provider status、
  trace 與 budget evidence。未核准 fallback 回 `DATA_UNAVAILABLE`。
- 只有 policy allowlist、PIT/freshness/quality 同時通過的 fallback 可降級繼續；任何
  degraded report都必須揭露來源與限制，且不得建立 target/order。

## 停止條件

- 出現future/unknown evidence、scope/identity drift、secret/authz anomaly、重複副作用，
  或任何 provider output 嘗試表達 quantity/order/execution。
- Outage 已越過 job deadline/budget，或 fallback 與primary衝突。

## 復原 gate

1. Exact endpoint/service identity與credential rotation通過；禁止沿用stale token/result。
2. 以同一canonical request做read-only probe，驗schema、PIT、freshness與content hash。
3. 新job使用新generation/nonce；舊result只能quarantine。
4. 解除degraded routing前確認0 reservation/order side effect。

## 稽核證據

保存provider kind/status、request/result artifact hashes、job generation/nonce、trace、
budget decision、fallback decision、terminal state及target/order count=0；不保存secret、
raw prompt或未去敏遠端內容。
