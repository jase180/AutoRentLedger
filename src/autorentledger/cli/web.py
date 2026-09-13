"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

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
from autorentledger.web import WebAuthConfigurationError


def register_commands(subparsers) -> None:
    web = subparsers.add_parser("web", help="serve the read-only owner overview locally")
    web.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    web.add_argument("--host", default=DEFAULT_WEB_HOST)
    web.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)


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
