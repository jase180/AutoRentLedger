"""Derive ledger records that currently need human attention."""

from dataclasses import dataclass
from enum import StrEnum

from autorentledger.identity import unresolved_sender_counts
from autorentledger.reconciliation import ReconciliationStatus, reconcile_all
from autorentledger.storage import SQLiteReconciliationRepository, SQLiteReviewRepository


class ReviewKind(StrEnum):
    UNRESOLVED_PAYER = "UNRESOLVED_PAYER"
    UNALLOCATED_PAYMENT = "UNALLOCATED_PAYMENT"
    UNPAID_OBLIGATION = "UNPAID_OBLIGATION"
    PARTIAL_OBLIGATION = "PARTIAL_OBLIGATION"
    UNPARSED_EMAIL = "UNPARSED_EMAIL"


class ReviewInvariantError(RuntimeError):
    """Stored payment allocation rows violate ledger invariants."""


@dataclass(frozen=True)
class ReviewItem:
    kind: ReviewKind
    reference_id: int | None
    summary: str
    amount_cents: int | None = None
    period: str | None = None
    unit_label: str | None = None
    account_display_name: str | None = None
    count: int | None = None


def collect_review_items(
    reconciliation: SQLiteReconciliationRepository,
    review: SQLiteReviewRepository,
) -> list[ReviewItem]:
    """Return the current review list without writing or persisting review state."""
    items = [
        ReviewItem(
            kind=ReviewKind.UNRESOLVED_PAYER,
            reference_id=None,
            summary=sender.sender_name,
            count=sender.count,
        )
        for sender in unresolved_sender_counts(
            review.list_sender_counts(), review.list_normalized_aliases()
        )
    ]

    for source in review.list_payment_allocation_totals():
        remaining_cents = source.amount_cents - source.allocated_cents
        if remaining_cents < 0:
            raise ReviewInvariantError(
                f"Review invariant violated for payment {source.payment_event_id}: "
                "allocated amount exceeds payment amount."
            )
        if remaining_cents > 0:
            items.append(
                ReviewItem(
                    kind=ReviewKind.UNALLOCATED_PAYMENT,
                    reference_id=source.payment_event_id,
                    summary="remaining unallocated",
                    amount_cents=remaining_cents,
                )
            )

    for obligation in reconcile_all(reconciliation):
        if obligation.status is ReconciliationStatus.UNPAID:
            kind = ReviewKind.UNPAID_OBLIGATION
        elif obligation.status is ReconciliationStatus.PARTIAL:
            kind = ReviewKind.PARTIAL_OBLIGATION
        else:
            continue
        items.append(
            ReviewItem(
                kind=kind,
                reference_id=obligation.obligation_id,
                summary="remaining",
                amount_cents=obligation.remaining_cents,
                period=obligation.period,
                unit_label=obligation.unit_label,
                account_display_name=obligation.account_display_name,
            )
        )

    items.extend(
        ReviewItem(
            kind=ReviewKind.UNPARSED_EMAIL,
            reference_id=source.raw_email_id,
            summary=source.subject,
        )
        for source in review.list_unparsed_emails()
    )
    return sorted(
        items,
        key=lambda item: (item.kind.value, item.reference_id or 0, item.summary.casefold()),
    )
