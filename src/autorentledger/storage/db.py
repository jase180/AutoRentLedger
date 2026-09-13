"""Small shared SQLite connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_connection(database_path: Path) -> sqlite3.Connection:
    """Open a writable connection with the project's standard safety settings."""
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def open_read_only_connection(database_path: Path) -> sqlite3.Connection:
    """Open a read-only connection with the project's standard row behavior."""
    connection = sqlite3.connect(
        database_path.resolve().as_uri() + "?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
