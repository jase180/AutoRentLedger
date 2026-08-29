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
    ManualPaymentAllocationConflictStorageError,
    ManualPaymentDuplicateRecord,
    ManualPaymentDuplicateStorageError,
    ManualPaymentEvidenceRecord,
    ManualPaymentGmailDerivedStorageError,
    ManualPaymentHistoryStorageResult,
    ManualPaymentNoChangeStorageError,
    ManualPaymentNotFoundStorageError,
    ManualPaymentRevisionRecord,
    ManualPaymentRevisionStorageResult,
    ManualPaymentVoidedStorageError,
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


class ManualPaymentNotFoundError(ValueError):
    pass


class ManualPaymentSourceError(ValueError):
    pass


class ManualPaymentVoidedError(ValueError):
    pass


class ManualPaymentAllocationConflictError(ValueError):
    def __init__(self, payment_event_id: int, allocated_cents: int) -> None:
        super().__init__(
            f"Payment {payment_event_id} has {_format_currency(allocated_cents)} allocated."
        )
        self.allocated_cents = allocated_cents


@dataclass(frozen=True)
class ManualPaymentCreationResult:
    evidence: ManualPaymentEvidenceRecord
    payment_event: PaymentEventRecord


@dataclass(frozen=True)
class ManualPaymentRevisionResult:
    revision: ManualPaymentRevisionRecord
    payment_event: PaymentEventRecord


@dataclass(frozen=True)
class ManualPaymentHistory:
    evidence: ManualPaymentEvidenceRecord
    revisions: tuple[ManualPaymentRevisionRecord, ...]
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


def correct_manual_payment(
    repository: SQLiteManualPaymentRepository,
    payment_event_id: int,
    *,
    reason: str,
    sender_name: str | None = None,
    amount: str | None = None,
    occurred_on: str | None = None,
    note: str | None = None,
    confirm_duplicate: bool = False,
) -> ManualPaymentRevisionResult:
    """Append a full correction revision and atomically update the payment projection."""
    normalized_reason = _required_reason(reason)
    if all(value is None for value in (sender_name, amount, occurred_on, note)):
        raise ManualPaymentValidationError(
            "At least one of --sender, --amount, --date, or --note is required."
        )
    sender = None
    if sender_name is not None:
        sender = sender_name.strip()
        if not sender:
            raise ManualPaymentValidationError("Sender must not be blank.")
    amount_cents = None
    if amount is not None:
        try:
            amount_cents = parse_currency_cents(amount)
        except ObligationValidationError as error:
            raise ManualPaymentValidationError(str(error)) from error
    payment_date = None
    if occurred_on is not None:
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
        result = repository.correct_checked(
            payment_event_id,
            sender_name=sender,
            amount_cents=amount_cents,
            occurred_on=payment_date,
            note=normalized_note,
            note_provided=note is not None,
            reason=normalized_reason,
            confirm_duplicate=confirm_duplicate,
            normalize_sender=normalize_alias,
        )
    except ManualPaymentDuplicateStorageError as error:
        raise ManualPaymentDuplicateError(error.matches) from error
    except ManualPaymentAllocationConflictStorageError as error:
        raise ManualPaymentAllocationConflictError(
            payment_event_id, error.allocated_cents
        ) from error
    except ManualPaymentNoChangeStorageError as error:
        raise ManualPaymentValidationError(
            "The supplied correction does not change the payment."
        ) from error
    except (
        ManualPaymentNotFoundStorageError,
        ManualPaymentGmailDerivedStorageError,
        ManualPaymentVoidedStorageError,
    ) as error:
        _translate_state_error(payment_event_id, error)
    return _revision_result(result)


def void_manual_payment(
    repository: SQLiteManualPaymentRepository,
    payment_event_id: int,
    *,
    reason: str,
) -> ManualPaymentRevisionResult:
    """Append a void revision and atomically deactivate an unallocated manual payment."""
    normalized_reason = _required_reason(reason)
    try:
        result = repository.void_checked(payment_event_id, normalized_reason)
    except ManualPaymentAllocationConflictStorageError as error:
        raise ManualPaymentAllocationConflictError(
            payment_event_id, error.allocated_cents
        ) from error
    except (
        ManualPaymentNotFoundStorageError,
        ManualPaymentGmailDerivedStorageError,
        ManualPaymentVoidedStorageError,
    ) as error:
        _translate_state_error(payment_event_id, error)
    return _revision_result(result)


def get_manual_payment_history(
    repository: SQLiteManualPaymentRepository, payment_event_id: int
) -> ManualPaymentHistory:
    try:
        history = repository.get_history(payment_event_id)
    except (ManualPaymentNotFoundStorageError, ManualPaymentGmailDerivedStorageError) as error:
        _translate_state_error(payment_event_id, error)
    return _history_result(history)


def _required_reason(reason: str) -> str:
    normalized = reason.strip()
    if not normalized:
        raise ManualPaymentValidationError("Reason must not be blank.")
    return normalized


def _translate_state_error(payment_event_id: int, error: Exception) -> None:
    if isinstance(error, ManualPaymentNotFoundStorageError):
        raise ManualPaymentNotFoundError(
            f"Payment {payment_event_id} does not exist."
        ) from error
    if isinstance(error, ManualPaymentGmailDerivedStorageError):
        raise ManualPaymentSourceError(
            f"Payment {payment_event_id} is Gmail-derived and cannot be manually changed."
        ) from error
    raise ManualPaymentVoidedError(f"Payment {payment_event_id} is already voided.") from error


def _revision_result(
    result: ManualPaymentRevisionStorageResult,
) -> ManualPaymentRevisionResult:
    return ManualPaymentRevisionResult(result.revision, result.payment_event)


def _history_result(result: ManualPaymentHistoryStorageResult) -> ManualPaymentHistory:
    return ManualPaymentHistory(result.evidence, result.revisions, result.payment_event)


def _format_currency(amount_cents: int) -> str:
    dollars, cents = divmod(amount_cents, 100)
    return f"${dollars:,}.{cents:02d}"
