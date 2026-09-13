"""Stable facade for focused web read composition."""

# ruff: noqa: F401 - established names are intentionally re-exported here.

from autorentledger.web.composition.allocation_plan import build_web_allocation_plan
from autorentledger.web.composition.attention import AttentionPage, build_web_attention
from autorentledger.web.composition.common import WebDetailNotFoundError
from autorentledger.web.composition.obligations import ObligationsPage, build_web_obligations
from autorentledger.web.composition.overview import build_web_owner_overview
from autorentledger.web.composition.payments import (
    PaymentAllocationDetail,
    PaymentDetail,
    PaymentsPage,
    build_web_payment_detail,
    build_web_payments,
)
from autorentledger.web.composition.rent_accounts import (
    ContributingPaymentDetail,
    RentAccountDetail,
    RentAccountObligationDetail,
    build_web_rent_account_detail,
)

__all__ = [name for name in globals() if not name.startswith("_")]
