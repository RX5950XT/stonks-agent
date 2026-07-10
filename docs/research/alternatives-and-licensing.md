# 替代方案與授權邊界研究

> 研究基準日：2026-07-10。這是工程授權風險盤點，不是法律意見。

## 先講結論

核心平台不應直接拼接七個 repository。最穩健的做法是建立自有、typed、event-driven 的 domain core，再透過 adapters/workers 接上各專案能力。原因不只在程式品質，也在授權：`dexter` 的 README 宣稱 MIT 但缺完整 license text/file；`AI-Trader` 不但缺 license file，根目錄 MIT badge 還和 `service/README.md` 明稱 proprietary server 相互矛盾；`OpenBB` 則是 AGPL-3.0。前兩者在授權補齊前只借鏡公開概念，不複製或衍生程式碼；OpenBB 應隔離成可替換的外部服務或使用者自行啟用的 optional provider。

## 七個指定專案的授權分區

| 專案 | GitHub 標示 / repository 證據 | 核心整合政策 |
|---|---|---|
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | MIT | 可閱讀與重用，但優先包成 strategy/agent adapter，避免把其應用層直接變成本專案核心。 |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | Apache-2.0 | 可重用 agent debate 與 risk workflow；保留 notice，透過本專案 contract 隔離 LangGraph 細節。 |
| [Kronos](https://github.com/shiyu-coder/Kronos) | MIT | 適合做 optional forecast worker；模型輸出只能是 evidence/signal，不能直接下單。 |
| [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) | MIT | 適合吸收排程、briefing、通知與報告流程；資料與 prompt 仍需轉成共用 schema。 |
| [dexter](https://github.com/virattt/dexter#-license) | README 宣稱 MIT，但缺 license text/file | 在 upstream 補齊完整條款前，僅 clean-room 重做產品概念與 UX；不複製 source、prompt 或 assets。 |
| [AI-Trader](https://github.com/HKUDS/AI-Trader) | README 有 MIT badge，但 `LICENSE` 不存在，且 [`service/README.md`](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/README.md) 明稱 proprietary server | 視為不可重用程式碼；僅透過公開 API adapter，或 clean-room 實作 agent/skill/competition 概念。 |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB/blob/1c74893140292944e71ff5cdd9536edf12f05483/LICENSE) | AGPL-3.0-only | 不放進 permissive core；以明確 network/process boundary 接入 unmodified service，修改時遵守對應 source-disclosure 義務。sidecar 是技術隔離，不是自動免除 AGPL 判定。 |

## 更值得納入藍圖的專案

| 專案 | 能補上的缺口 | 授權 / 成熟度 | 建議 |
|---|---|---|---|
| [Microsoft Qlib](https://github.com/microsoft/qlib) | dataset、feature、model、portfolio strategy、executor、backtest/analysis 的完整 quant research lifecycle | MIT；持續維護；Python 3.12 可用 | 第一優先。作為 optional research/backtest backend，比自行拼湊 notebook 更可靠；官方 dataset 暫停供應，因此資料轉換器與 data-quality gate 要由本專案負責。 |
| [Microsoft RD-Agent](https://github.com/microsoft/RD-Agent) | 自動 factor/model 提案、實作、評估與迭代 | MIT；與 Qlib 對齊；目前只支援 Linux | 第二階段接成 Linux container 的 strategy-lab worker；產物必須經 deterministic evaluation gate 才能進 registry，不能在主程序內執行 agent 產生的程式碼。 |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | deterministic event-driven backtest/live parity、multi-venue execution、嚴謹 order/risk semantics | LGPL-3.0；活躍且 production-oriented | 真正進入 paper/live execution 時優先評估；用 adapter 與其 Python/Rust runtime 互通，不讓 engine 型別滲入 domain core。 |
| [QuantConnect LEAN](https://github.com/QuantConnect/Lean) | 多資產、成熟 event-driven backtest/live engine、brokerage adapters | Apache-2.0；成熟但 C#/Docker 邊界較重 | 作為另一個 execution/backtest backend；若需要 equities/options broker breadth，優先於自己寫撮合引擎。 |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | DRL environments/agents 與教學 benchmark | MIT；原專案已指向下一代 FinRL-X/FinRL-Trading | 只當實驗性 model worker 或 benchmark，不放在首版 production path。 |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | crypto execution、dry-run、backtest、risk 與 bot operations | GPL-3.0；成熟 | 若產品需要 crypto，僅作獨立服務 adapter；不適合股票優先的核心，也不直接併碼。 |
| [vectorbt](https://github.com/polakowo/vectorbt/blob/bf7aff6d081fda1e9cd7dc0464d68f98309875a1/LICENSE.md) | 大規模參數掃描與快速 portfolio experiments | Apache-2.0 + Commons Clause | 商業化限制需先釐清；預設不用，Qlib/LEAN/Nautilus 已能覆蓋主要需求。 |

## 建議的授權安全架構

```text
permissive core (自有程式碼)
  ├─ domain contracts / evidence / signals / decisions / orders
  ├─ workflow orchestration / policy / audit / replay
  ├─ MIT or Apache adapters (可 in-process)
  └─ external-provider ports
       ├─ OpenBB service (AGPL process/network boundary)
       ├─ NautilusTrader runtime (LGPL adapter boundary)
       ├─ Freqtrade service (GPL process/network boundary)
       └─ clean-room UX inspired by no-license repositories
```

所有 adapter 都必須輸入/輸出本專案 schema，不得讓上游內部型別成為核心 API。這也能讓資料供應商、LLM、forecast model、backtester 與 broker 各自替換。OpenBB/Freqtrade/Nautilus 等 process boundary 只解決依賴與部署耦合；實際發行、修改及 network use 的授權義務仍需逐案判定。

## 建議的 worker contracts

- `QuantResearchJob`：輸入 immutable dataset snapshot、feature/label spec、universe、cost model 與 split policy；Qlib worker 回傳 predictions、positions、metrics、artifact hashes 和完整 provenance。
- `StrategyEvolutionJob`：RD-Agent 只能在 Linux container/sandbox 內讀取已核准的 dataset snapshot，輸出 candidate factor/model source 與 evaluation request；核心端重新執行測試、look-ahead/leakage checks、walk-forward 與成本敏感度分析後才可註冊。
- `BacktestJob`：LEAN/Nautilus adapters 共用 orders/fills/positions contract，結果需包含 calendar、corporate actions、fees、slippage、latency 與 engine version，禁止只回傳單一報酬數字。
- `ExecutionCommand`：live engine 僅接收通過 risk policy 且帶 idempotency key 的 order intents；所有 fills/events 回寫 append-only audit log。

上游套件版本與重量差距很大（TypeScript/Bun、Python/LangGraph、PyTorch/Hugging Face、Rust/C#、Linux-only worker），因此不建立一個包含所有 optional dependency 的巨大 Python environment。各 worker 使用獨立 lockfile/container；core 只依賴小型 contracts SDK。

## 不採用「把所有 agent 投票平均」的原因

不同專案的輸出不是同一種可比較訊號：新聞摘要、fundamental thesis、Kronos forecast、risk veto 與 execution constraint 的時間尺度、可信度和失效模式都不同。整合層應保存每筆 evidence 的 provenance、`as_of`、有效期限與 confidence calibration，先經 risk/policy gates，再由明確的 portfolio decision policy 合成；LLM debate 只能提供可追蹤的 thesis，不能繞過 deterministic controls。
