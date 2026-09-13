"""Thin GET-only routes over canonical read services."""

from __future__ import annotations

from pathlib import Path

from flask import current_app, redirect, render_template, request, url_for

from autorentledger.obligations import ObligationValidationError, parse_monthly_period
from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition
from autorentledger.web.auth import login_required


def register_routes(blueprint) -> None:
    @blueprint.get("/obligations")
    @login_required
    def obligations():
        if "period" not in request.args:
            today = current_app.config["AUTORENTLEDGER_TODAY"]()
            period = f"{today.year:04d}-{today.month:02d}"
            return redirect(url_for("web.obligations", period=period))

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
                    active_page="obligations",
                ),
                400,
            )

        database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
        try:
            require_current_schema(database_path)
            obligations_page = composition.build_web_obligations(database_path, period)
        except DatabaseSchemaError:
            return (
                render_template(
                    "error.html",
                    title="Database not ready",
                    message="The AutoRentLedger database is missing, outdated, or invalid.",
                    commands=("autorentledger db status", "autorentledger db upgrade"),
                    active_page="obligations",
                ),
                503,
            )
        except Exception:  # Safe browser boundary for domain/storage failures.
            current_app.logger.exception("Unable to build obligations view")
            return (
                render_template(
                    "error.html",
                    title="Obligations unavailable",
                    message="Unable to build obligations view.",
                    commands=("autorentledger db check",),
                    active_page="obligations",
                ),
                500,
            )

        return render_template(
            "obligations.html", obligations=obligations_page, active_page="obligations"
        )
