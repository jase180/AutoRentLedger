"""GET-only monthly Property cash routes."""

from __future__ import annotations

from pathlib import Path

from flask import current_app, render_template, request

from autorentledger.obligations import ObligationValidationError, parse_monthly_period
from autorentledger.property_cash import PropertyCashPropertyNotFoundError
from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema
from autorentledger.web import composition
from autorentledger.web.auth import login_required
from autorentledger.web.routes.common import _database_not_ready, _safe_unavailable


def register_routes(blueprint) -> None:
    @blueprint.get("/property-cash")
    @login_required
    def property_cash():
        if set(request.args) - {"period", "property"}:
            return _invalid_request()
        period_values = request.args.getlist("period")
        property_values = request.args.getlist("property")
        if len(period_values) > 1 or len(property_values) > 1:
            return _invalid_request()
        try:
            supplied_period = (
                period_values[0]
                if period_values
                else current_app.config["AUTORENTLEDGER_TODAY"]().strftime("%Y-%m")
            )
            period = parse_monthly_period(supplied_period).value
            property_id = _parse_property(property_values)
        except (ObligationValidationError, ValueError):
            return _invalid_request()

        database_path = Path(current_app.config["AUTORENTLEDGER_DATABASE"])
        try:
            require_current_schema(database_path)
            portfolio = composition.build_web_property_cash(
                database_path, period, property_id
            )
        except DatabaseSchemaError:
            return _database_not_ready("property-cash")
        except PropertyCashPropertyNotFoundError as error:
            return (
                render_template(
                    "error.html",
                    title="Property not found",
                    message=str(error),
                    commands=(),
                    active_page="property-cash",
                ),
                404,
            )
        except Exception:  # Safe browser boundary for domain/storage failures.
            current_app.logger.exception("Unable to build Property cash view")
            return _safe_unavailable(
                "Property Cash unavailable",
                "Unable to build Property Cash view.",
                "property-cash",
            )
        return render_template(
            "property_cash.html",
            portfolio=portfolio,
            selected=portfolio.properties[0] if property_id is not None else None,
            active_page="property-cash",
        )


def _parse_property(values: list[str]) -> int | None:
    if not values:
        return None
    property_id = int(values[0])
    if property_id <= 0 or str(property_id) != values[0]:
        raise ValueError
    return property_id


def _invalid_request():
    return (
        render_template(
            "error.html",
            title="Invalid Property Cash filter",
            message="Invalid filters. Use one period in YYYY-MM form and an optional Property ID.",
            commands=(),
            active_page="property-cash",
        ),
        400,
    )


__all__ = ["register_routes"]
