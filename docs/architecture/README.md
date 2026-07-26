# Architecture index

本目錄只記錄目前 canonical architecture 與已接受的跨領域決策。狀態詞固定如下：

- `implemented`：repository 內有實作與自動測試證據。
- `configured`：manifest／workflow 已固定路徑，但不等於外部環境執行成功。
- `externally_verified`：由 repository 外真實服務或 publication 所產生且可核驗的證據。

目前狀態為 implemented：P0-P6.11 的 repository gates；configured: public repository
的SemVer tag protection、required-reviewer environment、immutable release與正式
keyless路徑；externally_verified: GitHub Actions CI、unsigned supply-chain candidate
與bounded optional integration matrix。`v0.1.0`與`v0.1.1`已驗證protected-tag
build/scan與GHCR exact image publication；`v0.1.1`另有image及GitHub attestations，
但五證據final closure仍fail closed，沒有GitHub Release。正式`v0.1.2`完成前不擴張宣稱。真實外部IdP/cloud secret manager、
remote telemetry backend與跨主機網路控制仍沒有外部成功證據。

## 文件

- [整合架構藍圖](integration-blueprint.md)：canonical flow、process boundary、adoption
  matrix 與狀態邊界。
- [ADR-0001：Paper authority 與 artifact replay](adr/0001-paper-authority-and-artifact-replay.md)
- [ADR-0002：Process、dependency 與 license isolation](adr/0002-process-dependency-license-isolation.md)
- [ADR-0003：Unsigned candidate 與 keyless release trust](adr/0003-unsigned-and-keyless-release-trust.md)

操作面由 [API index](../api/README.md)、[runbook index](../runbooks/README.md) 與
[P6 handoff evidence](../verification/p6-handoff-evidence.md) 接續；三者不擴張本目錄的
authority 或 external verification 宣稱。
