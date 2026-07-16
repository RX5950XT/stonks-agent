"""Explicit environment gate for trusted local database CLI principals."""

from __future__ import annotations

import os

import typer

from stonks_agent.domain.auth import AccessTarget, LocalPrincipal, Role

_LOCAL_ENVIRONMENTS = frozenset({"local", "development", "test"})


def local_cli_principal(
    *,
    subject: str,
    role: Role,
    targets: frozenset[AccessTarget] = frozenset(),
) -> LocalPrincipal:
    environment = os.environ.get("STONKS_ENVIRONMENT")
    if environment not in _LOCAL_ENVIRONMENTS:
        raise typer.BadParameter(
            "local database CLI is unavailable in this environment"
        )
    return LocalPrincipal(
        subject=subject,
        roles=frozenset({role}),
        targets=targets,
    )
