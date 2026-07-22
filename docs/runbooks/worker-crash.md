# Worker crash and lease-expiry drill

本 runbook 適用paper-only core runner與research workers；remote worker沒有DB、queue、
risk、ledger或execution authority。

## 觸發與隔離

- Worker消失、timeout或connection reset時不自行猜測結果；等待DB-authoritative lease
  expiry，保留attempt generation、nonce、owner、deadline與checkpoint。
- Reclaim必須產生更高generation與新nonce。舊worker回傳一律quarantine，不能commit。
- Receipt已commit但checkpoint前crash時，重領只能replay既有receipt，不得重送order。

## 停止條件

- Lease graph、checkpoint hash、result artifact、generation/nonce或account sequence不一致。
- 出現duplicate fill/journal/receipt、deadline已過、attempts耗盡或unknown execution state。

## 復原 gate

1. 驗證DB clock、current lease、run/event/outbox hash chain與last checkpoint。
2. 重領後先查canonical receipt；存在時只做replay/reconciliation。
3. 無receipt才可從最後完成stage重試，且仍受原deadline/budget與paper risk限制。
4. 完成後比較fill/journal/receipt count與idempotency hash。

## 稽核證據

保存lease/reclaim/quarantine audit、generation/nonce、checkpoint/result hashes、attempts、
terminal state、duplicate side-effect count=0及recovery elapsed time；不記錄lease secret。
