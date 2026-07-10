# Wire contracts

`v1/` 是 `stonks-contracts` 的 deterministic JSON Schema snapshot。任何 breaking
change 必須新增 major 目錄，不可直接覆寫既有 consumer 所依賴的 major version。

產生與驗證：

```powershell
uv run python scripts/export_schemas.py
uv run python scripts/export_schemas.py --check
```

