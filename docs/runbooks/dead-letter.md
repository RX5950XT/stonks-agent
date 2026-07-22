# Dead-letter drill

本 runbook適用paper-only durable jobs。`dead_letter`是明確terminal state，不是隱藏的
retry queue；operator不得用手動重排製造追單。

## 觸發與隔離

- DB clock判定deadline越界或attempt上限耗盡時，原子寫job state、run event、outbox與
  audit graph；budget exhaustion與canonical validation failure屬non-retry。
- Lease expiry可增加attempt/generation；stale nonce/result只能quarantine。
- 若execution receipt已commit，dead-letter/recovery只能replay，不能再次dispatch order。

## 停止條件

- Partial dead-letter graph、event/outbox hash不一致、attempt倒退、duplicate notification、
  unknown error分類、任何自動追單或fill/journal重複。

## 復原 gate

1. 驗證job/run/event/outbox/audit closed graph與terminal reason。
2. 先查canonical receipt、artifact與side effects；unknown就維持dead_letter。
3. 需要重跑時建立新job/new idempotency scope並重新通過PIT、budget、risk與authorization；
   不修改舊job或重用舊nonce。
4. 新job完成後仍保留舊dead-letter與兩者關聯audit。

## 稽核證據

保存job/run IDs、attempt/generation、deadline reason、event/outbox hashes、quarantine、
receipt/fill/journal counts、operator decision與recovery elapsed time；error message須redact。
