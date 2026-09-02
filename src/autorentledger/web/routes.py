"""Thin GET-only routes over canonical read services."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from autorentledger.allocation_planning import AllocationPlanValidationError
from autorentledger.obligations import ObligationValidationError, parse_monthly_period
from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition
from autorentledger.web.auth import login_required

web_blueprint = Blueprint("web", __name__)


@web_blueprint.get("/")
@login_required
def root():
    today = current_app.config["AUTORENTLEDGER_TODAY"]()
    return redirect(url_for("web.overview", period=f"{today.year:04d}-{today.month:02d}"))


@web_blueprint.get("/overview")
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

    return render_template(
        "overview.html", overview=owner_overview, active_page="overview"
    )


@web_blueprint.get("/attention")
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

    return render_template(
        "attention.html", attention=attention_page, active_page="attention"
    )


@web_blueprint.get("/payments")
@login_required
def payments():
    try:
        unallocated_only, unresolved_only = _payment_filters()
    except ValueError:
        return (
            render_template(
                "error.html",
                title="Invalid payment filter",
                message=(
                    "Invalid payment filter. Use unallocated=1 and/or unresolved=1."
                ),
                commands=(),
                active_page="payments",
            ),
            400,
        )

    database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
    try:
        require_current_schema(database_path)
        payments_page = composition.build_web_payments(
            database_path,
            unallocated_only=unallocated_only,
            unresolved_only=unresolved_only,
        )
    except DatabaseSchemaError:
        return (
            render_template(
                "error.html",
                title="Database not ready",
                message="The AutoRentLedger database is missing, outdated, or invalid.",
                commands=("autorentledger db status", "autorentledger db upgrade"),
                active_page="payments",
            ),
            503,
        )
    except Exception:  # Safe browser boundary for domain/storage failures.
        current_app.logger.exception("Unable to build payments view")
        return (
            render_template(
                "error.html",
                title="Payments unavailable",
                message="Unable to build payments view.",
                commands=("autorentledger db check",),
                active_page="payments",
            ),
            500,
        )

    return render_template(
        "payments.html", payments=payments_page, active_page="payments"
    )


@web_blueprint.get("/obligations")
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


@web_blueprint.get("/allocation-plan")
@login_required
def allocation_plan():
    period_from = request.args.get("from", "")
    period_to = request.args.get("to", "")
    if not period_from and not period_to:
        return render_template(
            "allocation_plan.html",
            plan=None,
            period_from="",
            period_to="",
            validation_error=None,
            active_page="allocation-plan",
        )

    database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
    try:
        require_current_schema(database_path)
        plan = composition.build_web_allocation_plan(
            database_path, period_from, period_to
        )
    except AllocationPlanValidationError:
        return (
            render_template(
                "allocation_plan.html",
                plan=None,
                period_from=period_from,
                period_to=period_to,
                validation_error=(
                    "Invalid period range. Use YYYY-MM and ensure From is not after To."
                ),
                active_page="allocation-plan",
            ),
            400,
        )
    except DatabaseSchemaError:
        return _database_not_ready("allocation-plan")
    except Exception:  # Safe browser boundary for domain/storage failures.
        current_app.logger.exception("Unable to build allocation plan view")
        return _safe_unavailable(
            "Allocation plan unavailable",
            "Unable to build allocation plan view.",
            "allocation-plan",
        )
    return render_template(
        "allocation_plan.html",
        plan=plan,
        period_from=period_from,
        period_to=period_to,
        validation_error=None,
        active_page="allocation-plan",
    )


@web_blueprint.get("/payments/<int:payment_event_id>")
@login_required
def payment_detail(payment_event_id: int):
    database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
    try:
        require_current_schema(database_path)
        detail = composition.build_web_payment_detail(database_path, payment_event_id)
    except DatabaseSchemaError:
        return _database_not_ready("payments")
    except composition.WebDetailNotFoundError:
        return (
            render_template(
                "error.html",
                title="Payment not found",
                message="The requested payment does not exist.",
                commands=(),
                active_page="payments",
            ),
            404,
        )
    except Exception:  # Safe browser boundary for domain/storage failures.
        current_app.logger.exception("Unable to build payment detail")
        return _safe_unavailable(
            "Payment unavailable", "Unable to build payment detail.", "payments"
        )
    return render_template(
        "payment_detail.html", detail=detail, active_page="payments"
    )


@web_blueprint.get("/rent-accounts/<int:rent_account_id>")
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
    return render_template(
        "rent_account_detail.html", detail=detail, active_page="obligations"
    )


def _database_not_ready(active_page: str):
    return (
        render_template(
            "error.html",
            title="Database not ready",
            message="The AutoRentLedger database is missing, outdated, or invalid.",
            commands=("autorentledger db status", "autorentledger db upgrade"),
            active_page=active_page,
        ),
        503,
    )


def _safe_unavailable(title: str, message: str, active_page: str):
    return (
        render_template(
            "error.html",
            title=title,
            message=message,
            commands=("autorentledger db check",),
            active_page=active_page,
        ),
        500,
    )


def _payment_filters() -> tuple[bool, bool]:
    if set(request.args) - {"unallocated", "unresolved"}:
        raise ValueError

    def enabled(name: str) -> bool:
        values = request.args.getlist(name)
        if not values:
            return False
        if values != ["1"]:
            raise ValueError
        return True

    return enabled("unallocated"), enabled("unresolved")
