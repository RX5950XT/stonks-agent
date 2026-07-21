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
6. Formal manifest、verification report、image signature、build provenance 與 SBOM
   attestation全部驗證後，才可建立 GitHub release。

## Fail-closed 條件

- Mutable image/tag、lock drift、unknown license、missing notice/source、secret finding、
  dependency vulnerability或任何未抑制 High/Critical CVE。
- Grype scan descriptor與封存 DB identity不同，或VEX不是exact CVE/product/
  justification binding。
- Bundle traversal、symlink/hardlink、case collision、unknown/duplicate file、size/hash
  drift、非canonical archive或source inventory drift。
- Keyless identity、signature、attestation、protected ref或human authorization缺失。

## 已驗證證據與邊界

- Local unsigned candidate：192 artifacts、136,809,165 bytes，所有semantic gates通過。
- Core inventory：97 packages、865 CycloneDX components；0個未抑制High/Critical。
- Alpine source：37 packages、27 origins、244 files；兩次產生bytes相同。
- Python source：3 exact sdists；三次產生bytes相同。
- OpenBB source：26 members；兩次clean build產生bytes相同。
- 本機不模擬GitHub OIDC、不發布registry/tag，因此正式signature/provenance只由受保護
  release workflow證明。本文件與automated policy不是法律意見。
