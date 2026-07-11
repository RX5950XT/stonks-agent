# Third-Party Notices

目前 Stonks Agent core 沒有複製、修改、vendor 或散布研究快照中的上游程式碼。
`.research/upstreams/` 僅是被 `.gitignore` 排除的本機研究資料，不屬於本專案
發行內容。

Python runtime 與開發依賴依各自套件中附帶的授權散布；exact versions 固定於
`uv.lock`，CI 會執行 dependency audit。若未來移植或散布上游程式碼，必須先
依 `docs/legal/license-policy.md` 登錄 notice id、來源 commit、copyright 與
完整授權義務。

## OPENBB-AGPL-3.0-SIDECAR

Optional `sidecars/openbb/` 是與 Apache-2.0 core 分離發行的
`AGPL-3.0-only` OpenBB REST sidecar。它使用 OpenBB snapshot
`1c74893140292944e71ff5cdd9536edf12f05483` 作為 license/research review 基準；
runtime 則獨立 pin 已發布套件。exact versions、PyPI sdist URLs、SHA-256、patch
狀態與 build recipe 記錄於
`sidecars/openbb/provider-manifest.yaml`、`SOURCE_OFFER.md` 與獨立 `uv.lock`。

Sidecar image 包含完整 GNU AGPL v3 license，所有 HTTP response 都廣告
`Link: </source>; rel="source"`，`GET /source` 免費提供實際部署版本的 source/build
archive，且 archive 內含四個實際安裝版本的 OpenBB source sdists。OpenBB process
boundary 是技術隔離，不免除 AGPL 或資料 provider 條款。
