# 授權與上游採用政策

本文件是工程治理規則，不是法律意見。自有 core 預設使用
Apache-2.0；每次引入上游程式碼、模型或資料前，仍須個別確認授權與
散布義務。

## 不可繞過的邊界

- `.research/upstreams/` 僅供研究，永遠不作 runtime import、vendor source
  或發行內容。
- Dexter 缺完整授權文字，只能 clean-room 重做公開概念；禁止複製 source、
  prompt、skills、assets 或 frontend。
- AI-Trader 缺 LICENSE，且 server 有 proprietary 聲明；只能使用公開 HTTP
  contract 建立 external adapter，禁止複製其程式碼或讓它提交 canonical
  order。
- OpenBB 是 `AGPL-3.0-only`。它只能在另行核准的 optional sidecar 中使用，
  不得成為 core dependency 或 import；process boundary 不會消除 AGPL 義務。
- PyTorch、LangGraph、Qlib、RD-Agent、NautilusTrader、LEAN 及各 heavy
  upstream 只能存在於獨立 lock/image，不得進入 core lock。

## Manifest 與 notice 流程

`upstream-manifest.yaml` 固定 repository、snapshot commit、授權證據 hash、
採用方式與 core 邊界。研究快照存在時，policy checker 會比對 Git HEAD 與
證據檔 SHA-256；任何差異都 fail closed。更新 snapshot 必須在同一變更中
重新完成授權審查並更新 manifest。

只要將 MIT/Apache 等上游程式碼移植、修改、vendor 或散布，就必須：

1. 在 manifest 把 `notice.required` 設為 `true` 並給定唯一 `id`。
2. 在 `THIRD_PARTY_NOTICES.md` 加入同一 `id`、copyright、license、來源
   repository 與 exact commit。
3. 保留上游 LICENSE/NOTICE 要求，並讓 CI policy gate 通過。
4. 另行追蹤資料、模型權重、provider ToS；程式碼授權不代表這些權利已取得。

缺 manifest、未知或衝突授權、缺 required notice、研究證據漂移、禁用 vendor
路徑、core heavy dependency 或禁用 import，全部視為 release blocker。

## Runtime image 授權與 source closure

Container 的 `org.opencontainers.image.licenses` 只描述 Stonks Agent application，
不能代表整個 image 只有 Apache-2.0。正式 release 必須從實際 image digest
產生 package/SBOM inventory，並對每個 runtime component 保存 exact
version、license expression 與 source provenance；unknown license 或 inventory
drift 一律阻擋。

Core image 另遵守 `config/release/core-runtime-legal.json`：

- CPython 必須是 exact 3.12.13 base，並保留完整
  `/usr/local/lib/python3.12/LICENSE.txt` 與固定 SHA-256。完整 Python distribution
  使用 `Python-2.0`；`PSF-2.0` 只代表其中主要條款，不能取代歷史授權全文。
- 修改 CPython stdlib 時必須保存 base/upstream/installed hashes、exact upstream
  commit、source URL、授權與修改摘要。Selective backport 不得宣稱為官方 CPython
  release，也不得宣稱未移植的 upstream 變更或其他 CVE 已涵蓋。
- Alpine `/lib/apk/db/installed` 的 license 欄位只是資訊性 metadata。含
  GPL/LGPL/MPL 等元件時，必須另行完成 applicable notice、source、patch 與 build
  recipe 義務；SBOM 或 aports commit 本身不等於 corresponding source。
- 缺 exact Alpine corresponding-source archive 或 archive 未納入同一 signed
  release manifest 時，正式 release 必須 fail closed。本政策不以外部網站仍可用
  作為已完成散布義務的宣稱。

## 自動化 gate

執行 `uv run python scripts/check_upstream_policy.py`。它固定執行：

- `NO_VENDOR_DEXTER_CODE`
- `NO_VENDOR_AI_TRADER_CODE`
- `NO_OPENBB_IMPORT_IN_CORE`
- core `pyproject.toml` 與 `uv.lock` heavy-dependency 檢查
- manifest schema、snapshot/evidence hash 與 required notice 檢查

CI 另執行 frozen install、dependency audit 與 secret scan。不能用移除 manifest
欄位、略過 research drift 或改名 dependency 的方式繞過 gate。
