import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.allocation_planning import build_allocation_plan
from autorentledger.allocations import AllocationValidationError, create_allocation
from autorentledger.cli import main
from autorentledger.database.health import check_database
from autorentledger.email import EmailMessageSummary
from autorentledger.gmail_payments import GmailPaymentAllocationConflictError, void_gmail_payment
from autorentledger.identity import normalize_alias
from autorentledger.late_fee_allocations import (
    LateFeeAllocationValidationError,
    create_late_fee_allocation,
    remove_late_fee_allocation,
)
from autorentledger.late_fees import assess_late_fee, get_late_fee_history, void_late_fee
from autorentledger.manual_payments import (
    ManualPaymentAllocationConflictError,
    correct_manual_payment,
    create_manual_payment,
    void_manual_payment,
)
from autorentledger.parsing import PaymentNotification
from autorentledger.payment_listing import list_payment_records
from autorentledger.review import ReviewKind, collect_review_items
from autorentledger.storage import (
    PaymentRebuildAllocationConflictStorageError,
    SQLiteAllocationPlanningRepository,
    SQLiteAllocationRepository,
    SQLiteGmailPaymentRepository,
    SQLiteLateFeeAllocationRepository,
    SQLiteLateFeeRepository,
    SQLiteManualPaymentRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLitePaymentListingRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MigrationError,
    upgrade_database,
)
from autorentledger.suggestions import find_allocation_suggestions
from autorentledger.web.app import create_app
from autorentledger.web.auth import WebAuthConfig


def snapshot(path):
    with sqlite3.connect(path) as connection:
        tables = tuple(
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
             for table in tables},
        )


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "late-fee-allocations.sqlite3"
    upgrade_database(path)
    rentals = SQLiteRentalRepository(path)
    unit = rentals.create_unit("Synthetic Unit")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = SQLiteObligationRepository(path).create(
        account.id, "2026-05", 135000, date(2026, 5, 5)
    )
    fee = assess_late_fee(
        SQLiteLateFeeRepository(path), obligation.id, "50", "2026-05-10", "Synthetic fee"
    )
    return path, account, obligation, fee


def manual(path, amount="1400", sender="Synthetic Sender", occurred="2026-05-03"):
    return create_manual_payment(
        SQLiteManualPaymentRepository(path), sender, amount, occurred
    ).payment_event


def gmail(path, amount=5000):
    raws = SQLiteRawEmailRepository(path)
    raws.insert(
        EmailMessageSummary(
            "synthetic-fee-allocation-message", datetime(2026, 5, 3, tzinfo=UTC),
            "synthetic@example.test", "Synthetic notification",
        ),
        b"SYNTHETIC_RAW_MIME",
    )
    raw = raws.get("synthetic-fee-allocation-message")
    payments = SQLitePaymentEventRepository(path)
    payments.insert(
        raw.id,
        PaymentNotification("synthetic", "Synthetic Sender", amount, date(2026, 5, 3), None),
    )
    return payments.get_by_raw_email_id(raw.id)


def allocate(repo, payment, fee, amount="25"):
    return create_late_fee_allocation(repo, payment.id, fee.charge.id, amount)


def test_unpaid_partial_paid_and_explicit_removal(ledger):
    path, _, _, fee = ledger
    repository = SQLiteLateFeeAllocationRepository(path)
    first, second = manual(path, "25", "Synthetic One"), manual(path, "25", "Synthetic Two")
    assert get_late_fee_history(SQLiteLateFeeRepository(path), fee.charge.id).status == "UNPAID"
    allocation = allocate(repository, first, fee)
    partial = get_late_fee_history(SQLiteLateFeeRepository(path), fee.charge.id)
    assert (partial.allocated_cents, partial.remaining_cents, partial.status) == (2500, 2500, "PARTIAL")
    allocate(repository, second, fee)
    paid = get_late_fee_history(SQLiteLateFeeRepository(path), fee.charge.id)
    assert (paid.allocated_cents, paid.remaining_cents, paid.status) == (5000, 0, "PAID")
    removed = remove_late_fee_allocation(repository, allocation.id)
    assert removed == allocation
    restored = get_late_fee_history(SQLiteLateFeeRepository(path), fee.charge.id)
    assert (restored.allocated_cents, restored.remaining_cents, restored.status) == (2500, 2500, "PARTIAL")
    assert len(repository.list_summaries(late_fee_charge_id=fee.charge.id)) == 1
    with pytest.raises(LateFeeAllocationValidationError, match="does not exist"):
        remove_late_fee_allocation(repository, 999)


@pytest.mark.parametrize("amount", ["0", "-1", "banana", "1.234"])
def test_amount_validation_writes_nothing(ledger, amount):
    path, _, _, fee = ledger
    payment = manual(path)
    before = snapshot(path)
    with pytest.raises(LateFeeAllocationValidationError):
        allocate(SQLiteLateFeeAllocationRepository(path), payment, fee, amount)
    assert snapshot(path) == before


def test_missing_voided_duplicate_and_fee_limit_validation(ledger):
    path, _, obligation, fee = ledger
    repository = SQLiteLateFeeAllocationRepository(path)
    payment = manual(path)
    with pytest.raises(LateFeeAllocationValidationError, match="Payment 999 does not exist"):
        create_late_fee_allocation(repository, 999, fee.charge.id, "1")
    with pytest.raises(LateFeeAllocationValidationError, match="Late fee 999 does not exist"):
        create_late_fee_allocation(repository, payment.id, 999, "1")
    allocate(repository, payment, fee, "25")
    with pytest.raises(LateFeeAllocationValidationError, match="already has"):
        allocate(repository, payment, fee, "1")
    other = assess_late_fee(
        SQLiteLateFeeRepository(path), obligation.id, "10", "2026-05-11", "Other synthetic fee"
    )
    with pytest.raises(LateFeeAllocationValidationError, match=r"remaining balance of \$10.00"):
        allocate(repository, payment, other, "10.01")
    void_late_fee(SQLiteLateFeeRepository(path), other.charge.id, reason="Synthetic waiver")
    with pytest.raises(LateFeeAllocationValidationError, match="voided"):
        allocate(repository, payment, other, "1")
    voided_payment = manual(path, "10", "Synthetic Voided")
    void_manual_payment(SQLiteManualPaymentRepository(path), voided_payment.id, reason="Synthetic void")
    with pytest.raises(LateFeeAllocationValidationError, match="voided"):
        allocate(repository, voided_payment, fee, "1")


def test_combined_payment_limit_works_in_both_directions(ledger):
    path, _, obligation, fee = ledger
    rent = SQLiteAllocationRepository(path)
    fees = SQLiteLateFeeAllocationRepository(path)
    first = manual(path)
    create_allocation(rent, first.id, obligation.id, "1350")
    allocate(fees, first, fee, "50")
    listing = next(
        row for row in list_payment_records(SQLitePaymentListingRepository(path))
        if row.payment_event_id == first.id
    )
    assert (listing.rent_allocated_cents, listing.late_fee_allocated_cents) == (135000, 5000)
    assert (listing.allocated_cents, listing.unallocated_cents) == (140000, 0)
    other_fee = assess_late_fee(
        SQLiteLateFeeRepository(path), obligation.id, "10", "2026-05-11", "Other synthetic fee"
    )
    with pytest.raises(LateFeeAllocationValidationError, match="combined remaining balance"):
        allocate(fees, first, other_fee, "1")

    second_obligation = SQLiteObligationRepository(path).create(
        obligation.rent_account_id, "2026-06", 135000, date(2026, 6, 5)
    )
    second = manual(path, "1400", "Synthetic Second")
    allocate(fees, second, fee=other_fee, amount="10")
    with pytest.raises(AllocationValidationError, match=r"remaining amount of \$1,390.00"):
        create_allocation(rent, second.id, second_obligation.id, "1390.01")
    create_allocation(rent, second.id, second_obligation.id, "1350")
    balance = rent.payment_balance(second.id)
    assert (balance.allocated_cents, balance.remaining_cents) == (136000, 4000)


def test_allocated_fee_and_payments_cannot_be_voided_until_removed(ledger):
    path, _, _, fee = ledger
    allocations = SQLiteLateFeeAllocationRepository(path)
    manual_payment = manual(path, "50")
    manual_link = allocate(allocations, manual_payment, fee, "25")
    before_failed_void = snapshot(path)
    with pytest.raises(ValueError, match="Remove its late-fee allocations"):
        void_late_fee(SQLiteLateFeeRepository(path), fee.charge.id, reason="Synthetic waiver")
    assert snapshot(path) == before_failed_void
    with pytest.raises(ManualPaymentAllocationConflictError) as manual_error:
        void_manual_payment(SQLiteManualPaymentRepository(path), manual_payment.id, reason="Synthetic void")
    assert manual_error.value.allocated_cents == 2500
    remove_late_fee_allocation(allocations, manual_link.id)
    void_late_fee(SQLiteLateFeeRepository(path), fee.charge.id, reason="Synthetic waiver")
    void_manual_payment(SQLiteManualPaymentRepository(path), manual_payment.id, reason="Synthetic void")
    assert get_late_fee_history(SQLiteLateFeeRepository(path), fee.charge.id).status == "VOIDED"


def test_schema_unique_pair_and_health_detects_late_fee_overallocation(ledger):
    path, _, _, fee = ledger
    first = manual(path, "50", "Synthetic One")
    allocate(SQLiteLateFeeAllocationRepository(path), first, fee, "25")
    with sqlite3.connect(path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO late_fee_allocations "
            "(payment_event_id, late_fee_charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 1, 'synthetic')",
            (first.id, fee.charge.id),
        )
    second = manual(path, "50", "Synthetic Two")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO late_fee_allocations "
            "(payment_event_id, late_fee_charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 3000, 'synthetic')",
            (second.id, fee.charge.id),
        )
    health = check_database(path)
    assert not health.healthy
    assert any("Late fee" in issue.message for issue in health.issues)


def test_gmail_void_rebuild_and_manual_correction_use_combined_total(ledger):
    path, _, obligation, fee = ledger
    allocations = SQLiteLateFeeAllocationRepository(path)
    gmail_payment = gmail(path, 5000)
    gmail_link = allocate(allocations, gmail_payment, fee, "50")
    with pytest.raises(GmailPaymentAllocationConflictError) as gmail_error:
        void_gmail_payment(SQLiteGmailPaymentRepository(path), gmail_payment.id, reason="Synthetic void")
    assert gmail_error.value.allocated_cents == 5000
    with pytest.raises(PaymentRebuildAllocationConflictStorageError):
        SQLitePaymentEventRepository(path).update_rebuilt_checked(
            gmail_payment.id, gmail_payment.raw_email_id, gmail_payment.parsed_at,
            PaymentNotification("synthetic", "Synthetic Sender", 4999, date(2026, 5, 3), None),
            "synthetic-parser",
        )
    remove_late_fee_allocation(allocations, gmail_link.id)
    void_gmail_payment(SQLiteGmailPaymentRepository(path), gmail_payment.id, reason="Synthetic void")

    active_fee = assess_late_fee(
        SQLiteLateFeeRepository(path), obligation.id, "25", "2026-05-11", "Other synthetic fee"
    )
    manual_payment = manual(path, "100", "Synthetic Manual")
    allocate(allocations, manual_payment, active_fee, "25")
    with pytest.raises(ManualPaymentAllocationConflictError) as correction_error:
        correct_manual_payment(
            SQLiteManualPaymentRepository(path), manual_payment.id,
            reason="Synthetic correction", amount="24.99",
        )
    assert correction_error.value.allocated_cents == 2500
    corrected = correct_manual_payment(
        SQLiteManualPaymentRepository(path), manual_payment.id,
        reason="Synthetic correction", amount="25",
    )
    assert corrected.payment_event.amount_cents == 2500


def test_review_suggestion_and_rent_only_planner_subtract_fee_money(ledger):
    path, account, obligation, fee = ledger
    payers = SQLitePayerRepository(path)
    payer = payers.create_payer("Synthetic Sender")
    payers.add_alias(payer.id, "Synthetic Sender", normalize_alias("Synthetic Sender"))
    SQLiteRentalRepository(path).add_payer(account.id, payer.id)
    payment = manual(path)
    allocate(SQLiteLateFeeAllocationRepository(path), payment, fee, "50")
    suggestions = find_allocation_suggestions(
        SQLiteSuggestionRepository(path), SQLiteReconciliationRepository(path), payment.id
    )
    assert suggestions[0].payment_remaining_cents == 135000
    plan = build_allocation_plan(
        SQLiteAllocationPlanningRepository(path), "2026-05", "2026-05"
    )
    assert len(plan.planned_allocations) == 1
    assert plan.planned_allocations[0].rent_obligation_id == obligation.id
    assert plan.planned_allocations[0].amount_cents == 135000
    assert SQLiteLateFeeAllocationRepository(path).list_summaries() != ()
    assert len(SQLiteAllocationRepository(path).list_summaries()) == 0
    review = collect_review_items(
        SQLiteReconciliationRepository(path), SQLiteReviewRepository(path)
    )
    item = next(item for item in review if item.kind is ReviewKind.UNALLOCATED_PAYMENT)
    assert item.amount_cents == 135000


def test_cli_and_web_show_separate_allocations_read_only(ledger, capsys):
    path, account, obligation, fee = ledger
    payment = manual(path)
    rent = create_allocation(SQLiteAllocationRepository(path), payment.id, obligation.id, "100")
    assert main([
        "late-fee", "allocation", "add", "--payment", str(payment.id),
        "--late-fee", str(fee.charge.id), "--amount", "25", "--database", str(path),
    ]) == 0
    output = capsys.readouterr().out
    assert "Created late-fee allocation" in output and "Amount: $25.00" in output
    fee_link = SQLiteLateFeeAllocationRepository(path).list_summaries()[0]
    assert main(["late-fee", "history", str(fee.charge.id), "--database", str(path)]) == 0
    history_output = capsys.readouterr().out
    assert "Status: PARTIAL" in history_output and f"payment {payment.id}" in history_output

    app = create_app(path, WebAuthConfig("synthetic-hash", "synthetic-secret"))
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    before = snapshot(path)
    payment_html = client.get(f"/payments/{payment.id}").get_data(as_text=True)
    assert "Rent allocations" in payment_html and "Late-fee allocations" in payment_html
    assert f">{rent.id}<" in payment_html and f">{fee_link.id}<" in payment_html
    assert "$125.00" in payment_html and "$1,275.00" in payment_html
    account_html = client.get(f"/rent-accounts/{account.id}").get_data(as_text=True)
    assert "PARTIAL" in account_html and "$25.00" in account_html
    assert f">{payment.id}</a>" in account_html
    assert snapshot(path) == before
    assert client.post(f"/payments/{payment.id}").status_code == 405

    assert main([
        "late-fee", "allocation", "remove", str(fee_link.id), "--database", str(path),
    ]) == 0
    assert "Removed late-fee allocation" in capsys.readouterr().out


def test_v12_to_v13_migration_preserves_existing_rows_and_rolls_back(tmp_path):
    path = tmp_path / "v12.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 13):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 12")
    rentals = SQLiteRentalRepository(path)
    unit = rentals.create_unit("Synthetic Unit")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = SQLiteObligationRepository(path).create(
        account.id, "2026-05", 135000, date(2026, 5, 5)
    )
    fee = assess_late_fee(
        SQLiteLateFeeRepository(path), obligation.id, "50", "2026-05-10", "Synthetic fee"
    )
    before = snapshot(path)
    result = upgrade_database(path)
    after = snapshot(path)
    assert (result.from_version, result.to_version) == (12, 13)
    assert after[0] == CURRENT_SCHEMA_VERSION == 13
    assert all(after[1][name] == rows for name, rows in before[1].items())
    assert after[1]["late_fee_allocations"] == []
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        fks = connection.execute("PRAGMA foreign_key_list(late_fee_allocations)").fetchall()
        assert {row[2] for row in fks} == {"payment_events", "late_fee_charges"}
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO late_fee_allocations "
                "(payment_event_id, late_fee_charge_id, amount_cents, created_at) "
                "VALUES (999, ?, 1, 'synthetic')", (fee.charge.id,)
            )

    rollback = tmp_path / "v12-rollback.sqlite3"
    with sqlite3.connect(rollback) as connection:
        for version in range(1, 13):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 12")
    rollback_before = snapshot(rollback)
    def fail(connection):
        MIGRATIONS[13](connection)
        raise sqlite3.OperationalError("Synthetic migration failure")
    with pytest.raises(MigrationError):
        upgrade_database(rollback, migrations={**MIGRATIONS, 13: fail})
    assert snapshot(rollback) == rollback_before
