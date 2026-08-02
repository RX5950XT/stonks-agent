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
| Financial Datasets | 最低購買 USD 20 credits | `paid / not_free` | 不列入免費來源 |
| Cboe delayed quote page | 延遲頁面 | `prohibited` | 頁面明列禁止 automated extraction，不建立 scraper |

官方依據：

- [OpenBB historical provider reference](https://docs.openbb.co/odp/python/reference/equity/price/historical)
- [Alpaca market-data plans](https://alpaca.markets/data) 與
  [single-symbol snapshot](https://docs.alpaca.markets/us/reference/stocksnapshotsingle)
- [Finnhub API documentation](https://api.finnhub.io/docs/api/rate-limit)
- [Alpha Vantage documentation](https://www.alphavantage.co/documentation/)
- [Twelve Data pricing and usage rights](https://twelvedata.com/pricing)
- [Financial Datasets pricing](https://www.financialdatasets.ai/pricing)
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

## 新來源的啟用門檻

新增來源必須先有獨立 adapter 與 secret boundary，再逐項通過：

1. 官方文件與當前條款確認 automated access、local display、保存與使用者類型。
2. API key 不進 URL、log、artifact、browser storage 或 committed config。
3. symbol／exchange／timestamp／session／currency／adjustment 語意正規化。
4. rate limit、timeout、quota、empty、stale 與 entitlement failure typed fail closed。
5. actual external runtime、contract tests、PIT tests 與桌面／窄版 GUI 驗證。
6. 只有完成以上條件的來源才能加入 active registry；replay 或其他來源不得冒充 fallback。
