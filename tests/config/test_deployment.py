from __future__ import annotations

from pathlib import Path

import pytest

from stonks_agent.config.deployment import (
    DeploymentConfigurationError,
    load_deployment_settings,
    load_runtime_role_settings,
)


def environment(password_file: Path) -> dict[str, str]:
    return {
        "STONKS_ENVIRONMENT": "production",
        "STONKS_EXECUTION_MODE": "paper",
        "STONKS_BUILD_REVISION": "97f08f5",
        "STONKS_DEPLOYMENT_ROOT": "/opt/stonks",
        "STONKS_DB_HOST": "postgres",
        "STONKS_DB_PORT": "5432",
        "STONKS_DB_NAME": "stonks",
        "STONKS_DB_USER": "stonks_runtime",
        "STONKS_DB_PASSWORD_FILE": str(password_file),
        "STONKS_DB_CONNECT_TIMEOUT_SECONDS": "3",
        "STONKS_DB_POOL_SIZE": "4",
        "STONKS_DB_MAX_OVERFLOW": "2",
        "STONKS_SERVER_HOST": "0.0.0.0",
        "STONKS_SERVER_PORT": "8000",
    }


def test_deployment_settings_build_secret_safe_structured_database_url(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "db-password"
    password_file.write_text("s3cret-value\n", encoding="utf-8")

    settings = load_deployment_settings(environment(password_file))
    url = settings.database.sqlalchemy_url()

    assert settings.execution_mode == "paper"
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "postgres"
    assert url.username == "stonks_runtime"
    assert url.password == "s3cret-value"
    assert "s3cret-value" not in repr(settings)
    assert "s3cret-value" not in str(url)
    assert "s3cret-value" not in settings.model_dump_json()


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("STONKS_ENVIRONMENT", "local"),
        ("STONKS_EXECUTION_MODE", "live"),
        ("STONKS_DB_HOST", "POSTGRES"),
        ("STONKS_DB_PORT", "0"),
        ("STONKS_DB_NAME", "../stonks"),
        ("STONKS_DB_USER", "postgres superuser"),
        ("STONKS_DB_CONNECT_TIMEOUT_SECONDS", "0"),
        ("STONKS_DB_POOL_SIZE", "0"),
        ("STONKS_DB_MAX_OVERFLOW", "-1"),
        ("STONKS_SERVER_HOST", "example.com"),
        ("STONKS_SERVER_PORT", "70000"),
        ("STONKS_BUILD_REVISION", "latest"),
    ),
)
def test_deployment_settings_reject_invalid_or_unsafe_values(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    password_file = tmp_path / "db-password"
    password_file.write_text("valid-secret", encoding="utf-8")
    values = environment(password_file)
    values[name] = value

    with pytest.raises(DeploymentConfigurationError):
        load_deployment_settings(values)


def test_raw_database_url_is_rejected_even_when_structured_settings_exist(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "db-password"
    password_file.write_text("valid-secret", encoding="utf-8")
    values = environment(password_file)
    values["STONKS_DATABASE_URL"] = "postgresql://root:secret@postgres/stonks"

    with pytest.raises(DeploymentConfigurationError):
        load_deployment_settings(values)


@pytest.mark.parametrize(
    "name",
    (
        "DATABASE_URL",
        "SQLALCHEMY_URL",
        "PGPASSWORD",
        "PGPASSFILE",
        "PGSERVICE",
        "STONKS_DB_PASSWORD",
        "STONKS_DB_URL",
    ),
)
def test_ambient_or_unknown_database_authority_is_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    password_file = tmp_path / "db-password"
    password_file.write_text("valid-secret", encoding="utf-8")
    values = environment(password_file)
    values[name] = "ambient-authority"

    with pytest.raises(DeploymentConfigurationError):
        load_deployment_settings(values)


@pytest.mark.parametrize("contents", ("", " leading", "trailing ", "line\nbreak"))
def test_database_password_file_must_contain_one_bounded_secret(
    tmp_path: Path,
    contents: str,
) -> None:
    password_file = tmp_path / "db-password"
    password_file.write_text(contents, encoding="utf-8")

    with pytest.raises(DeploymentConfigurationError):
        load_deployment_settings(environment(password_file))


def test_database_password_file_rejects_missing_symlink_and_oversize(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(DeploymentConfigurationError):
        load_deployment_settings(environment(missing))

    target = tmp_path / "target"
    target.write_text("valid-secret", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(DeploymentConfigurationError):
        load_deployment_settings(environment(link))

    target.write_text("x" * 257, encoding="utf-8")
    with pytest.raises(DeploymentConfigurationError):
        load_deployment_settings(environment(target))


def test_unknown_deployment_environment_key_is_rejected(tmp_path: Path) -> None:
    password_file = tmp_path / "db-password"
    password_file.write_text("valid-secret", encoding="utf-8")
    values = environment(password_file)
    values["STONKS_DEPLOYMENT_UNDOCUMENTED"] = "true"

    with pytest.raises(DeploymentConfigurationError):
        load_deployment_settings(values)


def test_packaged_deployment_root_is_exact(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "db-password"
    password_file.write_text("valid-secret", encoding="utf-8")
    values = environment(password_file)
    values["STONKS_DEPLOYMENT_ROOT"] = "/opt/stonks"

    settings = load_deployment_settings(values)

    assert settings.execution_mode == "paper"
    assert settings.deployment_root == "/opt/stonks"

    values["STONKS_DEPLOYMENT_ROOT"] = "/tmp/stonks"
    with pytest.raises(DeploymentConfigurationError):
        load_deployment_settings(values)


def test_runtime_login_role_uses_distinct_secret_and_fixed_group(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "runtime-password"
    password_file.write_text("runtime-secret", encoding="utf-8")
    values = {
        "STONKS_RUNTIME_DB_USER": "stonks_runtime",
        "STONKS_RUNTIME_DB_PASSWORD_FILE": str(password_file),
    }

    role = load_runtime_role_settings(values, owner_user="postgres")

    assert role.login_name == "stonks_runtime"
    assert role.group_role == "stonks_app"
    assert role.reveal_password() == "runtime-secret"
    assert "runtime-secret" not in repr(role)
    assert "runtime-secret" not in role.model_dump_json()


@pytest.mark.parametrize("login_name", ("postgres", "stonks_app", "Bad-Role"))
def test_runtime_login_role_rejects_owner_group_or_invalid_name(
    tmp_path: Path,
    login_name: str,
) -> None:
    password_file = tmp_path / "runtime-password"
    password_file.write_text("runtime-secret", encoding="utf-8")

    with pytest.raises(DeploymentConfigurationError):
        load_runtime_role_settings(
            {
                "STONKS_RUNTIME_DB_USER": login_name,
                "STONKS_RUNTIME_DB_PASSWORD_FILE": str(password_file),
            },
            owner_user="postgres",
        )


def test_runtime_role_rejects_raw_or_unknown_credential_setting(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "runtime-password"
    password_file.write_text("runtime-secret", encoding="utf-8")

    with pytest.raises(DeploymentConfigurationError):
        load_runtime_role_settings(
            {
                "STONKS_RUNTIME_DB_USER": "stonks_runtime",
                "STONKS_RUNTIME_DB_PASSWORD_FILE": str(password_file),
                "STONKS_RUNTIME_DB_PASSWORD": "ambient-authority",
            },
            owner_user="postgres",
        )
