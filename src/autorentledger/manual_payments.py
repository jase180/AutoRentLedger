"""Explicit creation of normalized payment evidence that did not originate in Gmail."""

from __future__ import annotations

from dataclasses import dataclass

from autorentledger.identity import normalize_alias
from autorentledger.obligations import (
    ObligationValidationError,
    parse_currency_cents,
    parse_iso_date,
)
from autorentledger.storage import (
    ManualPaymentDuplicateRecord,
    ManualPaymentDuplicateStorageError,
    ManualPaymentEvidenceRecord,
    PaymentEventRecord,
    SQLiteManualPaymentRepository,
)

MANUAL_PAYMENT_PARSER_VERSION = "manual"


class ManualPaymentValidationError(ValueError):
    """Manual payment input is invalid."""


class ManualPaymentDuplicateError(ValueError):
    """Matching manual evidence already exists and needs explicit confirmation."""

    def __init__(self, matches: tuple[ManualPaymentDuplicateRecord, ...]) -> None:
        super().__init__("Possible duplicate manual payment.")
        self.matches = matches


@dataclass(frozen=True)
class ManualPaymentCreationResult:
    evidence: ManualPaymentEvidenceRecord
    payment_event: PaymentEventRecord


def create_manual_payment(
    repository: SQLiteManualPaymentRepository,
    sender_name: str,
    amount: str,
    occurred_on: str,
    note: str | None = None,
    *,
    confirm_duplicate: bool = False,
) -> ManualPaymentCreationResult:
    """Validate and atomically persist manual evidence plus one normalized event."""
    sender = sender_name.strip()
    if not sender:
        raise ManualPaymentValidationError("Sender must not be blank.")
    try:
        amount_cents = parse_currency_cents(amount)
    except ObligationValidationError as error:
        raise ManualPaymentValidationError(str(error)) from error
    try:
        payment_date = parse_iso_date(occurred_on)
    except ObligationValidationError as error:
        raise ManualPaymentValidationError(
            f"Invalid date {occurred_on!r}; expected YYYY-MM-DD."
        ) from error
    normalized_note = note.strip() if note is not None else None
    if normalized_note == "":
        normalized_note = None

    try:
        result = repository.create_checked(
            sender,
            amount_cents,
            payment_date,
            normalized_note,
            MANUAL_PAYMENT_PARSER_VERSION,
            confirm_duplicate=confirm_duplicate,
            normalize_sender=normalize_alias,
        )
    except ManualPaymentDuplicateStorageError as error:
        raise ManualPaymentDuplicateError(error.matches) from error
    return ManualPaymentCreationResult(result.evidence, result.payment_event)
