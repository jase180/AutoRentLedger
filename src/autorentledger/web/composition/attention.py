"""Read-only composition helpers shared by local web screens."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autorentledger.review import ReviewItem, ReviewKind, collect_review_items
from autorentledger.storage import (
    SQLiteReconciliationRepository,
    SQLiteReviewRepository,
)
from autorentledger.web.composition.common import _items_of_kind


@dataclass(frozen=True)
class AttentionPage:
    """Presentation grouping of canonical review items without new review semantics."""

    unresolved_payers: tuple[ReviewItem, ...]
    unallocated_payments: tuple[ReviewItem, ...]
    partial_obligations: tuple[ReviewItem, ...]
    unpaid_obligations: tuple[ReviewItem, ...]
    unparsed_emails: tuple[ReviewItem, ...]

    @property
    def total_count(self) -> int:
        return sum(
            len(items)
            for items in (
                self.unresolved_payers,
                self.unallocated_payments,
                self.partial_obligations,
                self.unpaid_obligations,
                self.unparsed_emails,
            )
        )

def build_web_attention(database_path: Path) -> AttentionPage:
    """Group canonical review results for rendering while preserving item order."""
    items = collect_review_items(
        SQLiteReconciliationRepository(database_path),
        SQLiteReviewRepository(database_path),
    )
    return AttentionPage(
        unresolved_payers=_items_of_kind(items, ReviewKind.UNRESOLVED_PAYER),
        unallocated_payments=_items_of_kind(items, ReviewKind.UNALLOCATED_PAYMENT),
        partial_obligations=_items_of_kind(items, ReviewKind.PARTIAL_OBLIGATION),
        unpaid_obligations=_items_of_kind(items, ReviewKind.UNPAID_OBLIGATION),
        unparsed_emails=_items_of_kind(items, ReviewKind.UNPARSED_EMAIL),
    )
