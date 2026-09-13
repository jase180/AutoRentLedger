"""Thin GET-only routes over canonical read services."""

from __future__ import annotations

from flask import render_template, request


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
