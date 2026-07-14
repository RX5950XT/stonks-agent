# NautilusTrader backtest sidecar

這是 default-off、process-isolated 的 NautilusTrader `1.230.0` backtest
adapter。它只接受 P5.4 `BacktestJob`，只回傳 `BacktestResult`，不持有 DB、queue、
provider、paper account、broker、risk、reservation 或 ledger credentials。

## Execution mapping

- 每個 job 建立新的 low-level `BacktestEngine`，使用 synthetic bar-open quote 與
  canonical schedule 重播 market/limit orders。
- Canonical scheduler負責TIF、session、shared volume cap與未成交outcome；Nautilus只對
  已排定的fillable child執行原生order/fill lifecycle，並映射DAY/GTC/IOC。每筆raw
  fill的trade ID、price、commission、timestamp payload hash會綁入canonical fill
  `external_ref`；隨機`event_id`不進deterministic raw-content hash。
- Nautilus原生 fill price/fee不是本專案的交易成本真相。Adapter會依 P5.4
  deterministic next-bar policy正規化spread、slippage、impact與fee，core收到後仍會
  重新驗證next-bar、volume cap、outcome及cash/position projection。
- `semantic_hash`排除engine-specific fill ID/ref，因此相同經濟結果可跨runtime比較；
  這不代表Nautilus與reference broker原生撮合bit-identical。

目前只接受`<positive integer>{m|h|d}` bar interval與canonical equity contracts。
Opening positions與cash的權威projection仍由canonical mapper重建；Nautilus內部
margin account只用於authority-free simulation，不會成為paper帳戶。

## Runtime identity

啟動時必須提供：

- `STONKS_NAUTILUS_RUNTIME_HASH`：以`compute_runtime_hash()`計算的adapter source與
  lock hash；不一致即拒絕啟動。
- `STONKS_NAUTILUS_IMAGE_DIGEST`：實際部署OCI image的`sha256:...` digest。
- `STONKS_NAUTILUS_SERVICE_TOKEN`：至少32字元的internal service bearer token；只用於
  `POST /v1/backtests`，不得寫入job、log或artifact。
- 可選的`STONKS_NAUTILUS_MAX_ORDERS`、`STONKS_NAUTILUS_MAX_BARS`與
  `STONKS_NAUTILUS_MAX_REQUEST_BYTES`；order×bar work與schedule children另受
  `STONKS_NAUTILUS_MAX_ORDER_BAR_EVALUATIONS`、
  `STONKS_NAUTILUS_MAX_SCHEDULE_CHILDREN`限制，concurrency預設為1。

HTTP surface只有`GET /healthz`與`POST /v1/backtests`。Request只接受bounded、
identity-encoded JSON；所有錯誤使用`success/status/data/error/metadata` envelope。
同一worker有bounded concurrency；deadline在engine返回後再次驗證，late result不得成功。

## Verification

```powershell
uv lock --check --project sidecars/nautilus
uv sync --frozen --project sidecars/nautilus
$env:PYTHONPATH = ".;src;packages/contracts/src"
uv run --project sidecars/nautilus pytest -q --no-cov sidecars/nautilus/tests
uv export --project sidecars/nautilus --frozen --no-dev --no-emit-project `
  --no-emit-local --format requirements.txt `
  --output-file .data/nautilus-requirements.txt
uv run pip-audit --strict --requirement .data/nautilus-requirements.txt
docker build -f sidecars/nautilus/Dockerfile -t stonks-nautilus-sidecar:p5.5 .
```

授權與可替換wheel說明見[NOTICE.md](./NOTICE.md)。
