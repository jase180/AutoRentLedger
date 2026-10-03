"""Compose canonical ledger projections into one read-only owner snapshot."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

from autorentledger.reconciliation import ReconciliationStatus
from autorentledger.rental_context import UnitContext, UnitContextProjection
from autorentledger.reporting import build_monthly_report
from autorentledger.review import ReviewKind, collect_review_items
from autorentledger.schedules import GenerationAction, plan_obligation_generation
from autorentledger.storage import (
    SQLiteOverviewRepository,
    SQLiteReconciliationRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.suggestions import SuggestionReason, find_allocation_suggestions


@dataclass(frozen=True)
class OverviewRentSummary:
    owed_cents: int
    allocated_cents: int
    remaining_cents: int
    paid_count: int
    partial_count: int
    unpaid_count: int
    total_obligation_count: int


@dataclass(frozen=True)
class OverviewAccountRow(UnitContextProjection):
    rent_obligation_id: int
    rent_account_id: int
    unit: UnitContext
    account_display_name: str
    payer_names: tuple[str, ...]
    period: str
    due_date: str
    owed_cents: int
    allocated_cents: int
    remaining_cents: int
    status: ReconciliationStatus
    last_payment_event_id: int | None
    last_payment_date: date | None
    last_payment_rent_cents: int | None


@dataclass(frozen=True)
class OverviewPaymentSummary:
    received_cents: int
    allocated_from_in_month_payments_cents: int
    unallocated_from_in_month_payments_cents: int


@dataclass(frozen=True)
class OverviewAttentionSummary:
    unresolved_payers: int
    unallocated_payments: int
    partial_obligations: int
    unpaid_obligations: int
    unparsed_emails: int


@dataclass(frozen=True)
class OverviewMissingObligation:
    schedule_id: int
    rent_account_id: int
    unit_label: str
    account_display_name: str
    period: str
    amount_cents: int
    due_day: int


@dataclass(frozen=True)
class OverviewSuggestion:
    payment_event_id: int
    rent_obligation_id: int
    rent_account_id: int
    unit_label: str
    account_display_name: str
    period: str
    suggested_amount_cents: int
    reason: SuggestionReason


@dataclass(frozen=True)
class OwnerOverview:
    period: str
    rent: OverviewRentSummary
    accounts: tuple[OverviewAccountRow, ...]
    payment_intake: OverviewPaymentSummary
    attention: OverviewAttentionSummary
    missing_obligations: tuple[OverviewMissingObligation, ...]
    actionable_suggestions: tuple[OverviewSuggestion, ...]


def build_owner_overview(
    reconciliation_repository: SQLiteReconciliationRepository,
    reporting_repository: SQLiteReportingRepository,
    review_repository: SQLiteReviewRepository,
    suggestion_repository: SQLiteSuggestionRepository,
    schedule_repository: SQLiteRentScheduleRepository,
    overview_repository: SQLiteOverviewRepository,
    period: str,
) -> OwnerOverview:
    """Build one strictly read-only snapshot from existing canonical services."""
    report = build_monthly_report(
        reconciliation_repository, reporting_repository, period
    )
    payer_names_by_account: dict[int, list[str]] = {}
    for payer in overview_repository.list_account_payers():
        payer_names_by_account.setdefault(payer.rent_account_id, []).append(
            payer.payer_display_name
        )
    latest_payment_by_account = {
        payment.rent_account_id: payment
        for payment in overview_repository.list_latest_rent_payment_contributions()
    }
    account_rows: list[OverviewAccountRow] = []
    for row in report.obligations:
        latest_payment = latest_payment_by_account.get(row.rent_account_id)
        account_rows.append(
            OverviewAccountRow(
                rent_obligation_id=row.obligation_id,
                rent_account_id=row.rent_account_id,
                unit=row.unit,
                account_display_name=row.account_display_name,
                payer_names=tuple(payer_names_by_account.get(row.rent_account_id, ())),
                period=row.period,
                due_date=row.due_date,
                owed_cents=row.owed_cents,
                allocated_cents=row.allocated_cents,
                remaining_cents=row.remaining_cents,
                status=row.status,
                last_payment_event_id=(
                    latest_payment.payment_event_id if latest_payment else None
                ),
                last_payment_date=(
                    date.fromisoformat(latest_payment.occurred_on)
                    if latest_payment and latest_payment.occurred_on
                    else None
                ),
                last_payment_rent_cents=(
                    latest_payment.rent_cents if latest_payment else None
                ),
            )
        )
    accounts = tuple(account_rows)
    rent = OverviewRentSummary(
        owed_cents=report.total_owed_cents,
        allocated_cents=report.total_allocated_cents,
        remaining_cents=report.total_remaining_cents,
        paid_count=report.paid_count,
        partial_count=report.partial_count,
        unpaid_count=report.unpaid_count,
        total_obligation_count=len(accounts),
    )
    payment_intake = OverviewPaymentSummary(
        received_cents=report.payment_received_cents,
        allocated_from_in_month_payments_cents=report.payment_allocated_cents,
        unallocated_from_in_month_payments_cents=report.payment_unallocated_cents,
    )

    review_counts = Counter(
        item.kind
        for item in collect_review_items(
            reconciliation_repository, review_repository
        )
    )
    attention = OverviewAttentionSummary(
        unresolved_payers=review_counts[ReviewKind.UNRESOLVED_PAYER],
        unallocated_payments=review_counts[ReviewKind.UNALLOCATED_PAYMENT],
        partial_obligations=review_counts[ReviewKind.PARTIAL_OBLIGATION],
        unpaid_obligations=review_counts[ReviewKind.UNPAID_OBLIGATION],
        unparsed_emails=review_counts[ReviewKind.UNPARSED_EMAIL],
    )

    suggestion_results = find_allocation_suggestions(
        suggestion_repository, reconciliation_repository
    )
    actionable_suggestions = tuple(
        OverviewSuggestion(
            payment_event_id=suggestion.payment_event_id,
            rent_obligation_id=suggestion.rent_obligation_id,
            rent_account_id=suggestion.rent_account_id,
            unit_label=suggestion.unit_label,
            account_display_name=suggestion.account_display_name,
            period=suggestion.period,
            suggested_amount_cents=suggestion.suggested_amount_cents,
            reason=suggestion.reason,
        )
        for result in suggestion_results
        if (suggestion := result.suggestion) is not None
    )

    generation_plan = plan_obligation_generation(schedule_repository, report.period)
    missing_obligations = tuple(
        OverviewMissingObligation(
            schedule_id=item.schedule_id,
            rent_account_id=item.rent_account_id,
            unit_label=item.unit_label,
            account_display_name=item.account_display_name,
            period=item.period,
            amount_cents=item.amount_cents,
            due_day=item.due_date.day,
        )
        for item in generation_plan.items
        if item.action is GenerationAction.CREATE
    )

    return OwnerOverview(
        period=report.period,
        rent=rent,
        accounts=accounts,
        payment_intake=payment_intake,
        attention=attention,
        missing_obligations=missing_obligations,
        actionable_suggestions=actionable_suggestions,
    )
