# Architecture index

本目錄只記錄目前 canonical architecture 與已接受的跨領域決策。狀態詞固定如下：

- `implemented`：repository 內有實作與自動測試證據。
- `configured`：manifest／workflow 已固定路徑，但不等於外部環境執行成功。
- `externally_verified`：由 repository 外真實服務或 publication 所產生且可核驗的證據。

目前狀態為 implemented：P0-P6.11 的 repository gates；configured: public repository
的SemVer tag protection、required-reviewer environment與immutable releases；
externally_verified: GitHub Actions CI、unsigned supply-chain candidate、bounded optional
integration matrix及formal `v0.1.2` keyless release。Release run `30200908948`的六個
jobs與五證據closure成功，GHCR digest
`sha256:9c61a2d5dd59d07d30318b483a7a205ac8af394236662b45021574e42ff19976`
已由registry與GitHub attestations獨立重驗；`v0.1.0`／`v0.1.1`只保留為immutable
fail-closed證據。真實外部IdP/cloud secret manager、remote telemetry backend與跨主機
網路控制仍沒有外部成功證據。

## 文件

- [整合架構藍圖](integration-blueprint.md)：canonical flow、process boundary、adoption
  matrix 與狀態邊界。
- [ADR-0001：Paper authority 與 artifact replay](adr/0001-paper-authority-and-artifact-replay.md)
- [ADR-0002：Process、dependency 與 license isolation](adr/0002-process-dependency-license-isolation.md)
- [ADR-0003：Unsigned candidate 與 keyless release trust](adr/0003-unsigned-and-keyless-release-trust.md)

操作面由 [API index](../api/README.md)、[runbook index](../runbooks/README.md) 與
[P6 handoff evidence](../verification/p6-handoff-evidence.md) 接續；三者不擴張本目錄的
authority 或 external verification 宣稱。
