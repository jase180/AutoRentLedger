"""Local persistence adapters."""

from autorentledger.storage.sqlite import RawEmailRecord, SQLiteRawEmailRepository

__all__ = ["RawEmailRecord", "SQLiteRawEmailRepository"]
