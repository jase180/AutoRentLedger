"""Deterministic parsing of stored payment notification emails."""

from autorentledger.parsing.models import NotificationParseError, PaymentNotification
from autorentledger.parsing.parser import parse_payment_notification
from autorentledger.parsing.version import (
    CURRENT_PAYMENT_PARSER_VERSION,
    LEGACY_UNVERSIONED_PARSER_VERSION,
)

__all__ = [
    "CURRENT_PAYMENT_PARSER_VERSION",
    "LEGACY_UNVERSIONED_PARSER_VERSION",
    "NotificationParseError",
    "PaymentNotification",
    "parse_payment_notification",
]
