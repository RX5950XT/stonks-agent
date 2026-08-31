# Stonks Agent

[![CI](https://github.com/RX5950XT/stonks-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/RX5950XT/stonks-agent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/RX5950XT/stonks-agent)](https://github.com/RX5950XT/stonks-agent/releases/tag/v0.1.2)
[![License](https://img.shields.io/github/license/RX5950XT/stonks-agent)](./LICENSE)

Stonks Agent 是本機執行的投資研究與 paper trading 平台。它把市場資料、AI 研究、Kronos
預測、回測與模擬交易接在同一條可稽核、可重播的資料流上。

> [!IMPORTANT]
> 只支援 `paper`，不支援 live trading，也不能連券商下單。專案目前是 `pre-alpha`，不構成
> 投資、法律或財務建議。

## 目前能做什麼

| 功能 | 現況 |
|---|---|
| Stonks Desk 本機網頁介面 | 已實測；支援 market、paper、research 三種模式 |
| 美股／台股行情 | 已實測；OpenBB → yfinance 歷史 OHLCV，標示資料來源與時效 |
| K 線 | `1m`、`2m`、`5m`、`15m`、`30m`、`90m`、`1h`、日／週／月／年線 |
| 時間範圍 | `1日`、`5日`、`1週`、`1月`、`3月`、`6月`、`YTD`、`1年`、`5年`、`10年`、`全部` |
| 標的儀錶板 | 公司資料、SEC／TWSE 財報 facts、近期申報與有限歷史比較 |
| 研究聊天室 | 中文操作與 `ADD`、`DROP`、`RESEARCH`、`REFRESH`、`HELP` 進階命令 |
| AI 研究 | snapshot-bound evidence、claims、引用、反方觀點與風險；需要自備 LLM |
| Kronos | 真實 CPU forecast 已接入，但維持 `shadow`，paper weight 為 `0` |
| Paper flow | evidence → signal → 風控 → 資金保留 → 模擬成交 → 平衡帳務 → replay |
| Optional workers | TradingAgents、Qlib、RD-Agent、NautilusTrader、LEAN，各自隔離執行 |

K 線的年線由已驗證的月線在 core 聚合，不把未實測的 provider 能力當成已支援功能。長期
資料仍有 bounded query、回應大小與 provider 限制；「全部」不是無限制下載。

## 資料來源

目前預設啟用且已實際驗證的來源：

- `OpenBB → yfinance`：美股／台股歷史 OHLCV，包含日內 bars。
- `SEC EDGAR`：美國公司識別、XBRL company facts 與近期申報。
- `TWSE OpenAPI`：台股月營收、綜合損益與資產負債等公開資料。

所有行情都不是交易所 tick；回應會標示 `is_real_time=false`、`provider`、`observed_at`、
`available_at`、品質與 freshness。失敗或過期時會明確回報，不用 fixture、舊快取或硬編碼數值
冒充成功。

Financial Datasets 只在明確設定 `STONKS_FINANCIAL_DATASETS_API_KEY` 且主來源失敗時作為
美股日線 fallback；它不是免費來源，真實 key 的 runtime 可用性仍需另外驗證。BLS、FRED、
新聞、內部人、13F、估值與預測市場目前只完成盤點，未列為 active provider。完整來源、條款與
GitHub 上游對照見[免費市場資料來源](./docs/research/free-market-data-sources.md)與
[上游研究](./docs/research/virattt-projects.md)。

## 快速開始

### 需求

- Windows、Linux 或 macOS
- Git、`uv`、Python 3.12（`>=3.12,<3.13`）
- Docker Engine／Desktop 與 Compose v2

### 安裝

```powershell
git clone https://github.com/RX5950XT/stonks-agent.git
cd stonks-agent
uv python install 3.12
uv sync --frozen --python 3.12
```

### 啟動 Stonks Desk

```powershell
.\start.ps1 -Mode market     # 行情與儀錶板
.\start.ps1 -Mode paper      # 另加唯讀 paper 投資組合
.\start.ps1                  # research：研究、SSE、Kronos
```

Windows 也可雙擊 `start.cmd`；它會使用 paper DB port `55434`。Linux／macOS 使用
`./start.sh`。只檢查條件、不啟動服務：

```powershell
.\start.ps1 -Mode market -Check
```

啟動後開啟 `http://127.0.0.1:8787`。輸入 `AAPL` 或 `2330.TW`，再選 K 線週期和時間範圍。
研究區聊天室可輸入「查看 NVDA」、「切換 5 分鐘」、「切換年線」、「加入 NVDA」，也可使用
`AAPL 5m`、`ADD NVDA` 等進階命令；它只會轉成既有安全命令，不執行任意 shell。

### 啟用研究

Research 需要 LLM 設定與 Kronos 權重。先在介面的「模型連線」輸入 endpoint、model、key，
按「儲存並驗證」。API key 只在本次程序記憶體存在，不寫入瀏覽器、資料庫、artifact 或 log。

取得 pinned Kronos 權重：

```powershell
uv run --frozen python scripts/fetch_kronos_model.py
.\start.ps1
```

權重會依 `workers/kronos/model-manifest.json` 的 revision、檔案大小與 SHA-256 驗證。Kronos
只產生預測路徑；它沒有 DB、broker 或下單權限，尚未通過 evaluation／promotion 前不會產生
paper order。

## 離線示範

不用網路、LLM、PostgreSQL 或 sidecar，也能驗證完整 paper／replay 流程：

```powershell
uv run stonks fake-cycle --symbol AAPL --as-of 2026-01-02T21:00:00Z --idempotency-key demo
```

這是 deterministic fixture 示範，不是最新行情。

## CLI 與驗證

```powershell
uv run --frozen stonks-gui serve --help
uv run stonks --help
uv run stonks-deploy --help
uv run stonks-worker --help
uv run python scripts/verify.py
uv run python scripts/verify.py --skip-audit
uv run python scripts/export_schemas.py --check
```

`verify.py` 會跑 format、Ruff、strict `mypy`、pytest／coverage、schema、upstream policy、
secret 與 dependency checks。需要 PostgreSQL 的完整 gate 另需設定
`STONKS_TEST_DATABASE_URL`，再執行：

```powershell
uv run python scripts/verify.py --with-postgres
```

清理只允許刪除可重建輸出：

```powershell
uv run --frozen python scripts/clean_workspace.py --dry-run
uv run --frozen python scripts/clean_workspace.py
```

工具會保留原始碼、`.data`、`.research`、模型、資料庫 volume 與 runtime 環境，不使用
`git clean` 或 system-wide Docker prune。

## 安全邊界

```text
Evidence / ResearchArtifact
  -> AgentOpinion / AlphaSignal / ForecastSignal
  -> deterministic PortfolioTarget
  -> RiskDecision
  -> AccountReservation
  -> OrderIntent
  -> ExecutionReceipt / Fill
  -> balanced Journal
```

LLM、Kronos、TradingAgents、社群資料與 optional worker 都不能直接建立 target 或 order，
也不能跳過風控。研究資料必須先封存成同一份 point-in-time snapshot，Agent 只使用 audited、
read-only tools。

## 專案狀態與文件

原始核心的 `v0.1.2` release closure 已完成；該 release 不含 Local GUI。現在 `main` 是未發布
的 `0.2.0` candidate，且尚未組合成 production business API。這不代表可連券商、可實盤或可
暴露在公網。

- [文件中心](./docs/README.md)
- [Local GUI 操作手冊](./docs/runbooks/local-gui.md)
- [API contracts](./docs/api/README.md)
- [架構與 authority](./docs/architecture/README.md)
- [Optional integrations](./docs/runbooks/optional-integrations.md)
- [上游研究與授權](./docs/research/README.md)
- [P6 驗證證據](./docs/verification/p6-handoff-evidence.md)
- [開發交接](./CONTEXT.md)

## License

Core 使用 [Apache-2.0](./LICENSE)。OpenBB 與其他 optional upstream 使用各自授權；對應的
license、source-offer 與資料權利見 [license policy](./docs/legal/license-policy.md) 與
[third-party notices](./THIRD_PARTY_NOTICES.md)。
