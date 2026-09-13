"""Thin GET-only routes over canonical read services."""

from __future__ import annotations

from pathlib import Path

from flask import current_app, render_template

from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition
from autorentledger.web.auth import login_required


def register_routes(blueprint) -> None:
    @blueprint.get("/attention")
    @login_required
    def attention():
        database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
        try:
            require_current_schema(database_path)
            attention_page = composition.build_web_attention(database_path)
        except DatabaseSchemaError:
            return (
                render_template(
                    "error.html",
                    title="Database not ready",
                    message="The AutoRentLedger database is missing, outdated, or invalid.",
                    commands=("autorentledger db status", "autorentledger db upgrade"),
                    active_page="attention",
                ),
                503,
            )
        except Exception:  # Safe browser boundary for domain/storage failures.
            current_app.logger.exception("Unable to build attention view")
            return (
                render_template(
                    "error.html",
                    title="Attention unavailable",
                    message="Unable to build attention view.",
                    commands=("autorentledger db check",),
                    active_page="attention",
                ),
                500,
            )

        return render_template("attention.html", attention=attention_page, active_page="attention")
