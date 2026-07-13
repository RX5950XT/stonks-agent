"""Fixed-origin, bounded artifact capability resolver for the worker."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from stonks_contracts.tradingagents import SignedEvidenceArtifact


class FixedOriginArtifactResolver:
    __slots__ = ("_client", "_clock", "_max_bytes", "_origin", "_timeout")

    def __init__(
        self,
        *,
        client: httpx.Client,
        origin: str,
        max_bytes: int,
        timeout_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
        ):
            raise ValueError("artifact origin is invalid")
        if not 1 <= max_bytes <= 16_777_216 or not 0 < timeout_seconds <= 60:
            raise ValueError("artifact resolver limits are invalid")
        self._client = client
        self._origin = origin.rstrip("/")
        self._max_bytes = max_bytes
        self._timeout = timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def resolve(self, artifact: SignedEvidenceArtifact) -> str:
        expected_hash = artifact.artifact_ref.removeprefix("sha256:")
        parsed = urlsplit(artifact.signed_url)
        actual_origin = f"{parsed.scheme}://{parsed.netloc}"
        if (
            actual_origin != self._origin
            or parsed.path != f"/v1/artifacts/{expected_hash}"
            or self._clock() >= artifact.expires_at
        ):
            raise ValueError("artifact capability is outside worker scope")
        with self._client.stream(
            "GET",
            artifact.signed_url,
            headers={"Accept": "text/plain", "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise ValueError("artifact service rejected capability")
            if (
                response.headers.get("content-encoding", "identity").lower()
                != "identity"
            ):
                raise ValueError("encoded artifact body is denied")
            body = bytearray()
            chunks = (
                (response.content,)
                if response.is_stream_consumed
                else response.iter_raw()
            )
            for chunk in chunks:
                if len(chunk) > self._max_bytes - len(body):
                    raise ValueError("artifact body exceeds worker limit")
                body.extend(chunk)
        payload = bytes(body)
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise ValueError("artifact body hash mismatch")
        return payload.decode("utf-8")
