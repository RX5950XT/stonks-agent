# QuantConnect LEAN backtest sidecar

這是 default-off、process-isolated 的 QuantConnect LEAN `17917` backtest
adapter。它只接受 P5.4 `BacktestJob`、只回傳 `BacktestResult`，不持有 DB、queue、
provider、paper account、broker、risk、reservation 或 ledger credentials。

## Execution mapping

- Canonical scheduler 負責 TIF、session、shared volume cap、cost 與 projection；LEAN
  只執行已排定且可成交的 child order，不能建立 target/order 或覆寫 risk。
- Canonical bar 轉成 LEAN minute equity data；目前只接受 XNAS/XNYS、USD、整數股數、
  最多四位小數價格與 `<positive integer>{m|h|d}` interval。
- 原生 MARKET/LIMIT 與 DAY/GTC 會保留；因 LEAN 沒有原生 IOC，canonical scheduler
  只排第一個 IOC child，再以 DAY 執行。LEAN 原生 fee/slippage 固定為零，最終經濟
  結果由 canonical deterministic model 正規化，core 仍會重驗。
- 每個 job 使用新的 `dotnet` process；固定 command、sanitized environment、無 shell、
  無 stdin/output capture，並受 request deadline、engine timeout、trace size 與 schedule
  child cap 約束。late/invalid output 一律 fail closed。

Corporate action 與 calendar 真相已由 canonical immutable dataset 驗證並烘焙；固定
algorithm 使用 Raw data 與 always-open replay，不能把 LEAN 內部 calendar 當成權威。

## Runtime identity

啟動必須提供 `STONKS_LEAN_RUNTIME_HASH`、實際 OCI
`STONKS_LEAN_IMAGE_DIGEST` 與至少 32 字元的 `STONKS_LEAN_SERVICE_TOKEN`。HTTP surface
只有 `GET /healthz` 與 bearer-protected `POST /v1/backtests`。可調整 bounded
`STONKS_LEAN_MAX_*` limits；concurrency 預設 1。

## Verification

```powershell
uv lock --check --project sidecars/lean
uv sync --frozen --project sidecars/lean
$env:PYTHONPATH = ".;src;packages/contracts/src"
uv run --project sidecars/lean pytest -q --no-cov sidecars/lean/tests tests/contracts/test_lean_sidecar.py
docker build -f sidecars/lean/Dockerfile -t stonks-lean-sidecar:p5.6 .
```

修改與散布資訊見 [NOTICE.md](./NOTICE.md) 與
`distribution-manifest.yaml`。
