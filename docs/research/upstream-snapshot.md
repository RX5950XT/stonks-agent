# 上游研究快照

研究日期：2026-07-10。所有 repository 均以 `git clone --depth 1` 放在未來應排除版控的 `.research/upstreams/`，供架構與介面研究；這些目錄不是本專案 source dependency。

| Repository | Snapshot commit | Commit time |
|---|---|---|
| `virattt/ai-hedge-fund` | `3a18702cb25777fb4bdb4b2527a0c868bc8297f4` | 2026-07-03T13:18:57-04:00 |
| `virattt/dexter` | `bae661670c3d77e909942777ac32ece21e8af35d` | 2026-07-03T07:40:18-04:00 |
| `TauricResearch/TradingAgents` | `01477f9afb7a47b849ed4c9259d3a9a4738d9fda` | 2026-07-05T14:29:07Z |
| `shiyu-coder/Kronos` | `67b630e67f6a18c9e9be918d9b4337c960db1e9a` | 2026-04-13T20:38:49+08:00 |
| `ZhuLinsen/daily_stock_analysis` | `aa513135d67425d2484cdc9c643402c0f4c3ae07` | 2026-07-08T22:12:24+08:00 |
| `HKUDS/AI-Trader` | `d03ff6c056b32ced735adf7c19ed8175adb1c8df` | 2026-06-11T17:26:01+08:00 |
| `OpenBB-finance/OpenBB` | `1c74893140292944e71ff5cdd9536edf12f05483` | 2026-07-08T16:21:29Z |
| `microsoft/qlib` | `d5379c520f66a39953bad76234a7019a72796fd0` | 2026-04-22T15:08:01+08:00 |
| `microsoft/RD-Agent` | `4f9ecb005881cddc08df0124a2e894c018007679` | 2026-05-06T09:39:25+08:00 |

重新研究時應建立新快照並重新檢查 license、public API、dependency constraints 與 migration notes，不能假設上游 main branch 相容。

## 僅用於替代案授權證據

`polakowo/vectorbt` 未下載到本機，僅以 GitHub commit `bf7aff6d081fda1e9cd7dc0464d68f98309875a1`（2026-07-10）固定其 Commons Clause license 證據；它不是目前採用或可直接重用的 dependency。
