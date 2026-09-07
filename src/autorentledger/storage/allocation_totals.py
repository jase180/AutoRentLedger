"""Shared combined payment-allocation accounting for all target types."""

import sqlite3


def combined_payment_allocated_cents(
    connection: sqlite3.Connection, payment_event_id: int
) -> int:
    """Return rent plus late-fee allocations for one payment."""
    expression = combined_payment_allocated_sql(connection)
    row = connection.execute(
        f"SELECT {expression} AS allocated_cents FROM payment_events WHERE id = ?",
        (payment_event_id,),
    ).fetchone()
    if row is None:
        return 0
    return int(row["allocated_cents"] if isinstance(row, sqlite3.Row) else row[0])


def combined_payment_allocated_sql(
    connection: sqlite3.Connection, payment_alias: str = "payment_events"
) -> str:
    """SQL expression for correlated combined allocation reads."""
    if payment_alias not in {"payment_events", "source"}:
        raise ValueError("Unsupported payment table alias.")
    rent = (
        "COALESCE((SELECT SUM(rent_part.amount_cents) FROM payment_allocations AS rent_part "
        f"WHERE rent_part.payment_event_id = {payment_alias}.id), 0)"
    )
    has_fee_allocations = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'late_fee_allocations'"
    ).fetchone()
    if has_fee_allocations is None:
        return rent
    return rent + (
        " + COALESCE((SELECT SUM(fee_part.amount_cents) "
        "FROM late_fee_allocations AS fee_part "
        f"WHERE fee_part.payment_event_id = {payment_alias}.id), 0)"
    )


def late_fee_payment_allocated_sql(
    connection: sqlite3.Connection, payment_alias: str = "payment_events"
) -> str:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'late_fee_allocations'"
    ).fetchone() is None:
        return "0"
    return (
        "COALESCE((SELECT SUM(fee_part.amount_cents) "
        "FROM late_fee_allocations AS fee_part "
        f"WHERE fee_part.payment_event_id = {payment_alias}.id), 0)"
    )
