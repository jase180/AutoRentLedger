from datetime import date

import pytest

from autorentledger.rental import UnitContext
from autorentledger.rental_context import extract_unit_context
from autorentledger.storage import SQLiteObligationRepository, SQLiteRentalRepository
from tests.property_helpers import create_test_property, create_test_unit


def test_unit_context_carries_the_existing_unit_identity():
    context = UnitContext(
        property_id=3,
        property_name="Property A",
        unit_id=7,
        unit_label="Synthetic Unit",
    )

    assert context.property_id == 3
    assert context.property_name == "Property A"
    assert context.unit_id == 7
    assert context.unit_label == "Synthetic Unit"


def test_extract_unit_context_removes_all_canonical_context_keys():
    values = {
        "property_id": 3,
        "property_name": "Property A",
        "unit_id": 7,
        "unit_label": "2F",
        "amount_cents": 145_000,
    }

    context = extract_unit_context(values)

    assert context == UnitContext(3, "Property A", 7, "2F")
    assert values == {"amount_cents": 145_000}


@pytest.mark.parametrize(
    "missing_key",
    ("property_id", "property_name", "unit_id", "unit_label"),
)
def test_extract_unit_context_fails_clearly_when_context_is_missing(missing_key):
    values = {
        "property_id": 3,
        "property_name": "Property A",
        "unit_id": 7,
        "unit_label": "2F",
    }
    del values[missing_key]

    with pytest.raises(KeyError, match=missing_key):
        extract_unit_context(values)


def test_rental_summaries_share_unit_context_and_flat_compatibility(tmp_path):
    database_path = tmp_path / "unit-context.sqlite3"
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    unit = create_test_unit(rentals, "Synthetic Unit")
    property_record = create_test_property(rentals)
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = obligations.create(
        account.id,
        "2026-05",
        100_000,
        date(2026, 5, 5),
    )

    account_summary = rentals.get_rent_account_summary(account.id)
    obligation_summary = obligations.get_summary(obligation.id)

    expected = UnitContext(
        property_id=property_record.id,
        property_name=property_record.display_name,
        unit_id=unit.id,
        unit_label=unit.label,
    )
    assert account_summary is not None
    assert obligation_summary is not None
    assert account_summary.unit == expected
    assert obligation_summary.unit == expected
    assert (
        account_summary.property_id,
        account_summary.property_name,
        account_summary.unit_id,
        account_summary.unit_label,
    ) == (property_record.id, property_record.display_name, unit.id, unit.label)
    assert (obligation_summary.unit_id, obligation_summary.unit_label) == (
        unit.id,
        unit.label,
    )


def test_unit_context_does_not_conflate_distinct_units(tmp_path):
    database_path = tmp_path / "distinct-unit-contexts.sqlite3"
    rentals = SQLiteRentalRepository(database_path)
    first = create_test_unit(rentals, "Synthetic Unit A")
    second = create_test_unit(rentals, "Synthetic Unit B")
    rentals.create_rent_account(first.id, "First Household", None, None)
    rentals.create_rent_account(second.id, "Second Household", None, None)

    summaries = rentals.list_rent_accounts()
    property_record = create_test_property(rentals)

    assert [summary.unit for summary in summaries] == [
        UnitContext(property_record.id, property_record.display_name, first.id, first.label),
        UnitContext(property_record.id, property_record.display_name, second.id, second.label),
    ]
