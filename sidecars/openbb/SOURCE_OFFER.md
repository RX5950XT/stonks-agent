# OpenBB sidecar Corresponding Source offer

本 sidecar 與其 wrapper 以 `AGPL-3.0-only` 散布，沒有任何保固。依 GNU AGPL
v3 §13，所有透過網路互動的使用者都可免費由 `/source` 下載此執行版本的
wrapper source、exact lock、Dockerfile、package/source manifest 與重建說明。

## Exact upstream source

- OpenBB license/research review snapshot（不宣稱等同已發布 package source）：
  `https://github.com/OpenBB-finance/OpenBB/tree/1c74893140292944e71ff5cdd9536edf12f05483`
- `openbb-core==1.6.13` sdist SHA-256
  `c26cfc2ae37c1700e01db9ca0fd2cd02118715cec2097ea870f47a70186402bb`
- `openbb-platform-api==1.3.6` sdist SHA-256
  `4a23a6ca542a1bbe309a1b9c1246d9e905ec03ab28c3da8e9a8f9a0db6ff8659`
- `openbb-equity==1.6.2` sdist SHA-256
  `143bfcc2766227af14e804312645e3e77762b2954d0750ead0a1c7f8ec2e64dc`
- `openbb-yfinance==1.6.3` sdist SHA-256
  `b25bc0fe17552f2331c771f9f91ab66c138e7bb54b07af282381dec2c273bd5b`

四個已發布 package 的 sdist 才是此 image 實際安裝版本的 exact upstream source。
它們不只列 URL：Docker build 會用 `ADD --checksum` 驗證後放進
`upstream/`，因此執行中 `/source` 回傳的 archive 已包含實際 upstream source。
完整 URL 位於 `provider-manifest.yaml`；`uv.lock` 記錄所有 transitive artifacts
與 hashes。image 中的完整 AGPL v3 license 取自上述 pinned OpenBB commit，並以
SHA-256 驗證後一併放進 archive。

## Patch state 與 build recipe

OpenBB packages 未被 patch；唯一新增程式為本目錄的 `app.py`，用途是掛載原始
OpenBB FastAPI app、加入 `/source`，並在每個 HTTP response 宣告 source link。

```sh
uv sync --frozen
uv run openbb-build
uv run uvicorn app:app --host 0.0.0.0 --port 6900 --no-access-log
```

Docker 的 exact recipe 是 archive 內的 `Dockerfile`，base image 以 OCI digest
鎖定。若任何 OpenBB package、patch、lock 或 build script 改變，必須重新產生
archive、SBOM、hash manifest，並讓 `/source` 指向實際部署版本。

License text: `https://www.gnu.org/licenses/agpl-3.0.html`
