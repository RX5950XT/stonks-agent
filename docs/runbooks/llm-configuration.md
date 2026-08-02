# 自訂 LLM 設定

Research worker 使用 OpenAI-compatible `chat/completions` contract。Local GUI 可直接設定
route、model、API key、價格與 request budgets；環境變數仍可作啟動時的初始設定。

## GUI 設定（建議）

1. 在專案根目錄執行 `.\start.ps1`。
2. 開啟「AI 研究」中的「LLM 模型連線」。
3. 輸入 Base URL、Model ID 與 API key；需要時展開進階 budgets。
4. 按「儲存並驗證」。系統會執行一次 bounded structured completion，成功後才原子套用
   到下一筆 research lease；失敗不會覆蓋先前可用設定。

設定只存在本次 server process 記憶體。API key 不會回傳到 capability／settings GET、
不寫入 HTML、browser storage、DB、artifact、event 或 log，送出後密碼欄位立即清空；
server 重啟後需重新輸入。研究歷史仍可在未設定模型時讀取，但新的研究 CTA 與 POST
會 fail closed。

Public HTTPS endpoint 會先解析並 pin 住允許的 public IP，拒絕 private／loopback／metadata
解析、redirect、proxy、`.netrc` 與 DNS rebinding。只有 local／development／test 可使用
exact `http://127.0.0.1:<port>` 或 `http://localhost:<port>`。Provider 若在成功回應中
回顯 exact API key，core 會在 parse 與 artifact archive 前拒絕。

## 環境變數初始設定（選用）

四個必要變數完整時，research composition 會在啟動時自動執行同一個 structured
completion probe；成功後 GUI 直接顯示已驗證，保留既有 env-based launcher 行為。
失敗時 GUI 仍會啟動但研究保持 disabled，可在面板改用另一組設定。

```powershell
$env:STONKS_ENVIRONMENT = "local"
$env:STONKS_LLM_BASE_URL = "https://llm.example.com"
$env:STONKS_LLM_MODEL = "provider-model-id"
$env:STONKS_LLM_API_KEY = "<secret>"
```

預設 endpoint 是 `/v1/chat/completions`。若服務使用不同路徑：

```powershell
$env:STONKS_LLM_ENDPOINT = "/v1/chat/completions"
```

local／development 可使用 `http://127.0.0.1:<port>` 或
`http://localhost:<port>` 的 Ollama／LM Studio 等 loopback endpoint；其他 HTTP origin、
staging 與 production 一律拒絕，後兩者仍要求 HTTPS。

## 保守 budget

未覆寫時，每次 request 最多輸出 4,096 tokens、總 tokens 32,768、成本 USD 1、timeout
30 秒、response 1 MiB、transient retry 1 次、structured repair 1 次。可用下列變數縮小
或調整：

- `STONKS_LLM_MAX_OUTPUT_TOKENS`
- `STONKS_LLM_MAX_TOTAL_TOKENS`
- `STONKS_LLM_MAX_COST_USD`
- `STONKS_LLM_TIMEOUT_SECONDS`
- `STONKS_LLM_MAX_RESPONSE_BYTES`
- `STONKS_LLM_MAX_TRANSIENT_RETRIES`
- `STONKS_LLM_MAX_REPAIRS`
- `STONKS_LLM_INPUT_COST_PER_MILLION`
- `STONKS_LLM_CACHED_INPUT_COST_PER_MILLION`
- `STONKS_LLM_CACHE_WRITE_INPUT_COST_PER_MILLION`
- `STONKS_LLM_OUTPUT_COST_PER_MILLION`

所有數值必須是無空白、finite 的十進位或整數。缺 key、未知 route、schema-invalid output、
超過 repair/budget 或 provider failure 都會讓 durable job 以 typed error fail closed，
不會產生 fixture report 或假成功。

## 啟動與研究

以 source checkout 一鍵啟動：

```powershell
.\start.ps1
```

`--with-research` 會同時啟動 paper 投影、PostgreSQL、live OpenBB snapshot materialization
、authenticated Kronos CPU 與單一 background core worker，不需另跑 Kronos verifier。
模型驗證完成後，畫面按「開始 AI 研究」或輸入 `RESEARCH AAPL`。Research POST 是唯一
canonical workflow mutation；模型設定 PUT／DELETE只更新process-memory設定。三者都接受
same-origin intent，Browser 無法指定 owner、account、target 或 order。

Kronos 會以獨立 canonical `1d` snapshot 產生並封存真 forecast；目前仍為 shadow、
paper weight 0。沒有 genuine evaluation/promotion artifact 時，GUI 的 alpha 會明示
blocked，paper 決策維持 no-order。
