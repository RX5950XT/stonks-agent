from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
JWKS_CONTAINER_PATH = "/run/secrets/stonks-service-jwks.json"
COMMON_OIDC_ENVIRONMENT = {
    "STONKS_SERVICE_OIDC_ISSUER": "${STONKS_SERVICE_OIDC_ISSUER:?required}",
    "STONKS_SERVICE_OIDC_CORE_SUBJECT": (
        "${STONKS_SERVICE_OIDC_CORE_SUBJECT:?required}"
    ),
    "STONKS_SERVICE_OIDC_CORE_CLIENT_ID": (
        "${STONKS_SERVICE_OIDC_CORE_CLIENT_ID:?required}"
    ),
    "STONKS_SERVICE_OIDC_ALGORITHMS": "RS256",
    "STONKS_SERVICE_OIDC_JWKS_FILE": JWKS_CONTAINER_PATH,
}
RUNTIMES = (
    ("compose.kronos.yaml", "kronos-cpu", "kronos", "KRONOS"),
    ("compose.kronos.yaml", "kronos-cuda", "kronos", "KRONOS"),
    (
        "compose.tradingagents.yaml",
        "tradingagents-paper",
        "tradingagents",
        "TRADINGAGENTS",
    ),
    (
        "compose.tradingagents.yaml",
        "tradingagents-backtest",
        "tradingagents",
        "TRADINGAGENTS",
    ),
    (
        "compose.tradingagents.yaml",
        "tradingagents-production",
        "tradingagents",
        "TRADINGAGENTS",
    ),
    ("compose.quant-lab.yaml", "quant-lab", "quant_lab", "QUANT_LAB"),
    ("compose.nautilus.yaml", "nautilus", "nautilus", "NAUTILUS"),
    ("compose.lean.yaml", "lean", "lean", "LEAN"),
)


@pytest.mark.parametrize(
    ("compose_name", "service_name", "receiver", "audience_prefix"), RUNTIMES
)
def test_isolated_runtime_mounts_exact_public_oidc_trust(
    compose_name: str,
    service_name: str,
    receiver: str,
    audience_prefix: str,
) -> None:
    compose = yaml.safe_load((ROOT / "infra" / compose_name).read_text("utf-8"))
    service = compose["services"][service_name]
    environment = service["environment"]

    assert COMMON_OIDC_ENVIRONMENT.items() <= environment.items()
    assert environment["STONKS_SERVICE_OIDC_RECEIVER"] == receiver
    assert environment["STONKS_SERVICE_OIDC_AUDIENCE"] == (
        f"${{STONKS_{audience_prefix}_SERVICE_OIDC_AUDIENCE:?required}}"
    )
    assert (
        f"${{STONKS_SERVICE_OIDC_JWKS_HOST_FILE:?required}}:{JWKS_CONTAINER_PATH}:ro"
    ) in service["volumes"]


@pytest.mark.parametrize(
    ("compose_name", "service_name", "_receiver", "_audience_prefix"), RUNTIMES
)
def test_isolated_runtime_manifest_has_no_privileged_credentials(
    compose_name: str,
    service_name: str,
    _receiver: str,
    _audience_prefix: str,
) -> None:
    compose = yaml.safe_load((ROOT / "infra" / compose_name).read_text("utf-8"))
    environment = compose["services"][service_name]["environment"]
    forbidden = (
        "DATABASE",
        "POSTGRES",
        "REDIS",
        "BROKER",
        "QUEUE",
        "EXECUTION",
        "PROVIDER",
        "SIGNING",
        "PRIVATE",
        "SERVICE_TOKEN",
        "API_KEY",
    )

    assert not any(
        marker in name.upper() for name in environment for marker in forbidden
    )
