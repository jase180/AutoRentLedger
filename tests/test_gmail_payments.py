import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.allocations import (
    AllocationValidationError,
    create_allocation,
    remove_allocation,
)
from autorentledger.cli import build_parser, main
from autorentledger.discovery import build_bootstrap_discovery_report
from autorentledger.email import EmailMessageSummary
from autorentledger.gmail_payments import (
    GmailPaymentAllocationConflictError,
    GmailPaymentAlreadyVoidedError,
    GmailPaymentNotFoundError,
    GmailPaymentSourceError,
    GmailPaymentValidationError,
    get_gmail_payment_history,
    void_gmail_payment,
)
from autorentledger.manual_payments import correct_manual_payment, create_manual_payment
from autorentledger.parsing import PaymentNotification
from autorentledger.payment_listing import list_payment_records
from autorentledger.review import ReviewKind, collect_review_items
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteDiscoveryRepository,
    SQLiteGmailPaymentRepository,
    SQLiteManualPaymentRepository,
    SQLiteObligationRepository,
    SQLitePaymentEventRepository,
    SQLitePaymentListingRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteReviewRepository,
)
from autorentledger.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MigrationError,
    upgrade_database,
)

RAW_MIME = b"PRIVATE_SYNTHETIC_RAW_MIME_SENTINEL"
MEMO = "PRIVATE_SYNTHETIC_MEMO_SENTINEL"
GMAIL_MESSAGE_ID = "PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL"


def create_database(tmp_path):
    database_path = tmp_path / "gmail-void.sqlite3"
    upgrade_database(database_path)
    return database_path


def add_gmail_payment(
    database_path,
    *,
    number=1,
    sender="SYNTHETIC SENDER",
    amount_cents=72500,
    occurred_on=date(2026, 6, 3),
):
    gmail_id = f"{GMAIL_MESSAGE_ID}-{number}"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    raws.insert(
        EmailMessageSummary(
            gmail_id,
            datetime(2026, 6, number, 12, tzinfo=UTC),
            "synthetic-forwarder@example.test",
            "Synthetic notification",
        ),
        RAW_MIME + str(number).encode(),
    )
    raw = raws.get(gmail_id)
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic-provider", sender, amount_cents, occurred_on, MEMO
        ),
    )
    return raw, payments.get_by_raw_email_id(raw.id)


def table_rows(database_path, table):
    with sqlite3.connect(database_path) as connection:
        return connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def test_cli_commands_exist_and_reason_is_required():
    parser = build_parser()
    history = parser.parse_args(["payment", "gmail-history", "19"])
    void = parser.parse_args(
        ["payment", "gmail-void", "19", "--reason", "Synthetic duplicate"]
    )

    assert history.payment_command == "gmail-history"
    assert void.payment_command == "gmail-void"
    with pytest.raises(SystemExit):
        parser.parse_args(["payment", "gmail-void", "19"])


def test_void_is_audited_and_preserves_gmail_evidence_and_payment_facts(tmp_path):
    database_path = create_database(tmp_path)
    raw, payment = add_gmail_payment(database_path)
    raw_before = table_rows(database_path, "raw_emails")
    facts_before = (
        payment.id,
        payment.raw_email_id,
        payment.sender_name,
        payment.amount_cents,
        payment.occurred_on,
        payment.memo,
    )

    result = void_gmail_payment(
        SQLiteGmailPaymentRepository(database_path),
        payment.id,
        reason="  Confirmed synthetic duplicate  ",
    )
    stored = SQLitePaymentEventRepository(database_path).get(payment.id)

    assert result.void.reason == "Confirmed synthetic duplicate"
    assert result.payment_event.voided_at == result.void.created_at
    assert len(table_rows(database_path, "gmail_payment_voids")) == 1
    assert table_rows(database_path, "raw_emails") == raw_before
    assert (
        stored.id,
        stored.raw_email_id,
        stored.sender_name,
        stored.amount_cents,
        stored.occurred_on,
        stored.memo,
    ) == facts_before
    assert stored.raw_email_id == raw.id


def test_void_validation_rejects_invalid_sources_states_and_reasons(tmp_path):
    database_path = create_database(tmp_path)
    _, gmail = add_gmail_payment(database_path)
    manual = create_manual_payment(
        SQLiteManualPaymentRepository(database_path),
        "Synthetic Manual Sender",
        "725.00",
        "2026-06-03",
    )
    repository = SQLiteGmailPaymentRepository(database_path)

    for reason in ("", "   "):
        with pytest.raises(GmailPaymentValidationError):
            void_gmail_payment(repository, gmail.id, reason=reason)
    with pytest.raises(GmailPaymentNotFoundError):
        void_gmail_payment(repository, 999999, reason="Synthetic reason")
    with pytest.raises(GmailPaymentSourceError):
        void_gmail_payment(
            repository, manual.payment_event.id, reason="Synthetic reason"
        )
    void_gmail_payment(repository, gmail.id, reason="Synthetic reason")
    with pytest.raises(GmailPaymentAlreadyVoidedError):
        void_gmail_payment(repository, gmail.id, reason="Synthetic second reason")
    assert len(table_rows(database_path, "gmail_payment_voids")) == 1


def test_allocated_payment_requires_explicit_removal_and_voided_payment_rejects_allocation(
    tmp_path, capsys
):
    database_path = create_database(tmp_path)
    _, payment = add_gmail_payment(database_path)
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Synthetic Unit")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = SQLiteObligationRepository(database_path).create(
        account.id, "2026-06", 72500, date(2026, 6, 1)
    )
    allocations = SQLiteAllocationRepository(database_path)
    allocation = create_allocation(allocations, payment.id, obligation.id, "725.00")

    with pytest.raises(GmailPaymentAllocationConflictError) as error:
        void_gmail_payment(
            SQLiteGmailPaymentRepository(database_path),
            payment.id,
            reason="Confirmed synthetic duplicate",
        )
    assert error.value.allocated_cents == 72500
    assert allocations.get(allocation.id) == allocation
    assert main(
        [
            "payment",
            "gmail-void",
            str(payment.id),
            "--reason",
            "Confirmed synthetic duplicate",
            "--database",
            str(database_path),
        ]
    ) == 1
    assert "Remove its allocations explicitly" in capsys.readouterr().out

    remove_allocation(allocations, allocation.id)
    void_gmail_payment(
        SQLiteGmailPaymentRepository(database_path),
        payment.id,
        reason="Confirmed synthetic duplicate",
    )
    with pytest.raises(AllocationValidationError, match="voided"):
        create_allocation(allocations, payment.id, obligation.id, "1.00")


@pytest.mark.parametrize("failure_point", ["insert", "update"])
def test_void_transaction_rolls_back_on_late_failure(tmp_path, failure_point):
    database_path = create_database(tmp_path)
    _, payment = add_gmail_payment(database_path)
    if failure_point == "insert":
        trigger = """
            CREATE TRIGGER reject_synthetic_gmail_audit
            BEFORE INSERT ON gmail_payment_voids
            BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END
        """
    else:
        trigger = """
            CREATE TRIGGER reject_synthetic_gmail_projection
            BEFORE UPDATE OF voided_at ON payment_events
            BEGIN SELECT RAISE(ABORT, 'synthetic projection failure'); END
        """
    with sqlite3.connect(database_path) as connection:
        connection.execute(trigger)

    with pytest.raises(sqlite3.IntegrityError):
        void_gmail_payment(
            SQLiteGmailPaymentRepository(database_path),
            payment.id,
            reason="Synthetic rollback reason",
        )

    assert table_rows(database_path, "gmail_payment_voids") == []
    assert SQLitePaymentEventRepository(database_path).get(payment.id).voided_at is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_history_cli_is_safe_before_and_after_void(tmp_path, capsys):
    database_path = create_database(tmp_path)
    _, payment = add_gmail_payment(database_path)
    repository = SQLiteGmailPaymentRepository(database_path)

    active = get_gmail_payment_history(repository, payment.id)
    assert active.void is None
    assert main(
        ["payment", "gmail-history", str(payment.id), "--database", str(database_path)]
    ) == 0
    active_output = capsys.readouterr().out
    assert "ACTIVE" in active_output and "Void:\n  None" in active_output

    void_gmail_payment(repository, payment.id, reason="Synthetic audit reason")
    assert main(
        ["payment", "gmail-history", str(payment.id), "--database", str(database_path)]
    ) == 0
    output = capsys.readouterr().out
    assert "VOIDED" in output
    assert "Synthetic audit reason" in output
    assert "Raw email ID:" in output
    assert RAW_MIME.decode() not in output
    assert MEMO not in output
    assert GMAIL_MESSAGE_ID not in output


def test_history_rejects_manual_and_missing_payments(tmp_path):
    database_path = create_database(tmp_path)
    manual = create_manual_payment(
        SQLiteManualPaymentRepository(database_path),
        "Synthetic Manual Sender",
        "725.00",
        "2026-06-03",
    )
    repository = SQLiteGmailPaymentRepository(database_path)

    with pytest.raises(GmailPaymentSourceError):
        get_gmail_payment_history(repository, manual.payment_event.id)
    with pytest.raises(GmailPaymentNotFoundError):
        get_gmail_payment_history(repository, 999999)


def test_void_updates_discovery_review_and_audit_listing_without_deleting_event(tmp_path):
    database_path = create_database(tmp_path)
    _, first = add_gmail_payment(database_path, number=1)
    _, second = add_gmail_payment(
        database_path, number=2, sender="Synthetic  Sender"
    )
    discovery_repository = SQLiteDiscoveryRepository(database_path)
    before = build_bootstrap_discovery_report(discovery_repository)
    assert before.active_payment_count == 2
    assert len(before.possible_duplicates) == 1

    void_gmail_payment(
        SQLiteGmailPaymentRepository(database_path),
        second.id,
        reason=f"Synthetic duplicate of payment {first.id}",
    )

    after = build_bootstrap_discovery_report(discovery_repository)
    assert after.active_payment_count == 1
    assert after.possible_duplicates == ()
    assert sum(sender.total_cents for sender in after.senders) == first.amount_cents
    review = collect_review_items(
        SQLiteReconciliationRepository(database_path),
        SQLiteReviewRepository(database_path),
    )
    assert not any(
        item.reference_id == second.id
        and item.kind in {ReviewKind.UNRESOLVED_PAYER, ReviewKind.UNALLOCATED_PAYMENT}
        for item in review
    )
    listed = list_payment_records(SQLitePaymentListingRepository(database_path))
    listed_void = next(
        record for record in listed if record.payment_event_id == second.id
    )
    assert listed_void.voided_at is not None
    assert SQLitePaymentEventRepository(database_path).get(second.id) is not None


def test_v10_to_v11_migration_preserves_rows_and_is_transactional(tmp_path):
    database_path = tmp_path / "v10.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 11):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 10")
    raw, payment = add_gmail_payment(database_path)
    manual = create_manual_payment(
        SQLiteManualPaymentRepository(database_path),
        "Synthetic Manual Sender",
        "900.00",
        "2026-06-04",
    )
    correct_manual_payment(
        SQLiteManualPaymentRepository(database_path),
        manual.payment_event.id,
        reason="Synthetic note correction",
        note="Synthetic corrected note",
    )
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Synthetic Migration Unit")
    account = rentals.create_rent_account(
        unit.id, "Synthetic Migration Household", None, None
    )
    obligation = SQLiteObligationRepository(database_path).create(
        account.id, "2026-06", 72500, date(2026, 6, 1)
    )
    create_allocation(
        SQLiteAllocationRepository(database_path),
        payment.id,
        obligation.id,
        "100.00",
    )
    raw_before = table_rows(database_path, "raw_emails")
    payment_before = table_rows(database_path, "payment_events")
    manual_evidence_before = table_rows(database_path, "manual_payment_evidence")
    manual_revisions_before = table_rows(database_path, "manual_payment_revisions")
    allocations_before = table_rows(database_path, "payment_allocations")

    result = upgrade_database(database_path)

    assert (result.from_version, result.to_version) == (10, 12)
    assert table_rows(database_path, "raw_emails") == raw_before
    assert table_rows(database_path, "payment_events") == payment_before
    assert table_rows(database_path, "manual_payment_evidence") == manual_evidence_before
    assert table_rows(database_path, "manual_payment_revisions") == manual_revisions_before
    assert table_rows(database_path, "payment_allocations") == allocations_before
    assert SQLitePaymentEventRepository(database_path).get(payment.id).raw_email_id == raw.id
    assert table_rows(database_path, "gmail_payment_voids") == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    rollback_path = tmp_path / "v10-rollback.sqlite3"
    with sqlite3.connect(rollback_path) as connection:
        for version in range(1, 11):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 10")

    def fail_after_create(connection):
        MIGRATIONS[11](connection)
        raise sqlite3.OperationalError("synthetic v11 migration failure")

    migrations = dict(MIGRATIONS)
    migrations[11] = fail_after_create
    with pytest.raises(MigrationError):
        upgrade_database(rollback_path, migrations=migrations)
    with sqlite3.connect(rollback_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'gmail_payment_voids'"
        ).fetchone() is None


def test_schema_is_current_and_has_no_generic_payment_action_tables(tmp_path):
    database_path = create_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
    assert CURRENT_SCHEMA_VERSION == 12
    assert "gmail_payment_voids" in tables
    assert "payment_actions" not in tables
