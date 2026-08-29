"""Validation and orchestration for explicit payment allocations."""

from autorentledger.obligations import ObligationValidationError, parse_currency_cents
from autorentledger.storage import (
    AllocationExceedsObligationError,
    AllocationExceedsPaymentError,
    AllocationObligationNotFoundError,
    AllocationPairExistsError,
    AllocationPaymentNotFoundError,
    AllocationPaymentVoidedError,
    PaymentAllocationRecord,
    SQLiteAllocationRepository,
)


class AllocationValidationError(ValueError):
    """An allocation input or remaining-amount constraint is invalid."""


class AllocationNotFoundError(ValueError):
    """The requested allocation does not exist."""


def create_allocation(
    repository: SQLiteAllocationRepository,
    payment_event_id: int,
    rent_obligation_id: int,
    amount: str,
) -> PaymentAllocationRecord:
    try:
        amount_cents = parse_currency_cents(amount)
    except ObligationValidationError as error:
        raise AllocationValidationError(str(error)) from error

    try:
        return repository.create_checked(payment_event_id, rent_obligation_id, amount_cents)
    except AllocationPaymentNotFoundError as error:
        raise AllocationValidationError(
            f"Payment {payment_event_id} does not exist."
        ) from error
    except AllocationPaymentVoidedError as error:
        raise AllocationValidationError(
            f"Payment {payment_event_id} is voided and cannot be allocated."
        ) from error
    except AllocationObligationNotFoundError as error:
        raise AllocationValidationError(
            f"Rent obligation {rent_obligation_id} does not exist."
        ) from error
    except AllocationPairExistsError as error:
        raise AllocationValidationError(
            f"Payment {payment_event_id} already has an allocation "
            f"to obligation {rent_obligation_id}."
        ) from error
    except AllocationExceedsPaymentError as error:
        raise AllocationValidationError(
            f"Allocation exceeds payment {payment_event_id} remaining amount "
            f"of {_format_currency(error.remaining_cents)}."
        ) from error
    except AllocationExceedsObligationError as error:
        raise AllocationValidationError(
            f"Allocation exceeds obligation {rent_obligation_id} remaining amount "
            f"of {_format_currency(error.remaining_cents)}."
        ) from error


def remove_allocation(
    repository: SQLiteAllocationRepository, allocation_id: int
) -> PaymentAllocationRecord:
    allocation = repository.remove(allocation_id)
    if allocation is None:
        raise AllocationNotFoundError(f"Allocation {allocation_id} does not exist.")
    return allocation


def _format_currency(amount_cents: int) -> str:
    dollars, cents = divmod(amount_cents, 100)
    return f"${dollars:,}.{cents:02d}"
