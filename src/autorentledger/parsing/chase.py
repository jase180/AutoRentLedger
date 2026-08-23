"""Parser for the observed forwarded Chase Zelle notification format."""

import re
import time
from datetime import date

from autorentledger.parsing.mime import DecodedEmail
from autorentledger.parsing.models import NotificationParseError, PaymentNotification
from autorentledger.parsing.values import currency_to_cents, optional_value

PROVIDER = "chase"


def matches(message: DecodedEmail) -> bool:
    return bool(
        re.search(r"(?im)^From:\s*Chase(?:\s|<)", message.text)
        and re.search(r"(?i)Zelle", message.text)
    )


def parse(message: DecodedEmail) -> PaymentNotification:
    amount_match = re.search(
        r"(?im)^Amount\s*:?\s*\$(?P<amount>\d[\d,]*\.\d{2})\s*$",
        message.text,
    )
    sender_match = re.search(
        r"(?im)^(?P<sender>[^\r\n]+?)\s+sent\s+you\s+money\s*$",
        message.text,
    )
    if amount_match is None and sender_match is None:
        raise NotificationParseError("unrecognized_chase_format", PROVIDER)
    if amount_match is None:
        raise NotificationParseError("missing_required_amount", PROVIDER)
    if sender_match is None:
        raise NotificationParseError("missing_required_sender", PROVIDER)

    date_match = re.search(
        r"(?im)^Sent\s+on\s+(?P<date>[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s*$",
        message.text,
    )
    memo_match = re.search(r"(?im)^Memo(?:\s*:\s*|\s+)(?P<memo>.+?)\s*$", message.text)

    return PaymentNotification(
        provider=PROVIDER,
        sender_name=sender_match.group("sender").strip(),
        amount_cents=currency_to_cents(amount_match.group("amount")),
        occurred_on=_parse_date(date_match.group("date")) if date_match else None,
        memo=optional_value(memo_match.group("memo")) if memo_match else None,
    )


def _parse_date(value: str) -> date:
    try:
        parsed = time.strptime(value, "%b %d, %Y")
        return date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday)
    except ValueError as error:
        raise NotificationParseError("invalid_occurred_date", PROVIDER) from error
