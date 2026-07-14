# Third-Party Notices

目前 Stonks Agent core 沒有複製、修改、vendor 或散布研究快照中的上游程式碼。
`.research/upstreams/` 僅是被 `.gitignore` 排除的本機研究資料，不屬於本專案
發行內容。

Python runtime 與開發依賴依各自套件中附帶的授權散布；exact versions 固定於
`uv.lock`，CI 會執行 dependency audit。若未來移植或散布上游程式碼，必須先
依 `docs/legal/license-policy.md` 登錄 notice id、來源 commit、copyright 與
完整授權義務。

## TRADINGAGENTS-APACHE-2.0-WORKER

Optional `workers/tradingagents/` 是與core lock隔離的Apache-2.0 research worker，
使用TauricResearch/TradingAgents v0.3.1、commit
`01477f9afb7a47b849ed4c9259d3a9a4738d9fda`。上游source archive與完整transitive
dependency resolution固定於worker自己的`pyproject.toml`和`uv.lock`；完整
Apache-2.0 license與`workers/tradingagents/NOTICE.md`會放進image。

Worker只把上游Trader、Portfolio Manager與risk debate結果轉成
`AnalysisBundle/AgentOpinion` research output，不授予target、risk、order、broker、
ledger或execution authority。TradingAgents runtime packages不進core dependency graph。

## AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY

`src/stonks_agent/strategies/pead.py`與
`src/stonks_agent/analytics/event_study.py`選擇性移植並重寫
Virat Singh 的 `virattt/ai-hedge-fund` PEAD清理規則與event-study pure statistics，
來源固定於commit `3a18702cb25777fb4bdb4b2527a0c868bc8297f4`。原作與衍生部分依
MIT License使用；完整copyright與license text收錄於
`docs/legal/notices/AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY.md`。

本策略固定為`draft`、confidence 0，只能產生research-only `AlphaSignal`；沒有
portfolio sizing、risk override、order或execution authority。

## KRONOS-MIT-WORKER

Optional `workers/kronos/` 是與core lock隔離的MIT Kronos forecast worker，
使用ShiYu的Kronos commit
`67b630e67f6a18c9e9be918d9b4337c960db1e9a`；Copyright (c) 2025 ShiYu。
source archive、Kronos-small模型與Kronos-Tokenizer-base的revision、size與
SHA-256固定於`workers/kronos/model-manifest.json`。完整MIT license與notice會
放進CPU/CUDA images。

模型只在process startup從唯讀本機目錄驗證並warm一次，runtime不下載模型；
PyTorch與Kronos code不進core dependency graph。模型輸出沒有target、risk、order、
broker、ledger或execution authority，且模型/資料權利仍須由部署者另行確認。

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

## NAUTILUS-TRADER-LGPL-3.0-SIDECAR

Optional `sidecars/nautilus/` 是與core lock隔離的NautilusTrader backtest sidecar，
動態使用未修改的`nautilus_trader==1.230.0` wheel；reviewed source固定為tag
`v1.230.0`、commit `8160730c7c550480b0a439fb11086a4c4de15f0b`，由Nautech
Systems Pty Ltd與contributors依`LGPL-3.0-or-later`提供。上游copyright為
`Copyright (C) 2015-2026 Nautech Systems Pty Ltd`，repository為
https://github.com/nautechsystems/nautilus_trader。

Exact wheel與transitive resolution固定於sidecar自己的`uv.lock`。Image保留wheel
附帶的完整LGPL與GPLv3 license、exact source sdist，使用者可在derived image替換該dynamic library；wrapper
未修改、未vendor或static link NautilusTrader。Sidecar只輸出canonical backtest
evaluation，沒有paper account、risk、reservation、broker、ledger或execution
authority。完整散布與replacement說明見`sidecars/nautilus/NOTICE.md`。
