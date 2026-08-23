from datetime import UTC, datetime

from autorentledger.cli import (
    DEFAULT_DATABASE,
    DEFAULT_QUERY,
    build_parser,
    print_search_results,
    run_ingestion,
)
from autorentledger.email import EmailMessageSummary


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
        "Found: 1\n"
        "Inserted: 1\n"
        "Already present: 0\n"
        "Found: 1\n"
        "Inserted: 0\n"
        "Already present: 1\n"
    )
