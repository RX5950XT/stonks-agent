# Third-Party Notices

`.research/upstreams/` 僅是被 `.gitignore` 排除的本機研究資料，不屬於本專案
發行內容。Core 與 optional runtimes 只使用下列明確登錄的上游元件或衍生部分；
未登錄的研究 snapshot 不得被 import、vendor 或散布。

Python runtime、Alpine packages 與開發依賴各自適用不同授權；exact Python/APK
runtime 證據固定於 `config/release/core-runtime-legal.json`，Python dependencies
固定於 `uv.lock`，正式 release 必須通過 dependency、license、source 與 notice
gates。後續移植或散布其他上游程式碼時，仍須先依
`docs/legal/license-policy.md` 登錄 notice id、來源 commit、copyright 與完整
授權義務。

## CPYTHON-PYTHON-2.0-COOKIE-SECURITY-BACKPORT

Core 使用 CPython 3.12.13，完整 Python license（`Python-2.0`，含
`PSF-2.0` 與歷史條款）保留於
`/usr/local/lib/python3.12/LICENSE.txt`。`http/cookies.py` 只 selective
backport CPython commit `57e88c1cf95e1481b94ae57abe1010469d47a6b4`
中與 CVE-2026-3644 相關的 validated update、in-place update、unpickle 與
`js_output` 防護；這不是 CPython 官方 3.12.x release。

Exact source/installed hashes、修改摘要、copyright 與完整授權位置見
`docs/legal/notices/CPYTHON-PYTHON-2.0-COOKIE-SECURITY-BACKPORT.md`。

## ALPINE-3.23-CORE-RUNTIME

Core base 固定為 Python 3.12.13 / Alpine 3.23 exact digest。Final runtime 的
`/lib/apk/db/installed` 有 37 個 packages，包含 GPL、LGPL、MPL 與其他授權；
SBOM/license metadata 不能取代對應的散布義務。

Exact Alpine corresponding-source archive 已封存 27 個 origins 的 recipe、patch、
build scripts 與 checksum-verified distfiles；正式 release 必須將它納入同一 signed
manifest，任何 archive 或 inventory drift 都會阻擋。完整 package/version/license/
origin/build-commit inventory 與限制見
`docs/legal/notices/ALPINE-3.23-CORE-RUNTIME.md`。

## SEAWEEDFS-APACHE-2.0-S3-TEST

P6.6 integration smoke 使用未修改、digest-pinned 的 SeaweedFS 4.34
Apache-2.0 image，來源 commit 為
`c6cf5a5bd7c87694c8d71ab41571f1412170ab2a`。該 runtime 只驗證本專案的
S3-compatible SigV4/HTTP adapter，不進 core dependency graph，也不作為
production backend 發布。Exact image/source/license identity與相容性限制見
`docs/legal/notices/SEAWEEDFS-APACHE-2.0-S3-TEST.md`。

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
Sidecar 只能輸出 canonical observation，沒有 target、risk、order、broker、ledger
或 execution authority。

## QLIB-MIT-WORKER

Optional `workers/quant_lab/` 是與 core lock 隔離的 Qlib evaluation worker，使用
Microsoft Qlib commit `d5379c520f66a39953bad76234a7019a72796fd0`；
Copyright (c) Microsoft Corporation，依 MIT License 提供。Exact source archive
SHA-256、獨立 dependency resolution 與完整授權位置固定於
`workers/quant_lab/NOTICE.md`、`pyproject.toml`、`uv.lock` 與 image build。

Worker 只允許 closed typed contract 的 pinned Qlib `LinearModel` OLS path，不接受
任意 module、serialized model、expression、dataset path、provider credential 或
generated code。結果只作 evaluation，沒有 target、risk、reservation、order、
broker、ledger 或 execution authority。

## RD-AGENT-MIT-SANDBOX

Optional `workers/quant_lab/rd_agent/` image 保存 Microsoft RD-Agent commit
`4f9ecb005881cddc08df0124a2e894c018007679` 的 unmodified source snapshot；
Copyright (c) Microsoft Corporation，依 MIT License 提供。Exact source、完整授權、
獨立 lock 與 sandbox distribution identity 固定於
`workers/quant_lab/rd_agent/NOTICE.md` 及同目錄 manifests。

Stonks adapter 不 import 或執行 upstream RD-Agent code，只在 separate one-shot
sandbox 評估已封存 proposal 的 documented factor-only expression subset。Sandbox
沒有 network、DB、provider、target、risk、reservation、order、broker、ledger 或
execution authority。

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

## QUANTCONNECT-LEAN-APACHE-2.0-SIDECAR

Optional `sidecars/lean/` 是與 core lock 隔離的 QuantConnect LEAN backtest
sidecar，使用 tag `17917`、commit
`c22774e49ee80ecef5ca84f57616f6b66fad8bc5`。LEAN source files 標示
Copyright 2014 QuantConnect Corporation，依 Apache-2.0 授權。

Image 從 SHA-256 pinned source archive 建置，保留完整原始 archive、license 與所有
Stonks modifications。為了移除已知 vulnerable/unreachable runtime dependency
chains，build 會套用已記錄 patch，以 bounded clean-room `System.IO.Compression`
compatibility layer 取代使用到的 Ionic.Zip surface，並只編譯 backtest 所需的
EventMessagingHandler。Exact base images、NuGet lock graph 與 modification hashes 見
`sidecars/lean/distribution-manifest.yaml`，完整修改聲明見
`sidecars/lean/NOTICE.md`。

LEAN process 只接收 canonical scheduler 已核准的 child orders，輸出
authority-free trace；沒有 paper account、risk、reservation、broker、ledger 或
execution authority。這個修改版不是 QuantConnect 官方 binary。
