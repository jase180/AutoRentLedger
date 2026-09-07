"""Explicit payment allocations to late-fee charges."""

from autorentledger.obligations import ObligationValidationError, parse_currency_cents
from autorentledger.storage.late_fee_allocations import (
    LateFeeAllocationExceedsFeeError,
    LateFeeAllocationExceedsPaymentError,
    LateFeeAllocationFeeNotFoundError,
    LateFeeAllocationFeeVoidedError,
    LateFeeAllocationNotFoundError,
    LateFeeAllocationPairExistsError,
    LateFeeAllocationPaymentNotFoundError,
    LateFeeAllocationPaymentVoidedError,
    LateFeeAllocationRecord,
    SQLiteLateFeeAllocationRepository,
)


class LateFeeAllocationValidationError(ValueError):
    pass


def create_late_fee_allocation(
    repository: SQLiteLateFeeAllocationRepository,
    payment_event_id: int,
    late_fee_charge_id: int,
    amount: str,
) -> LateFeeAllocationRecord:
    try:
        amount_cents = parse_currency_cents(amount)
    except ObligationValidationError as error:
        raise LateFeeAllocationValidationError(str(error)) from error
    try:
        return repository.create_checked(payment_event_id, late_fee_charge_id, amount_cents)
    except LateFeeAllocationPaymentNotFoundError as error:
        raise LateFeeAllocationValidationError(
            f"Payment {payment_event_id} does not exist."
        ) from error
    except LateFeeAllocationPaymentVoidedError as error:
        raise LateFeeAllocationValidationError(
            f"Payment {payment_event_id} is voided and cannot be allocated."
        ) from error
    except LateFeeAllocationFeeNotFoundError as error:
        raise LateFeeAllocationValidationError(
            f"Late fee {late_fee_charge_id} does not exist."
        ) from error
    except LateFeeAllocationFeeVoidedError as error:
        raise LateFeeAllocationValidationError(
            f"Late fee {late_fee_charge_id} is voided and cannot receive allocations."
        ) from error
    except LateFeeAllocationPairExistsError as error:
        raise LateFeeAllocationValidationError(
            f"Payment {payment_event_id} already has an allocation to late fee {late_fee_charge_id}."
        ) from error
    except LateFeeAllocationExceedsPaymentError as error:
        raise LateFeeAllocationValidationError(
            f"Allocation exceeds the payment's combined remaining balance of "
            f"{_format_currency(error.remaining_cents)}."
        ) from error
    except LateFeeAllocationExceedsFeeError as error:
        raise LateFeeAllocationValidationError(
            f"Allocation exceeds the late fee's remaining balance of "
            f"{_format_currency(error.remaining_cents)}."
        ) from error


def remove_late_fee_allocation(
    repository: SQLiteLateFeeAllocationRepository, allocation_id: int
) -> LateFeeAllocationRecord:
    try:
        return repository.remove_checked(allocation_id)
    except LateFeeAllocationNotFoundError as error:
        raise LateFeeAllocationValidationError(
            f"Late-fee allocation {allocation_id} does not exist."
        ) from error


def _format_currency(cents: int) -> str:
    dollars, remainder = divmod(cents, 100)
    return f"${dollars:,}.{remainder:02d}"
