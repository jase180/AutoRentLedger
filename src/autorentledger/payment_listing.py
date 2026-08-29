"""Canonical composed read model for normalized payment history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from autorentledger.identity import normalize_alias
from autorentledger.storage import SQLitePaymentListingRepository


class PaymentListingInvariantError(RuntimeError):
    """Stored allocation totals violate payment amount invariants."""


@dataclass(frozen=True)
class PaymentListRecord:
    payment_event_id: int
    occurred_on: date | None
    provider: str
    sender_name: str
    amount_cents: int
    payer_id: int | None
    payer_display_name: str | None
    allocated_cents: int
    unallocated_cents: int
    voided_at: str | None


def list_payment_records(
    repository: SQLitePaymentListingRepository,
) -> tuple[PaymentListRecord, ...]:
    """Compose payments with current exact-alias interpretation and allocation totals."""
    aliases = {alias.normalized_alias: alias for alias in repository.list_aliases()}
    records: list[PaymentListRecord] = []
    for source in repository.list_payment_sources():
        unallocated_cents = (
            0
            if source.voided_at is not None
            else source.amount_cents - source.allocated_cents
        )
        if (
            source.allocated_cents < 0
            or source.allocated_cents > source.amount_cents
            or unallocated_cents < 0
        ):
            raise PaymentListingInvariantError(
                f"Payment {source.payment_event_id} is allocated above its payment amount."
            )
        alias = aliases.get(normalize_alias(source.sender_name))
        records.append(
            PaymentListRecord(
                payment_event_id=source.payment_event_id,
                occurred_on=(
                    date.fromisoformat(source.occurred_on)
                    if source.occurred_on is not None
                    else None
                ),
                provider=source.provider,
                sender_name=source.sender_name,
                amount_cents=source.amount_cents,
                payer_id=alias.payer_id if alias else None,
                payer_display_name=alias.payer_display_name if alias else None,
                allocated_cents=source.allocated_cents,
                unallocated_cents=unallocated_cents,
                voided_at=source.voided_at,
            )
        )
    return tuple(records)
