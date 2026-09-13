from datetime import date

from autorentledger.rental import UnitContext
from autorentledger.storage import SQLiteObligationRepository, SQLiteRentalRepository


def test_unit_context_carries_the_existing_unit_identity():
    context = UnitContext(unit_id=7, unit_label="Synthetic Unit")

    assert context.unit_id == 7
    assert context.unit_label == "Synthetic Unit"


def test_rental_summaries_share_unit_context_and_flat_compatibility(tmp_path):
    database_path = tmp_path / "unit-context.sqlite3"
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    unit = rentals.create_unit("Synthetic Unit")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = obligations.create(
        account.id,
        "2026-05",
        100_000,
        date(2026, 5, 5),
    )

    account_summary = rentals.get_rent_account_summary(account.id)
    obligation_summary = obligations.get_summary(obligation.id)

    expected = UnitContext(unit_id=unit.id, unit_label=unit.label)
    assert account_summary is not None
    assert obligation_summary is not None
    assert account_summary.unit == expected
    assert obligation_summary.unit == expected
    assert (account_summary.unit_id, account_summary.unit_label) == (unit.id, unit.label)
    assert (obligation_summary.unit_id, obligation_summary.unit_label) == (
        unit.id,
        unit.label,
    )


def test_unit_context_does_not_conflate_distinct_units(tmp_path):
    database_path = tmp_path / "distinct-unit-contexts.sqlite3"
    rentals = SQLiteRentalRepository(database_path)
    first = rentals.create_unit("Synthetic Unit A")
    second = rentals.create_unit("Synthetic Unit B")
    rentals.create_rent_account(first.id, "First Household", None, None)
    rentals.create_rent_account(second.id, "Second Household", None, None)

    summaries = rentals.list_rent_accounts()

    assert [summary.unit for summary in summaries] == [
        UnitContext(first.id, first.label),
        UnitContext(second.id, second.label),
    ]
