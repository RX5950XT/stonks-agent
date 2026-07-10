# Third-Party Notices

目前 Stonks Agent core 沒有複製、修改、vendor 或散布研究快照中的上游程式碼。
`.research/upstreams/` 僅是被 `.gitignore` 排除的本機研究資料，不屬於本專案
發行內容。

Python runtime 與開發依賴依各自套件中附帶的授權散布；exact versions 固定於
`uv.lock`，CI 會執行 dependency audit。若未來移植或散布上游程式碼，必須先
依 `docs/legal/license-policy.md` 登錄 notice id、來源 commit、copyright 與
完整授權義務。

