# Runbook index

Runbook 是 operator 停止條件與復原程序，不會授予新 authority，也不會把 synthetic
drill 或 configured workflow 升格為 production SLA／external verification。

## Default deployment 與共通控制

- [Core deployment](core-deployment.md)
- [Artifact storage](artifact-storage.md)
- [Observability](observability.md)
- [Service OIDC key rotation](service-oidc-key-rotation.md)
- [Supply-chain release](supply-chain-release.md)
- [Optional integrations](optional-integrations.md)

跨服務的 budget、SLO、alerts 與 bounded capacity 證據見
[SLO operations](../operations/slo.md) 與
[capacity operations](../operations/capacity.md)。

## Incident 與 resilience drills

- [Provider outage](provider-outage.md)
- [Worker crash](worker-crash.md)
- [Database restore](db-restore.md)
- [Ledger mismatch](ledger-mismatch.md)
- [Kill switch](kill-switch.md)
- [Dead letter](dead-letter.md)

事件處理必須保留 immutable evidence、structured error、operator identity、時間與
correlation；unknown/partial/forbidden side effect、缺 evidence 或 unsafe recovery 不得
算通過。Restore 不自動 promote，dead-letter 不自動追單，ledger drift 先 rollback 再
啟動 kill switch。Phase-to-evidence 對照見 [P6 handoff evidence](../verification/p6-handoff-evidence.md)。
