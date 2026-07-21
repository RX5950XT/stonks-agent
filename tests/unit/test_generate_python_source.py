from __future__ import annotations

import gzip
import io
import json
import sys
import tarfile
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import generate_python_source as GENERATOR  # noqa: E402
from scripts import python_source_contract as CONTRACT  # noqa: E402

PythonSourceError = CONTRACT.PythonSourceError
LockedSource = CONTRACT.LockedSource
build_archive_bytes: Any = CONTRACT.build_archive_bytes
load_source_plan: Any = CONTRACT.load_source_plan
verify_source_archive: Any = CONTRACT.verify_source_archive
download_locked_source: Any = GENERATOR.download_locked_source
generate_python_source: Any = GENERATOR.generate_python_source
generate_from_files: Any = GENERATOR.generate_from_files
PolicyRedirectHandler = GENERATOR.PolicyRedirectHandler

POLICY = ROOT / "config" / "release" / "python-source-policy.json"
LOCK = ROOT / "uv.lock"


def test_policy_resolves_only_exact_sdists_from_uv_lock() -> None:
    plan = load_source_plan(POLICY, LOCK)

    assert [
        (item.name, item.version, item.sha256, item.size) for item in plan.sources
    ] == [
        (
            "certifi",
            "2026.6.17",
            "024c88eeec92ca068db80f02b8b07c9cef7b9fe261d1d535abfd5abd6f6af432",
            134594,
        ),
        (
            "psycopg",
            "3.3.4",
            "e21207764952cff81b6b8bdacad9a3939f2793367fdac2987b3aac36a651b5bc",
            165799,
        ),
        (
            "psycopg-c",
            "3.3.4",
            "ed8106128b2d04359c185fc9641b4409abfce4d0b6fb1d1ff6800646e27f1a22",
            647111,
        ),
    ]
    assert {item.url.split("/", 3)[2] for item in plan.sources} == {
        "files.pythonhosted.org"
    }


def test_policy_rejects_lock_url_outside_allowlist(tmp_path: Path) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    lock = LOCK.read_text(encoding="utf-8").replace(
        "https://files.pythonhosted.org/packages/c9/c7/",
        "https://evil.invalid/packages/c9/c7/",
        1,
    )
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(lock, encoding="utf-8")

    with pytest.raises(PythonSourceError, match="approved host"):
        load_source_plan(policy_path, lock_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schema_version="wrong"), "schema"),
        (lambda value: value.update(extra=True), "fields"),
        (lambda value: value.update(allowed_hosts=[]), "hosts"),
        (
            lambda value: value.update(
                allowed_hosts=["files.pythonhosted.org", "files.pythonhosted.org"]
            ),
            "duplicates",
        ),
        (lambda value: value.update(allowed_hosts=["pypi.org"]), "drifted"),
        (lambda value: value.update(max_source_bytes=0), "max_source_bytes"),
        (lambda value: value.update(max_total_source_bytes=1), "total exceeds"),
        (lambda value: value.update(max_archive_bytes=1), "archive bound"),
        (lambda value: value.update(packages=[]), "package policy"),
    ],
)
def test_policy_rejects_unsafe_or_drifted_bounds(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    mutation(policy)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(PythonSourceError, match=message):
        load_source_plan(policy_path, LOCK)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'registry = "https://pypi.org/simple"',
            'registry = "https://mirror.invalid/simple"',
            "not locked to PyPI",
        ),
        ('hash = "sha256:024c', 'hash = "sha512:024c', "SHA-256"),
        ("size = 134594", "size = 1048577", "size exceeds"),
        ("certifi-2026.6.17.tar.gz", "certifi-2026.6.17.zip", "filename"),
    ],
)
def test_lock_sdist_metadata_is_fail_closed(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    lock = LOCK.read_text(encoding="utf-8").replace(old, new)
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(lock, encoding="utf-8")

    with pytest.raises(PythonSourceError, match=message):
        load_source_plan(POLICY, lock_path)


class _Headers(dict[str, str]):
    def get(self, key: str, default: str | None = None) -> str | None:
        return super().get(key, default)


class _Response(io.BytesIO):
    def __init__(self, body: bytes, declared: int | None = None) -> None:
        super().__init__(body)
        self.headers = _Headers()
        if declared is not None:
            self.headers["Content-Length"] = str(declared)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.request: urllib.request.Request | None = None

    def open(self, request: urllib.request.Request, timeout: int) -> _Response:
        assert timeout == 60
        self.request = request
        return self.response


def _source(body: bytes = b"sdist") -> Any:
    import hashlib

    return LockedSource(
        name="certifi",
        version="2026.6.17",
        filename="certifi-2026.6.17.tar.gz",
        url="https://files.pythonhosted.org/packages/certifi.tar.gz",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )


def test_download_is_exact_bounded_and_sends_no_credentials() -> None:
    source = _source()
    opener = _Opener(_Response(b"sdist", declared=5))

    assert download_locked_source(source, opener=opener) == b"sdist"
    assert opener.request is not None
    assert opener.request.get_header("Authorization") is None
    assert opener.request.full_url == source.url

    with pytest.raises(PythonSourceError, match="declared size"):
        download_locked_source(
            source,
            opener=_Opener(_Response(b"sdist", declared=6)),
        )
    with pytest.raises(PythonSourceError, match="exact size"):
        download_locked_source(
            source,
            opener=_Opener(_Response(b"sdist!", declared=None)),
        )
    with pytest.raises(PythonSourceError, match="SHA-256"):
        download_locked_source(
            replace(source, sha256="0" * 64),
            opener=_Opener(_Response(b"sdist", declared=5)),
        )


def test_redirects_are_revalidated_against_exact_hosts() -> None:
    handler = PolicyRedirectHandler(frozenset({"files.pythonhosted.org"}))
    request = urllib.request.Request(
        "https://files.pythonhosted.org/packages/source.tar.gz"
    )

    approved = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://files.pythonhosted.org/packages/moved.tar.gz",
    )
    assert approved is not None

    with pytest.raises(PythonSourceError, match="approved host"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://evil.invalid/source.tar.gz",
        )


def test_download_rejects_untrusted_initial_url_before_network() -> None:
    opener = _Opener(_Response(b"sdist", declared=5))
    source = replace(_source(), url="https://evil.invalid/packages/source.tar.gz")

    with pytest.raises(PythonSourceError, match="approved host"):
        download_locked_source(source, opener=opener)

    assert opener.request is None


def test_download_wraps_transport_failure_without_leaking_details() -> None:
    class BrokenOpener:
        def open(self, _request: object, timeout: int) -> None:
            assert timeout == 60
            raise OSError("credential-shaped-internal-detail")

    with pytest.raises(PythonSourceError, match="download failed") as error:
        download_locked_source(_source(), opener=BrokenOpener())

    assert "credential-shaped" not in str(error.value)


def test_archive_is_canonical_and_manifest_verifies_all_sources(tmp_path: Path) -> None:
    plan = load_source_plan(POLICY, LOCK)
    payloads = {
        item.filename: (item.name + item.version).encode() for item in plan.sources
    }
    sources = tuple(
        replace(
            item,
            size=len(payloads[item.filename]),
            sha256=__import__("hashlib").sha256(payloads[item.filename]).hexdigest(),
        )
        for item in plan.sources
    )
    plan = replace(plan, sources=sources)

    first = build_archive_bytes(plan, payloads)
    second = build_archive_bytes(plan, dict(reversed(tuple(payloads.items()))))

    assert first == second
    summary = verify_source_archive(first, plan)
    assert summary.source_count == 3
    assert summary.total_source_bytes == sum(map(len, payloads.values()))
    assert int.from_bytes(first[4:8], "little") == 0

    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == sorted(
            member.name for member in members
        )
        assert all(
            member.uid == member.gid == member.mtime == 0
            and member.mode == 0o644
            and member.uname == member.gname == ""
            for member in members
        )
        manifest_handle = archive.extractfile("manifest.json")
        assert manifest_handle is not None
        manifest = json.load(manifest_handle)
        assert manifest["schema_version"] == "stonks-agent/python-source/v1"
        assert manifest["sources"][0]["name"] == "certifi"

    decompressed = bytearray(gzip.decompress(first))
    decompressed[-1025] ^= 1
    tampered = gzip.compress(bytes(decompressed), mtime=0)
    with pytest.raises(PythonSourceError):
        verify_source_archive(tampered, plan)


def test_generator_self_verifies_and_is_byte_identical(tmp_path: Path) -> None:
    plan = load_source_plan(POLICY, LOCK)
    payloads = {
        item.filename: (item.name + item.version).encode() for item in plan.sources
    }
    sources = tuple(
        replace(
            item,
            size=len(payloads[item.filename]),
            sha256=__import__("hashlib").sha256(payloads[item.filename]).hexdigest(),
        )
        for item in plan.sources
    )
    plan = replace(plan, sources=sources)
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    def fetch(source: Any) -> bytes:
        return payloads[source.filename]

    one = generate_python_source(plan=plan, output=first, fetcher=fetch)
    two = generate_python_source(plan=plan, output=second, fetcher=fetch)

    assert one == two
    assert first.read_bytes() == second.read_bytes()


def test_generate_from_files_composes_the_validated_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = load_source_plan(POLICY, LOCK)
    summary = CONTRACT.SourceArchiveSummary("a" * 64, "b" * 64, 3, 947504)
    opener = object()
    monkeypatch.setattr(GENERATOR, "load_source_plan", lambda *_args: plan)
    monkeypatch.setattr(GENERATOR, "build_source_opener", lambda _hosts: opener)
    monkeypatch.setattr(GENERATOR, "generate_python_source", lambda **_kwargs: summary)

    assert (
        generate_from_files(
            policy_path=POLICY,
            lock_path=LOCK,
            output=tmp_path / "source.tar.gz",
        )
        == summary
    )


@pytest.mark.parametrize("module", [CONTRACT, GENERATOR])
@pytest.mark.parametrize("success", [True, False])
def test_cli_returns_structured_result(
    module: Any,
    success: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary = CONTRACT.SourceArchiveSummary("a" * 64, "b" * 64, 3, 947504)

    def invoke(*_args: Any, **_kwargs: Any) -> Any:
        if not success:
            raise PythonSourceError("internal detail")
        return summary

    target = (
        "verify_source_archive_path" if module is CONTRACT else "generate_from_files"
    )
    monkeypatch.setattr(module, target, invoke)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tool",
            "--policy",
            str(POLICY),
            "--uv-lock",
            str(LOCK),
            "--archive" if module is CONTRACT else "--output",
            "artifact.tar.gz",
        ],
    )

    assert module.main() == (0 if success else 1)
    result = json.loads(capsys.readouterr().out)
    assert result["success"] is success
    assert "internal detail" not in json.dumps(result)
