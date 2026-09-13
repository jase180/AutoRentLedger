"""Thin GET-only routes over canonical read services."""

from __future__ import annotations

from flask import current_app, redirect, url_for

from autorentledger.web.auth import login_required


def register_routes(blueprint) -> None:
    @blueprint.get("/")
    @login_required
    def root():
        today = current_app.config["AUTORENTLEDGER_TODAY"]()
        return redirect(url_for("web.overview", period=f"{today.year:04d}-{today.month:02d}"))
