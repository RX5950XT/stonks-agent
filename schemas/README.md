# Wire contracts

`v1/` 是 `stonks-contracts` 的 deterministic JSON Schema snapshot。任何 breaking
change 必須新增 major 目錄，不可直接覆寫既有 consumer 所依賴的 major version。

`openapi/v1/` 是七份 OpenAPI 3.1 snapshots，exact surface、runtime auth/RBAC、統一
envelope、SSE 與部署限制見 [API index](../docs/api/README.md)。六份 business／health
snapshot 未內嵌 security scheme 不會放寬 runtime authenticator、permission 或
ownership gate；GUI 沒有人類 auth，只以 loopback admission 保護 Browser／JSON route。

產生與驗證：

```powershell
uv run python scripts/export_schemas.py
uv run python scripts/export_schemas.py --check
uv run python scripts/export_openapi.py
```
