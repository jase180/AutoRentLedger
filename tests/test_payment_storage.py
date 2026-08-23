import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.email import EmailMessageSummary
from autorentledger.parsing import PaymentNotification
from autorentledger.storage import SQLitePaymentEventRepository, SQLiteRawEmailRepository


def create_repositories(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    raw_repository = SQLiteRawEmailRepository(database_path)
    payment_repository = SQLitePaymentEventRepository(database_path)
    return database_path, raw_repository, payment_repository


def insert_synthetic_raw(raw_repository, message_id="synthetic-raw-1"):
    summary = EmailMessageSummary(
        message_id=message_id,
        received_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        sender="Synthetic Forwarder <forwarder@example.test>",
        subject="Synthetic notification",
    )
    assert raw_repository.insert(summary, b"Synthetic raw MIME") is True
    return raw_repository.get(message_id)


def synthetic_notification(*, amount_cents=123456, occurred_on=date(2026, 1, 15), memo=None):
    return PaymentNotification(
        provider="synthetic_provider",
        sender_name="Alex Example",
        amount_cents=amount_cents,
        occurred_on=occurred_on,
        memo=memo,
    )


def test_payment_events_schema_initialization(tmp_path):
    database_path, _, _ = create_repositories(tmp_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(payment_events)").fetchall()
        }
        foreign_keys = connection.execute("PRAGMA foreign_key_list(payment_events)").fetchall()

    assert columns == {
        "id",
        "raw_email_id",
        "provider",
        "sender_name",
        "amount_cents",
        "occurred_on",
        "memo",
        "parsed_at",
    }
    assert any(
        row[2] == "raw_emails" and row[3] == "raw_email_id" and row[4] == "id"
        for row in foreign_keys
    )


def test_successful_notification_persists_exact_normalized_values(tmp_path):
    _, raw_repository, payment_repository = create_repositories(tmp_path)
    raw_email = insert_synthetic_raw(raw_repository)
    notification = synthetic_notification(memo="Synthetic memo")

    assert payment_repository.insert(raw_email.id, notification) is True

    event = payment_repository.get_by_raw_email_id(raw_email.id)
    assert event is not None
    assert event.provider == "synthetic_provider"
    assert event.sender_name == "Alex Example"
    assert event.amount_cents == 123456
    assert event.occurred_on == "2026-01-15"
    assert event.memo == "Synthetic memo"
    assert event.parsed_at


def test_raw_email_foreign_key_is_enforced(tmp_path):
    _, _, payment_repository = create_repositories(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        payment_repository.insert(999, synthetic_notification())


def test_same_raw_email_cannot_create_two_payment_events(tmp_path):
    _, raw_repository, payment_repository = create_repositories(tmp_path)
    raw_email = insert_synthetic_raw(raw_repository)

    assert payment_repository.insert(raw_email.id, synthetic_notification()) is True
    assert payment_repository.insert(raw_email.id, synthetic_notification(amount_cents=999)) is False
    assert payment_repository.count() == 1


def test_two_raw_emails_create_distinct_payment_events(tmp_path):
    _, raw_repository, payment_repository = create_repositories(tmp_path)
    first_raw = insert_synthetic_raw(raw_repository, "synthetic-raw-1")
    second_raw = insert_synthetic_raw(raw_repository, "synthetic-raw-2")

    assert payment_repository.insert(first_raw.id, synthetic_notification()) is True
    assert (
        payment_repository.insert(
            second_raw.id,
            synthetic_notification(amount_cents=4207, occurred_on=date(2026, 2, 3)),
        )
        is True
    )

    assert [event.raw_email_id for event in payment_repository.list_all()] == [
        first_raw.id,
        second_raw.id,
    ]


def test_nullable_date_and_memo_are_preserved(tmp_path):
    _, raw_repository, payment_repository = create_repositories(tmp_path)
    raw_email = insert_synthetic_raw(raw_repository)

    notification = synthetic_notification(occurred_on=None, memo=None)
    assert payment_repository.insert(raw_email.id, notification) is True

    event = payment_repository.get_by_raw_email_id(raw_email.id)
    assert event.occurred_on is None
    assert event.memo is None
