# P6 handoff evidence index

本索引把 P6.1-P6.11 的 success criteria 對應到 repository 內可重跑證據。CI job 名稱
指 [CI workflow](../../.github/workflows/ci.yml)；正式 release path 見
[release workflow](../../.github/workflows/release.yml)，scheduled supply-chain path 見
[security workflow](../../.github/workflows/security.yml)。Workflow/configured path 不等於
該外部 workflow 曾成功執行。

| Gate | CI job／command | Canonical tests／artifact | 誠實的 external-state boundary |
|---|---|---|---|
| P6.1 | `verify`、`postgres`；`uv run python scripts/verify.py` | `tests/security/test_oidc_auth.py`、`test_auth_dependencies.py`、`test_service_identity_ingress.py` | Pinned與ephemeral OIDC/JWKS已驗；真實外部 IdP 未驗證。 |
| P6.2 | `verify`、`postgres`；repository secret scan | `tests/security/test_secret_consumers.py`、`tests/integration/postgres/test_secret_persistence_guard.py` | Injected cloud client已驗；真實cloud secret manager/workload identity未驗證。 |
| P6.3 | `verify` | `tests/security/test_api_security.py`、`test_api_security_composition.py`、`test_ssrf_endpoint_guard.py` | 單process rate limit已驗；distributed store、trusted proxy、public TLS/HSTS未驗證。 |
| P6.4 | `verify` | `tests/policy/test_observability_infra.py`、OTLP runtime smoke | 本機loopback/tmpfs/nop trace sink已驗；remote backend、multi-host TLS/network policy未驗證。 |
| P6.5 | `verify` | `tests/policy/test_observability_alerts.py`、budget/SLO contract tests | Alert policy已驗；Alertmanager/paging delivery未驗證，文件不宣稱production SLO。 |
| P6.6 | `s3-artifact`、`postgres` | `tests/security/test_s3_artifact_boundary.py`、`tests/integration/test_artifact_store.py` | Digest-pinned SeaweedFS SigV4 smoke已驗；真實cloud IAM/SSE-KMS/Object Lock/vendor parity未驗證。 |
| P6.7 | `core-deployment`、`postgres` | `tests/policy/test_core_deployment.py`、clean migration/restart/replay smoke | Single-host Docker/CI已驗；orchestrator、public ingress、跨主機mTLS與network policy未驗證。 |
| P6.8 | `supply-chain`、release `build-scan` | `tests/policy/test_release_supply_chain.py`、local unsigned candidate verification | protected tag publication: 未驗證；formal keyless signature / provenance: 未產生。 |
| P6.9 | `resilience` | `resilience-report-${{ github.run_id }}`、`tests/resilience/`、`scripts/drill_postgres_restore.py` | Synthetic single-host restore measurement，不是production RTO/RPO SLA；managed DB/cross-region未驗證。 |
| P6.10 | `capacity` | `capacity-report-${{ github.run_id }}`、`tests/performance/`、`scripts/run_capacity_probe.py` | Single-host synthetic/actual repository primitives與`probe_process`資源，不是production SLA；business API/dispatcher/GPU/VRAM未實測。 |
| P6.11 | `verify`、`postgres`、`optional-integration-manifests`、`supply-chain` | `tests/policy/test_docs_handoff.py`、`tests/policy/test_api_docs.py`、`tests/policy/test_release_supply_chain.py`、`tests/unit/test_release_verifier_final.py`、`tests/security/test_optional_integrations.py` | Docs/local links、feature notices、五份formal evidence verifier與10-profile/zero-default matrix是machine checks；protected tag、registry、GitHub OIDC keyless formal evidence仍未外部驗證。 |

## Release closure boundary

Repository 能重跑 unsigned candidate、schema/OpenAPI drift、lock、SBOM、CVE/VEX、
license/source、secret、resilience 與 capacity gates。正式 signature/provenance 只能對
registry exact digest 由 GitHub OIDC keyless workflow 產生；在沒有 protected tag、
release environment approval、registry publication 與可核驗 attestation 前，handoff
只保留 configured／未驗證狀態，不能建立 placeholder external evidence。

目前P6.11 local unsigned candidate為201 artifacts、136,858,939 bytes、97 packages／
865 CycloneDX components、0個未抑制High/Critical；deterministic serial exact綁定
image，formal verifier要求SBOM attestation predicate與canonical signed SBOM exact相同。
這是本機unsigned結構與語意證據，不是protected-tag signature/provenance。

## Operator entrypoints

- [Architecture decisions](../architecture/README.md)
- [API contracts](../api/README.md)
- [Runbooks](../runbooks/README.md)
- [Wire schemas](../../schemas/README.md)
