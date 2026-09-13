"""Thin GET-only routes over canonical read services."""

from __future__ import annotations

from pathlib import Path

from flask import current_app, render_template

from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition
from autorentledger.web.auth import login_required
from autorentledger.web.routes.common import (
    _database_not_ready,
    _safe_unavailable,
)


def register_routes(blueprint) -> None:
    @blueprint.get("/rent-accounts/<int:rent_account_id>")
    @login_required
    def rent_account_detail(rent_account_id: int):
        database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
        try:
            require_current_schema(database_path)
            detail = composition.build_web_rent_account_detail(database_path, rent_account_id)
        except DatabaseSchemaError:
            return _database_not_ready("obligations")
        except composition.WebDetailNotFoundError:
            return (
                render_template(
                    "error.html",
                    title="Rent account not found",
                    message="The requested rent account does not exist.",
                    commands=(),
                    active_page="obligations",
                ),
                404,
            )
        except Exception:  # Safe browser boundary for domain/storage failures.
            current_app.logger.exception("Unable to build rent account detail")
            return _safe_unavailable(
                "Rent account unavailable",
                "Unable to build rent account detail.",
                "obligations",
            )
        return render_template("rent_account_detail.html", detail=detail, active_page="obligations")
