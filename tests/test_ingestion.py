from datetime import UTC, datetime

from autorentledger.email import EmailMessageSummary
from autorentledger.ingestion import IngestionResult, ingest_raw_emails
from autorentledger.storage import SQLiteRawEmailRepository


class SyntheticEmailSource:
    def __init__(self, summaries, raw_messages):
        self.summaries = summaries
        self.raw_messages = raw_messages
        self.raw_requests = []

    def search(self, query, max_results=100):
        return self.summaries[:max_results]

    def get_raw_message(self, message_id):
        self.raw_requests.append(message_id)
        return self.raw_messages[message_id]


def test_repeated_ingestion_is_idempotent_and_skips_second_download(tmp_path):
    summary = EmailMessageSummary(
        message_id="synthetic-ingest-1",
        received_at=datetime(2024, 8, 22, 14, 0, tzinfo=UTC),
        sender="sender@example.test",
        subject="Synthetic notification",
    )
    source = SyntheticEmailSource(
        [summary],
        {"synthetic-ingest-1": b"Synthetic raw MIME bytes"},
    )
    repository = SQLiteRawEmailRepository(tmp_path / "ledger.sqlite3")

    first = ingest_raw_emails(source, repository, "subject:synthetic")
    second = ingest_raw_emails(source, repository, "subject:synthetic")

    assert first == IngestionResult(found=1, inserted=1, already_present=0)
    assert second == IngestionResult(found=1, inserted=0, already_present=1)
    assert repository.count() == 1
    assert source.raw_requests == ["synthetic-ingest-1"]
