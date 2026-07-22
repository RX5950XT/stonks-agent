# PostgreSQL backup and restore drill

本 runbook 驗證paper-only canonical DB的可恢復性；restore target永遠是fresh isolated
database，未驗證前不得取代active source。

## 演練流程

```powershell
uv run python scripts/drill_postgres_restore.py --output resilience-report.json
```

Drill使用digest-pinned PostgreSQL、bounded `pg_dump` custom archive與`pg_restore`，量測
RTO及RPO evidence，然後重驗single Alembic head、canonical seed rows、row/content hashes、
event/operator chains、replay與append-only constraints。量測值不是production SLA。

## 停止條件

- Source/target identity混用、dump超過上限、command timeout、Alembic drift、row/hash-chain/
  replay mismatch、append-only mutation成功或任何credential出現在argv/log/report。
- Restore驗證只完成部分、回傳unknown、或來源仍在寫入而無一致性邊界。

## 復原 gate

1. 保留source為read-only/隔離狀態，建立新的target與one-shot migration authority。
2. 以exact backup hash執行pg_restore；禁止覆寫既有非空target。
3. 完成所有semantic、authority、hash-chain、replay與schema checks。
4. 驗證失敗即丟棄target；通過仍須人工change control才可切換，不由drill自動promote。

## 稽核證據

保存image digest、backup SHA-256/bytes、source/target opaque IDs、Alembic head、row/hash/
replay counts、RTO/RPO measurements、每項check與cleanup結果；不得包含DSN/password。
