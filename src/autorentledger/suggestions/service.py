"""Conservative, explainable, read-only allocation suggestions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from autorentledger.identity import normalize_alias
from autorentledger.reconciliation import ReconciliationRecord, reconcile_all
from autorentledger.storage import (
    SQLiteReconciliationRepository,
    SQLiteSuggestionRepository,
    SuggestionAccountSourceRecord,
    SuggestionPaymentSourceRecord,
)


class SuggestionReason(StrEnum):
    EXACT_AMOUNT = "EXACT_AMOUNT"
    PARTIAL_AMOUNT = "PARTIAL_AMOUNT"
    UNRESOLVED_PAYER = "UNRESOLVED_PAYER"
    NO_RENT_ACCOUNT = "NO_RENT_ACCOUNT"
    MULTIPLE_RENT_ACCOUNTS = "MULTIPLE_RENT_ACCOUNTS"
    NO_OUTSTANDING_OBLIGATION = "NO_OUTSTANDING_OBLIGATION"
    MULTIPLE_OUTSTANDING_OBLIGATIONS = "MULTIPLE_OUTSTANDING_OBLIGATIONS"
    FULLY_ALLOCATED_PAYMENT = "FULLY_ALLOCATED_PAYMENT"


class SuggestionInvariantError(RuntimeError):
    """Stored allocation facts violate ledger invariants."""


class SuggestionPaymentNotFoundError(ValueError):
    """The requested payment event does not exist."""


@dataclass(frozen=True)
class AllocationSuggestion:
    payment_event_id: int
    sender_name: str
    payer_id: int
    payer_display_name: str
    rent_account_id: int
    unit_label: str
    account_display_name: str
    rent_obligation_id: int
    period: str
    payment_remaining_cents: int
    obligation_remaining_cents: int
    suggested_amount_cents: int
    reason: SuggestionReason


@dataclass(frozen=True)
class SuggestionResult:
    payment_event_id: int
    sender_name: str
    payment_remaining_cents: int
    reason: SuggestionReason
    suggestion: AllocationSuggestion | None


def find_allocation_suggestions(
    suggestion_repository: SQLiteSuggestionRepository,
    reconciliation_repository: SQLiteReconciliationRepository,
    payment_event_id: int | None = None,
) -> list[SuggestionResult]:
    """Derive one conservative result per payment in stable ID order."""
    payments = suggestion_repository.list_payment_sources(payment_event_id)
    if payment_event_id is not None and not payments:
        raise SuggestionPaymentNotFoundError(
            f"Payment {payment_event_id} does not exist."
        )

    aliases = {
        source.normalized_alias: (source.payer_id, source.payer_display_name)
        for source in suggestion_repository.list_alias_sources()
    }
    accounts_by_payer: dict[int, list[SuggestionAccountSourceRecord]] = defaultdict(list)
    for account in suggestion_repository.list_account_sources():
        accounts_by_payer[account.payer_id].append(account)

    obligations_by_account: dict[int, list[ReconciliationRecord]] = defaultdict(list)
    for obligation in reconcile_all(reconciliation_repository):
        if obligation.remaining_cents > 0:
            obligations_by_account[obligation.rent_account_id].append(obligation)

    results: list[SuggestionResult] = []
    for payment in payments:
        if payment.amount_cents < 0 or payment.allocated_cents < 0:
            raise SuggestionInvariantError(
                f"Suggestion invariant violated for payment {payment.payment_event_id}: "
                "amount is negative."
            )
        payment_remaining = payment.amount_cents - payment.allocated_cents
        if payment_remaining < 0:
            raise SuggestionInvariantError(
                f"Suggestion invariant violated for payment {payment.payment_event_id}: "
                "allocated amount exceeds payment amount."
            )
        if payment_remaining == 0:
            results.append(
                _unmatched(payment, payment_remaining, SuggestionReason.FULLY_ALLOCATED_PAYMENT)
            )
            continue

        payer = aliases.get(normalize_alias(payment.sender_name))
        if payer is None:
            results.append(
                _unmatched(payment, payment_remaining, SuggestionReason.UNRESOLVED_PAYER)
            )
            continue
        payer_id, payer_display_name = payer
        accounts = accounts_by_payer[payer_id]
        if not accounts:
            results.append(
                _unmatched(payment, payment_remaining, SuggestionReason.NO_RENT_ACCOUNT)
            )
            continue
        if len(accounts) > 1:
            results.append(
                _unmatched(payment, payment_remaining, SuggestionReason.MULTIPLE_RENT_ACCOUNTS)
            )
            continue

        account = accounts[0]
        obligations = obligations_by_account[account.rent_account_id]
        if not obligations:
            results.append(
                _unmatched(
                    payment,
                    payment_remaining,
                    SuggestionReason.NO_OUTSTANDING_OBLIGATION,
                )
            )
            continue
        if len(obligations) > 1:
            results.append(
                _unmatched(
                    payment,
                    payment_remaining,
                    SuggestionReason.MULTIPLE_OUTSTANDING_OBLIGATIONS,
                )
            )
            continue

        obligation = obligations[0]
        suggested_amount = min(payment_remaining, obligation.remaining_cents)
        reason = (
            SuggestionReason.EXACT_AMOUNT
            if payment_remaining == obligation.remaining_cents
            else SuggestionReason.PARTIAL_AMOUNT
        )
        suggestion = AllocationSuggestion(
            payment_event_id=payment.payment_event_id,
            sender_name=payment.sender_name,
            payer_id=payer_id,
            payer_display_name=payer_display_name,
            rent_account_id=account.rent_account_id,
            unit_label=account.unit_label,
            account_display_name=account.account_display_name,
            rent_obligation_id=obligation.obligation_id,
            period=obligation.period,
            payment_remaining_cents=payment_remaining,
            obligation_remaining_cents=obligation.remaining_cents,
            suggested_amount_cents=suggested_amount,
            reason=reason,
        )
        results.append(
            SuggestionResult(
                payment.payment_event_id,
                payment.sender_name,
                payment_remaining,
                reason,
                suggestion,
            )
        )
    return results


def _unmatched(
    payment: SuggestionPaymentSourceRecord,
    payment_remaining: int,
    reason: SuggestionReason,
) -> SuggestionResult:
    return SuggestionResult(
        payment.payment_event_id,
        payment.sender_name,
        payment_remaining,
        reason,
        None,
    )
