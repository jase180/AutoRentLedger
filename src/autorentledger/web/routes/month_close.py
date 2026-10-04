"""Authenticated GET-only Month Close route."""

from __future__ import annotations

from pathlib import Path

from flask import current_app, render_template, request

from autorentledger.obligations import ObligationValidationError, parse_monthly_period
from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition
from autorentledger.web.auth import login_required
from autorentledger.web.routes.common import _database_not_ready, _safe_unavailable


def register_routes(blueprint) -> None:
    @blueprint.get("/month-close")
    @login_required
    def month_close():
        if set(request.args) - {"period"} or len(request.args.getlist("period")) > 1:
            return _invalid_period()
        supplied_period = request.args.get(
            "period", current_app.config["AUTORENTLEDGER_TODAY"]().strftime("%Y-%m")
        )
        try:
            period = parse_monthly_period(supplied_period).value
        except ObligationValidationError:
            return _invalid_period()

        database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
        try:
            require_current_schema(database_path)
            summary = composition.build_web_month_close(database_path, period)
        except DatabaseSchemaError:
            return _database_not_ready("month-close")
        except Exception:  # Safe browser boundary for domain/storage failures.
            current_app.logger.exception("Unable to build Month Close view")
            return _safe_unavailable(
                "Month Close unavailable",
                "Unable to build Month Close view.",
                "month-close",
            )
        return render_template("month_close.html", summary=summary, active_page="month-close")


def _invalid_period():
    return (
        render_template(
            "error.html",
            title="Invalid Month Close period",
            message="Invalid period. Expected one month in YYYY-MM form.",
            commands=(),
            active_page="month-close",
        ),
        400,
    )


__all__ = ["register_routes"]
