# Ledger mismatch drill

本 runbook 處理paper-only fill、journal、settled projection或account event drift。Ledger
reconcile是唯一真相檢查，read model與report不能覆寫它。

## 觸發與隔離

- 在account lock內重播fill→balanced journal→cash/position/head；任何mismatch先rollback。
- Rollback後以獨立transaction啟動global kill switch，終止pending orders並釋放仍open
  reservations；既有fill/journal保持append-only。
- 使用受權principal執行`stonks paper reconcile`，保存structured mismatch reasons。

## 停止條件

- Commodity不平、sequence/hash gap、orphan fill/journal、projection drift、duplicate execution、
  reconciliation本身失敗，或global kill switch未能啟動。

## 復原 gate

1. Kill switch維持active，先定位第一個bad sequence與完整上游evidence。
2. 修復只能走approved migration/compensating journal，不得UPDATE/DELETE歷史row。
3. 所有accounts完整reconcile、event/operator action chain與projection hash一致。
4. `paper resume`另行執行locked reconciliation；任何drift維持switch active。

## 稽核證據

保存rollback、reconcile report/hash、mismatch reasons、kill-switch action chain、cancel/release
IDs、existing fill/journal counts與resume拒絕/核准結果；不得輸出position owner secret。
