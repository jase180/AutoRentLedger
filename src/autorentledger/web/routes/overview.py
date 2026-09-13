"""Thin GET-only routes over canonical read services."""

from __future__ import annotations

from pathlib import Path

from flask import current_app, render_template, request

from autorentledger.obligations import ObligationValidationError, parse_monthly_period
from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition
from autorentledger.web.auth import login_required


def register_routes(blueprint) -> None:
    @blueprint.get("/overview")
    @login_required
    def overview():
        supplied_period = request.args.get("period", "")
        try:
            period = parse_monthly_period(supplied_period).value
        except ObligationValidationError:
            return (
                render_template(
                    "error.html",
                    title="Invalid period",
                    message="Invalid period. Expected YYYY-MM.",
                    commands=(),
                ),
                400,
            )

        database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
        try:
            require_current_schema(database_path)
            owner_overview = composition.build_web_owner_overview(database_path, period)
        except DatabaseSchemaError:
            return (
                render_template(
                    "error.html",
                    title="Database not ready",
                    message="The AutoRentLedger database is missing, outdated, or invalid.",
                    commands=("autorentledger db status", "autorentledger db upgrade"),
                ),
                503,
            )
        except Exception:  # Safe browser boundary for domain/storage failures.
            current_app.logger.exception("Unable to build owner overview")
            return (
                render_template(
                    "error.html",
                    title="Overview unavailable",
                    message="Unable to build owner overview.",
                    commands=("autorentledger db check",),
                ),
                500,
            )

        return render_template("overview.html", overview=owner_overview, active_page="overview")
