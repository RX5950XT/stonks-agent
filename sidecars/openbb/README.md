# OpenBB optional sidecar

這個目錄是與 Apache-2.0 core 隔離的 `AGPL-3.0-only` 發行單元，只安裝
OpenBB REST API、equity router 與 `yfinance` provider。core 不 import 或 link
OpenBB；唯一整合介面是固定、read-only HTTP contract。

## 重現與啟動

```sh
uv lock --check
uv sync --frozen
docker compose -f ../../infra/compose.openbb.yaml build --pull
docker compose -f ../../infra/compose.openbb.yaml up openbb
```

套件、sdist URL、SHA-256 與上游 commit 記錄於 `provider-manifest.yaml`；完整
transitive resolution 與 artifact hashes 在 `uv.lock`，CycloneDX SBOM 在
`sbom.cdx.json`；逐套件 frozen SPDX inventory、allowlist 與 AGPL review 在
`license-policy.yaml`。SBOM 重新產生後，以
`uv run python ../../scripts/normalize_openbb_sbom_licenses.py` deterministic 正規化，
再用 `--check` 驗證無 drift。image build 會下載並以 SHA-256 驗證四個 pinned OpenBB source
sdists，再連同 wrapper/build inputs 與實際使用的 `packages/service-auth` source 封存。
服務所有 response 都帶
`Link: </source>; rel="source"`，而 `/source` 直接提供該完整 archive。

`surface.py` 在 OpenBB router 前 fail closed。`GET /healthz` 與 `GET /source`
維持匿名；`GET /api/v1/equity/price/historical` 必須先通過 pinned asymmetric
service OIDC authn，才會解析 bounded query，且 token 必須精確指派
`MARKET:US/{symbol}`、receiver=`openbb`，並綁定 canonical method/path/query hash。
OpenBB market fetch 是 generation `0` 的短效、無 lease dispatch，token 的 nonce hash
必須等於 request hash。固定 provider 只能是 `yfinance`；其餘 method、path、query
與 WebSocket 都不會抵達 OpenBB app。

runtime 必須提供 `STONKS_SERVICE_OIDC_ISSUER`、`AUDIENCE`、`CORE_SUBJECT`、
`CORE_CLIENT_ID` 與 read-only mounted public JWKS；缺少或錯誤設定會在 startup
fail closed。Bearer token 不得寫入 image、Compose 或 log。

`GET /healthz` 只驗證 immutable app/build identity，不會觸發 provider request；
`openbb-build` 僅在 image build 執行，runtime 明確設為 `OPENBB_AUTO_BUILD=false`。

`compose.openbb.yaml` 只把 plaintext port 綁到 `127.0.0.1:6900`；core adapter
也只接受這個 exact loopback origin，request 無法改 origin/provider/endpoint。因此
sidecar 不可被 LAN 或 container network client 當成 core origin。若未來 core 也
containerize，必須另建受信任 TLS gateway 並更新 allowlist/RFC，不得擴大為任意
HTTP origin。sidecar runtime 需要 Yahoo Finance egress；production 必須以 network
policy 限制目的地，不能開放任意內網或 metadata ranges。

OpenBB 與 Yahoo Finance 資料不保證正確、完整或可再散布；啟用前仍須自行確認
資料來源條款。本 sidecar 只允許 paper research，不提供 live execution。
