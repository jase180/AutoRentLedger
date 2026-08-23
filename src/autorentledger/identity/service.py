"""Resolve observed payment senders through explicit payer aliases."""

from collections.abc import Collection
from dataclasses import dataclass

from autorentledger.identity.normalization import normalize_alias
from autorentledger.storage import (
    PayerRecord,
    PaymentSenderCount,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
)


@dataclass(frozen=True)
class UnresolvedSender:
    sender_name: str
    count: int


def resolve_payer(sender_name: str, repository: SQLitePayerRepository) -> PayerRecord | None:
    alias = repository.get_alias(normalize_alias(sender_name))
    return repository.get_payer(alias.payer_id) if alias else None


def unresolved_senders(
    payments: SQLitePaymentEventRepository,
    payers: SQLitePayerRepository,
) -> list[UnresolvedSender]:
    return unresolved_sender_counts(
        payments.list_sender_counts(), payers.list_normalized_aliases()
    )


def unresolved_sender_counts(
    sender_counts: list[PaymentSenderCount], normalized_aliases: Collection[str]
) -> list[UnresolvedSender]:
    """Resolve grouped sender facts against an explicit set of alias keys."""
    return [
        UnresolvedSender(sender.sender_name, sender.count)
        for sender in sender_counts
        if normalize_alias(sender.sender_name) not in normalized_aliases
    ]
