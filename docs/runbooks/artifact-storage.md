# Artifact storage 操作手冊

## 安全邊界

- Production 只接受 HTTPS、固定 origin/bucket/prefix 與 workload-identity
  credential provider；httpx 關閉 ambient proxy、`.netrc` 與 redirect，DNS
  全部解析結果必須是 public address 並在 process lifetime 內固定。
- Startup 必須以 signed API 實際確認 bucket versioning=`Enabled` 與
  Object Lock=`Enabled`；設定檔中的布林值不能取代 provider preflight。
- Canonical key 固定為
  `<prefix>/objects/<hash-prefix>/<sha256>` 與
  `<prefix>/manifests/<hash-prefix>/<sha256>.json`。Object 驗證完成後才以
  conditional PUT 發布 manifest。
- `artifact://sha256/<hash>` 仍是 canonical reference。Presigned URL 是
  excluded `SecretStr` capability，只允許單一 finalized object、GET 與最長
  900 秒，不能寫入 DB、event 或 log。

## Retention 與 legal hold

- Finalize 依 sensitivity 套用 `GOVERNANCE`/`COMPLIANCE` retain-until，
  並重驗 SSE algorithm、KMS key、retention mode/time、size、metadata 與
  SHA-256。
- Operator use case 只允許延長 retention 或把 legal hold 設為 `ON`；不提供
  縮短、解除 hold 或 `BypassGovernanceRetention`。
- 每次外部 mutation 前先寫 PostgreSQL requested event；完成或失敗再寫
  terminal event。Append-only chain 綁定 command hash 與不洩漏 version ID
  的 result hash。

## Orphan GC 與 restore

- 任何 current 或歷史 finalized manifest version 都代表 canonical artifact，
  永不由 GC 實體刪除。Canonical deletion/crypto-shred 必須另立 RFC。
- GC 只掃描 exact objects prefix；只刪除超過 cutoff、沒有任何 finalized
  manifest version、legal hold 關閉且 retention 已到期的 exact version ID。
  Unknown/list/head/delete error 一律保留並回報 `retained_unknown`。
- Restore 只移除 object 與 manifest 各自最新的 exact delete marker，最多兩個；
  不可上傳新 bytes 偽裝還原。移除後必須重新驗 manifest、size、SSE 與 hash。

## 驗證與事故處理

1. 先停用 artifact finalize/maintenance job ingress，不改動既有 canonical bytes。
2. 檢查 structured error、operation ID 與 append-only audit chain；不可把
   provider raw error、presigned URL 或 credential 寫入 ticket/log。
3. Bucket control、retention 或 checksum 無法確認時維持 fail closed；不要切換
   anonymous/default credential chain或 governance bypass。
4. 以 `uv run --frozen python -m pytest tests/adapters/artifacts tests/policy/test_s3_artifact_infra.py`
   重驗 fake failure matrix 與 pinned runtime smoke。

`infra/compose.artifacts.yaml` 使用 SeaweedFS 4.34 exact digest、non-root、
read-only、tmpfs、loopback ingress、telemetry disabled 與 runtime-generated
credentials。此 smoke 只證明 SigV4、conditional PUT、checksum/metadata round
trip、AES256 response identity與presigned GET；尚未連真實 cloud IAM/KMS，
也不驗證 SeaweedFS 的 Object Lock fidelity或跨 vendor 完整相容性。
