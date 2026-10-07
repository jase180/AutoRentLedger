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


class MonthCloseCheckStatus(StrEnum):
    PASS = "PASS"
    ATTENTION = "ATTENTION"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class MonthCloseCheck:
    key: str
    label: str
    status: MonthCloseCheckStatus
    detail: str
    count: int | None = None


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
    checks: tuple[MonthCloseCheck, ...]
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
    checks = _build_checks(rent, attention, recurring_rent, expenses, property_cash)

    return MonthCloseSummary(
        period=canonical_period,
        status=_derive_status(checks),
        checks=checks,
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


def _build_checks(
    rent: MonthCloseRentSummary,
    attention: MonthCloseAttentionSummary,
    recurring_rent: MonthCloseRecurringRentSummary,
    expenses: MonthCloseExpenseSummary,
    property_cash: tuple[PropertyCashSummary, ...],
) -> tuple[MonthCloseCheck, ...]:
    if rent.obligation_count == 0:
        rent_check = MonthCloseCheck(
            "rent_balances",
            "Rent balances",
            MonthCloseCheckStatus.NOT_APPLICABLE,
            "No rent obligations for this month",
            0,
        )
    elif rent.partial_count or rent.unpaid_count:
        rent_check = MonthCloseCheck(
            "rent_balances",
            "Rent balances",
            MonthCloseCheckStatus.ATTENTION,
            f"{rent.partial_count} partial, {rent.unpaid_count} unpaid",
            rent.partial_count + rent.unpaid_count,
        )
    else:
        rent_check = MonthCloseCheck(
            "rent_balances",
            "Rent balances",
            MonthCloseCheckStatus.PASS,
            f"{rent.obligation_count} "
            f"{'obligation' if rent.obligation_count == 1 else 'obligations'} fully paid",
            rent.obligation_count,
        )

    recurring_count = (
        recurring_rent.existing_obligation_count + recurring_rent.missing_expected_obligation_count
    )
    if recurring_rent.missing_expected_obligation_count:
        recurring_check = MonthCloseCheck(
            "recurring_rent",
            "Recurring rent coverage",
            MonthCloseCheckStatus.ATTENTION,
            (
                f"{recurring_rent.missing_expected_obligation_count} expected obligation is missing"
                if recurring_rent.missing_expected_obligation_count == 1
                else (
                    f"{recurring_rent.missing_expected_obligation_count} "
                    "expected obligations are missing"
                )
            ),
            recurring_rent.missing_expected_obligation_count,
        )
    elif recurring_count:
        recurring_check = MonthCloseCheck(
            "recurring_rent",
            "Recurring rent coverage",
            MonthCloseCheckStatus.PASS,
            f"{recurring_rent.existing_obligation_count} expected "
            f"{'obligation already exists' if recurring_rent.existing_obligation_count == 1 else 'obligations already exist'}",
            recurring_rent.existing_obligation_count,
        )
    else:
        recurring_check = MonthCloseCheck(
            "recurring_rent",
            "Recurring rent coverage",
            MonthCloseCheckStatus.NOT_APPLICABLE,
            "No recurring rent applies this month",
            0,
        )

    if attention.active_payment_count == 0:
        unallocated_check = MonthCloseCheck(
            "unallocated_payments",
            "Unallocated payment money",
            MonthCloseCheckStatus.NOT_APPLICABLE,
            "No active observed payments",
            0,
        )
        identity_check = MonthCloseCheck(
            "payer_identity",
            "Sender identity resolution",
            MonthCloseCheckStatus.NOT_APPLICABLE,
            "No active observed payments",
            0,
        )
    else:
        unallocated_count = attention.payments_with_unallocated_count
        unallocated_check = MonthCloseCheck(
            "unallocated_payments",
            "Unallocated payment money",
            (MonthCloseCheckStatus.ATTENTION if unallocated_count else MonthCloseCheckStatus.PASS),
            (
                f"{unallocated_count} "
                f"{'payment' if unallocated_count == 1 else 'payments'}, "
                f"{_format_cents(attention.unallocated_cents)} remaining"
                if unallocated_count
                else "No active payment money remains unallocated"
            ),
            unallocated_count,
        )
        unresolved_count = attention.unresolved_sender_count
        identity_check = MonthCloseCheck(
            "payer_identity",
            "Sender identity resolution",
            (MonthCloseCheckStatus.ATTENTION if unresolved_count else MonthCloseCheckStatus.PASS),
            (
                f"{unresolved_count} unresolved {'sender' if unresolved_count == 1 else 'senders'}"
                if unresolved_count
                else "All active payment senders resolved"
            ),
            unresolved_count,
        )

    suggestion_count = attention.actionable_suggestion_count
    suggestion_check = MonthCloseCheck(
        "allocation_suggestions",
        "Allocation suggestions",
        (MonthCloseCheckStatus.ATTENTION if suggestion_count else MonthCloseCheckStatus.PASS),
        (
            f"{suggestion_count} actionable suggestions"
            if suggestion_count != 1
            else "1 actionable suggestion"
        ),
        suggestion_count,
    )
    if not suggestion_count:
        suggestion_check = MonthCloseCheck(
            "allocation_suggestions",
            "Allocation suggestions",
            MonthCloseCheckStatus.PASS,
            "No actionable suggestions",
            0,
        )

    unparsed_count = attention.unparsed_evidence_count
    evidence_check = MonthCloseCheck(
        "evidence_parsing",
        "Evidence parsing",
        MonthCloseCheckStatus.ATTENTION if unparsed_count else MonthCloseCheckStatus.PASS,
        (
            "1 unparsed email"
            if unparsed_count == 1
            else (f"{unparsed_count} unparsed emails" if unparsed_count else "No unparsed evidence")
        ),
        unparsed_count,
    )

    expense_count = expenses.active_expense_count
    expense_check = MonthCloseCheck(
        "expenses",
        "Expense review",
        (MonthCloseCheckStatus.PASS if expense_count else MonthCloseCheckStatus.NOT_APPLICABLE),
        (
            f"{expense_count} active expenses recorded"
            if expense_count != 1
            else "1 active expense recorded"
        )
        if expense_count
        else "No active expenses recorded",
        expense_count,
    )

    property_count = len(property_cash)
    property_cash_check = MonthCloseCheck(
        "property_cash",
        "Property cash review",
        (MonthCloseCheckStatus.PASS if property_count else MonthCloseCheckStatus.NOT_APPLICABLE),
        (
            f"{property_count} Properties summarized"
            if property_count != 1
            else "1 Property summarized"
        )
        if property_count
        else "No Properties configured",
        property_count,
    )

    return (
        rent_check,
        recurring_check,
        unallocated_check,
        identity_check,
        suggestion_check,
        evidence_check,
        expense_check,
        property_cash_check,
    )


def _derive_status(checks: tuple[MonthCloseCheck, ...]) -> MonthCloseStatus:
    if any(check.status is MonthCloseCheckStatus.ATTENTION for check in checks):
        return MonthCloseStatus.NEEDS_ATTENTION
    return MonthCloseStatus.CLEAR


def _format_cents(amount_cents: int) -> str:
    return f"${amount_cents / 100:,.2f}"


__all__ = [
    "MonthCloseAttentionSummary",
    "MonthCloseCheck",
    "MonthCloseCheckStatus",
    "MonthCloseExpenseSummary",
    "MonthCloseRecurringRentSummary",
    "MonthCloseRentSummary",
    "MonthCloseStatus",
    "MonthCloseSummary",
    "build_month_close",
]
