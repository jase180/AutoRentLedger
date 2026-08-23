from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP

from autorentledger.cli import (
    DEFAULT_DATABASE,
    DEFAULT_QUERY,
    build_parser,
    print_search_results,
    run_ingestion,
    run_parsing,
    run_payment_listing,
    run_processing,
)
from autorentledger.email import EmailMessageSummary
from autorentledger.parsing import PaymentNotification
from autorentledger.storage import SQLitePaymentEventRepository, SQLiteRawEmailRepository


class StubEmailSource:
    def __init__(self, messages):
        self.messages = messages
        self.call = None

    def search(self, query, max_results=100):
        self.call = (query, max_results)
        return self.messages

    def get_raw_message(self, message_id):
        return b"From: synthetic@example.test\r\n\r\nSynthetic body."


def test_search_command_defaults():
    args = build_parser().parse_args(["search"])

    assert args.query == DEFAULT_QUERY
    assert args.max_results == 100


def test_ingest_command_defaults():
    args = build_parser().parse_args(["ingest"])

    assert args.query == "subject:zelle"
    assert args.max_results == 100
    assert args.database == DEFAULT_DATABASE


def test_parse_command_defaults():
    args = build_parser().parse_args(["parse"])

    assert args.database == DEFAULT_DATABASE


def test_process_and_payments_command_defaults():
    process_args = build_parser().parse_args(["process"])
    payment_args = build_parser().parse_args(["payments"])

    assert process_args.database == DEFAULT_DATABASE
    assert payment_args.database == DEFAULT_DATABASE


def test_print_search_results_uses_source_neutral_interface(capsys):
    source = StubEmailSource(
        [
            EmailMessageSummary(
                message_id="abc123",
                received_at=datetime(2024, 8, 22, 14, 0, tzinfo=UTC),
                sender="sender@example.com",
                subject="Synthetic message summary",
            )
        ]
    )

    result = print_search_results(source, "zelle", 10)

    assert result == 0
    assert source.call == ("zelle", 10)
    assert capsys.readouterr().out == (
        "ID: abc123\n"
        "Received: 2024-08-22T14:00:00+00:00\n"
        "From: sender@example.com\n"
        "Subject: Synthetic message summary\n\n"
    )


def test_print_search_results_handles_no_matches(capsys):
    source = StubEmailSource([])

    assert print_search_results(source, "zelle", 10) == 0
    assert capsys.readouterr().out == "No matching messages found.\n"


def test_run_ingestion_prints_safe_summary(tmp_path, capsys):
    source = StubEmailSource(
        [
            EmailMessageSummary(
                message_id="synthetic-cli-1",
                received_at=datetime(2024, 8, 22, 14, 0, tzinfo=UTC),
                sender="synthetic@example.test",
                subject="Synthetic notification",
            )
        ]
    )
    database_path = tmp_path / "cli.sqlite3"

    assert run_ingestion(source, database_path, "subject:synthetic", 10) == 0
    assert run_ingestion(source, database_path, "subject:synthetic", 10) == 0

    assert capsys.readouterr().out == (
        "Found: 1\nInserted: 1\nAlready present: 0\nFound: 1\nInserted: 0\nAlready present: 1\n"
    )


def test_parse_cli_never_prints_raw_body(tmp_path, capsys):
    database_path = tmp_path / "parse.sqlite3"
    repository = SQLiteRawEmailRepository(database_path)
    message = EmailMessage()
    message["From"] = "unknown@example.test"
    message["Subject"] = "Unknown synthetic message"
    message.set_content("PRIVATE_RAW_SENTINEL must never appear in CLI output")
    summary = EmailMessageSummary(
        message_id="synthetic-cli-parse-1",
        received_at=datetime(2024, 8, 22, 14, 0, tzinfo=UTC),
        sender="unknown@example.test",
        subject="Unknown synthetic message",
    )
    repository.insert(summary, message.as_bytes(policy=SMTP))

    assert run_parsing(database_path) == 0

    output = capsys.readouterr().out
    assert "PRIVATE_RAW_SENTINEL" not in output
    assert "Message: synthetic-cli-parse-1" in output
    assert "Status: failed" in output
    assert "Reason: unsupported_provider" in output
    assert "Stored: 1\nParsed: 0\nFailed: 1\n" in output


def test_process_cli_summary_does_not_print_raw_body(tmp_path, capsys):
    database_path = tmp_path / "process.sqlite3"
    repository = SQLiteRawEmailRepository(database_path)
    message = EmailMessage()
    message["From"] = "unknown@example.test"
    message["Subject"] = "Unknown synthetic message"
    message.set_content("PROCESS_PRIVATE_BODY_SENTINEL")
    summary = EmailMessageSummary(
        message_id="synthetic-process-1",
        received_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        sender="unknown@example.test",
        subject="Unknown synthetic message",
    )
    repository.insert(summary, message.as_bytes(policy=SMTP))

    assert run_processing(database_path) == 0

    output = capsys.readouterr().out
    assert "PROCESS_PRIVATE_BODY_SENTINEL" not in output
    assert output == (
        "Raw emails: 1\n"
        "Created: 0\n"
        "Already processed: 0\n"
        "Parse failures: 1\n"
        "Failure reason: unsupported_provider (1)\n"
    )


def test_payments_cli_displays_normalized_event(tmp_path, capsys):
    database_path = tmp_path / "payments.sqlite3"
    raw_repository = SQLiteRawEmailRepository(database_path)
    payment_repository = SQLitePaymentEventRepository(database_path)
    summary = EmailMessageSummary(
        message_id="synthetic-payment-1",
        received_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        sender="forwarder@example.test",
        subject="Synthetic notification",
    )
    raw_repository.insert(summary, b"Synthetic raw MIME")
    raw_email = raw_repository.get("synthetic-payment-1")
    payment_repository.insert(
        raw_email.id,
        PaymentNotification(
            provider="synthetic_provider",
            sender_name="Alex Example",
            amount_cents=123456,
            occurred_on=None,
            memo=None,
        ),
    )

    assert run_payment_listing(database_path) == 0

    output = capsys.readouterr().out
    assert "ID" in output
    assert "DATE" in output
    assert "Alex Example" in output
    assert "$1,234.56" in output
    assert "synthetic_provider" in output
