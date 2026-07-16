"""Shared fail-closed secret isolation for remote worker processes."""

from __future__ import annotations

from collections.abc import Mapping

_FORBIDDEN_EXACT = frozenset(
    {
        "DATABASE_URL",
        "STONKS_DATABASE_URL",
        "STONKS_TEST_DATABASE_URL",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_DB",
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGPASSWORD",
        "REDIS_URL",
        "BROKER_URL",
        "QUEUE_URL",
        "EXECUTION_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "FINANCIAL_DATASETS_API_KEY",
        "AI_TRADER_API_KEY",
    }
)
_FORBIDDEN_PREFIXES = (
    "STONKS_SERVICE_SIGNING_",
    "STONKS_SERVICE_AUDIENCE_",
    "STONKS_SERVICE_ISSUER",
    "STONKS_EXECUTION_",
    "STONKS_DATABASE_",
    "STONKS_BROKER_",
    "STONKS_QUEUE_",
)


def validate_isolated_runtime_environment(environment: Mapping[str, str]) -> None:
    forbidden = tuple(
        name
        for name, value in environment.items()
        if value
        and (
            name.upper() in _FORBIDDEN_EXACT
            or name.upper().startswith(_FORBIDDEN_PREFIXES)
        )
    )
    if forbidden:
        raise RuntimeError("forbidden credential entered isolated runtime")
