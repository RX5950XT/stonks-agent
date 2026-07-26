# P6 handoff evidence index

本索引把 P6.1-P6.11 的 success criteria 對應到 repository 內可重跑證據。CI job 名稱
指 [CI workflow](../../.github/workflows/ci.yml)；正式 release path 見
[release workflow](../../.github/workflows/release.yml)，scheduled supply-chain path 見
[security workflow](../../.github/workflows/security.yml)。GitHub Actions CI與unsigned
supply-chain已在下列exact run驗證；public repository已配置SemVer tag protection、
required-reviewer environment、tag-only deployment policy與immutable releases，但
formal publication成功前仍只宣稱configured。

| Gate | CI job／command | Canonical tests／artifact | 誠實的 external-state boundary |
|---|---|---|---|
| P6.1 | `verify`、`postgres`；`uv run python scripts/verify.py` | `tests/security/test_oidc_auth.py`、`test_auth_dependencies.py`、`test_service_identity_ingress.py` | Pinned與ephemeral OIDC/JWKS已驗；真實外部 IdP 未驗證。 |
| P6.2 | `verify`、`postgres`；repository secret scan | `tests/security/test_secret_consumers.py`、`tests/integration/postgres/test_secret_persistence_guard.py` | Injected cloud client已驗；真實cloud secret manager/workload identity未驗證。 |
| P6.3 | `verify` | `tests/security/test_api_security.py`、`test_api_security_composition.py`、`test_ssrf_endpoint_guard.py` | 單process rate limit已驗；distributed store、trusted proxy、public TLS/HSTS未驗證。 |
| P6.4 | `verify` | `tests/policy/test_observability_infra.py`、OTLP runtime smoke | 本機loopback/tmpfs/nop trace sink已驗；remote backend、multi-host TLS/network policy未驗證。 |
| P6.5 | `verify` | `tests/policy/test_observability_alerts.py`、budget/SLO contract tests | Alert policy已驗；Alertmanager/paging delivery未驗證，文件不宣稱production SLO。 |
| P6.6 | `s3-artifact`、`postgres` | `tests/security/test_s3_artifact_boundary.py`、`tests/integration/test_artifact_store.py` | Digest-pinned SeaweedFS SigV4 smoke已驗；真實cloud IAM/SSE-KMS/Object Lock/vendor parity未驗證。 |
| P6.7 | `core-deployment`、`postgres` | `tests/policy/test_core_deployment.py`、clean migration/restart/replay smoke | CI run `30194459987` attempt 2已驗single-host Docker/CI；orchestrator、public ingress、跨主機mTLS與network policy未驗證。 |
| P6.8 | `supply-chain`、release `build-scan` | `tests/policy/test_release_supply_chain.py`、`unsigned-supply-chain-candidate` | Supply-chain run `30199460724`已驗`v0.1.1` unsigned candidate；release run `30199745730`發布exact image並產生image/GitHub attestations，但五證據closure因互斥CLI flags fail closed，未產生GitHub Release。 |
| P6.9 | `resilience` | `resilience-report-${{ github.run_id }}`、`tests/resilience/`、`scripts/drill_postgres_restore.py` | Run `30194459987` artifact已產生；synthetic single-host restore measurement不是production RTO/RPO SLA，managed DB/cross-region未驗證。 |
| P6.10 | `capacity` | `capacity-report-${{ github.run_id }}`、`tests/performance/`、`scripts/run_capacity_probe.py` | Run `30194459987` artifact已產生；single-host primitives與`probe_process`資源不是production SLA，business API/dispatcher/GPU/VRAM未實測。 |
| P6.11 | `verify`、`postgres`、`optional-integration-manifests`、`supply-chain` | `tests/policy/test_docs_handoff.py`、`tests/policy/test_api_docs.py`、`tests/policy/test_release_supply_chain.py`、`tests/unit/test_release_verifier_final.py`、`tests/security/test_optional_integrations.py`、`optional-profile-smoke-30194459987` | GitHub Actions CI已驗證；bounded matrix為4 actual、5 blocked、1 unsupported與0 canonical side effects。`v0.1.0`／`v0.1.1`都未完成五份formal evidence closure，修正版`v0.1.2`完成前不宣稱externally verified。 |

## GitHub external validation

- Exact commit：`93a1c51c9ec7cb0ef0f57d0931b7b9e524858706`。
- CI run `30194459987` attempt 2全綠；attempt 1在進入runtime前遇一次
  `compose_build_core` transient setup failure，同SHA failed-jobs rerun通過
  `core-deployment`後才執行optional aggregate。
- `optional-profile-smoke-30194459987`（artifact `8629782731`，
  digest `sha256:21c3e085208db9301f57b6be5b3153665ce8ce75b0b04247852b3c5f7b674a98`）
  經frozen Pydantic policy重驗provenance、10-profile exact matrix、readiness invariance及
  zero canonical paper side effects；只有OpenBB、NautilusTrader、LEAN與RD-Agent
  宣稱actual runtime。
- Supply-chain run `30194459983`的`unsigned-supply-chain-candidate`（artifact
  `8629712567`，digest
  `sha256:ba057da4f0964adbc48c51bf875f4118a311fee48d3f35cf2ad72f4d8ee3adf3`）
  下載後由canonical verifier重驗201 artifacts、136,872,645 bytes、exact
  repository/tag/commit/image，且`signatures_verified=false`。
- Protected `v0.1.0` release run `30196542394`通過exact-tag gate、human approval、
  clean build/scan與GHCR publication，image digest為
  `sha256:068e41e374faf4d3752332bbb91f80b62060990c598f6e34062567a55fe122ca`。
  該次`cosign sign --bundle`已產生本地bundle，但Cosign v3不接受後續
  `cosign verify --bundle`，因此簽章job立即失敗，provenance/SBOM attestations、
  signed artifact與GitHub Release皆未產生；`v0.1.0`嘗試已在Cosign v3驗證階段fail closed。
- Protected `v0.1.1` release run `30199745730`通過exact-tag、human approval、
  clean build/scan、GHCR publication、saved bundle／registry雙重驗證及GitHub
  provenance/SBOM attestations；image digest為
  `sha256:dc7566fc578cf49e79a2aadbf316e8e1430b463ec273939db17d97c7f73832c3`。
  Final verifier因GitHub CLI禁止同時使用`--cert-identity`與`--signer-workflow`
  而fail closed，signed artifact與GitHub Release皆未產生。

## Release closure boundary

Repository 能重跑 unsigned candidate、schema/OpenAPI drift、lock、SBOM、CVE/VEX、
license/source、secret、resilience 與 capacity gates。正式 signature/provenance 只能對
registry exact digest 由 GitHub OIDC keyless workflow 產生；在沒有 protected tag、
release environment approval、registry publication 與可核驗 attestation 前，handoff
只保留 configured／未驗證狀態，不能建立 placeholder external evidence。Repository
已公開並啟用active tag ruleset、required reviewer、tag-only environment policy與
immutable releases；失敗的`v0.1.0`／`v0.1.1` tags保持immutable且不得冒充formal，
正式`v0.1.2`成功前仍維持fail closed。

目前remote unsigned candidate為201 artifacts、136,872,645 bytes；deterministic
serial exact綁定image，formal verifier要求SBOM attestation predicate與canonical
signed SBOM exact相同。這是外部unsigned結構與語意證據，不是protected-tag
signature/provenance。

## Operator entrypoints

- [Architecture decisions](../architecture/README.md)
- [API contracts](../api/README.md)
- [Runbooks](../runbooks/README.md)
- [Wire schemas](../../schemas/README.md)
