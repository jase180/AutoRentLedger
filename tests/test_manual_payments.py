import sqlite3
from datetime import UTC, date, datetime

import pytest
from werkzeug.security import generate_password_hash

from autorentledger.allocations import create_allocation
from autorentledger.cli import main
from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.manual_payments import (
    MANUAL_PAYMENT_PARSER_VERSION,
    ManualPaymentDuplicateError,
    ManualPaymentValidationError,
    create_manual_payment,
)
from autorentledger.operations import run_sync
from autorentledger.overview import build_owner_overview
from autorentledger.parsing import PaymentNotification
from autorentledger.payment_listing import list_payment_records
from autorentledger.rebuilding import PaymentRebuildNotEligibleError, rebuild_payments
from autorentledger.reconciliation import ReconciliationStatus, reconcile_period
from autorentledger.reporting import build_monthly_report
from autorentledger.review import ReviewKind, collect_review_items
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteManualPaymentRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLitePaymentListingRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database
from autorentledger.suggestions import find_allocation_suggestions
from autorentledger.web import WebAuthConfig, create_app


def create_database(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    upgrade_database(database_path)
    return database_path


def table_count(database_path, table):
    with sqlite3.connect(database_path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def create_manual(database_path, *, sender="Synthetic Tenant", confirm=False):
    return create_manual_payment(
        SQLiteManualPaymentRepository(database_path),
        sender,
        "1450.00",
        "2026-05-03",
        "Historical rent entered from synthetic owner records",
        confirm_duplicate=confirm,
    )


def add_gmail_payment(database_path):
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    raws.insert(
        EmailMessageSummary(
            "synthetic-gmail-1",
            datetime(2026, 5, 3, 12, tzinfo=UTC),
            "forwarder@example.test",
            "Synthetic payment notification",
        ),
        b"PRIVATE_SYNTHETIC_RAW_SENTINEL",
    )
    raw = raws.get("synthetic-gmail-1")
    assert raw is not None
    assert payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider",
            "Synthetic Tenant",
            145000,
            date(2026, 5, 3),
            "PRIVATE_SYNTHETIC_MEMO_SENTINEL",
        ),
    )
    event = payments.get_by_raw_email_id(raw.id)
    assert event is not None
    return raw, event


def test_manual_creation_is_atomic_and_uses_explicit_source(tmp_path):
    database_path = create_database(tmp_path)

    result = create_manual(database_path)

    assert result.evidence.id == result.payment_event.manual_evidence_id
    assert result.evidence.sender_name == "Synthetic Tenant"
    assert result.evidence.amount_cents == 145000
    assert result.evidence.occurred_on == "2026-05-03"
    assert result.evidence.note == "Historical rent entered from synthetic owner records"
    assert result.payment_event.raw_email_id is None
    assert result.payment_event.provider == "manual"
    assert result.payment_event.parser_version == MANUAL_PAYMENT_PARSER_VERSION
    assert result.payment_event.sender_name == result.evidence.sender_name
    assert result.payment_event.amount_cents == result.evidence.amount_cents
    assert result.payment_event.occurred_on == result.evidence.occurred_on
    assert table_count(database_path, "manual_payment_evidence") == 1
    assert table_count(database_path, "payment_events") == 1
    for table in (
        "payers",
        "payer_aliases",
        "units",
        "rent_accounts",
        "rent_obligations",
        "payment_allocations",
    ):
        assert table_count(database_path, table) == 0


@pytest.mark.parametrize(
    ("sender", "amount", "occurred_on"),
    [
        ("  ", "1450.00", "2026-05-03"),
        ("Synthetic Tenant", "0", "2026-05-03"),
        ("Synthetic Tenant", "-1.00", "2026-05-03"),
        ("Synthetic Tenant", "not-money", "2026-05-03"),
        ("Synthetic Tenant", "1450.00", "2026-5-03"),
        ("Synthetic Tenant", "1450.00", "2026-02-30"),
    ],
)
def test_manual_creation_rejects_invalid_input_without_writes(
    tmp_path, sender, amount, occurred_on
):
    database_path = create_database(tmp_path)

    with pytest.raises(ManualPaymentValidationError):
        create_manual_payment(
            SQLiteManualPaymentRepository(database_path), sender, amount, occurred_on
        )

    assert table_count(database_path, "manual_payment_evidence") == 0
    assert table_count(database_path, "payment_events") == 0


def test_event_insert_failure_rolls_back_manual_evidence(tmp_path):
    database_path = create_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_synthetic_manual_event
            BEFORE INSERT ON payment_events
            WHEN NEW.manual_evidence_id IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'synthetic event failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        create_manual(database_path)

    assert table_count(database_path, "manual_payment_evidence") == 0
    assert table_count(database_path, "payment_events") == 0


def test_manual_duplicate_guard_is_normalized_scoped_and_explicit(tmp_path):
    database_path = create_database(tmp_path)
    first = create_manual(database_path, sender="  Synthetic   Tenant ")

    with pytest.raises(ManualPaymentDuplicateError) as duplicate:
        create_manual(database_path, sender="synthetic tenant")

    assert duplicate.value.matches[0].payment_event_id == first.payment_event.id
    assert table_count(database_path, "manual_payment_evidence") == 1
    second = create_manual(database_path, sender="SYNTHETIC TENANT", confirm=True)
    assert second.payment_event.id != first.payment_event.id
    assert table_count(database_path, "manual_payment_evidence") == 2


def test_matching_gmail_payment_does_not_block_manual_creation(tmp_path):
    database_path = create_database(tmp_path)
    _, gmail_event = add_gmail_payment(database_path)

    manual = create_manual(database_path)

    assert manual.payment_event.id != gmail_event.id
    assert table_count(database_path, "payment_events") == 2


def test_database_enforces_exactly_one_unique_evidence_source(tmp_path):
    database_path = create_database(tmp_path)
    raw, _ = add_gmail_payment(database_path)
    manual = create_manual(database_path, sender="Another Synthetic Tenant")
    common_values = (
        "synthetic_provider",
        "Synthetic Sender",
        100,
        "2026-05-04",
        None,
        datetime.now(UTC).isoformat(),
        "synthetic",
    )
    insert = """
        INSERT INTO payment_events (
            raw_email_id, manual_evidence_id, provider, sender_name, amount_cents,
            occurred_on, memo, parsed_at, parser_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, (None, None, *common_values))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                insert,
                (raw.id, manual.evidence.id, *common_values),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, (raw.id, None, *common_values))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                insert,
                (None, manual.evidence.id, *common_values),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_rebuild_batch_ignores_manual_and_filtered_rebuild_rejects_it(tmp_path):
    database_path = create_database(tmp_path)
    _, gmail_event = add_gmail_payment(database_path)
    manual = create_manual(database_path)
    repository = SQLitePaymentEventRepository(database_path)
    before = repository.get(manual.payment_event.id)

    batch = rebuild_payments(repository, dry_run=True)

    assert [item.payment_event_id for item in batch.results] == [gmail_event.id]
    with pytest.raises(PaymentRebuildNotEligibleError):
        rebuild_payments(
            repository, dry_run=False, payment_event_id=manual.payment_event.id
        )
    assert repository.get(manual.payment_event.id) == before


def test_manual_add_cli_output_duplicate_override_and_schema_guard(tmp_path, capsys):
    database_path = create_database(tmp_path)
    args = [
        "payment",
        "manual-add",
        "--sender",
        "Synthetic Tenant",
        "--amount",
        "1450.00",
        "--date",
        "2026-05-03",
        "--note",
        "Historical synthetic record",
        "--database",
        str(database_path),
    ]

    assert main(args) == 0
    output = capsys.readouterr().out
    assert "Created manual payment" in output
    assert "Source: manual" in output
    assert "$1,450.00" in output
    assert main(["payments", "--database", str(database_path)]) == 0
    listing_output = capsys.readouterr().out
    assert "Synthetic Tenant" in listing_output
    assert "manual" in listing_output
    assert main(args) == 1
    assert "Possible duplicate manual payment" in capsys.readouterr().out
    assert main([*args, "--confirm-duplicate"]) == 0
    assert table_count(database_path, "payment_events") == 2
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
    assert CURRENT_SCHEMA_VERSION == 9


def test_sync_and_processing_do_not_modify_manual_evidence(tmp_path):
    class EmptyEmailSource:
        def search(self, query, max_results=100):
            return []

        def get_raw_message(self, message_id):
            raise AssertionError("No raw message should be requested.")

    database_path = create_database(tmp_path)
    manual = create_manual(database_path)
    before_evidence = manual.evidence
    before_event = manual.payment_event

    result = run_sync(
        EmptyEmailSource(),
        SQLiteRawEmailRepository(database_path),
        SQLitePaymentEventRepository(database_path),
        SQLiteReconciliationRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
        "subject:synthetic",
        100,
    )

    assert result.ingestion.found == 0
    assert result.processing.raw_emails == 0
    assert result.processing.created == 0
    assert SQLitePaymentEventRepository(database_path).get(before_event.id) == before_event
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM manual_payment_evidence WHERE id = ?", (before_evidence.id,)
        ).fetchone()
    assert row == tuple(before_evidence.__dict__.values())


def test_manual_payment_flows_through_existing_ledger_read_models(tmp_path):
    database_path = create_database(tmp_path)
    manual = create_manual(database_path)
    payment_id = manual.payment_event.id
    reconciliation = SQLiteReconciliationRepository(database_path)
    review_repository = SQLiteReviewRepository(database_path)

    initial_review = collect_review_items(reconciliation, review_repository)
    assert any(
        item.kind is ReviewKind.UNRESOLVED_PAYER
        and item.summary == "Synthetic Tenant"
        for item in initial_review
    )
    assert any(
        item.kind is ReviewKind.UNALLOCATED_PAYMENT
        and item.reference_id == payment_id
        and item.amount_cents == 145000
        for item in initial_review
    )

    payers = SQLitePayerRepository(database_path)
    payer = payers.create_payer("Synthetic Payer")
    payers.add_alias(
        payer.id, "Synthetic Tenant", normalize_alias("Synthetic Tenant")
    )
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Synthetic Unit")
    account = rentals.create_rent_account(
        unit.id, "Synthetic Household", None, None
    )
    rentals.add_payer(account.id, payer.id)
    obligation = SQLiteObligationRepository(database_path).create(
        account.id, "2026-05", 145000, date(2026, 5, 1)
    )

    listing = list_payment_records(SQLitePaymentListingRepository(database_path))
    listed = next(item for item in listing if item.payment_event_id == payment_id)
    assert listed.provider == "manual"
    assert listed.payer_id == payer.id
    assert listed.payer_display_name == "Synthetic Payer"
    assert listed.allocated_cents == 0
    assert listed.unallocated_cents == 145000

    suggestion_results = find_allocation_suggestions(
        SQLiteSuggestionRepository(database_path), reconciliation, payment_id
    )
    suggestion = suggestion_results[0].suggestion
    assert suggestion is not None
    assert suggestion.payment_event_id == payment_id
    assert suggestion.rent_obligation_id == obligation.id
    assert suggestion.suggested_amount_cents == 145000

    auth = WebAuthConfig(
        password_hash=generate_password_hash("synthetic-password"),
        secret_key="synthetic-secret-key",
    )
    client = create_app(database_path, auth).test_client()
    client.post("/login", data={"password": "synthetic-password"})
    web_output = client.get("/payments").get_data(as_text=True)
    assert "Synthetic Tenant" in web_output
    assert "Synthetic Payer" in web_output
    assert ">manual<" in web_output

    allocation = create_allocation(
        SQLiteAllocationRepository(database_path), payment_id, obligation.id, "1450.00"
    )
    assert allocation.payment_event_id == payment_id
    reconciled = reconcile_period(reconciliation, "2026-05")
    assert reconciled[0].status is ReconciliationStatus.PAID
    assert reconciled[0].allocated_cents == 145000

    report = build_monthly_report(
        reconciliation, SQLiteReportingRepository(database_path), "2026-05"
    )
    assert report.payment_received_cents == 145000
    assert report.payment_allocated_cents == 145000
    assert report.payment_unallocated_cents == 0
    overview = build_owner_overview(
        reconciliation,
        SQLiteReportingRepository(database_path),
        review_repository,
        SQLiteSuggestionRepository(database_path),
        SQLiteRentScheduleRepository(database_path),
        "2026-05",
    )
    assert overview.payment_intake.received_cents == 145000
    assert overview.rent.allocated_cents == 145000
    assert overview.rent.remaining_cents == 0
