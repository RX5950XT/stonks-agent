# P6 handoff evidence index

本索引把 P6.1-P6.11 的 success criteria 對應到 repository 內可重跑證據。CI job 名稱
指 [CI workflow](../../.github/workflows/ci.yml)；正式 release path 見
[release workflow](../../.github/workflows/release.yml)，scheduled supply-chain path 見
[security workflow](../../.github/workflows/security.yml)。GitHub Actions CI、unsigned
supply-chain、bounded optional matrix與formal `v0.1.2` keyless release已在下列exact
run驗證；public repository的SemVer tag protection、required-reviewer environment、
tag-only deployment policy與immutable releases均保持啟用。

| Gate | CI job／command | Canonical tests／artifact | 誠實的 external-state boundary |
|---|---|---|---|
| P6.1 | `verify`、`postgres`；`uv run python scripts/verify.py` | `tests/security/test_oidc_auth.py`、`test_auth_dependencies.py`、`test_service_identity_ingress.py` | Pinned與ephemeral OIDC/JWKS已驗；真實外部 IdP 未驗證。 |
| P6.2 | `verify`、`postgres`；repository secret scan | `tests/security/test_secret_consumers.py`、`tests/integration/postgres/test_secret_persistence_guard.py` | Injected cloud client已驗；真實cloud secret manager/workload identity未驗證。 |
| P6.3 | `verify` | `tests/security/test_api_security.py`、`test_api_security_composition.py`、`test_ssrf_endpoint_guard.py` | 單process rate limit已驗；distributed store、trusted proxy、public TLS/HSTS未驗證。 |
| P6.4 | `verify` | `tests/policy/test_observability_infra.py`、OTLP runtime smoke | 本機loopback/tmpfs/nop trace sink已驗；remote backend、multi-host TLS/network policy未驗證。 |
| P6.5 | `verify` | `tests/policy/test_observability_alerts.py`、budget/SLO contract tests | Alert policy已驗；Alertmanager/paging delivery未驗證，文件不宣稱production SLO。 |
| P6.6 | `s3-artifact`、`postgres` | `tests/security/test_s3_artifact_boundary.py`、`tests/integration/test_artifact_store.py` | Digest-pinned SeaweedFS SigV4 smoke已驗；真實cloud IAM/SSE-KMS/Object Lock/vendor parity未驗證。 |
| P6.7 | `core-deployment`、`postgres` | `tests/policy/test_core_deployment.py`、clean migration/restart/replay smoke | CI run `30194459987` attempt 2已驗single-host Docker/CI；orchestrator、public ingress、跨主機mTLS與network policy未驗證。 |
| P6.8 | `supply-chain`、release `build-scan` | `tests/policy/test_release_supply_chain.py`、`unsigned-supply-chain-candidate` | Supply-chain run `30200612154`與release run `30200908948`全綠；formal `v0.1.2` image、五份evidence、immutable Release與assets均已重驗。 |
| P6.9 | `resilience` | `resilience-report-${{ github.run_id }}`、`tests/resilience/`、`scripts/drill_postgres_restore.py` | Run `30194459987` artifact已產生；synthetic single-host restore measurement不是production RTO/RPO SLA，managed DB/cross-region未驗證。 |
| P6.10 | `capacity` | `capacity-report-${{ github.run_id }}`、`tests/performance/`、`scripts/run_capacity_probe.py` | Run `30194459987` artifact已產生；single-host primitives與`probe_process`資源不是production SLA，business API/dispatcher/GPU/VRAM未實測。 |
| P6.11 | `verify`、`postgres`、`optional-integration-manifests`、`supply-chain` | `tests/policy/test_docs_handoff.py`、`tests/policy/test_api_docs.py`、`tests/policy/test_release_supply_chain.py`、`tests/unit/test_release_verifier_final.py`、`tests/security/test_optional_integrations.py`、`optional-profile-smoke-30194459987` | CI run `30200612158`為13/13 success；bounded matrix為4 actual、5 blocked、1 unsupported與0 canonical side effects；formal `v0.1.2`五證據closure為externally verified。 |

## GitHub external validation

- Formal exact commit：`5e9c2973b782cd1bd7274e6e6852cbe1df08a4f9`。
- CI run `30200612158`的13個jobs全數成功；Supply-chain run `30200612154`的
  `unsigned-supply-chain-candidate` artifact `8631582545`為134,629,231 bytes。
- `optional-profile-smoke-30194459987`（artifact `8629782731`，
  digest `sha256:21c3e085208db9301f57b6be5b3153665ce8ce75b0b04247852b3c5f7b674a98`）
  經frozen Pydantic policy重驗provenance、10-profile exact matrix、readiness invariance及
  zero canonical paper side effects；只有OpenBB、NautilusTrader、LEAN與RD-Agent
  宣稱actual runtime。
- Earlier Supply-chain run `30194459983`的`unsigned-supply-chain-candidate`（artifact
  `8629712567`）曾由canonical verifier重驗201 artifacts、136,872,645 bytes且
  `signatures_verified=false`；只保留為pre-public歷史。
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
- Protected `v0.1.2` release run `30200908948`的六個jobs全數成功；signed artifact
  `8631709866`由fixed Cosign v3.0.6 canonical verifier重驗201 artifacts、
  136,874,188 bytes與五份Sigstore evidence，`evidence_count=5`、
  `signatures_verified=true`、`status=passed`。Exact image為
  `sha256:9c61a2d5dd59d07d30318b483a7a205ac8af394236662b45021574e42ff19976`；
  registry signature/sign-v1 attestation、GitHub SLSA provenance/CycloneDX SBOM及
  [immutable Release](https://github.com/RX5950XT/stonks-agent/releases/tag/v0.1.2)
  均綁定同一workflow/tag/commit與GitHub-hosted runner；正式archive與workflow
  artifact各208 files且hash-identical。
- Release assets `stonks-agent-v0.1.2.tar.gz`與
  `stonks-agent-v0.1.2.tar.gz.sha256`已由`gh release verify-asset`通過；SHA-256分別為
  `823dc70999557c770e7c1cd5c7857cf0d9e155147743435a5013a38a98b85434`及
  `8015b3e11470987b6760f480bd208f9c84c08f476205fde0276ff3b2ad65570e`。

## Release closure boundary

Repository 能重跑 unsigned candidate、schema/OpenAPI drift、lock、SBOM、CVE/VEX、
license/source、secret、resilience 與 capacity gates。正式 signature/provenance 只對
registry exact digest由GitHub OIDC keyless workflow產生；`v0.1.2`已在protected tag、
三次required-reviewer environment authorization、registry publication、五證據closure
及immutable Release asset attestations後升為formal verified。失敗的`v0.1.0`／
`v0.1.1` tags仍保持immutable，不得重指或冒充formal。

Formal verifier要求deterministic serial exact綁定image，且SBOM attestation predicate
與canonical signed SBOM完全相同。Signed bundle與短期Actions artifact只作可重驗證據；
持久下載入口是immutable Release。此closure只證明paper-only release supply chain，
不代表live trading、production SLA或未列出的外部runtime整合。

## Operator entrypoints

- [Architecture decisions](../architecture/README.md)
- [API contracts](../api/README.md)
- [Runbooks](../runbooks/README.md)
- [Wire schemas](../../schemas/README.md)
