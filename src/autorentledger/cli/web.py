"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

from getpass import getpass
from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    WEB_LOOPBACK_ERROR,
)
from autorentledger.storage.migrations import (
    DatabaseSchemaError,
    require_current_schema,
)
from autorentledger.web import (
    LOCAL_WEB_CONFIG,
    WebAuthConfigurationError,
    write_local_web_auth_config,
)


def register_commands(subparsers) -> None:
    web = subparsers.add_parser("web", help="serve the read-only owner overview locally")
    web.set_defaults(handler=_handle_web, requires_schema=False)
    web.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    web.add_argument("--host", default=DEFAULT_WEB_HOST)
    web.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)

    web_config = subparsers.add_parser(
        "web-config", help="create the ignored local web authentication config"
    )
    web_config.set_defaults(handler=_handle_web_config, requires_schema=False)
    web_config.add_argument(
        "--force",
        action="store_true",
        help="replace an existing .env.local after prompting for a new password",
    )


def _handle_web(args) -> int:
    return run_web(args.database, args.host, args.port)


def _handle_web_config(args) -> int:
    return run_web_config(force=args.force)


def run_web_config(*, force: bool = False, config_path: Path = LOCAL_WEB_CONFIG) -> int:
    if config_path.exists() and not force:
        print(
            f"Local web configuration already exists at {config_path}.\n"
            "Refusing to overwrite it; use --force to replace it explicitly."
        )
        return 1
    password = getpass("Choose an AutoRentLedger web password: ")
    confirmation = getpass("Confirm the web password: ")
    if not password:
        print("The web password cannot be empty.")
        return 1
    if password != confirmation:
        print("The web passwords did not match; no configuration was written.")
        return 1
    try:
        write_local_web_auth_config(password, config_path=config_path, overwrite=force)
    except (OSError, ValueError) as error:
        print(f"Could not write local web configuration: {error}")
        return 1
    print(f"Created local web authentication configuration at {config_path}.")
    print("The plaintext password was not stored.")
    return 0


def run_web(database_path: Path, host: str, port: int) -> int:
    import autorentledger.cli as cli_facade

    if not _is_loopback_host(host):
        print(WEB_LOOPBACK_ERROR)
        return 1
    try:
        auth_config = cli_facade.load_web_auth_config()
    except WebAuthConfigurationError as error:
        print(error)
        return 1
    try:
        require_current_schema(database_path)
    except DatabaseSchemaError as error:
        print(error)
        return 1
    app = cli_facade.create_app(database_path, auth_config)
    app.run(host=host, port=port, debug=False, use_reloader=False)
    return 0


def _is_loopback_host(host: str) -> bool:
    return host.casefold() in {"127.0.0.1", "localhost", "::1"}
