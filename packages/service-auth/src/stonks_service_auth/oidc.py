"""Pinned asymmetric OIDC verifier for isolated service ingress."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .authorization import (
    ServiceAccessTarget,
    ServiceIdentity,
    ServicePermission,
    ServicePrincipal,
    ServiceReceiver,
    ServiceResourceKind,
)

type AsymmetricAlgorithm = Literal["RS256", "ES256", "EdDSA"]

_MAX_JWKS_BYTES = 65_536
_MAX_JWKS_KEYS = 20
_MAX_TARGETS = 256
_REQUIRED_CLAIMS = frozenset(
    {
        "iss",
        "sub",
        "aud",
        "exp",
        "iat",
        "nbf",
        "jti",
        "client_id",
        "azp",
        "stonks_service_identity",
        "stonks_receiver",
        "stonks_permission",
        "stonks_attempt_generation",
        "stonks_attempt_nonce_hash",
        "stonks_request_hash",
        "stonks_targets",
    }
)
_ALLOWED_HEADER_FIELDS = frozenset({"alg", "kid", "typ"})


class ServiceOIDCSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str = Field(min_length=12, max_length=512)
    audience: str = Field(min_length=1, max_length=255)
    allowed_algorithms: tuple[AsymmetricAlgorithm, ...] = Field(
        min_length=1, max_length=3
    )
    core_subject: str = Field(min_length=1, max_length=255)
    core_client_id: str = Field(min_length=1, max_length=255)
    receiver: ServiceReceiver
    max_token_lifetime_seconds: int = Field(default=300, ge=60, le=900)
    clock_skew_seconds: int = Field(default=30, ge=0, le=120)

    @model_validator(mode="after")
    def validate_settings(self) -> ServiceOIDCSettings:
        parsed = urlsplit(self.issuer)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or self.issuer.endswith("/")
        ):
            raise ValueError("service OIDC issuer must be exact HTTPS")
        if len(self.allowed_algorithms) != len(set(self.allowed_algorithms)):
            raise ValueError("service OIDC algorithms must be unique")
        for value in (self.audience, self.core_subject, self.core_client_id):
            if not _valid_identifier(value, maximum=255):
                raise ValueError("service OIDC identity is invalid")
        return self


class StaticOIDCServiceAuthenticator:
    """Verify short-lived service JWTs against an immutable mounted JWK set."""

    __slots__ = ("_clock", "_keys", "_settings")

    def __init__(
        self,
        *,
        settings: ServiceOIDCSettings,
        keys: Sequence[Mapping[str, object]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._keys = _validated_keys(keys)
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(issuer={self._settings.issuer!r}, "
            f"audience={self._settings.audience!r})"
        )

    def authenticate(self, authorization: str | None) -> ServicePrincipal | None:
        credential = _bearer_credential(authorization)
        if credential is None:
            return None
        try:
            header = jwt.get_unverified_header(credential)
            key = self._selected_key(header)
            claims = jwt.decode(
                credential,
                key,
                algorithms=list(self._settings.allowed_algorithms),
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                leeway=self._settings.clock_skew_seconds,
                options={
                    "require": list(_REQUIRED_CLAIMS),
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            return self._principal(claims)
        except (jwt.PyJWTError, TypeError, ValueError, ValidationError):
            return None

    def _selected_key(self, header: Mapping[str, Any]) -> jwt.PyJWK:
        if set(header) != _ALLOWED_HEADER_FIELDS or header.get("typ") != "at+jwt":
            raise ValueError("service JWT header is invalid")
        algorithm = header.get("alg")
        kid = header.get("kid")
        if (
            not isinstance(algorithm, str)
            or algorithm not in self._settings.allowed_algorithms
            or not isinstance(kid, str)
        ):
            raise ValueError("service JWT key is invalid")
        key = self._keys.get(kid)
        if key is None or key.algorithm_name != algorithm:
            raise ValueError("service JWT key is invalid")
        return key

    def _principal(self, claims: Mapping[str, Any]) -> ServicePrincipal:
        if set(claims) != _REQUIRED_CLAIMS:
            raise ValueError("service JWT claims are not allowlisted")
        now = int(self._clock().timestamp())
        _validate_registered_claims(claims, self._settings, now)
        subject = _required_string(claims, "sub", maximum=255)
        client_id = _required_string(claims, "client_id", maximum=255)
        identity = _required_string(claims, "stonks_service_identity", maximum=64)
        receiver = _required_string(claims, "stonks_receiver", maximum=64)
        permission = ServicePermission(
            _required_string(claims, "stonks_permission", maximum=64)
        )
        if (
            subject != self._settings.core_subject
            or client_id != self._settings.core_client_id
            or identity != ServiceIdentity.CORE_RUNNER.value
            or receiver != self._settings.receiver.value
        ):
            raise ValueError("service OIDC identity is not assigned")
        targets = _targets(claims.get("stonks_targets"))
        return ServicePrincipal(
            subject=subject,
            identity=ServiceIdentity.CORE_RUNNER,
            receiver=self._settings.receiver,
            permission=permission,
            targets=targets,
            attempt_generation=_required_nonnegative_integer(
                claims, "stonks_attempt_generation"
            ),
            attempt_nonce_hash=_required_sha256(claims, "stonks_attempt_nonce_hash"),
            request_hash=_required_sha256(claims, "stonks_request_hash"),
            token_id=_required_string(claims, "jti", maximum=255),
            issued_at=_required_timestamp(claims, "iat"),
            expires_at=_required_timestamp(claims, "exp"),
        )


def load_static_oidc_service_authenticator(
    environment: Mapping[str, str],
) -> StaticOIDCServiceAuthenticator:
    """Load non-secret OIDC trust metadata and mounted public JWK material."""

    names = {
        "issuer": "STONKS_SERVICE_OIDC_ISSUER",
        "audience": "STONKS_SERVICE_OIDC_AUDIENCE",
        "core_subject": "STONKS_SERVICE_OIDC_CORE_SUBJECT",
        "core_client_id": "STONKS_SERVICE_OIDC_CORE_CLIENT_ID",
        "receiver": "STONKS_SERVICE_OIDC_RECEIVER",
        "jwks_file": "STONKS_SERVICE_OIDC_JWKS_FILE",
    }
    values = {key: environment.get(name, "") for key, name in names.items()}
    if any(not value for value in values.values()):
        raise RuntimeError("service OIDC configuration is incomplete")
    path = Path(values["jwks_file"])
    try:
        if not path.is_absolute():
            raise ValueError("mounted JWK set path is invalid")
        raw = _read_public_jwks_file(path)
        if not 1 <= len(raw) <= _MAX_JWKS_BYTES:
            raise ValueError("mounted JWK set size is invalid")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"keys"}:
            raise ValueError("mounted JWK set is invalid")
        keys = payload["keys"]
        if not isinstance(keys, list):
            raise ValueError("mounted JWK set is invalid")
        algorithms = tuple(
            cast(AsymmetricAlgorithm, value)
            for value in environment.get(
                "STONKS_SERVICE_OIDC_ALGORITHMS", "RS256"
            ).split(",")
        )
        settings = ServiceOIDCSettings(
            issuer=values["issuer"],
            audience=values["audience"],
            core_subject=values["core_subject"],
            core_client_id=values["core_client_id"],
            receiver=ServiceReceiver(values["receiver"]),
            allowed_algorithms=algorithms,
        )
        return StaticOIDCServiceAuthenticator(settings=settings, keys=keys)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as error:
        raise RuntimeError("service OIDC configuration is invalid") from error


def _validated_keys(
    values: Sequence[Mapping[str, object]],
) -> dict[str, jwt.PyJWK]:
    if not 1 <= len(values) <= _MAX_JWKS_KEYS:
        raise ValueError("JWK set size is invalid")
    parsed: dict[str, jwt.PyJWK] = {}
    for raw in values:
        if not isinstance(raw, Mapping):
            raise ValueError("JWK entry is invalid")
        if not _public_jwk_shape_is_valid(raw):
            raise ValueError("JWK public shape is invalid")
        kid = raw.get("kid")
        algorithm = raw.get("alg")
        if (
            raw.get("use") != "sig"
            or raw.get("kty") not in {"RSA", "EC", "OKP"}
            or not isinstance(kid, str)
            or not _valid_identifier(kid, maximum=255)
            or algorithm not in {"RS256", "ES256", "EdDSA"}
            or kid in parsed
        ):
            raise ValueError("JWK identity is invalid")
        key_ops = raw.get("key_ops")
        if key_ops is not None and key_ops != ["verify"]:
            raise ValueError("JWK operations are invalid")
        key = jwt.PyJWK.from_dict(dict(raw), algorithm=algorithm)
        if not _strong_public_key(key, algorithm):
            raise ValueError("JWK public key is invalid")
        parsed[kid] = key
    return parsed


def _public_jwk_shape_is_valid(raw: Mapping[str, object]) -> bool:
    common = {"kty", "kid", "alg", "use", "key_ops"}
    shapes: dict[str, tuple[set[str], set[str]]] = {
        "RSA": ({"kty", "kid", "alg", "use", "n", "e"}, common | {"n", "e"}),
        "EC": (
            {"kty", "kid", "alg", "use", "crv", "x", "y"},
            common | {"crv", "x", "y"},
        ),
        "OKP": (
            {"kty", "kid", "alg", "use", "crv", "x"},
            common | {"crv", "x"},
        ),
    }
    key_type = raw.get("kty")
    if not isinstance(key_type, str):
        return False
    shape = shapes.get(key_type)
    if shape is None:
        return False
    required, allowed = shape
    fields: set[str] = set(raw)
    return required <= fields <= allowed


def _strong_public_key(key: jwt.PyJWK, algorithm: str) -> bool:
    material = key.key
    if algorithm == "RS256":
        return isinstance(material, rsa.RSAPublicKey) and material.key_size >= 2048
    if algorithm == "ES256":
        return isinstance(material, ec.EllipticCurvePublicKey) and isinstance(
            material.curve, ec.SECP256R1
        )
    if algorithm == "EdDSA":
        return isinstance(material, ed25519.Ed25519PublicKey)
    return False


def _read_public_jwks_file(path: Path) -> bytes:
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("mounted JWK set path is invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ) or not stat.S_ISREG(opened.st_mode):
            raise ValueError("mounted JWK set path is invalid")
        return os.read(descriptor, _MAX_JWKS_BYTES + 1)
    finally:
        os.close(descriptor)


def _validate_registered_claims(
    claims: Mapping[str, Any], settings: ServiceOIDCSettings, now: int
) -> None:
    issued_at = _required_timestamp(claims, "iat")
    not_before = _required_timestamp(claims, "nbf")
    expires_at = _required_timestamp(claims, "exp")
    skew = settings.clock_skew_seconds
    if (
        issued_at > now + skew
        or not_before > now + skew
        or expires_at <= now - skew
        or expires_at <= issued_at
        or expires_at - issued_at > settings.max_token_lifetime_seconds
    ):
        raise ValueError("service OIDC token time claims are invalid")
    _required_string(claims, "jti", maximum=255)
    client_id = _required_string(claims, "client_id", maximum=255)
    azp = _required_string(claims, "azp", maximum=255)
    if client_id != azp or client_id != settings.core_client_id:
        raise ValueError("service OIDC authorized party is invalid")
    audience = claims.get("aud")
    if audience != settings.audience and audience != [settings.audience]:
        raise ValueError("service OIDC audience is not exact")


def _targets(value: object) -> frozenset[ServiceAccessTarget]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_TARGETS:
        raise ValueError("service OIDC targets are invalid")
    raw = tuple(value)
    if any(not isinstance(item, str) for item in raw):
        raise ValueError("service OIDC targets are invalid")
    values = tuple(cast(str, item) for item in raw)
    if len(values) != len(set(values)):
        raise ValueError("service OIDC targets are invalid")
    parsed: list[ServiceAccessTarget] = []
    for item in values:
        kind, separator, identifier = item.partition(":")
        if not separator or not identifier:
            raise ValueError("service OIDC target is invalid")
        parsed.append(
            ServiceAccessTarget(
                kind=ServiceResourceKind(kind),
                identifier=identifier,
            )
        )
    return frozenset(parsed)


def _required_string(claims: Mapping[str, Any], name: str, *, maximum: int) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not _valid_identifier(value, maximum=maximum):
        raise ValueError(f"service OIDC {name} claim is invalid")
    return value


def _required_timestamp(claims: Mapping[str, Any], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"service OIDC {name} claim is invalid")
    return value


def _required_nonnegative_integer(claims: Mapping[str, Any], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"service OIDC {name} claim is invalid")
    return value


def _required_sha256(claims: Mapping[str, Any], name: str) -> str:
    value = _required_string(claims, name, maximum=64)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"service OIDC {name} claim is invalid")
    return value


def _bearer_credential(value: str | None) -> str | None:
    if value is None or not value.startswith("Bearer "):
        return None
    credential = value.removeprefix("Bearer ")
    return credential if _valid_identifier(credential, maximum=4096) else None


def _valid_identifier(value: str, *, maximum: int) -> bool:
    return (
        1 <= len(value) <= maximum
        and value.strip() == value
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )
