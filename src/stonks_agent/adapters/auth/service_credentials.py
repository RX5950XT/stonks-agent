"""Short-lived RS256 service credentials minted by the trusted core runner."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretBytes,
    SecretStr,
    model_validator,
)

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.service_credentials import (
    ServiceBearerCredential,
    ServiceCredentialProvider,
    ServiceCredentialRequest,
    ServiceReceiver,
)

_MAX_PRIVATE_KEY_BYTES = 16_384


class ReceiverAudience(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receiver: ServiceReceiver
    audience: str = Field(min_length=1, max_length=255)


class ServiceIssuerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str = Field(min_length=12, max_length=512)
    subject: str = Field(min_length=1, max_length=255)
    client_id: str = Field(min_length=1, max_length=255)
    key_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+=-]{0,254}$",
    )
    audiences: tuple[ReceiverAudience, ...] = Field(min_length=6, max_length=6)
    max_token_lifetime_seconds: int = Field(default=120, ge=30, le=300)

    @model_validator(mode="after")
    def validate_settings(self) -> Self:
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
            raise ValueError("service issuer must be exact HTTPS")
        if tuple(item.receiver for item in self.audiences) != tuple(ServiceReceiver):
            raise ValueError("service receiver audiences are incomplete")
        values = tuple(item.audience for item in self.audiences)
        if len(values) != len(set(values)) or any(
            not 1 <= len(value) <= 255
            or value.strip() != value
            or any(character.isspace() for character in value)
            for value in values
        ):
            raise ValueError("service receiver audiences must be bounded and unique")
        for value in (self.subject, self.client_id):
            if not _valid_identifier(value, maximum=255):
                raise ValueError("service issuer identity is invalid")
        return self

    def audience_for(self, receiver: ServiceReceiver) -> str:
        return next(
            item.audience for item in self.audiences if item.receiver is receiver
        )


class RS256ServiceCredentialProvider(ServiceCredentialProvider):
    """Sign one receiver-specific, attempt-bound access token per dispatch."""

    __slots__ = ("_clock", "_key", "_settings", "_token_id")

    def __init__(
        self,
        *,
        settings: ServiceIssuerSettings,
        private_key_pem: SecretBytes,
        clock: Callable[[], datetime] | None = None,
        token_id: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._key = _load_private_key(private_key_pem)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_id = token_id or (lambda: str(uuid4()))

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(issuer={self._settings.issuer!r}, "
            f"client_id={self._settings.client_id!r})"
        )

    def issue(
        self,
        request: ServiceCredentialRequest,
    ) -> Result[ServiceBearerCredential]:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return _failure("Service credential clock is invalid")
        now = now.astimezone(UTC)
        expires_at = min(
            now + timedelta(seconds=self._settings.max_token_lifetime_seconds),
            request.expires_no_later_than,
        )
        issued_timestamp = int(now.timestamp())
        expires_timestamp = int(expires_at.timestamp())
        if expires_timestamp <= issued_timestamp:
            return _failure("Service credential deadline expired")
        try:
            credential = jwt.encode(
                self._claims(request, now, expires_at),
                self._key,
                algorithm="RS256",
                headers={
                    "alg": "RS256",
                    "kid": self._settings.key_id,
                    "typ": "at+jwt",
                },
            )
            return Success(ServiceBearerCredential(token=SecretStr(credential)))
        except (jwt.PyJWTError, TypeError, ValueError):
            return _failure("Service credential issuance failed")

    def _claims(
        self,
        request: ServiceCredentialRequest,
        issued_at: datetime,
        expires_at: datetime,
    ) -> dict[str, object]:
        audience = self._settings.audience_for(request.receiver)
        token_id = self._token_id()
        if not _valid_identifier(token_id, maximum=255):
            raise ValueError("service credential token id is invalid")
        return {
            "iss": self._settings.issuer,
            "sub": self._settings.subject,
            "aud": audience,
            "exp": int(expires_at.timestamp()),
            "iat": int(issued_at.timestamp()),
            "nbf": int(issued_at.timestamp()),
            "jti": token_id,
            "client_id": self._settings.client_id,
            "azp": self._settings.client_id,
            "stonks_service_identity": "core_runner",
            "stonks_receiver": request.receiver.value,
            "stonks_permission": request.permission.value,
            "stonks_attempt_generation": request.attempt_generation,
            "stonks_attempt_nonce_hash": request.attempt_nonce_hash,
            "stonks_request_hash": request.request_hash,
            "stonks_targets": [
                f"{request.target.kind.value}:{request.target.identifier}"
            ],
        }


def load_rs256_service_credential_provider(
    environment: Mapping[str, str],
) -> RS256ServiceCredentialProvider:
    names = {
        "issuer": "STONKS_SERVICE_ISSUER",
        "subject": "STONKS_SERVICE_CORE_SUBJECT",
        "client_id": "STONKS_SERVICE_CORE_CLIENT_ID",
        "key_id": "STONKS_SERVICE_SIGNING_KEY_ID",
        "key_file": "STONKS_SERVICE_SIGNING_KEY_FILE",
    }
    values = {key: environment.get(name, "") for key, name in names.items()}
    audience_values = {
        receiver: environment.get(
            f"STONKS_SERVICE_AUDIENCE_{receiver.value.upper()}", ""
        )
        for receiver in ServiceReceiver
    }
    if any(not value for value in values.values()) or any(
        not audience for audience in audience_values.values()
    ):
        raise RuntimeError("service issuer configuration is incomplete")
    audiences = tuple(
        ReceiverAudience(
            receiver=receiver,
            audience=audience_values[receiver],
        )
        for receiver in ServiceReceiver
    )
    path = Path(values["key_file"])
    try:
        if not path.is_absolute():
            raise ValueError("service signing key path is invalid")
        payload = _read_private_key_file(path)
        if not 1 <= len(payload) <= _MAX_PRIVATE_KEY_BYTES:
            raise ValueError("service signing key size is invalid")
        settings = ServiceIssuerSettings(
            issuer=values["issuer"],
            subject=values["subject"],
            client_id=values["client_id"],
            key_id=values["key_id"],
            audiences=audiences,
        )
        return RS256ServiceCredentialProvider(
            settings=settings,
            private_key_pem=SecretBytes(payload),
        )
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeError("service issuer configuration is invalid") from error


def _load_private_key(value: SecretBytes) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(
            value.get_secret_value(),
            password=None,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("service signing key is invalid") from error
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError("service signing key is invalid")
    return key


def _read_private_key_file(path: Path) -> bytes:
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("service signing key path is invalid")
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
        same_file = (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino)
        if (
            not same_file
            or not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (os.name != "nt" and opened.st_mode & 0o077)
        ):
            raise ValueError("service signing key path is invalid")
        payload = os.read(descriptor, _MAX_PRIVATE_KEY_BYTES + 1)
    finally:
        os.close(descriptor)
    return payload


def _valid_identifier(value: str, *, maximum: int) -> bool:
    return (
        1 <= len(value) <= maximum
        and value.strip() == value
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _failure(message: str) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.UNAUTHORIZED,
            message=message,
        )
    )
