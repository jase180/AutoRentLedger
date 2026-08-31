"""Deterministic, review-first planning over explicit identity and accounting state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from autorentledger.identity import normalize_alias
from autorentledger.obligations import ObligationValidationError, parse_monthly_period
from autorentledger.reconciliation import (
    ReconciliationStatus,
    derive_reconciliation_status,
)
from autorentledger.storage import (
    AllocationExceedsObligationError,
    AllocationExceedsPaymentError,
    AllocationObligationNotFoundError,
    AllocationPairExistsError,
    AllocationPaymentNotFoundError,
    AllocationPaymentVoidedError,
    AllocationPlanningObligationSourceRecord,
    AllocationPlanningPaymentSourceRecord,
    PaymentAllocationRecord,
    SQLiteAllocationPlanningRepository,
    SQLiteAllocationRepository,
)


class AllocationPlanValidationError(ValueError):
    pass


class AllocationPlanNotActionableError(ValueError):
    def __init__(self, plan: AllocationPlan) -> None:
        super().__init__("Plan is not fully actionable.")
        self.plan = plan


class AllocationPlanApplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannedAllocation:
    payment_event_id: int
    rent_obligation_id: int
    obligation_period: str
    amount_cents: int


@dataclass(frozen=True)
class PlannedPayment:
    payment_event_id: int
    occurred_on: date
    amount_cents: int
    remaining_before_cents: int
    allocations: tuple[PlannedAllocation, ...]


@dataclass(frozen=True)
class ProjectedObligation:
    rent_obligation_id: int
    period: str
    due_date: date
    owed_cents: int
    allocated_cents: int
    remaining_cents: int
    status: ReconciliationStatus | None


@dataclass(frozen=True)
class AllocationPlanIssue:
    code: str
    message: str
    payment_event_id: int | None = None
    rent_account_id: int | None = None
    rent_obligation_id: int | None = None


@dataclass(frozen=True)
class AccountAllocationPlan:
    rent_account_id: int
    account_name: str
    unit_label: str
    payments: tuple[PlannedPayment, ...]
    projected_obligations: tuple[ProjectedObligation, ...]
    issues: tuple[AllocationPlanIssue, ...]

    @property
    def planned_allocations(self) -> tuple[PlannedAllocation, ...]:
        return tuple(link for payment in self.payments for link in payment.allocations)


@dataclass(frozen=True)
class AllocationPlan:
    period_from: str
    period_to: str
    accounts: tuple[AccountAllocationPlan, ...]
    global_issues: tuple[AllocationPlanIssue, ...]

    @property
    def planned_allocations(self) -> tuple[PlannedAllocation, ...]:
        return tuple(link for account in self.accounts for link in account.planned_allocations)

    @property
    def actionable(self) -> bool:
        return not self.global_issues and not any(account.issues for account in self.accounts)


@dataclass(frozen=True)
class AllocationPlanApplyResult:
    plan: AllocationPlan
    allocations: tuple[PaymentAllocationRecord, ...]


def build_allocation_plan(
    repository: SQLiteAllocationPlanningRepository,
    period_from: str,
    period_to: str,
) -> AllocationPlan:
    """Build a read-only oldest-outstanding-first simulation."""
    period_from, period_to = _validated_range(period_from, period_to)
    payments = repository.list_payment_sources()
    obligations = repository.list_obligation_sources(period_from, period_to)
    aliases = {
        source.normalized_alias: source for source in repository.list_alias_sources()
    }
    accounts_by_payer: dict[int, list[int]] = {}
    account_details: dict[int, tuple[str, str]] = {}
    for source in repository.list_account_sources():
        accounts_by_payer.setdefault(source.payer_id, []).append(source.rent_account_id)
        account_details[source.rent_account_id] = (
            source.account_display_name,
            source.unit_label,
        )
    obligations_by_account: dict[int, list[AllocationPlanningObligationSourceRecord]] = {}
    global_issues: list[AllocationPlanIssue] = []
    account_issues: dict[int, list[AllocationPlanIssue]] = {}
    for obligation in obligations:
        obligations_by_account.setdefault(obligation.rent_account_id, []).append(obligation)
        account_details.setdefault(
            obligation.rent_account_id,
            (obligation.account_display_name, obligation.unit_label),
        )
        if obligation.allocated_cents > obligation.owed_cents:
            issue = AllocationPlanIssue(
                "OBLIGATION_OVERALLOCATED",
                f"Obligation {obligation.obligation_id} is allocated above its owed amount.",
                rent_account_id=obligation.rent_account_id,
                rent_obligation_id=obligation.obligation_id,
            )
            account_issues.setdefault(obligation.rent_account_id, []).append(issue)

    eligible_by_account: dict[int, list[tuple[AllocationPlanningPaymentSourceRecord, date]]] = {}
    for payment in payments:
        remaining = payment.amount_cents - payment.allocated_cents
        if remaining < 0:
            global_issues.append(
                AllocationPlanIssue(
                    "PAYMENT_OVERALLOCATED",
                    f"Payment {payment.payment_event_id} is allocated above its payment amount.",
                    payment_event_id=payment.payment_event_id,
                )
            )
            continue
        if remaining == 0:
            continue
        alias = aliases.get(normalize_alias(payment.sender_name))
        if alias is None:
            global_issues.append(
                AllocationPlanIssue(
                    "UNRESOLVED_SENDER",
                    f"Payment {payment.payment_event_id} sender is unresolved.",
                    payment_event_id=payment.payment_event_id,
                )
            )
            continue
        associated_accounts = tuple(sorted(set(accounts_by_payer.get(alias.payer_id, ()))))
        relevant_accounts = tuple(
            account_id
            for account_id in associated_accounts
            if account_id in obligations_by_account
        )
        if not relevant_accounts:
            code = "NO_RENT_ACCOUNT" if not associated_accounts else "NO_OUTSTANDING_OBLIGATION"
            global_issues.append(
                AllocationPlanIssue(
                    code,
                    f"Payment {payment.payment_event_id} has no eligible obligation in range.",
                    payment_event_id=payment.payment_event_id,
                    rent_account_id=(
                        associated_accounts[0] if len(associated_accounts) == 1 else None
                    ),
                )
            )
            continue
        if len(relevant_accounts) > 1:
            global_issues.append(
                AllocationPlanIssue(
                    "MULTIPLE_RENT_ACCOUNTS",
                    f"Payment {payment.payment_event_id} payer has multiple candidate rent accounts: "
                    + ", ".join(str(value) for value in relevant_accounts)
                    + ".",
                    payment_event_id=payment.payment_event_id,
                )
            )
            continue
        if payment.occurred_on is None:
            global_issues.append(
                AllocationPlanIssue(
                    "NULL_PAYMENT_DATE",
                    f"Payment {payment.payment_event_id} has no occurred date.",
                    payment_event_id=payment.payment_event_id,
                    rent_account_id=relevant_accounts[0],
                )
            )
            continue
        try:
            occurred_on = date.fromisoformat(payment.occurred_on)
        except ValueError:
            global_issues.append(
                AllocationPlanIssue(
                    "INVALID_PAYMENT_DATE",
                    f"Payment {payment.payment_event_id} has an invalid occurred date.",
                    payment_event_id=payment.payment_event_id,
                    rent_account_id=relevant_accounts[0],
                )
            )
            continue
        eligible_by_account.setdefault(relevant_accounts[0], []).append(
            (payment, occurred_on)
        )

    existing_pairs = repository.list_existing_pairs()
    account_plans = tuple(
        _build_account_plan(
            account_id,
            account_details[account_id],
            obligations_by_account[account_id],
            eligible_by_account.get(account_id, []),
            existing_pairs,
            account_issues.get(account_id, []),
        )
        for account_id in sorted(obligations_by_account)
    )
    return AllocationPlan(
        period_from,
        period_to,
        account_plans,
        tuple(global_issues),
    )


def apply_allocation_plan(
    planning_repository: SQLiteAllocationPlanningRepository,
    allocation_repository: SQLiteAllocationRepository,
    period_from: str,
    period_to: str,
) -> AllocationPlanApplyResult:
    """Rebuild current state and atomically persist every safe proposed link."""
    plan = build_allocation_plan(planning_repository, period_from, period_to)
    if not plan.actionable:
        raise AllocationPlanNotActionableError(plan)
    links = tuple(
        (link.payment_event_id, link.rent_obligation_id, link.amount_cents)
        for link in plan.planned_allocations
    )
    try:
        allocations = allocation_repository.create_many_checked(links)
    except (
        AllocationExceedsObligationError,
        AllocationExceedsPaymentError,
        AllocationObligationNotFoundError,
        AllocationPairExistsError,
        AllocationPaymentNotFoundError,
        AllocationPaymentVoidedError,
        sqlite3.Error,
    ) as error:
        raise AllocationPlanApplyError(
            "Allocation plan changed or failed authoritative allocation validation."
        ) from error
    return AllocationPlanApplyResult(plan, allocations)


def _build_account_plan(
    account_id: int,
    details: tuple[str, str],
    obligation_sources: list[AllocationPlanningObligationSourceRecord],
    payment_sources: list[tuple[AllocationPlanningPaymentSourceRecord, date]],
    existing_pairs: set[tuple[int, int]],
    initial_issues: list[AllocationPlanIssue],
) -> AccountAllocationPlan:
    obligations = sorted(
        obligation_sources, key=lambda source: (source.due_date, source.obligation_id)
    )
    remaining_by_obligation = {
        source.obligation_id: source.owed_cents - source.allocated_cents
        for source in obligations
    }
    planned_by_obligation = {source.obligation_id: 0 for source in obligations}
    issues = list(initial_issues)
    planned_payments: list[PlannedPayment] = []
    for payment, occurred_on in sorted(
        payment_sources, key=lambda item: (item[1], item[0].payment_event_id)
    ):
        remaining_before = payment.amount_cents - payment.allocated_cents
        remaining_payment = remaining_before
        links: list[PlannedAllocation] = []
        while remaining_payment > 0:
            obligation = next(
                (
                    source
                    for source in obligations
                    if remaining_by_obligation[source.obligation_id] > 0
                ),
                None,
            )
            if obligation is None:
                issues.append(
                    AllocationPlanIssue(
                        "NO_OUTSTANDING_OBLIGATION",
                        f"Payment {payment.payment_event_id} has remaining money but account "
                        f"{account_id} has no outstanding obligation in range.",
                        payment_event_id=payment.payment_event_id,
                        rent_account_id=account_id,
                    )
                )
                break
            pair = (payment.payment_event_id, obligation.obligation_id)
            if pair in existing_pairs:
                issues.append(
                    AllocationPlanIssue(
                        "EXISTING_ALLOCATION_PAIR",
                        f"Payment {payment.payment_event_id} already has an allocation to "
                        f"obligation {obligation.obligation_id}; the planner will not rewrite it.",
                        payment_event_id=payment.payment_event_id,
                        rent_account_id=account_id,
                        rent_obligation_id=obligation.obligation_id,
                    )
                )
                break
            amount = min(
                remaining_payment,
                remaining_by_obligation[obligation.obligation_id],
            )
            links.append(
                PlannedAllocation(
                    payment.payment_event_id,
                    obligation.obligation_id,
                    obligation.period,
                    amount,
                )
            )
            remaining_payment -= amount
            remaining_by_obligation[obligation.obligation_id] -= amount
            planned_by_obligation[obligation.obligation_id] += amount
        planned_payments.append(
            PlannedPayment(
                payment.payment_event_id,
                occurred_on,
                payment.amount_cents,
                remaining_before,
                tuple(links),
            )
        )
    projected = tuple(
        _projected_obligation(source, planned_by_obligation[source.obligation_id])
        for source in obligations
    )
    account_name, unit_label = details
    return AccountAllocationPlan(
        account_id,
        account_name,
        unit_label,
        tuple(planned_payments),
        projected,
        tuple(issues),
    )


def _projected_obligation(
    source: AllocationPlanningObligationSourceRecord, planned_cents: int
) -> ProjectedObligation:
    allocated = source.allocated_cents + planned_cents
    remaining = source.owed_cents - allocated
    status = (
        derive_reconciliation_status(source.owed_cents, allocated)
        if 0 <= allocated <= source.owed_cents
        else None
    )
    return ProjectedObligation(
        source.obligation_id,
        source.period,
        date.fromisoformat(source.due_date),
        source.owed_cents,
        allocated,
        remaining,
        status,
    )


def _validated_range(period_from: str, period_to: str) -> tuple[str, str]:
    try:
        canonical_from = parse_monthly_period(period_from).value
        canonical_to = parse_monthly_period(period_to).value
    except ObligationValidationError as error:
        raise AllocationPlanValidationError(str(error)) from error
    if canonical_from > canonical_to:
        raise AllocationPlanValidationError("--from must not be after --to.")
    return canonical_from, canonical_to
