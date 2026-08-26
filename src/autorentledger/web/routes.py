"""Thin GET-only routes over canonical read services."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from autorentledger.obligations import ObligationValidationError, parse_monthly_period
from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition

web_blueprint = Blueprint("web", __name__)


@web_blueprint.get("/")
def root():
    today = current_app.config["AUTORENTLEDGER_TODAY"]()
    return redirect(url_for("web.overview", period=f"{today.year:04d}-{today.month:02d}"))


@web_blueprint.get("/overview")
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

    return render_template(
        "overview.html", overview=owner_overview, active_page="overview"
    )


@web_blueprint.get("/attention")
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

    return render_template(
        "attention.html", attention=attention_page, active_page="attention"
    )
