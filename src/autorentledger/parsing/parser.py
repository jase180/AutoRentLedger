"""Identify a supported provider and parse a normalized notification."""

from autorentledger.parsing import chase, us_bank
from autorentledger.parsing.mime import decode_email
from autorentledger.parsing.models import NotificationParseError, PaymentNotification


def parse_payment_notification(raw_mime: bytes) -> PaymentNotification:
    message = decode_email(raw_mime)
    if chase.matches(message):
        return chase.parse(message)
    if us_bank.matches(message):
        return us_bank.parse(message)
    raise NotificationParseError("unsupported_provider")
