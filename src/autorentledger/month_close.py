"""Compose canonical read models into one derived monthly owner review."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from autorentledger.obligations import parse_monthly_period
from autorentledger.property_cash import PropertyCashSummary, build_property_cash_summary
from autorentledger.reconciliation import (
    ReconciliationRecord,
    ReconciliationStatus,
    reconcile_period,
)
from autorentledger.review import ReviewItem, ReviewKind, collect_review_items
from autorentledger.schedules import (
    GenerationAction,
    ObligationGenerationItem,
    plan_obligation_generation,
)
from autorentledger.storage import (
    SQLitePropertyCashRepository,
    SQLiteReconciliationRepository,
    SQLiteRentScheduleRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.suggestions import AllocationSuggestion, find_allocation_suggestions


class MonthCloseStatus(StrEnum):
    CLEAR = "CLEAR"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


@dataclass(frozen=True)
class MonthCloseRentSummary:
    obligation_count: int
    paid_count: int
    partial_count: int
    unpaid_count: int
    owed_cents: int
    allocated_cents: int
    remaining_cents: int
    obligations: tuple[ReconciliationRecord, ...]


@dataclass(frozen=True)
class MonthCloseAttentionSummary:
    active_payment_count: int
    unresolved_sender_count: int
    unresolved_payment_count: int
    unresolved_senders: tuple[ReviewItem, ...]
    payments_with_unallocated_count: int
    unallocated_cents: int
    unallocated_payments: tuple[ReviewItem, ...]
    actionable_suggestions: tuple[AllocationSuggestion, ...]
    unparsed_evidence_count: int
    unparsed_evidence: tuple[ReviewItem, ...]

    @property
    def actionable_suggestion_count(self) -> int:
        return len(self.actionable_suggestions)


@dataclass(frozen=True)
class MonthCloseRecurringRentSummary:
    missing_expected_obligations: tuple[ObligationGenerationItem, ...]
    existing_obligation_count: int

    @property
    def missing_expected_obligation_count(self) -> int:
        return len(self.missing_expected_obligations)


@dataclass(frozen=True)
class MonthCloseExpenseSummary:
    active_expense_count: int
    operating_expense_cents: int
    capital_improvement_cents: int


@dataclass(frozen=True)
class MonthCloseSummary:
    period: str
    status: MonthCloseStatus
    rent: MonthCloseRentSummary
    attention: MonthCloseAttentionSummary
    recurring_rent: MonthCloseRecurringRentSummary
    expenses: MonthCloseExpenseSummary
    property_cash: tuple[PropertyCashSummary, ...]


def build_month_close(
    reconciliation_repository: SQLiteReconciliationRepository,
    review_repository: SQLiteReviewRepository,
    suggestion_repository: SQLiteSuggestionRepository,
    schedule_repository: SQLiteRentScheduleRepository,
    property_cash_repository: SQLitePropertyCashRepository,
    period: str,
) -> MonthCloseSummary:
    """Build a read-only owner review without creating or repairing ledger facts."""
    canonical_period = parse_monthly_period(period).value
    obligations = tuple(reconcile_period(reconciliation_repository, canonical_period))
    rent = _rent_summary(obligations)

    review_items = collect_review_items(reconciliation_repository, review_repository)
    unresolved_senders = _review_items(review_items, ReviewKind.UNRESOLVED_PAYER)
    unallocated_payments = _review_items(review_items, ReviewKind.UNALLOCATED_PAYMENT)
    unparsed_evidence = _review_items(review_items, ReviewKind.UNPARSED_EMAIL)

    suggestion_results = find_allocation_suggestions(
        suggestion_repository, reconciliation_repository
    )
    actionable_suggestions = tuple(
        suggestion
        for result in suggestion_results
        if (suggestion := result.suggestion) is not None and suggestion.period == canonical_period
    )
    attention = MonthCloseAttentionSummary(
        active_payment_count=len(suggestion_results),
        unresolved_sender_count=len(unresolved_senders),
        unresolved_payment_count=sum(item.count or 0 for item in unresolved_senders),
        unresolved_senders=unresolved_senders,
        payments_with_unallocated_count=len(unallocated_payments),
        unallocated_cents=sum(item.amount_cents or 0 for item in unallocated_payments),
        unallocated_payments=unallocated_payments,
        actionable_suggestions=actionable_suggestions,
        unparsed_evidence_count=len(unparsed_evidence),
        unparsed_evidence=unparsed_evidence,
    )

    generation_plan = plan_obligation_generation(schedule_repository, canonical_period)
    recurring_rent = MonthCloseRecurringRentSummary(
        missing_expected_obligations=tuple(
            item for item in generation_plan.items if item.action is GenerationAction.CREATE
        ),
        existing_obligation_count=generation_plan.skip_count,
    )

    cash_portfolio = build_property_cash_summary(property_cash_repository, canonical_period)
    property_cash = cash_portfolio.properties
    expenses = MonthCloseExpenseSummary(
        active_expense_count=sum(len(item.expenses) for item in property_cash),
        operating_expense_cents=sum(item.operating_expense_cents for item in property_cash),
        capital_improvement_cents=sum(item.capital_improvement_cents for item in property_cash),
    )

    return MonthCloseSummary(
        period=canonical_period,
        status=_derive_status(rent, attention, recurring_rent),
        rent=rent,
        attention=attention,
        recurring_rent=recurring_rent,
        expenses=expenses,
        property_cash=property_cash,
    )


def _rent_summary(
    obligations: tuple[ReconciliationRecord, ...],
) -> MonthCloseRentSummary:
    return MonthCloseRentSummary(
        obligation_count=len(obligations),
        paid_count=sum(item.status is ReconciliationStatus.PAID for item in obligations),
        partial_count=sum(item.status is ReconciliationStatus.PARTIAL for item in obligations),
        unpaid_count=sum(item.status is ReconciliationStatus.UNPAID for item in obligations),
        owed_cents=sum(item.owed_cents for item in obligations),
        allocated_cents=sum(item.allocated_cents for item in obligations),
        remaining_cents=sum(item.remaining_cents for item in obligations),
        obligations=obligations,
    )


def _review_items(items: list[ReviewItem], kind: ReviewKind) -> tuple[ReviewItem, ...]:
    return tuple(item for item in items if item.kind is kind)


def _derive_status(
    rent: MonthCloseRentSummary,
    attention: MonthCloseAttentionSummary,
    recurring_rent: MonthCloseRecurringRentSummary,
) -> MonthCloseStatus:
    needs_attention = (
        rent.partial_count > 0
        or rent.unpaid_count > 0
        or attention.unresolved_sender_count > 0
        or attention.payments_with_unallocated_count > 0
        or attention.actionable_suggestion_count > 0
        or attention.unparsed_evidence_count > 0
        or recurring_rent.missing_expected_obligation_count > 0
    )
    if needs_attention:
        return MonthCloseStatus.NEEDS_ATTENTION
    return MonthCloseStatus.CLEAR


__all__ = [
    "MonthCloseAttentionSummary",
    "MonthCloseExpenseSummary",
    "MonthCloseRecurringRentSummary",
    "MonthCloseRentSummary",
    "MonthCloseStatus",
    "MonthCloseSummary",
    "build_month_close",
]
