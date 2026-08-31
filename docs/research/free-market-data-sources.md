# 免費市場資料來源邊界

「網路上所有免費 API」不是可封閉、可驗證的集合；免費額度也不等於允許在 GUI 顯示、
保存或重新散布。Stonks Agent 只會啟用同時通過官方條款、credential、display rights、
rate limit、schema、PIT 與 actual runtime 驗證的來源。

## 目前狀態

| 來源 | 免費層與時效 | 本專案狀態 | 原因 |
|---|---|---|---|
| OpenBB → yfinance historical | 無專案 API key；可取得 `1m` 至 `1d` bars | `active / actual_runtime_verified` | 只限本機研究 GUI；`is_real_time=false`，不宣稱交易所 tick 或再散布權 |
| Alpaca Basic | 需帳號與 key；免費即時 IEX，SIP 可為 15 分鐘延遲 | `requires_user_credential / not_composed` | 還缺 market-secret UI、entitlement 與 display-rights 驗證 |
| Finnhub free | 需 token；官方提供 US real-time quote | `requires_user_credential / not_composed` | 免費註冊受 personal／non-professional 條件約束，尚未完成合法顯示與 runtime gate |
| Alpha Vantage free | 需 key；free quote 預設 EOD | `not_selected` | 不會讓目前 `1m` 主行情更即時；real-time／15-minute delayed US data 是 premium |
| Twelve Data Basic | 需 key；免費 real-time US equities | `display_restricted / not_composed` | Basic 明列 internal non-display，不能直接當 GUI 報價來源 |
| Financial Datasets | 最低購買 USD 20 credits | `paid / optional_fallback` | 只支援已接入的 US daily fallback；不列入免費來源，真實 key runtime 尚未驗證 |
| SEC EDGAR `data.sec.gov` | 免費公開；submissions、companyfacts、companyconcept、frames、bulk ZIP | `active / dashboard+research` | 已實測；需合規 `User-Agent`、快取與 <=10 requests/sec，不能當任意爬蟲 |
| TWSE OpenAPI | 公開 JSON；行情、融資融券、三大法人、董監事、ESG、月營收、財報、EPS、股利、預估等 | `active / dashboard+research` | 已實測月營收、綜合損益、資產負債；即時交易與再散布另有費用／授權 |
| BLS Public Data API | v1 不用註冊；v2 註冊後可取更多歷史資料 | `cataloged / not_composed` | 已實測 API 回應；目前沒有接到標的 snapshot |
| FRED API | 資料免費，但每次 web-service request 都要 API key | `requires_user_credential / not_composed` | 未設定 key，不假裝可用 |
| Cboe delayed quote page | 延遲頁面 | `prohibited` | 頁面明列禁止 automated extraction，不建立 scraper |

官方依據：

- [OpenBB historical provider reference](https://docs.openbb.co/odp/python/reference/equity/price/historical)
- [Alpaca market-data plans](https://alpaca.markets/data) 與
  [single-symbol snapshot](https://docs.alpaca.markets/us/reference/stocksnapshotsingle)
- [Finnhub API documentation](https://api.finnhub.io/docs/api/rate-limit)
- [Alpha Vantage documentation](https://www.alphavantage.co/documentation/)
- [Twelve Data pricing and usage rights](https://twelvedata.com/pricing)
- [Financial Datasets pricing](https://www.financialdatasets.ai/pricing)
- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) 與
  [SEC developer fair-access](https://www.sec.gov/about/developer-resources)
- [TWSE OpenAPI Swagger](https://openapi.twse.com.tw/)、[TWSE real-time data fees](https://www.twse.com.tw/en/products/information/real-time.html)
- [BLS API features and limits](https://www.bls.gov/bls/api_features.htm)
- [FRED API keys](https://fred.stlouisfed.org/docs/api/fred/v2/api_key.html)
- [Cboe delayed quotes](https://www.cboe.com/delayed_quotes/API/quote_table/)
- [Yahoo Developer API terms](https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html)

## 已驗證主行情

2026-07-29 actual runtime 對 AAPL 查詢 `1m`／7 天：

- provider：`openbb:yfinance`
- 最新 event：`2026-07-29T15:49:00Z`
- request 時資料年齡：40 秒
- backend 判定：`freshness=current`、`quality=available`
- bars：1,699
- `is_real_time=false`，介面顯示「目前可用 · 非 tick」

GUI 預設 `1m`，只在分頁可見且沒有進行中請求時每 30 秒更新；watchlist 同樣使用
`1m`。這是目前不要求額外帳號時可驗證的最快路徑，不代表 SLA。Provider 每分鐘最多
30 次請求，連續失敗會短暫 cooldown；滿載回 typed `429 rate_limited`。

## 目前能取得的範圍

沒有一個可信的「整個網路總量」數字；能取得多少取決於來源條款、symbol、時間點、
額度與是否允許保存／顯示。現在可明確分成：

| 類別 | 已能取得 | 上限／限制 |
|---|---|---|
| 價格 | OpenBB → yfinance 的歷史 OHLCV／成交量，GUI 已支援日、週、月、年聚合 | 非 tick；查詢與 response 有 bounded 上限，不能宣稱交易所即時行情 |
| 美國公司 | SEC 公司識別、申報歷史、XBRL company facts（營收、淨利、資產、負債、權益、EPS、現金、股數） | SEC 公開資料更新快，但必須遵守 fair access；公司 facts 不是完整估值或所有自訂指標 |
| 台灣公司 | TWSE 月營收、綜合損益、資產負債、EPS 與部分期間／出表日期 | 公開 OpenAPI 與即時交易資料是兩件事；即時與再散布不能以免費 API 名義帶過 |
| 宏觀 | BLS 全調查歷史序列；FRED 等經濟資料可在完成 key／條款驗證後加入 | BLS v1 每次最多 25 series、10 年；v2 最多 50 series、20 年、每日 500 queries |
| 新聞／社群／內部人／13F／預測市場 | GitHub 上游列出這些類型，但本機尚未有已驗證的免費、可保存、可 PIT 重播來源 | 未通過 provider runtime 與權利檢查前維持未組合 |

「薅到多少」的實際答案是：目前一次標的研究可以封存行情 + SEC/TWSE 公司資料 + 申報紀錄；
不是把所有免費 endpoint 無限制掃一遍。每次資料都帶 `provider`、`source_url`、
`observed_at`、`available_at`、`as_of`，失敗或過期就回 typed failure，不用 fixture 補洞。

## 本輪已接入

- `GET /api/v1/instrument/overview?symbol=AAPL`：SEC 公司資料／財報 facts／近期 filings。
- `GET /api/v1/instrument/overview?symbol=2330.TW`：TWSE 月營收／綜合損益／資產負債。
- SEC facts 目前納入營收、毛利、營業利益、營業現金流、資本支出、營業費用、股利、淨利、資產、負債、權益、EPS、現金與股數；每項最多保存 12 筆可用歷史觀測。
- 公開 TWSE OpenAPI 實際回傳目前期間的月營收、季度綜合損益與資產負債彙總；端點沒有回傳歷史列時，儀錶板只顯示實際取得的 1 筆。
- 研究 snapshot policy：`us-research/1`、`tw-research/1`，把 OpenBB 行情與官方公司資料封存成同一份 snapshot。
- Agent read-only tools：`fundamental_snapshot`、`filing_history`；研究提示要求先列證據，看到相應種類就主動讀取。
- 可靠性：固定 host/path、8 秒 timeout、12 MiB body cap、60 requests/min process budget、5 分鐘 cache、JSON／時間點／欄位驗證，且禁止 redirect。
- MOPS 歷史 HTML 路徑本輪實測遭官方安全頁拒絕，未列入 active；不能以被封鎖的頁面爬取結果補成 TWSE 歷史資料。

## GitHub 上游能力對照

OpenBB upstream 的 provider extension 表列出 BLS、CFTC、Congress.gov、FRED、IMF、OECD、
SEC、Tiingo、yfinance 等免費或免 key connector，extras 還有 Alpha Vantage、ECB、Fama-French、
Federal Reserve、FINRA、Finviz、Nasdaq 等；這代表上游可裝，不代表目前本機 sidecar 已安裝、
端點已實測或資料可合法顯示。來源：[OpenBB platform README](https://github.com/OpenBB-finance/OpenBB/blob/develop/openbb_platform/README.md)。

TradingAgents 的實際工具分成行情／技術指標、fundamentals、balance sheet、cash flow、
income statement、news、global news、insider transactions、macro indicators、prediction
markets 與 verified market snapshot；目前本機只以 clean-room 方式接入 bounded research loop，
已完成的是 snapshot 行情、基本面、申報三類，其餘仍是缺口。來源：[agent_utils.py](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/agents/utils/agent_utils.py)
與 [trading_graph.py](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/graph/trading_graph.py)。

Dexter README 實際要求 Financial Datasets API key，Exa web search 是 optional，並主打財報、
工具規劃、自我驗證與 bounded step limit；所以它不是「免費資料已經存在本機」。本專案只採
clean-room 概念，沒有複製其 source／prompt。來源：[Dexter](https://github.com/virattt/dexter)。

## 新來源的啟用門檻

新增來源必須先有獨立 adapter 與 secret boundary，再逐項通過：

1. 官方文件與當前條款確認 automated access、local display、保存與使用者類型。
2. API key 不進 URL、log、artifact、browser storage 或 committed config。
3. symbol／exchange／timestamp／session／currency／adjustment 語意正規化。
4. rate limit、timeout、quota、empty、stale 與 entitlement failure typed fail closed。
5. actual external runtime、contract tests、PIT tests 與桌面／窄版 GUI 驗證。
6. 只有完成以上條件的來源才能加入 active registry；replay 或其他來源不得冒充 fallback。
