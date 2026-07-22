# Paper kill-switch drill

Kill switch只涵蓋paper-only execution。Global/account scope皆由`paper_operator`或admin
授權，不能由LLM、provider、worker或client自稱角色啟動/解除。

## 啟動與驗證

- 以version CAS執行`stonks paper activate`；global switch必須已有genesis state。
- 同transaction terminalize cancellable pending orders並release reservations；已存在的
  fill/journal/receipt永遠不刪除、不回滾。
- 啟動後任何新execution authorization應fail closed，read-only projection仍可用。

## 停止條件

- Version/action-chain drift、pending order或reservation殘留、仍可建立新order、既有
  fill/journal被修改，或scope/account不一致。

## 復原 gate

1. 驗完整operator action hash chain與current switch version。
2. 對scope內每個account做locked ledger/event/receipt reconcile。
3. 確認root cause已消失且沒有unknown execution/provider state。
4. 以新action ID、exact version執行`stonks paper resume`；失敗時switch保持active。

## 稽核證據

保存actor、scope、version、reason、cancel/release IDs、reconciliation hashes、action chain、
denied execution count與RTO/RPO measurement；不保存bearer token或DB credential。
