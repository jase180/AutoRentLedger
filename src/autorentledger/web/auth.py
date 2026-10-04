"""Minimal single-owner authentication for the local web adapter."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import unquote, urlsplit

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_HASH_ENV = "AUTORENTLEDGER_WEB_PASSWORD_HASH"
SECRET_KEY_ENV = "AUTORENTLEDGER_WEB_SECRET_KEY"
LOCAL_WEB_CONFIG = Path(".env.local")
AUTH_CONFIGURATION_ERROR = (
    "Web authentication is not configured.\n"
    "Run `autorentledger web-config` or set:\n"
    f"{PASSWORD_HASH_ENV}\n{SECRET_KEY_ENV}"
)


@dataclass(frozen=True)
class WebAuthConfig:
    """The two secrets required by the single-owner browser session."""

    password_hash: str
    secret_key: str


class WebAuthConfigurationError(RuntimeError):
    """Required web authentication configuration is absent."""


def load_web_auth_config(
    environ: Mapping[str, str] | None = None,
    config_path: Path = LOCAL_WEB_CONFIG,
) -> WebAuthConfig:
    """Load web secrets, preferring the process environment over local config."""
    source = os.environ if environ is None else environ
    password_hash = source.get(PASSWORD_HASH_ENV, "").strip()
    secret_key = source.get(SECRET_KEY_ENV, "").strip()
    if not password_hash or not secret_key:
        local_values = _load_local_web_config(config_path)
        password_hash = password_hash or local_values.get(PASSWORD_HASH_ENV, "").strip()
        secret_key = secret_key or local_values.get(SECRET_KEY_ENV, "").strip()
    if not password_hash or not secret_key:
        raise WebAuthConfigurationError(AUTH_CONFIGURATION_ERROR)
    return WebAuthConfig(password_hash=password_hash, secret_key=secret_key)


def write_local_web_auth_config(
    password: str,
    *,
    config_path: Path = LOCAL_WEB_CONFIG,
    overwrite: bool = False,
) -> Path:
    """Write only the password hash and a fresh session-signing key."""
    if not password:
        raise ValueError("The web password cannot be empty.")
    contents = (
        f"{PASSWORD_HASH_ENV}={generate_password_hash(password)}\n"
        f"{SECRET_KEY_ENV}={secrets.token_urlsafe(32)}\n"
    )
    mode = "w" if overwrite else "x"
    with config_path.open(mode, encoding="utf-8", newline="\n") as config_file:
        config_file.write(contents)
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    return config_path


def _load_local_web_config(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        return {}
    allowed_keys = {PASSWORD_HASH_ENV, SECRET_KEY_ENV}
    loaded: dict[str, str] = {}
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise WebAuthConfigurationError(
            f"Unable to read local web configuration: {config_path}"
        ) from error
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in allowed_keys or key in loaded:
            raise WebAuthConfigurationError(
                f"Invalid local web configuration at {config_path}:{line_number}. "
                f"Expected one unique KEY=value entry for {PASSWORD_HASH_ENV} and {SECRET_KEY_ENV}."
            )
        loaded[key] = value.strip()
    return loaded


auth_blueprint = Blueprint("auth", __name__)

ViewFunction = TypeVar("ViewFunction", bound=Callable[..., Any])


def login_required(view: ViewFunction) -> ViewFunction:
    """Require the one authenticated session before invoking a ledger route."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if session.get("authenticated") is not True:
            requested_path = request.full_path.removesuffix("?")
            return redirect(url_for("auth.login", next=requested_path))
        return view(*args, **kwargs)

    return cast(ViewFunction, wrapped)


@auth_blueprint.route("/login", methods=("GET", "POST"))
def login():
    if session.get("authenticated") is True:
        return redirect(url_for("web.root"))

    next_path = _safe_local_next(request.values.get("next"))
    invalid_password = False
    if request.method == "POST":
        supplied_password = request.form.get("password", "")
        configured_hash = current_app.config["AUTORENTLEDGER_WEB_PASSWORD_HASH"]
        try:
            password_matches = check_password_hash(configured_hash, supplied_password)
        except (ValueError, TypeError):
            password_matches = False
        session.clear()
        if password_matches:
            session["authenticated"] = True
            return redirect(next_path)
        invalid_password = True

    return render_template(
        "login.html",
        invalid_password=invalid_password,
        next_path=next_path,
    )


@auth_blueprint.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


def _safe_local_next(candidate: str | None) -> str:
    if not candidate:
        return "/"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/"
    decoded = unquote(candidate)
    if decoded.startswith("//") or "\\" in decoded:
        return "/"
    return candidate
