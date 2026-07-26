# Stonks Agent 文件中心

本頁是 repository 文件入口。若是第一次使用，先閱讀根目錄
[README](../README.md) 並執行離線 `fake-cycle`；若要核對完成狀態，直接查看
[P6 handoff evidence](verification/p6-handoff-evidence.md)。

## 依目的選擇文件

| 目的 | 起點 |
|---|---|
| 理解系統怎麼整合、誰有交易 authority | [Architecture index](architecture/README.md) |
| 查看 API routes、auth、envelope 與部署限制 | [API index](api/README.md) |
| 操作 core、optional integrations 或處理事故 | [Runbook index](runbooks/README.md) |
| 核對 P6、CI、release 與外部驗證證據 | [P6 handoff evidence](verification/p6-handoff-evidence.md) |
| 查看上游研究、授權與採用決策 | [Research index](research/README.md) |
| 查看 JSON Schema／OpenAPI snapshots | [Wire contracts](../schemas/README.md) |
| 接手目前程式與歷史決策 | [Development handoff](../CONTEXT.md) |

## Architecture

- [Architecture decisions](architecture/README.md)
- [Integration blueprint](architecture/integration-blueprint.md)
- [ADR-0001：Paper authority 與 artifact replay](architecture/adr/0001-paper-authority-and-artifact-replay.md)
- [ADR-0002：Process、dependency 與 license isolation](architecture/adr/0002-process-dependency-license-isolation.md)
- [ADR-0003：Unsigned candidate 與 keyless release trust](architecture/adr/0003-unsigned-and-keyless-release-trust.md)

## API 與 contracts

- [API index](api/README.md)
- [Wire contracts](../schemas/README.md)

API snapshot 的 `info.version` 是 contract snapshot version，目前為 `0.1.0`；它與
產品 release SemVer `v0.1.2` 是不同版本軸，不代表 release metadata 過期。

## Deployment 與 runbooks

- [Runbook index](runbooks/README.md)
- [Core deployment](runbooks/core-deployment.md)
- [Optional integrations](runbooks/optional-integrations.md)
- [Artifact storage](runbooks/artifact-storage.md)
- [Observability](runbooks/observability.md)
- [Supply-chain release](runbooks/supply-chain-release.md)
- [Service OIDC key rotation](runbooks/service-oidc-key-rotation.md)

事故與復原程序：

- [Provider outage](runbooks/provider-outage.md)
- [Worker crash](runbooks/worker-crash.md)
- [Database restore](runbooks/db-restore.md)
- [Ledger mismatch](runbooks/ledger-mismatch.md)
- [Kill switch](runbooks/kill-switch.md)
- [Dead letter](runbooks/dead-letter.md)

## Operations 與 verification

- [SLO、budget 與 alerts](operations/slo.md)
- [Performance 與 resource capacity](operations/capacity.md)
- [P6 handoff evidence](verification/p6-handoff-evidence.md)

Capacity、resilience 與 restore 數字只代表文件所列的 synthetic／single-host
fixtures，不是 production SLA。

## Research、legal 與 security

- [Research index](research/README.md)
- [License policy](legal/license-policy.md)
- [Core CVE review](security/core-cve-review.md)
- [Third-party notices](../THIRD_PARTY_NOTICES.md)

`.research/upstreams/` 只供本機閱讀，不能直接 import、vendor 或提交。OpenBB、
NautilusTrader 與其他 optional upstream 仍受各自授權與資料條款約束。

## 狀態用語

- `implemented`：repository 內有實作與可重跑測試。
- `configured`：manifest 或 workflow 已固定，但不代表外部 runtime 成功。
- `externally_verified`：已有 repository 外的 publication／runtime 證據。
- `blocked`：必要 identity、model 或部署前置條件缺失，系統已 fail closed。
- `unsupported`：目前驗證環境不支援，不能宣稱 runtime compatibility。

所有能力仍維持 paper-only；文件沒有提供或暗示 live trading 開關。
