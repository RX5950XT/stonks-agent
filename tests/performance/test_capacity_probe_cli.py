from __future__ import annotations

from pathlib import Path

import pytest

import scripts.run_capacity_probe as probe_script

pytestmark = pytest.mark.performance


def test_cli_rejects_database_url_argument_without_echoing_secret(
    capsys: object,
) -> None:
    secret = "postgresql+psycopg://root:super-secret@127.0.0.1/stonks_capacity"

    exit_code = probe_script.main(["--database-url", secret])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert exit_code == 2
    assert secret not in captured.out
    assert secret not in captured.err
    assert "super-secret" not in captured.out
    assert "super-secret" not in captured.err


def test_cli_rejects_symlink_report_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("preserve", encoding="utf-8")
    link = tmp_path / "report.json"
    try:
        link.symlink_to(target)
    except OSError:
        return

    exit_code = probe_script.main(["--output", str(link)])

    assert exit_code == 2
    assert target.read_text(encoding="utf-8") == "preserve"


def test_cli_error_boundary_never_echoes_internal_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "postgresql+psycopg://root:secret@127.0.0.1/stonks_capacity"
    monkeypatch.setenv("STONKS_CAPACITY_DATABASE_URL", secret)

    def explode(*_args: object) -> object:
        raise RuntimeError(secret)

    monkeypatch.setattr(probe_script, "run_capacity_probe", explode)

    exit_code = probe_script.main(["--output", str(tmp_path / "report.json")])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert secret not in captured.out
    assert secret not in captured.err
    assert "secret" not in captured.out
    assert "secret" not in captured.err
