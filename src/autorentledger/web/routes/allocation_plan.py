"""Thin GET-only routes over canonical read services."""

from __future__ import annotations

from pathlib import Path

from flask import current_app, render_template, request

from autorentledger.allocation_planning import AllocationPlanValidationError
from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition
from autorentledger.web.auth import login_required
from autorentledger.web.routes.common import (
    _database_not_ready,
    _safe_unavailable,
)


def register_routes(blueprint) -> None:
    @blueprint.get("/allocation-plan")
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
            plan = composition.build_web_allocation_plan(database_path, period_from, period_to)
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
