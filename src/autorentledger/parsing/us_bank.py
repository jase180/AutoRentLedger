"""Parser for the observed forwarded U.S. Bank Zelle notification formats."""

import re
import time
from datetime import date

from autorentledger.parsing.mime import DecodedEmail
from autorentledger.parsing.models import NotificationParseError, PaymentNotification
from autorentledger.parsing.values import currency_to_cents, optional_value

PROVIDER = "us_bank"


def matches(message: DecodedEmail) -> bool:
    return bool(
        re.search(r"(?im)^From:\s*U\.S\. Bank Alerts(?:\s|<)", message.text)
        and re.search(r"(?i)Zelle", message.text)
    )


def parse(message: DecodedEmail) -> PaymentNotification:
    payment_line = next(
        (
            line
            for line in message.text.splitlines()
            if "payment" in line.casefold()
            and "from" in line.casefold()
            and "deposited" in line.casefold()
        ),
        None,
    )
    if payment_line is None:
        raise NotificationParseError("unrecognized_us_bank_format", PROVIDER)

    amount_match = re.search(r"\$(?P<amount>\d[\d,]*\.\d{2})", payment_line)
    sender_match = re.search(
        r"(?i)\bfrom\s+(?P<sender>.+?)\s+was\s+deposited\b",
        payment_line,
    )
    if amount_match is None:
        raise NotificationParseError("missing_required_amount", PROVIDER)
    if sender_match is None:
        raise NotificationParseError("missing_required_sender", PROVIDER)

    date_match = re.search(
        r"(?im)^Received\s+date\s*:\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})\s*$",
        message.text,
    )
    memo_match = re.search(r"(?im)^Message\s+from\s+.+?:\s*(?P<memo>.+?)\s*$", message.text)

    return PaymentNotification(
        provider=PROVIDER,
        sender_name=sender_match.group("sender").strip(),
        amount_cents=currency_to_cents(amount_match.group("amount")),
        occurred_on=_parse_date(date_match.group("date")) if date_match else None,
        memo=optional_value(memo_match.group("memo")) if memo_match else None,
    )


def _parse_date(value: str) -> date:
    try:
        parsed = time.strptime(value, "%m/%d/%Y")
        return date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday)
    except ValueError as error:
        raise NotificationParseError("invalid_occurred_date", PROVIDER) from error
