"""Explicit, audited deactivation of Gmail-derived payment events."""

from __future__ import annotations

from dataclasses import dataclass

from autorentledger.storage import (
    GmailPaymentAllocationConflictStorageError,
    GmailPaymentAuditInvariantStorageError,
    GmailPaymentManualDerivedStorageError,
    GmailPaymentNotFoundStorageError,
    GmailPaymentVoidedStorageError,
    GmailPaymentVoidRecord,
    PaymentEventRecord,
    SQLiteGmailPaymentRepository,
)


class GmailPaymentValidationError(ValueError):
    pass


class GmailPaymentNotFoundError(ValueError):
    pass


class GmailPaymentSourceError(ValueError):
    pass


class GmailPaymentAlreadyVoidedError(ValueError):
    pass


class GmailPaymentInvariantError(RuntimeError):
    pass


class GmailPaymentAllocationConflictError(ValueError):
    def __init__(self, payment_event_id: int, allocated_cents: int) -> None:
        super().__init__(f"Payment {payment_event_id} has allocated money.")
        self.allocated_cents = allocated_cents


@dataclass(frozen=True)
class GmailPaymentVoidResult:
    payment_event: PaymentEventRecord
    void: GmailPaymentVoidRecord


@dataclass(frozen=True)
class GmailPaymentHistory:
    payment_event: PaymentEventRecord
    void: GmailPaymentVoidRecord | None


def void_gmail_payment(
    repository: SQLiteGmailPaymentRepository,
    payment_event_id: int,
    *,
    reason: str,
) -> GmailPaymentVoidResult:
    """Atomically record an audit reason and deactivate one Gmail payment."""
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise GmailPaymentValidationError("Reason must not be blank.")
    try:
        result = repository.void_checked(payment_event_id, normalized_reason)
    except GmailPaymentNotFoundStorageError as error:
        raise GmailPaymentNotFoundError(f"Payment {payment_event_id} does not exist.") from error
    except GmailPaymentManualDerivedStorageError as error:
        raise GmailPaymentSourceError(
            f"Payment {payment_event_id} is manual-derived and cannot be Gmail-voided."
        ) from error
    except GmailPaymentVoidedStorageError as error:
        raise GmailPaymentAlreadyVoidedError(
            f"Payment {payment_event_id} is already voided."
        ) from error
    except GmailPaymentAllocationConflictStorageError as error:
        raise GmailPaymentAllocationConflictError(
            payment_event_id, error.allocated_cents
        ) from error
    except GmailPaymentAuditInvariantStorageError as error:
        raise GmailPaymentInvariantError("Gmail payment audit state is inconsistent.") from error
    return GmailPaymentVoidResult(result.payment_event, result.void)


def get_gmail_payment_history(
    repository: SQLiteGmailPaymentRepository, payment_event_id: int
) -> GmailPaymentHistory:
    """Return safe normalized facts and the optional Gmail void audit record."""
    try:
        result = repository.get_history(payment_event_id)
    except GmailPaymentNotFoundStorageError as error:
        raise GmailPaymentNotFoundError(f"Payment {payment_event_id} does not exist.") from error
    except GmailPaymentManualDerivedStorageError as error:
        raise GmailPaymentSourceError(
            f"Payment {payment_event_id} is manual-derived; use manual-history."
        ) from error
    except GmailPaymentAuditInvariantStorageError as error:
        raise GmailPaymentInvariantError("Gmail payment audit state is inconsistent.") from error
    return GmailPaymentHistory(result.payment_event, result.void)
