"""Read-only composition helpers shared by local web screens."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autorentledger.allocation_planning import AllocationPlan, build_allocation_plan
from autorentledger.gmail_payments import GmailPaymentHistory, get_gmail_payment_history
from autorentledger.late_fees import list_late_fees
from autorentledger.manual_payments import ManualPaymentHistory, get_manual_payment_history
from autorentledger.overview import OwnerOverview, build_owner_overview
from autorentledger.payment_listing import PaymentListRecord, list_payment_records
from autorentledger.reconciliation import (
    ReconciliationRecord,
    ReconciliationStatus,
    reconcile_all,
    reconcile_period,
)
from autorentledger.review import ReviewItem, ReviewKind, collect_review_items
from autorentledger.storage import (
    LateFeeHistory,
    PayerRecord,
    PaymentEventRecord,
    RentAccountSummary,
    SQLiteAllocationPlanningRepository,
    SQLiteAllocationRepository,
    SQLiteGmailPaymentRepository,
    SQLiteLateFeeRepository,
    SQLiteManualPaymentRepository,
    SQLiteObligationRepository,
    SQLitePaymentEventRepository,
    SQLitePaymentListingRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)


class WebDetailNotFoundError(LookupError):
    """A requested payment or rent account does not exist."""


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


@dataclass(frozen=True)
class PaymentAllocationDetail:
    allocation_id: int
    rent_obligation_id: int
    rent_account_id: int
    period: str
    unit_label: str
    account_display_name: str
    amount_cents: int


@dataclass(frozen=True)
class PaymentDetail:
    payment: PaymentListRecord
    event: PaymentEventRecord
    allocations: tuple[PaymentAllocationDetail, ...]
    gmail_history: GmailPaymentHistory | None
    manual_history: ManualPaymentHistory | None

    @property
    def source_type(self) -> str:
        return "Manual" if self.event.manual_evidence_id is not None else "Gmail"


@dataclass(frozen=True)
class ContributingPaymentDetail:
    allocation_id: int
    payment_event_id: int
    occurred_on: str | None
    sender_name: str
    amount_cents: int


@dataclass(frozen=True)
class RentAccountObligationDetail:
    reconciliation: ReconciliationRecord
    contributions: tuple[ContributingPaymentDetail, ...]


@dataclass(frozen=True)
class RentAccountDetail:
    account: RentAccountSummary
    payers: tuple[PayerRecord, ...]
    obligations: tuple[RentAccountObligationDetail, ...]
    late_fees: tuple[LateFeeHistory, ...]


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
        if (not unallocated_only or (record.voided_at is None and record.unallocated_cents > 0))
        and (not unresolved_only or (record.voided_at is None and record.payer_id is None))
    )
    active_visible = tuple(record for record in visible if record.voided_at is None)
    return PaymentsPage(
        records=visible,
        unallocated_only=unallocated_only,
        unresolved_only=unresolved_only,
        observed_cents=sum(record.amount_cents for record in active_visible),
        allocated_cents=sum(record.allocated_cents for record in active_visible),
        unallocated_cents=sum(record.unallocated_cents for record in active_visible),
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


def build_web_allocation_plan(
    database_path: Path, period_from: str, period_to: str
) -> AllocationPlan:
    """Return the exact canonical M26 allocation-plan preview."""
    return build_allocation_plan(
        SQLiteAllocationPlanningRepository(database_path), period_from, period_to
    )


def build_web_payment_detail(
    database_path: Path, payment_event_id: int
) -> PaymentDetail:
    """Compose one normalized payment with canonical audit and allocation reads."""
    listing = list_payment_records(SQLitePaymentListingRepository(database_path))
    payment = next(
        (item for item in listing if item.payment_event_id == payment_event_id), None
    )
    event = SQLitePaymentEventRepository(database_path).get(payment_event_id)
    if payment is None or event is None:
        raise WebDetailNotFoundError(f"Payment {payment_event_id} does not exist.")

    obligation_repository = SQLiteObligationRepository(database_path)
    allocation_details: list[PaymentAllocationDetail] = []
    for allocation in SQLiteAllocationRepository(database_path).list_summaries(
        payment_event_id=payment_event_id
    ):
        obligation = obligation_repository.get_summary(allocation.rent_obligation_id)
        if obligation is None:
            raise RuntimeError("Payment allocation references a missing obligation.")
        allocation_details.append(
            PaymentAllocationDetail(
                allocation.id,
                allocation.rent_obligation_id,
                obligation.rent_account_id,
                allocation.period,
                allocation.unit_label,
                obligation.account_display_name,
                allocation.amount_cents,
            )
        )

    gmail_history = None
    manual_history = None
    if event.manual_evidence_id is not None:
        manual_history = get_manual_payment_history(
            SQLiteManualPaymentRepository(database_path), payment_event_id
        )
    else:
        gmail_history = get_gmail_payment_history(
            SQLiteGmailPaymentRepository(database_path), payment_event_id
        )
    return PaymentDetail(
        payment,
        event,
        tuple(allocation_details),
        gmail_history,
        manual_history,
    )


def build_web_rent_account_detail(
    database_path: Path, rent_account_id: int
) -> RentAccountDetail:
    """Compose account facts around canonical reconciliation records."""
    rental_repository = SQLiteRentalRepository(database_path)
    account = rental_repository.get_rent_account_summary(rent_account_id)
    if account is None:
        raise WebDetailNotFoundError(
            f"Rent account {rent_account_id} does not exist."
        )
    payers = tuple(rental_repository.list_account_payers(rent_account_id))
    reconciliations = sorted(
        (
            record
            for record in reconcile_all(SQLiteReconciliationRepository(database_path))
            if record.rent_account_id == rent_account_id
        ),
        key=lambda record: (record.period, record.due_date, record.obligation_id),
    )
    allocation_repository = SQLiteAllocationRepository(database_path)
    payment_repository = SQLitePaymentEventRepository(database_path)
    obligations: list[RentAccountObligationDetail] = []
    for reconciliation in reconciliations:
        contributions: list[ContributingPaymentDetail] = []
        for allocation in allocation_repository.list_summaries(
            rent_obligation_id=reconciliation.obligation_id
        ):
            payment = payment_repository.get(allocation.payment_event_id)
            if payment is None:
                raise RuntimeError("Obligation allocation references a missing payment.")
            contributions.append(
                ContributingPaymentDetail(
                    allocation.id,
                    payment.id,
                    payment.occurred_on,
                    payment.sender_name,
                    allocation.amount_cents,
                )
            )
        obligations.append(
            RentAccountObligationDetail(reconciliation, tuple(contributions))
        )
    late_fees = list_late_fees(
        SQLiteLateFeeRepository(database_path), account_id=rent_account_id
    )
    return RentAccountDetail(account, payers, tuple(obligations), late_fees)


def _items_of_kind(
    items: list[ReviewItem], kind: ReviewKind
) -> tuple[ReviewItem, ...]:
    return tuple(item for item in items if item.kind is kind)
