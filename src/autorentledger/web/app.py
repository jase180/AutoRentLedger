"""Flask application factory for the local read-only web view."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from flask import Flask

from autorentledger.web.routes import web_blueprint

_MONTH_NAMES = (
    "JANUARY",
    "FEBRUARY",
    "MARCH",
    "APRIL",
    "MAY",
    "JUNE",
    "JULY",
    "AUGUST",
    "SEPTEMBER",
    "OCTOBER",
    "NOVEMBER",
    "DECEMBER",
)


def create_app(database_path: Path) -> Flask:
    """Create the local web adapter without opening or changing the ledger."""
    app = Flask(__name__)
    app.config.update(
        AUTORENTLEDGER_DATABASE=Path(database_path),
        AUTORENTLEDGER_TODAY=date.today,
    )
    app.jinja_env.filters["money"] = _format_money
    app.jinja_env.filters["month_heading"] = _format_month_heading
    app.jinja_env.globals["collection_percentage"] = _collection_percentage
    app.jinja_env.globals["bounded_percentage"] = _bounded_percentage
    app.register_blueprint(web_blueprint)
    return app


def _format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{remainder:02d}"


def _format_month_heading(period: str) -> str:
    year, month = period.split("-")
    return f"{_MONTH_NAMES[int(month) - 1]} {year}"


def _collection_percentage(allocated_cents: int, owed_cents: int) -> float | None:
    if owed_cents == 0:
        return None
    return allocated_cents / owed_cents * 100


def _bounded_percentage(percentage: float | None) -> float:
    if percentage is None:
        return 0.0
    return min(max(percentage, 0.0), 100.0)
