"""Explicit owner-assessed late fees, separate from rent and payment allocation."""

from autorentledger.obligations import (
    ObligationValidationError,
    parse_currency_cents,
    parse_iso_date,
    parse_monthly_period,
)
from autorentledger.storage.late_fees import LateFeeHistory, SQLiteLateFeeRepository


class LateFeeValidationError(ValueError):
    pass


def _required_reason(reason: str) -> str:
    reason = reason.strip()
    if not reason:
        raise LateFeeValidationError("Reason must not be blank.")
    return reason


def assess_late_fee(
    repository: SQLiteLateFeeRepository,
    obligation_id: int,
    amount: str,
    assessed_on: str,
    reason: str,
    *,
    confirm_duplicate: bool = False,
) -> LateFeeHistory:
    """Record an owner's assessment; do not infer timeliness or legal entitlement."""
    reason = _required_reason(reason)
    try:
        cents = parse_currency_cents(amount)
    except ObligationValidationError as error:
        raise LateFeeValidationError(str(error)) from error
    try:
        assessed_date = parse_iso_date(assessed_on)
    except ObligationValidationError as error:
        raise LateFeeValidationError("Invalid assessed-on date. Expected YYYY-MM-DD.") from error
    return repository.assess_checked(
        obligation_id,
        cents,
        assessed_date.isoformat(),
        reason,
        confirm_duplicate=confirm_duplicate,
    )


def void_late_fee(
    repository: SQLiteLateFeeRepository, fee_id: int, *, reason: str
) -> LateFeeHistory:
    return repository.void_checked(fee_id, _required_reason(reason))


def get_late_fee_history(repository: SQLiteLateFeeRepository, fee_id: int) -> LateFeeHistory:
    return repository.get_history(fee_id)


def list_late_fees(
    repository: SQLiteLateFeeRepository,
    *,
    period: str | None = None,
    account_id: int | None = None,
    active_only: bool = False,
) -> tuple[LateFeeHistory, ...]:
    try:
        canonical_period = parse_monthly_period(period).value if period is not None else None
    except ObligationValidationError as error:
        raise LateFeeValidationError(str(error)) from error
    return repository.list_histories(
        period=canonical_period, account_id=account_id, active_only=active_only
    )
