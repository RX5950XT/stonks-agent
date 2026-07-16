"""Closed RBAC policy matching the domain authorization boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.domain.auth import (
    Permission,
    Role,
    ServiceIdentity,
    role_permissions,
    service_permissions,
)
from stonks_agent.domain.errors import ErrorCode, StructuredError


class RolePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Role
    permissions: tuple[Permission, ...] = Field(min_length=1, max_length=8)
    claim_values: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_exact_permissions(self) -> Self:
        _validate_claim_values(self.claim_values)
        expected = tuple(
            permission
            for permission in Permission
            if permission in role_permissions(self.role)
        )
        if self.permissions != expected:
            raise ValueError("role permission policy drifted")
        return self


class ServiceIdentityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: ServiceIdentity
    permissions: tuple[Permission, ...] = Field(min_length=1, max_length=4)
    subjects: tuple[str, ...] = Field(min_length=1, max_length=8)
    client_ids: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_exact_permissions(self) -> Self:
        _validate_claim_values(self.subjects)
        _validate_claim_values(self.client_ids)
        expected = tuple(
            permission
            for permission in Permission
            if permission in service_permissions(self.identity)
        )
        if self.permissions != expected:
            raise ValueError("service permission policy drifted")
        return self


class RBACClaimPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: Literal["stonks_roles"]
    targets: Literal["stonks_targets"]
    service_identity: Literal["stonks_service_identity"]


class RBACPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    roles: tuple[RolePolicy, ...] = Field(min_length=5, max_length=5)
    service_identities: tuple[ServiceIdentityPolicy, ...] = Field(
        min_length=3, max_length=3
    )
    claims: RBACClaimPolicy
    admin_all_targets: Literal[True]

    @model_validator(mode="after")
    def validate_complete_stable_policy(self) -> Self:
        if tuple(item.role for item in self.roles) != tuple(Role):
            raise ValueError("role policy is incomplete or reordered")
        if tuple(item.identity for item in self.service_identities) != tuple(
            ServiceIdentity
        ):
            raise ValueError("service identity policy is incomplete or reordered")
        return self

    def roles_for_claim_values(self, values: tuple[str, ...]) -> frozenset[Role] | None:
        if not values or len(values) != len(set(values)):
            return None
        mapping = {
            claim: item.role for item in self.roles for claim in item.claim_values
        }
        if len(mapping) != sum(len(item.claim_values) for item in self.roles):
            return None
        try:
            return frozenset(mapping[value] for value in values)
        except KeyError:
            return None

    def service_for_claims(
        self,
        *,
        subject: str,
        client_id: str,
        asserted_identity: str,
    ) -> ServiceIdentity | None:
        matches = tuple(
            item.identity
            for item in self.service_identities
            if subject in item.subjects
            and client_id in item.client_ids
            and asserted_identity == item.identity.value
        )
        return matches[0] if len(matches) == 1 else None


class RBACPolicyLoadError(RuntimeError):
    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__("RBAC policy configuration is invalid")


def load_rbac_policy(path: Path) -> RBACPolicy:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return RBACPolicy.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise RBACPolicyLoadError(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="RBAC policy configuration is invalid",
                details={"file": path.name},
            )
        ) from error


def _validate_claim_values(values: tuple[str, ...]) -> None:
    if len(values) != len(set(values)) or any(
        not 1 <= len(value) <= 255
        or value.strip() != value
        or any(character.isspace() for character in value)
        for value in values
    ):
        raise ValueError("identity claim values must be bounded and unique")
