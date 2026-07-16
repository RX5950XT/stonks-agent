"""Bounded asymmetric OIDC access-token authentication."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal, Protocol, Self, cast
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.config.rbac import RBACPolicy
from stonks_agent.domain.auth import (
    AccessTarget,
    LocalPrincipal,
    PrincipalKind,
    ResourceKind,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.authentication import AuthenticationRequest

type AsymmetricAlgorithm = Literal["RS256", "ES256", "EdDSA"]

_MAX_JWKS_BYTES = 65_536
_MAX_JWKS_KEYS = 20
_MAX_TARGETS = 256
_REQUIRED_CLAIMS = (
    "iss",
    "sub",
    "aud",
    "exp",
    "iat",
    "nbf",
    "jti",
    "client_id",
    "azp",
)
_ALLOWED_HEADER_FIELDS = frozenset({"alg", "kid", "typ"})


class OIDCSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str = Field(min_length=12, max_length=512)
    audience: str = Field(min_length=1, max_length=255)
    jwks_url: str = Field(min_length=12, max_length=1024)
    allowed_algorithms: tuple[AsymmetricAlgorithm, ...] = Field(
        min_length=1, max_length=3
    )
    allowed_client_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    max_token_lifetime_seconds: int = Field(ge=60, le=3600)
    clock_skew_seconds: int = Field(ge=0, le=120)
    jwks_cache_seconds: int = Field(default=300, ge=30, le=3600)
    jwks_min_refresh_seconds: int = Field(default=10, ge=1, le=300)
    jwks_timeout_seconds: float = Field(default=5.0, ge=0.1, le=10.0)

    @model_validator(mode="after")
    def validate_closed_urls_and_values(self) -> Self:
        _validate_https_url(self.issuer, allow_path=True)
        _validate_https_url(self.jwks_url, allow_path=True)
        if self.issuer.endswith("/"):
            raise ValueError("OIDC issuer must use its exact canonical URL")
        if self.jwks_min_refresh_seconds > self.jwks_cache_seconds:
            raise ValueError("OIDC JWKS refresh interval exceeds cache lifetime")
        for values in (self.allowed_algorithms, self.allowed_client_ids):
            if len(values) != len(set(values)):
                raise ValueError("OIDC allowlists must be unique")
        if any(
            not value
            or len(value) > 255
            or value.strip() != value
            or any(character.isspace() for character in value)
            for value in self.allowed_client_ids
        ):
            raise ValueError("OIDC client IDs must be bounded")
        return self


class JWKSetProvider(Protocol):
    def signing_key(self, kid: str, algorithm: str) -> Result[jwt.PyJWK]: ...


class StaticJWKSetProvider:
    """Validated immutable JWK set for tests and offline deployments."""

    __slots__ = ("_keys",)

    def __init__(self, keys: Sequence[Mapping[str, object]]) -> None:
        self._keys = _validated_keys(keys)

    def signing_key(self, kid: str, algorithm: str) -> Result[jwt.PyJWK]:
        key = self._keys.get(kid)
        if key is None or key.algorithm_name != algorithm:
            return _unauthorized()
        return Success(key)


class HTTPSJWKSetProvider:
    """Exact-URL, bounded, fail-closed JWK fetcher with rotation cache."""

    __slots__ = (
        "_client",
        "_clock",
        "_expires_at",
        "_keys",
        "_last_refresh",
        "_last_unknown_kid_refresh",
        "_lock",
        "_settings",
    )

    def __init__(
        self,
        settings: OIDCSettings,
        client: httpx.Client,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._settings = settings
        self._client = client
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0
        self._last_refresh = float("-inf")
        self._last_unknown_kid_refresh = float("-inf")
        self._lock = threading.Lock()

    def signing_key(self, kid: str, algorithm: str) -> Result[jwt.PyJWK]:
        now = self._clock()
        with self._lock:
            cached = self._keys.get(kid)
            if cached is not None and now < self._expires_at:
                return _matching_key(cached, algorithm)
            if self._keys and now < self._expires_at:
                return self._refresh_unknown_kid(kid, algorithm, now)
            if not self._refresh_allowed(now, self._last_refresh):
                return _unauthorized()
            if not self._refresh(now):
                return _unauthorized()
            selected = self._keys.get(kid)
            if selected is None:
                self._last_unknown_kid_refresh = now
                return _unauthorized()
            return _matching_key(selected, algorithm)

    def _refresh_unknown_kid(
        self,
        kid: str,
        algorithm: str,
        now: float,
    ) -> Result[jwt.PyJWK]:
        if not self._refresh_allowed(now, self._last_unknown_kid_refresh):
            return _unauthorized()
        self._last_unknown_kid_refresh = now
        if not self._refresh(now):
            return _unauthorized()
        selected = self._keys.get(kid)
        if selected is None:
            return _unauthorized()
        return _matching_key(selected, algorithm)

    def _refresh_allowed(self, now: float, previous: float) -> bool:
        return now - previous >= self._settings.jwks_min_refresh_seconds

    def _refresh(self, now: float) -> bool:
        self._last_refresh = now
        try:
            with self._client.stream(
                "GET",
                self._settings.jwks_url,
                headers={
                    "Accept": "application/jwk-set+json, application/json",
                    "Accept-Encoding": "identity",
                },
                follow_redirects=False,
                timeout=self._settings.jwks_timeout_seconds,
            ) as response:
                content = _bounded_jwks_body(response)
            if content is None:
                return False
            payload = json.loads(content)
            if not isinstance(payload, dict) or set(payload) != {"keys"}:
                return False
            raw_keys = payload["keys"]
            if not isinstance(raw_keys, list):
                return False
            keys = _validated_keys(raw_keys)
        except (
            httpx.HTTPError,
            jwt.PyJWTError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return False
        self._keys = keys
        self._expires_at = now + self._settings.jwks_cache_seconds
        return True


def _bounded_jwks_body(response: httpx.Response) -> bytes | None:
    if response.status_code != 200:
        return None
    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    content_encoding = response.headers.get("content-encoding", "identity").lower()
    if (
        content_type not in {"application/json", "application/jwk-set+json"}
        or content_encoding != "identity"
    ):
        return None
    declared_size = response.headers.get("content-length")
    if declared_size is not None:
        try:
            size = int(declared_size)
        except ValueError:
            return None
        if not 0 <= size <= _MAX_JWKS_BYTES:
            return None
    body = bytearray()
    chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
    for chunk in chunks:
        if len(chunk) > _MAX_JWKS_BYTES - len(body):
            return None
        body.extend(chunk)
    return bytes(body)


class OIDCAuthenticator:
    """Verify one RFC 9068-style access token and map server-side authority."""

    __slots__ = ("_clock", "_keys", "_policy", "_settings")

    def __init__(
        self,
        *,
        settings: OIDCSettings,
        policy: RBACPolicy,
        keys: JWKSetProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._keys = keys
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(issuer={self._settings.issuer!r}, "
            f"audience={self._settings.audience!r})"
        )

    def authenticate(
        self,
        request: AuthenticationRequest,
    ) -> Result[LocalPrincipal]:
        credential = _bearer_credential(request.authorization)
        if credential is None:
            return _unauthorized()
        try:
            header = jwt.get_unverified_header(credential)
            selected = self._selected_key(header)
            if isinstance(selected, Failure):
                return selected
            claims = jwt.decode(
                credential,
                selected.value,
                algorithms=list(self._settings.allowed_algorithms),
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                leeway=self._settings.clock_skew_seconds,
                options={"require": list(_REQUIRED_CLAIMS)},
            )
            principal = self._principal(claims)
        except (jwt.PyJWTError, TypeError, ValueError, ValidationError):
            return _unauthorized()
        return Success(principal)

    def _selected_key(self, header: Mapping[str, Any]) -> Result[jwt.PyJWK]:
        if set(header) != _ALLOWED_HEADER_FIELDS or header.get("typ") != "at+jwt":
            return _unauthorized()
        algorithm = header.get("alg")
        kid = header.get("kid")
        if (
            not isinstance(algorithm, str)
            or algorithm not in self._settings.allowed_algorithms
            or not isinstance(kid, str)
            or not _valid_identifier(kid, maximum=255)
        ):
            return _unauthorized()
        return self._keys.signing_key(kid, algorithm)

    def _principal(self, claims: Mapping[str, Any]) -> LocalPrincipal:
        now = int(self._clock().timestamp())
        _validate_registered_claims(claims, self._settings, now)
        subject = _required_string(claims, "sub", maximum=255)
        client_id = _required_string(claims, "client_id", maximum=255)
        role_values = _string_list(
            claims.get(self._policy.claims.roles),
            maximum=5,
            allow_empty=True,
        )
        targets = _targets(claims.get(self._policy.claims.targets))
        asserted_service = claims.get(self._policy.claims.service_identity)
        if asserted_service is None:
            roles = self._policy.roles_for_claim_values(role_values)
            if roles is None:
                raise ValueError("OIDC role claims are not allowlisted")
            return LocalPrincipal(subject=subject, roles=roles, targets=targets)
        if role_values or not isinstance(asserted_service, str):
            raise ValueError("OIDC service claims are ambiguous")
        service = self._policy.service_for_claims(
            subject=subject,
            client_id=client_id,
            asserted_identity=asserted_service,
        )
        if service is None or not targets:
            raise ValueError("OIDC service identity is not assigned")
        return LocalPrincipal(
            subject=subject,
            principal_kind=PrincipalKind.SERVICE,
            service_identity=service,
            targets=targets,
        )


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
        parsed_key = jwt.PyJWK.from_dict(dict(raw), algorithm=cast(str, algorithm))
        if raw.get("kty") == "RSA" and getattr(parsed_key.key, "key_size", 0) < 2048:
            raise ValueError("JWK RSA key is too weak")
        parsed[kid] = parsed_key
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
    fields = set(raw)
    return required <= fields <= allowed


def _matching_key(key: jwt.PyJWK, algorithm: str) -> Result[jwt.PyJWK]:
    return Success(key) if key.algorithm_name == algorithm else _unauthorized()


def _validate_registered_claims(
    claims: Mapping[str, Any],
    settings: OIDCSettings,
    now: int,
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
        raise ValueError("OIDC token time claims are invalid")
    _required_string(claims, "jti", maximum=255)
    client_id = _required_string(claims, "client_id", maximum=255)
    azp = _required_string(claims, "azp", maximum=255)
    if client_id != azp or client_id not in settings.allowed_client_ids:
        raise ValueError("OIDC authorized party is invalid")
    audience = claims.get("aud")
    if audience != settings.audience and audience != [settings.audience]:
        raise ValueError("OIDC audience set is not exact")


def _targets(value: object) -> frozenset[AccessTarget]:
    raw = _string_list(value, maximum=_MAX_TARGETS, allow_empty=True)
    parsed: list[AccessTarget] = []
    for item in raw:
        kind, separator, identifier = item.partition(":")
        if not separator or not identifier:
            raise ValueError("OIDC target claim is invalid")
        parsed.append(AccessTarget(kind=ResourceKind(kind), identifier=identifier))
    if len(parsed) != len(set(parsed)):
        raise ValueError("OIDC target claims must be unique")
    return frozenset(parsed)


def _string_list(
    value: object,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or ((not allow_empty) and not value):
        raise ValueError("OIDC list claim is invalid")
    if len(value) > maximum or any(
        not isinstance(item, str) or not _valid_identifier(item, maximum=512)
        for item in value
    ):
        raise ValueError("OIDC list claim is invalid")
    result = tuple(cast(str, item) for item in value)
    if len(result) != len(set(result)):
        raise ValueError("OIDC list claims must be unique")
    return result


def _required_string(claims: Mapping[str, Any], name: str, *, maximum: int) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not _valid_identifier(value, maximum=maximum):
        raise ValueError(f"OIDC {name} claim is invalid")
    return value


def _required_timestamp(claims: Mapping[str, Any], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"OIDC {name} claim is invalid")
    return value


def _valid_identifier(value: str, *, maximum: int) -> bool:
    return (
        1 <= len(value) <= maximum
        and value.strip() == value
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _bearer_credential(value: str | None) -> str | None:
    if value is None or not value.startswith("Bearer "):
        return None
    credential = value.removeprefix("Bearer ")
    if not _valid_identifier(credential, maximum=4096):
        return None
    return credential


def _validate_https_url(value: str, *, allow_path: bool) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or ((not allow_path) and parsed.path not in {"", "/"})
    ):
        raise ValueError("OIDC URL must be exact HTTPS")


def _unauthorized() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.UNAUTHORIZED,
            message="Authentication failed",
        )
    )
