import sqlite3
from datetime import UTC, date, datetime

from autorentledger.cli import build_parser, main
from autorentledger.discovery import build_bootstrap_discovery_report
from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.parsing import PaymentNotification
from autorentledger.storage import (
    SQLiteDiscoveryRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database

RAW_MIME_SENTINEL = b"PRIVATE_SYNTHETIC_RAW_MIME_BODY_SENTINEL"
MEMO_SENTINEL = "PRIVATE_SYNTHETIC_MEMO_SENTINEL"
GMAIL_ID_SENTINEL = "PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL"


def create_database(tmp_path):
    database_path = tmp_path / "discovery.sqlite3"
    upgrade_database(database_path)
    return database_path


def add_payment(
    database_path,
    *,
    message_number,
    sender,
    amount_cents,
    occurred_on,
    subject="Synthetic parsed notification",
):
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    gmail_id = f"synthetic-discovery-message-{message_number}"
    raws.insert(
        EmailMessageSummary(
            gmail_id,
            datetime(2026, 6, message_number, 12, tzinfo=UTC),
            "synthetic-bank@example.test",
            subject,
        ),
        RAW_MIME_SENTINEL + str(message_number).encode(),
    )
    raw = raws.get(gmail_id)
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic-provider",
            sender,
            amount_cents,
            occurred_on,
            MEMO_SENTINEL,
        ),
    )
    return payments.get_by_raw_email_id(raw.id)


def add_unparsed(database_path, *, message_id, subject, raw_mime=RAW_MIME_SENTINEL):
    SQLiteRawEmailRepository(database_path).insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 7, 1, 12, tzinfo=UTC),
            "synthetic-forwarder@example.test",
            subject,
        ),
        raw_mime,
    )


def populate_discovery_fixture(database_path):
    first = add_payment(
        database_path,
        message_number=1,
        sender="SYNTHETIC SENDER",
        amount_cents=72500,
        occurred_on=date(2026, 6, 3),
    )
    second = add_payment(
        database_path,
        message_number=2,
        sender="Synthetic  Sender",
        amount_cents=72500,
        occurred_on=date(2026, 6, 3),
    )
    add_payment(
        database_path,
        message_number=3,
        sender="SYNTHETIC SENDER",
        amount_cents=80000,
        occurred_on=date(2026, 6, 4),
    )
    add_payment(
        database_path,
        message_number=4,
        sender="OTHER SYNTHETIC",
        amount_cents=90000,
        occurred_on=None,
    )
    add_payment(
        database_path,
        message_number=5,
        sender="OTHER SYNTHETIC",
        amount_cents=90000,
        occurred_on=date(2026, 6, 4),
    )
    voided = add_payment(
        database_path,
        message_number=6,
        sender="SYNTHETIC SENDER",
        amount_cents=72500,
        occurred_on=date(2026, 6, 3),
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE payment_events SET voided_at = ? WHERE id = ?",
            ("2026-08-01T12:00:00+00:00", voided.id),
        )

    payers = SQLitePayerRepository(database_path)
    payer = payers.create_payer("Synthetic Resolved Payer")
    payers.add_alias(
        payer.id,
        "Synthetic Sender",
        normalize_alias("Synthetic Sender"),
    )
    add_unparsed(
        database_path,
        message_id=GMAIL_ID_SENTINEL,
        subject=" You received money with Zelle ",
    )
    add_unparsed(
        database_path,
        message_id="synthetic-unparsed-two",
        subject="You received money with Zelle",
    )
    add_unparsed(
        database_path,
        message_id="synthetic-unparsed-three",
        subject="A new synthetic payment is in your account",
    )
    add_unparsed(
        database_path,
        message_id="synthetic-unparsed-blank",
        subject="   ",
    )
    return payer, first, second


def database_snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        )
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            tables,
            {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            },
        )


def test_discovery_parser_and_empty_current_database(tmp_path, capsys):
    database_path = create_database(tmp_path)
    args = build_parser().parse_args(
        ["discovery", "payments", "--database", str(database_path)]
    )
    assert args.command == "discovery"
    assert args.discovery_command == "payments"

    before = database_snapshot(database_path)
    assert main(["discovery", "payments", "--database", str(database_path)]) == 0
    output = capsys.readouterr().out
    assert "Bootstrap payment discovery" in output
    assert "Active payments: 0" in output
    assert "Observed sender spellings: 0" in output
    assert "Possible duplicate groups: 0" in output
    assert "Unparsed Gmail messages: 0" in output
    assert "No ledger configuration was changed." in output
    assert database_snapshot(database_path) == before


def test_sender_inventory_uses_exact_spellings_and_exact_alias_resolution(tmp_path):
    database_path = create_database(tmp_path)
    payer, _, _ = populate_discovery_fixture(database_path)

    report = build_bootstrap_discovery_report(
        SQLiteDiscoveryRepository(database_path)
    )
    senders = {sender.sender_name: sender for sender in report.senders}

    assert set(senders) == {
        "OTHER SYNTHETIC",
        "SYNTHETIC SENDER",
        "Synthetic  Sender",
    }
    exact = senders["SYNTHETIC SENDER"]
    assert exact.payment_count == 2
    assert exact.total_cents == 152500
    assert exact.first_occurred_on == date(2026, 6, 3)
    assert exact.last_occurred_on == date(2026, 6, 4)
    assert exact.payer_id == payer.id
    assert exact.payer_display_name == "Synthetic Resolved Payer"
    variant = senders["Synthetic  Sender"]
    assert variant.payment_count == 1
    assert variant.payer_id == payer.id
    unresolved = senders["OTHER SYNTHETIC"]
    assert unresolved.payment_count == 2
    assert unresolved.total_cents == 180000
    assert unresolved.first_occurred_on == date(2026, 6, 4)
    assert unresolved.last_occurred_on == date(2026, 6, 4)
    assert unresolved.payer_id is None
    assert report.active_payment_count == 5
    assert report.resolved_sender_count == 2
    assert report.unresolved_sender_count == 1


def test_possible_duplicates_are_normalized_review_only_and_deterministic(tmp_path):
    database_path = create_database(tmp_path)
    _, first, second = populate_discovery_fixture(database_path)
    before = database_snapshot(database_path)

    report = build_bootstrap_discovery_report(
        SQLiteDiscoveryRepository(database_path)
    )

    assert len(report.possible_duplicates) == 1
    duplicate = report.possible_duplicates[0]
    assert duplicate.observed_senders == (
        "Synthetic  Sender",
        "SYNTHETIC SENDER",
    )
    assert duplicate.occurred_on == date(2026, 6, 3)
    assert duplicate.amount_cents == 72500
    assert duplicate.payment_event_ids == (first.id, second.id)
    assert database_snapshot(database_path) == before


def test_unparsed_subjects_group_exact_trimmed_metadata_only(tmp_path):
    database_path = create_database(tmp_path)
    populate_discovery_fixture(database_path)

    report = build_bootstrap_discovery_report(
        SQLiteDiscoveryRepository(database_path)
    )

    assert report.unparsed_email_count == 4
    assert [(subject.subject, subject.count) for subject in report.unparsed_subjects] == [
        ("(no subject)", 1),
        ("A new synthetic payment is in your account", 1),
        ("You received money with Zelle", 2),
    ]


def test_cli_report_is_read_only_private_safe_and_summarized(tmp_path, capsys):
    database_path = create_database(tmp_path)
    populate_discovery_fixture(database_path)
    before = database_snapshot(database_path)

    exit_code = main(
        ["discovery", "payments", "--database", str(database_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "SYNTHETIC SENDER" in output
    assert "Synthetic  Sender" in output
    assert "payer 1 - Synthetic Resolved Payer" in output
    assert "OTHER SYNTHETIC" in output and "unresolved" in output
    assert "POSSIBLE DUPLICATE NOTIFICATIONS" in output
    assert "possible duplicates only" in output
    assert "1, 2" in output
    assert "You received money with Zelle" in output
    assert "Active payments: 5" in output
    assert "Observed sender spellings: 3" in output
    assert "Resolved sender spellings: 2" in output
    assert "Unresolved sender spellings: 1" in output
    assert "Possible duplicate groups: 1" in output
    assert "Unparsed Gmail messages: 4" in output
    assert "No ledger configuration was changed." in output
    assert RAW_MIME_SENTINEL.decode() not in output
    assert MEMO_SENTINEL not in output
    assert GMAIL_ID_SENTINEL not in output
    assert database_snapshot(database_path) == before
    assert CURRENT_SCHEMA_VERSION == 11


def test_discovery_does_not_change_guided_setup_or_create_accounting(tmp_path):
    database_path = create_database(tmp_path)
    populate_discovery_fixture(database_path)
    before = database_snapshot(database_path)

    assert main(["discovery", "payments", "--database", str(database_path)]) == 0

    assert database_snapshot(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM rent_accounts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM rent_obligations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM payment_allocations").fetchone()[0] == 0
