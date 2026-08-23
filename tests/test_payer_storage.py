import sqlite3

import pytest

from autorentledger.storage import SQLitePayerRepository


def test_payer_and_alias_schema_initialization(tmp_path):
    database_path = tmp_path / "identity.sqlite3"
    SQLitePayerRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        payer_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(payers)").fetchall()
        }
        alias_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(payer_aliases)").fetchall()
        }
        foreign_keys = connection.execute("PRAGMA foreign_key_list(payer_aliases)").fetchall()
        indexes = connection.execute("PRAGMA index_list(payer_aliases)").fetchall()

    assert payer_columns == {"id", "display_name", "created_at"}
    assert alias_columns == {"id", "payer_id", "alias", "normalized_alias", "created_at"}
    assert any(
        row[2] == "payers"
        and row[3] == "payer_id"
        and row[4] == "id"
        and row[6].upper() == "RESTRICT"
        for row in foreign_keys
    )
    assert any(row[2] == 1 for row in indexes)


def test_creates_payers_without_merging_duplicate_display_names(tmp_path):
    repository = SQLitePayerRepository(tmp_path / "identity.sqlite3")

    first = repository.create_payer("Alex Example")
    second = repository.create_payer("Alex Example")

    assert first.id != second.id
    assert [payer.display_name for payer in repository.list_payers()] == [
        "Alex Example",
        "Alex Example",
    ]


def test_one_payer_can_have_multiple_preserved_aliases(tmp_path):
    repository = SQLitePayerRepository(tmp_path / "identity.sqlite3")
    payer = repository.create_payer("Taylor Example")

    first = repository.add_alias(payer.id, "TAYLOR Q EXAMPLE", "taylor q example")
    second = repository.add_alias(payer.id, "Taylor Example", "taylor example")

    assert first.alias == "TAYLOR Q EXAMPLE"
    assert [alias.alias for alias in repository.list_aliases(payer.id)] == [
        "TAYLOR Q EXAMPLE",
        "Taylor Example",
    ]
    assert second.payer_id == payer.id


def test_normalized_alias_is_unique_across_payers(tmp_path):
    repository = SQLitePayerRepository(tmp_path / "identity.sqlite3")
    first = repository.create_payer("Alex Example")
    second = repository.create_payer("Morgan Example")
    repository.add_alias(first.id, "ALEX EXAMPLE", "alex example")

    with pytest.raises(sqlite3.IntegrityError):
        repository.add_alias(second.id, "Alex Example", "alex example")

    assert repository.get_alias("alex example").payer_id == first.id


def test_alias_foreign_key_rejects_unknown_payer(tmp_path):
    repository = SQLitePayerRepository(tmp_path / "identity.sqlite3")

    with pytest.raises(sqlite3.IntegrityError):
        repository.add_alias(999, "Unknown Example", "unknown example")
