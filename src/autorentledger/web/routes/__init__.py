"""Stable web blueprint assembled from focused route modules."""

from flask import Blueprint

from autorentledger.web.routes import (
    allocation_plan,
    attention,
    obligations,
    overview,
    payments,
    rent_accounts,
    root,
)

web_blueprint = Blueprint("web", __name__)

root.register_routes(web_blueprint)
overview.register_routes(web_blueprint)
attention.register_routes(web_blueprint)
payments.register_routes(web_blueprint)
obligations.register_routes(web_blueprint)
allocation_plan.register_routes(web_blueprint)
rent_accounts.register_routes(web_blueprint)

__all__ = ["web_blueprint"]
