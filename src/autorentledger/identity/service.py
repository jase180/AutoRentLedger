"""Resolve observed payment senders through explicit payer aliases."""

from dataclasses import dataclass

from autorentledger.identity.normalization import normalize_alias
from autorentledger.storage import (
    PayerRecord,
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
    return [
        UnresolvedSender(sender.sender_name, sender.count)
        for sender in payments.list_sender_counts()
        if resolve_payer(sender.sender_name, payers) is None
    ]
