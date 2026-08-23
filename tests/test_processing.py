from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP

from autorentledger.email import EmailMessageSummary
from autorentledger.processing import ProcessingResult, process_raw_emails
from autorentledger.storage import SQLitePaymentEventRepository, SQLiteRawEmailRepository


def synthetic_chase_raw(sender="ALEX EXAMPLE", amount="123.45"):
    message = EmailMessage()
    message["From"] = "Synthetic Forwarder <forwarder@example.test>"
    message["Subject"] = "Synthetic forwarded notification"
    message.set_content(
        f"""\
From: Chase <alerts@chase.example.test>
Synthetic Zelle notification
{sender} sent you money
Amount: ${amount}
Sent on Jan 15, 2026
"""
    )
    return message.as_bytes(policy=SMTP)


def synthetic_unknown_raw():
    message = EmailMessage()
    message["From"] = "unknown@example.test"
    message["Subject"] = "Unsupported synthetic message"
    message.set_content("SYNTHETIC_PRIVATE_BODY_SENTINEL")
    return message.as_bytes(policy=SMTP)


def insert_raw(repository, message_id, raw_mime):
    summary = EmailMessageSummary(
        message_id=message_id,
        received_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        sender="Synthetic Forwarder <forwarder@example.test>",
        subject="Synthetic notification",
    )
    repository.insert(summary, raw_mime)


def test_processing_is_idempotent_and_retains_failed_raw_email(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    raw_repository = SQLiteRawEmailRepository(database_path)
    payment_repository = SQLitePaymentEventRepository(database_path)
    insert_raw(raw_repository, "synthetic-success", synthetic_chase_raw())
    insert_raw(raw_repository, "synthetic-failure", synthetic_unknown_raw())

    first = process_raw_emails(raw_repository, payment_repository)
    second = process_raw_emails(raw_repository, payment_repository)

    assert first == ProcessingResult(
        raw_emails=2,
        created=1,
        already_processed=0,
        parse_failures=1,
        failure_reasons=(("unsupported_provider", 1),),
    )
    assert second == ProcessingResult(
        raw_emails=2,
        created=0,
        already_processed=1,
        parse_failures=1,
        failure_reasons=(("unsupported_provider", 1),),
    )
    assert raw_repository.count() == 2
    assert payment_repository.count() == 1


def test_two_successful_raw_emails_create_two_events(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    raw_repository = SQLiteRawEmailRepository(database_path)
    payment_repository = SQLitePaymentEventRepository(database_path)
    insert_raw(raw_repository, "synthetic-one", synthetic_chase_raw())
    insert_raw(
        raw_repository,
        "synthetic-two",
        synthetic_chase_raw(sender="TAYLOR EXAMPLE", amount="42.07"),
    )

    result = process_raw_emails(raw_repository, payment_repository)

    assert result.created == 2
    assert result.parse_failures == 0
    assert [event.amount_cents for event in payment_repository.list_all()] == [12345, 4207]
