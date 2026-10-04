from pathlib import Path

import pytest
from werkzeug.security import check_password_hash

from autorentledger.cli import build_parser, main
from autorentledger.cli.web import run_web_config
from autorentledger.web import (
    PASSWORD_HASH_ENV,
    SECRET_KEY_ENV,
    WebAuthConfigurationError,
    load_web_auth_config,
    write_local_web_auth_config,
)


def write_config(path: Path, password_hash: str, secret_key: str) -> None:
    path.write_text(
        "# Synthetic local-only web configuration\n"
        f"{PASSWORD_HASH_ENV}={password_hash}\n"
        "\n"
        f"{SECRET_KEY_ENV}={secret_key}\n",
        encoding="utf-8",
    )


def test_environment_only_web_config_remains_supported(tmp_path):
    config = load_web_auth_config(
        {
            PASSWORD_HASH_ENV: "synthetic-environment-hash",
            SECRET_KEY_ENV: "synthetic-environment-secret",
        },
        tmp_path / "missing.env.local",
    )

    assert config.password_hash == "synthetic-environment-hash"
    assert config.secret_key == "synthetic-environment-secret"


def test_local_web_config_loads_from_working_directory_when_environment_is_absent(
    tmp_path, monkeypatch
):
    config_path = tmp_path / ".env.local"
    write_config(config_path, "synthetic-local-hash", "synthetic-local-secret")
    monkeypatch.chdir(tmp_path)

    config = load_web_auth_config({})

    assert config.password_hash == "synthetic-local-hash"
    assert config.secret_key == "synthetic-local-secret"


def test_environment_values_override_local_web_config(tmp_path):
    config_path = tmp_path / ".env.local"
    write_config(config_path, "synthetic-local-hash", "synthetic-local-secret")

    config = load_web_auth_config(
        {
            PASSWORD_HASH_ENV: "synthetic-environment-hash",
            SECRET_KEY_ENV: "synthetic-environment-secret",
        },
        config_path,
    )

    assert config.password_hash == "synthetic-environment-hash"
    assert config.secret_key == "synthetic-environment-secret"


@pytest.mark.parametrize(
    "malformed_line",
    (
        PASSWORD_HASH_ENV,
        f"export {PASSWORD_HASH_ENV}=synthetic-hash",
        "UNRELATED_KEY=synthetic-value",
        f"{PASSWORD_HASH_ENV}=first\n{PASSWORD_HASH_ENV}=second",
    ),
)
def test_malformed_or_unsupported_local_config_fails_without_exposing_values(
    tmp_path, malformed_line
):
    config_path = tmp_path / ".env.local"
    config_path.write_text(malformed_line + "\n", encoding="utf-8")

    with pytest.raises(WebAuthConfigurationError) as raised:
        load_web_auth_config({}, config_path)

    assert str(config_path) in str(raised.value)
    assert "synthetic" not in str(raised.value)


def test_local_config_is_data_not_shell_syntax(tmp_path):
    marker = tmp_path / "must-not-exist"
    config_path = tmp_path / ".env.local"
    config_path.write_text(
        f"{PASSWORD_HASH_ENV}=synthetic-hash\n"
        f"{SECRET_KEY_ENV}=synthetic-secret\n"
        f"UNRELATED=$(touch {marker})\n",
        encoding="utf-8",
    )

    with pytest.raises(WebAuthConfigurationError):
        load_web_auth_config({}, config_path)

    assert not marker.exists()


def test_config_writer_persists_only_hash_and_secret(tmp_path):
    config_path = tmp_path / ".env.local"
    plaintext = "synthetic-owner-password"

    write_local_web_auth_config(plaintext, config_path=config_path)

    contents = config_path.read_text(encoding="utf-8")
    lines = contents.splitlines()
    assert plaintext not in contents
    assert len(lines) == 2
    assert lines[0].startswith(f"{PASSWORD_HASH_ENV}=")
    assert lines[1].startswith(f"{SECRET_KEY_ENV}=")
    assert check_password_hash(lines[0].split("=", 1)[1], plaintext)
    assert lines[1].split("=", 1)[1]


def test_web_config_command_bypasses_schema_and_refuses_accidental_overwrite(
    tmp_path, monkeypatch, capsys
):
    parsed = build_parser().parse_args(["web-config"])
    assert parsed.requires_schema is False
    assert parsed.handler.__module__ == "autorentledger.cli.web"

    monkeypatch.chdir(tmp_path)
    supplied = iter(("synthetic-owner-password", "synthetic-owner-password"))
    monkeypatch.setattr("autorentledger.cli.web.getpass", lambda prompt: next(supplied))
    assert main(["web-config"]) == 0
    config_path = tmp_path / ".env.local"
    original = config_path.read_text(encoding="utf-8")
    assert "synthetic-owner-password" not in original

    monkeypatch.setattr(
        "autorentledger.cli.web.getpass",
        lambda prompt: pytest.fail("existing config should be rejected before prompting"),
    )
    assert run_web_config(config_path=config_path) == 1
    assert config_path.read_text(encoding="utf-8") == original
    output = capsys.readouterr().out
    assert "Refusing to overwrite" in output
    assert "synthetic-owner-password" not in output


def test_makefile_exposes_idempotent_setup_and_accurate_ergonomic_targets():
    makefile = (Path(__file__).parents[1] / "Makefile").read_text(encoding="utf-8")

    assert "VENV_CREATED = $(VENV)/.created" in makefile
    assert "VENV_INSTALLED = $(VENV)/.installed" in makefile
    assert "BOOTSTRAP_PYTHON ?= py -3" in makefile
    assert "BOOTSTRAP_PYTHON ?= python3" in makefile
    assert "py -3.11" not in makefile
    assert "python3.11" not in makefile
    assert "Python 3.11 or newer" in makefile
    assert "$(VENV_CREATED): | python-check" in makefile
    assert "start: $(VENV_INSTALLED)" in makefile
    assert "web-config: $(VENV_INSTALLED)" in makefile
    assert '"$(PYTHON)" -m ruff check . --fix' in makefile
    assert '"$(PYTHON)" -m ruff format .' in makefile

    gitignore = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert ".env.local" in gitignore.splitlines()
