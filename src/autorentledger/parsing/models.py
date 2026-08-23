"""Source-neutral parsing results and failures."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PaymentNotification:
    provider: str
    sender_name: str
    amount_cents: int
    occurred_on: date | None
    memo: str | None


class NotificationParseError(ValueError):
    """A safe, structured parse failure suitable for later manual review."""

    def __init__(self, reason: str, provider: str | None = None) -> None:
        self.reason = reason
        self.provider = provider
        super().__init__(reason)
