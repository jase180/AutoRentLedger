import hashlib
import sqlite3
from datetime import UTC, datetime

from autorentledger.email import EmailMessageSummary
from autorentledger.storage import SQLiteRawEmailRepository


def synthetic_summary(message_id="synthetic-1"):
    return EmailMessageSummary(
        message_id=message_id,
        received_at=datetime(2024, 8, 22, 14, 0, tzinfo=UTC),
        sender="Synthetic Sender <sender@example.test>",
        subject="Synthetic payment-like notification",
    )


def test_initializes_database_schema(tmp_path):
    database_path = tmp_path / "nested" / "ledger.sqlite3"

    SQLiteRawEmailRepository(database_path)

    assert database_path.exists()
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(raw_emails)").fetchall()
        }
    assert columns == {
        "id",
        "gmail_message_id",
        "received_at",
        "sender",
        "subject",
        "raw_mime",
        "content_sha256",
        "ingested_at",
    }


def test_inserts_raw_message_as_blob_with_matching_hash(tmp_path):
    repository = SQLiteRawEmailRepository(tmp_path / "ledger.db")
    raw_mime = b"From: sender@example.test\r\nSubject: Synthetic\r\n\r\nSynthetic body.\x00"

    assert repository.insert(synthetic_summary(), raw_mime) is True

    record = repository.get("synthetic-1")
    assert record is not None
    assert record.raw_mime == raw_mime
    assert record.content_sha256 == hashlib.sha256(raw_mime).hexdigest()
    assert repository.count() == 1


def test_gmail_message_id_is_unique(tmp_path):
    repository = SQLiteRawEmailRepository(tmp_path / "ledger.db")
    summary = synthetic_summary()

    assert repository.insert(summary, b"first synthetic message") is True
    assert repository.insert(summary, b"different synthetic message") is False

    assert repository.count() == 1
    assert repository.get(summary.message_id).raw_mime == b"first synthetic message"


def test_different_gmail_ids_remain_distinct_with_identical_content(tmp_path):
    repository = SQLiteRawEmailRepository(tmp_path / "ledger.db")
    raw_mime = b"identical synthetic raw MIME"

    assert repository.insert(synthetic_summary("synthetic-1"), raw_mime) is True
    assert repository.insert(synthetic_summary("synthetic-2"), raw_mime) is True

    first = repository.get("synthetic-1")
    second = repository.get("synthetic-2")
    assert repository.count() == 2
    assert first.content_sha256 == second.content_sha256
