# ADR-0003：Unsigned candidate 與 keyless release trust

- 狀態：Accepted
- 日期：2026-07-22
- 決策範圍：release candidate、registry digest、signature/attestation

## Context

本機 build 能驗證 bundle 完整性、SBOM、CVE/VEX、license/source closure 與 image
policy，但沒有 GitHub OIDC identity，也不能誠實產生正式 registry signature 或
provenance。若先簽 local image 再 publish，publication 後的 registry digest 也可能與
驗證對象不同。

## Decision

Release 分成兩個 trust level：

1. Unsigned candidate：任何乾淨環境都可建立；先驗 paper-only identity、frozen locks、
   canonical manifest、SBOM、CVE/VEX、licenses/notices、OpenBB/Alpine/Python exact
   corresponding source 與 secret/upstream gates。
2. Formal keyless release：只能由 protected exact tag 與核准的 GitHub `release`
   environment 執行。先 publish image，取得 registry exact digest，再用 GitHub OIDC
   keyless Cosign 簽 digest並產生 provenance/SBOM attestations，最後以 exact workflow
   issuer/identity 重驗。

Local unsigned candidate 絕不能標示 signed、provenance verified 或 released；workflow
存在也只代表 configured path，不是 externally verified publication。

## Consequences

- Signature/attestation 一律綁 registry digest，不綁 mutable tag 或 local image ID。
- Release environment approval 是外部控制，repository policy 無法替代其設定證據。
- Protected tag publication、formal keyless signature/provenance 目前維持未驗證／未產生。
- 發布前任何 manifest、source、license、CVE、secret 或 identity drift 都 fail closed。

## Repository evidence

[Supply-chain release runbook](../../runbooks/supply-chain-release.md) 與
[P6 evidence index](../../verification/p6-handoff-evidence.md) 區分 repository gate 與
external-state evidence。
