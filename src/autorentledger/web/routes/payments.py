"""Thin GET-only routes over canonical read services."""

from __future__ import annotations

from pathlib import Path

from flask import current_app, render_template

from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition
from autorentledger.web.auth import login_required
from autorentledger.web.routes.common import (
    _database_not_ready,
    _payment_filters,
    _safe_unavailable,
)


def register_routes(blueprint) -> None:
    @blueprint.get("/payments")
    @login_required
    def payments():
        try:
            unallocated_only, unresolved_only = _payment_filters()
        except ValueError:
            return (
                render_template(
                    "error.html",
                    title="Invalid payment filter",
                    message=("Invalid payment filter. Use unallocated=1 and/or unresolved=1."),
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

        return render_template("payments.html", payments=payments_page, active_page="payments")

    @blueprint.get("/payments/<int:payment_event_id>")
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
        return render_template("payment_detail.html", detail=detail, active_page="payments")
