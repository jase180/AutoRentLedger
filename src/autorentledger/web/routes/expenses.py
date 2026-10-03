"""GET-only Property expense routes."""

from __future__ import annotations

from pathlib import Path

from flask import current_app, render_template, request

from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition
from autorentledger.web.auth import login_required
from autorentledger.web.routes.common import _database_not_ready, _safe_unavailable


def register_routes(blueprint) -> None:
    @blueprint.get("/expenses")
    @login_required
    def expenses():
        values = request.args.getlist("include_voided")
        if set(request.args) - {"include_voided"} or values not in ([], ["1"]):
            return (
                render_template(
                    "error.html",
                    title="Invalid expense filter",
                    message="Invalid expense filter. Use include_voided=1.",
                    commands=(),
                    active_page="expenses",
                ),
                400,
            )
        database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
        try:
            require_current_schema(database_path)
            page = composition.build_web_expenses(
                database_path, include_voided=values == ["1"]
            )
        except DatabaseSchemaError:
            return _database_not_ready("expenses")
        except Exception:  # Safe browser boundary for domain/storage failures.
            current_app.logger.exception("Unable to build expenses view")
            return _safe_unavailable(
                "Expenses unavailable", "Unable to build expenses view.", "expenses"
            )
        return render_template("expenses.html", expenses=page, active_page="expenses")

    @blueprint.get("/expenses/<int:expense_id>")
    @login_required
    def expense_detail(expense_id: int):
        database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
        try:
            require_current_schema(database_path)
            expense = composition.build_web_expense_detail(database_path, expense_id)
        except DatabaseSchemaError:
            return _database_not_ready("expenses")
        except composition.WebDetailNotFoundError:
            return (
                render_template(
                    "error.html",
                    title="Expense not found",
                    message="The requested expense does not exist.",
                    commands=(),
                    active_page="expenses",
                ),
                404,
            )
        except Exception:  # Safe browser boundary for domain/storage failures.
            current_app.logger.exception("Unable to build expense detail")
            return _safe_unavailable(
                "Expense unavailable", "Unable to build expense detail.", "expenses"
            )
        return render_template(
            "expense_detail.html", expense=expense, active_page="expenses"
        )
