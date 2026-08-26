"""Read-only composition helpers shared by local web screens."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autorentledger.overview import OwnerOverview, build_owner_overview
from autorentledger.payment_listing import PaymentListRecord, list_payment_records
from autorentledger.reconciliation import (
    ReconciliationRecord,
    ReconciliationStatus,
    reconcile_period,
)
from autorentledger.review import ReviewItem, ReviewKind, collect_review_items
from autorentledger.storage import (
    SQLitePaymentListingRepository,
    SQLiteReconciliationRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)


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


@dataclass(frozen=True)
class PaymentsPage:
    """Visible payment records and exact totals for the active read-only filters."""

    records: tuple[PaymentListRecord, ...]
    unallocated_only: bool
    unresolved_only: bool
    observed_cents: int
    allocated_cents: int
    unallocated_cents: int

    @property
    def has_filters(self) -> bool:
        return self.unallocated_only or self.unresolved_only


@dataclass(frozen=True)
class ObligationsPage:
    """Selected-period canonical reconciliation records and their exact totals."""

    period: str
    records: tuple[ReconciliationRecord, ...]
    owed_cents: int
    allocated_cents: int
    remaining_cents: int
    paid_count: int
    partial_count: int
    unpaid_count: int


def build_web_owner_overview(database_path: Path, period: str) -> OwnerOverview:
    """Wire existing repositories into the canonical owner overview service."""
    return build_owner_overview(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
        SQLiteRentScheduleRepository(database_path),
        period,
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


def build_web_payments(
    database_path: Path,
    *,
    unallocated_only: bool = False,
    unresolved_only: bool = False,
) -> PaymentsPage:
    """Filter canonical payment records and total only the visible result set."""
    records = list_payment_records(SQLitePaymentListingRepository(database_path))
    visible = tuple(
        record
        for record in records
        if (not unallocated_only or record.unallocated_cents > 0)
        and (not unresolved_only or record.payer_id is None)
    )
    return PaymentsPage(
        records=visible,
        unallocated_only=unallocated_only,
        unresolved_only=unresolved_only,
        observed_cents=sum(record.amount_cents for record in visible),
        allocated_cents=sum(record.allocated_cents for record in visible),
        unallocated_cents=sum(record.unallocated_cents for record in visible),
    )


def build_web_obligations(database_path: Path, period: str) -> ObligationsPage:
    """Total actual obligations returned by canonical period reconciliation."""
    records = tuple(
        reconcile_period(SQLiteReconciliationRepository(database_path), period)
    )
    return ObligationsPage(
        period=period,
        records=records,
        owed_cents=sum(record.owed_cents for record in records),
        allocated_cents=sum(record.allocated_cents for record in records),
        remaining_cents=sum(record.remaining_cents for record in records),
        paid_count=sum(record.status is ReconciliationStatus.PAID for record in records),
        partial_count=sum(
            record.status is ReconciliationStatus.PARTIAL for record in records
        ),
        unpaid_count=sum(
            record.status is ReconciliationStatus.UNPAID for record in records
        ),
    )


def _items_of_kind(
    items: list[ReviewItem], kind: ReviewKind
) -> tuple[ReviewItem, ...]:
    return tuple(item for item in items if item.kind is kind)
