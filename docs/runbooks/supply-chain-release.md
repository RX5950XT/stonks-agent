# Supply-chain release 操作與限制

## 不可繞過的流程

1. `security.yml` 與 tag-only `release.yml` 先執行 frozen lock、upstream、secret、
   dependency、SBOM/license 與 Grype/VEX gates。
2. Core image 必須以 exact image ID 建立 canonical CycloneDX inventory；Linux runtime
   只接受 source-built `psycopg-c` 與 Alpine `libpq`。
3. Bundle 必須包含 OpenBB、Alpine 37-package closure，以及 certifi、psycopg、
   psycopg-c exact sdists。Verifier 會重驗 archive metadata、manifest、path、size、
   hash、package/lock binding 與總量限制。
4. Release job 必須在 `docker push` 前建立並通過 `unsigned-candidate`；這只證明
   bundle 結構與內容，不能宣稱 signature 或 provenance。
5. 只有 protected tag、`release` environment 核准後的 workflow 可發布 image；
   Cosign 只簽 registry 回傳的 exact digest，並固定 GitHub OIDC issuer、repository、
   workflow、ref、commit 與 trigger identity。
6. `config/features.yaml` 中七個有 supply-chain contract 的 integrations，其八個
   `notice_paths`、root notice identity 與 `execution_authority=false` 都必須進入
   signed payload；漏件、重複 path、未登錄 notice 或 authority drift 立即失敗。
7. GitHub provenance 與 SBOM bundles 落地後，`verify-final` 會重新執行完整 payload
   semantic gates，並對 image、manifest、verification report、provenance 與 SBOM
   五份 Sigstore evidence 做 closed-tree 驗證。Cosign/GitHub CLI 同時固定 exact
   repository、workflow、tag ref、commit、registry digest、OIDC issuer 與 predicate。
8. 只有上述 final verifier 通過後，bundle 才可傳給 GitHub release job。
9. GitHub release job只可恢復同tag的既有draft；若release已發布，重跑只能重驗
   immutable release與兩個asset attestations，不得重新建立或覆寫publication。

## Fail-closed 條件

- Mutable image/tag、lock drift、unknown license、missing notice/source、secret finding、
  dependency vulnerability或任何未抑制 High/Critical CVE。
- Grype scan descriptor與封存 DB identity不同，或VEX不是exact CVE/product/
  justification binding。
- Bundle traversal、symlink/hardlink、case collision、unknown/duplicate file、size/hash
  drift、非canonical archive或source inventory drift。
- Keyless identity、五份 evidence、signature、attestation、protected ref 或 human
  authorization 缺失；`signatures/` 出現 symlink、額外或不規則檔案也一律拒絕。

## 已驗證證據與邊界

- P6.11 local unsigned candidate以目前worktree驗證201 artifacts、136,858,939 bytes
  與全部semantic gates；canonical CycloneDX serial deterministic綁定exact image，
  final verifier另要求GitHub SBOM predicate與signed canonical SBOM exact相同。
- Core inventory：97 packages、865 CycloneDX components；0個未抑制High/Critical。
- Alpine source：37 packages、27 origins、244 files；兩次產生bytes相同。
- Python source：3 exact sdists；三次產生bytes相同。
- OpenBB source：26 members；兩次clean build產生bytes相同。
- 本機只執行 unsigned candidate 與 formal verifier 的 fixture/negative tests，不模擬
  GitHub OIDC、不發布 registry/tag，也不宣稱 protected-tag workflow 已執行。正式
  signature/provenance 只能由受保護 release workflow 的五份 exact evidence 證明。
  本文件與 automated policy 不是法律意見。
