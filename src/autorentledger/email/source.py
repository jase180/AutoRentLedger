"""Source-neutral email types used by the application."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class EmailMessageSummary:
    """The small amount of message metadata needed by Milestone 1."""

    message_id: str
    received_at: datetime
    sender: str
    subject: str


class EmailSource(Protocol):
    """An email provider capable of finding candidate messages."""

    def search(self, query: str, max_results: int = 100) -> list[EmailMessageSummary]:
        """Return message summaries matching a provider-specific query."""
        ...
