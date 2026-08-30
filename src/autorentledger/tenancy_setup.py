"""Preview-first orchestration for existing tenancy configuration primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from autorentledger.identity import normalize_alias
from autorentledger.obligations import (
    ObligationValidationError,
    parse_currency_cents,
    parse_iso_date,
)
from autorentledger.storage import (
    PayerRecord,
    RentAccountPayerRecord,
    RentAccountRecord,
    RentScheduleRecord,
    SQLiteTenancySetupRepository,
    TenancySetupAliasConflictStorageError,
    TenancySetupAliasInput,
    TenancySetupAliasStorageResult,
    TenancySetupPayerNotFoundStorageError,
    TenancySetupStorageResult,
    TenancySetupUnitLabelConflictStorageError,
    TenancySetupUnitNotFoundStorageError,
    UnitRecord,
)


class TenancySetupValidationError(ValueError):
    """The requested setup is internally invalid."""


class TenancySetupNotFoundError(ValueError):
    """An explicitly selected reusable record does not exist."""


class TenancySetupConflictError(ValueError):
    """The setup conflicts with an existing explicit record."""


class SetupAction(StrEnum):
    CREATE = "CREATE"
    REUSE = "REUSE"


@dataclass(frozen=True)
class TenancySetupRequest:
    account_name: str
    unit_id: int | None = None
    unit_label: str | None = None
    active_from: str | None = None
    active_to: str | None = None
    payer_id: int | None = None
    payer_name: str | None = None
    aliases: tuple[str, ...] = ()
    rent: str | None = None
    due_day: int | None = None


@dataclass(frozen=True)
class TenancySetupAliasPlan:
    alias: str
    normalized_alias: str
    action: SetupAction


@dataclass(frozen=True)
class TenancySetupPreview:
    request: TenancySetupRequest
    unit_action: SetupAction
    unit_id: int | None
    unit_label: str
    account_name: str
    active_from: date | None
    active_to: date | None
    payer_action: SetupAction
    payer_id: int | None
    payer_name: str
    aliases: tuple[TenancySetupAliasPlan, ...]
    rent_cents: int | None
    due_day: int | None


@dataclass(frozen=True)
class TenancySetupResult:
    unit: UnitRecord
    unit_reused: bool
    account: RentAccountRecord
    payer: PayerRecord
    payer_reused: bool
    aliases: tuple[TenancySetupAliasStorageResult, ...]
    association: RentAccountPayerRecord
    schedule: RentScheduleRecord | None


def preview_tenancy_setup(
    repository: SQLiteTenancySetupRepository,
    request: TenancySetupRequest,
) -> TenancySetupPreview:
    """Validate and inspect one setup without mutating the database."""
    validated = _validate_request(request)

    if validated.unit_id is not None:
        unit = repository.get_unit(validated.unit_id)
        if unit is None:
            raise TenancySetupNotFoundError(
                f"Unit {validated.unit_id} does not exist."
            )
        unit_action = SetupAction.REUSE
        unit_id = unit.id
        unit_label = unit.label
    else:
        existing_unit = repository.get_unit_by_label(validated.unit_label or "")
        if existing_unit is not None:
            raise TenancySetupConflictError(
                f'Unit "{validated.unit_label}" already exists as unit '
                f"{existing_unit.id}. Use --unit {existing_unit.id} to reuse it."
            )
        unit_action = SetupAction.CREATE
        unit_id = None
        unit_label = validated.unit_label or ""

    if validated.payer_id is not None:
        payer = repository.get_payer(validated.payer_id)
        if payer is None:
            raise TenancySetupNotFoundError(
                f"Payer {validated.payer_id} does not exist."
            )
        payer_action = SetupAction.REUSE
        payer_id = payer.id
        payer_name = payer.display_name
    else:
        payer_action = SetupAction.CREATE
        payer_id = None
        payer_name = validated.payer_name or ""

    alias_plans: list[TenancySetupAliasPlan] = []
    for alias in _effective_aliases(validated, payer_name):
        normalized = normalize_alias(alias)
        existing = repository.get_alias(normalized)
        if existing is None:
            action = SetupAction.CREATE
        elif payer_id is not None and existing.payer_id == payer_id:
            action = SetupAction.REUSE
        else:
            raise TenancySetupConflictError(
                f'Alias "{alias}" already belongs to payer {existing.payer_id}. '
                "Setup aborted."
            )
        alias_plans.append(TenancySetupAliasPlan(alias, normalized, action))

    return TenancySetupPreview(
        request=validated,
        unit_action=unit_action,
        unit_id=unit_id,
        unit_label=unit_label,
        account_name=validated.account_name,
        active_from=_optional_date(validated.active_from, "active-from"),
        active_to=_optional_date(validated.active_to, "active-to"),
        payer_action=payer_action,
        payer_id=payer_id,
        payer_name=payer_name,
        aliases=tuple(alias_plans),
        rent_cents=(
            parse_currency_cents(validated.rent)
            if validated.rent is not None
            else None
        ),
        due_day=validated.due_day,
    )


def apply_tenancy_setup(
    repository: SQLiteTenancySetupRepository,
    request: TenancySetupRequest,
) -> TenancySetupResult:
    """Apply one already-validatable setup as a single checked transaction."""
    preview = preview_tenancy_setup(repository, request)
    aliases = tuple(
        TenancySetupAliasInput(item.alias, item.normalized_alias)
        for item in preview.aliases
    )
    try:
        result = repository.apply_checked(
            unit_id=preview.unit_id,
            unit_label=(
                preview.unit_label
                if preview.unit_action is SetupAction.CREATE
                else None
            ),
            account_name=preview.account_name,
            active_from=preview.active_from,
            active_to=preview.active_to,
            payer_id=preview.payer_id,
            payer_name=(
                preview.payer_name
                if preview.payer_action is SetupAction.CREATE
                else None
            ),
            aliases=aliases,
            rent_cents=preview.rent_cents,
            due_day=preview.due_day,
        )
    except TenancySetupUnitNotFoundStorageError as error:
        raise TenancySetupNotFoundError(f"Unit {error.unit_id} does not exist.") from error
    except TenancySetupPayerNotFoundStorageError as error:
        raise TenancySetupNotFoundError(
            f"Payer {error.payer_id} does not exist."
        ) from error
    except TenancySetupUnitLabelConflictStorageError as error:
        raise TenancySetupConflictError(
            f'Unit "{error.label}" already exists as unit {error.unit_id}. '
            f"Use --unit {error.unit_id} to reuse it."
        ) from error
    except TenancySetupAliasConflictStorageError as error:
        raise TenancySetupConflictError(
            f'Alias "{error.alias}" already belongs to payer {error.owner_id}. '
            "Setup aborted."
        ) from error
    return _result_from_storage(result)


def _validate_request(request: TenancySetupRequest) -> TenancySetupRequest:
    if (request.unit_id is None) == (request.unit_label is None):
        raise TenancySetupValidationError(
            "Supply exactly one of --unit or --unit-label."
        )
    if (request.payer_id is None) == (request.payer_name is None):
        raise TenancySetupValidationError(
            "Supply exactly one of --payer or --payer-name."
        )
    unit_label = request.unit_label.strip() if request.unit_label is not None else None
    if request.unit_label is not None and not unit_label:
        raise TenancySetupValidationError("Unit label must not be empty.")
    account_name = request.account_name.strip()
    if not account_name:
        raise TenancySetupValidationError("Rent account name must not be empty.")
    payer_name = request.payer_name.strip() if request.payer_name is not None else None
    if request.payer_name is not None and not payer_name:
        raise TenancySetupValidationError("Payer display name must not be empty.")
    if (request.rent is None) != (request.due_day is None):
        raise TenancySetupValidationError(
            "Supply --rent and --due-day together, or neither."
        )
    if request.rent is not None and request.active_from is None:
        raise TenancySetupValidationError(
            "Schedule creation requires --active-from."
        )
    start = _optional_date(request.active_from, "active-from")
    end = _optional_date(request.active_to, "active-to")
    if start is not None and end is not None and end < start:
        raise TenancySetupValidationError(
            "Active-to date must not be before active-from date."
        )
    if request.rent is not None:
        try:
            parse_currency_cents(request.rent)
        except ObligationValidationError as error:
            raise TenancySetupValidationError(str(error)) from error
        if request.due_day is None or not 1 <= request.due_day <= 28:
            raise TenancySetupValidationError("Due day must be between 1 and 28.")
    cleaned_aliases: list[str] = []
    for alias in request.aliases:
        if not normalize_alias(alias):
            raise TenancySetupValidationError("Alias must not be empty.")
        cleaned_aliases.append(alias)
    return TenancySetupRequest(
        account_name=account_name,
        unit_id=request.unit_id,
        unit_label=unit_label,
        active_from=request.active_from,
        active_to=request.active_to,
        payer_id=request.payer_id,
        payer_name=payer_name,
        aliases=tuple(cleaned_aliases),
        rent=request.rent,
        due_day=request.due_day,
    )


def _effective_aliases(request: TenancySetupRequest, payer_name: str) -> tuple[str, ...]:
    candidates = (
        ((payer_name,) + request.aliases)
        if request.payer_id is None
        else request.aliases
    )
    unique: dict[str, str] = {}
    for alias in candidates:
        unique.setdefault(normalize_alias(alias), alias)
    return tuple(unique.values())


def _optional_date(value: str | None, option_name: str) -> date | None:
    if value is None:
        return None
    try:
        return parse_iso_date(value)
    except ObligationValidationError as error:
        raise TenancySetupValidationError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        ) from error


def _result_from_storage(result: TenancySetupStorageResult) -> TenancySetupResult:
    return TenancySetupResult(
        result.unit,
        result.unit_reused,
        result.account,
        result.payer,
        result.payer_reused,
        result.aliases,
        result.association,
        result.schedule,
    )


__all__ = [
    "SetupAction",
    "TenancySetupConflictError",
    "TenancySetupNotFoundError",
    "TenancySetupPreview",
    "TenancySetupRequest",
    "TenancySetupResult",
    "TenancySetupValidationError",
    "apply_tenancy_setup",
    "preview_tenancy_setup",
]
