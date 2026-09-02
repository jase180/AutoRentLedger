"""Focused SQLite persistence for explicit late-fee assessments and void audits."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class LateFeeNotFoundError(ValueError):
    pass


class LateFeeObligationNotFoundError(ValueError):
    pass


class LateFeeAlreadyVoidedError(ValueError):
    pass


class LateFeeDuplicateError(ValueError):
    def __init__(self, fee_ids: tuple[int, ...]) -> None:
        self.fee_ids = fee_ids
        super().__init__(
            "Possible duplicate active late fee: "
            + ", ".join(str(fee_id) for fee_id in fee_ids)
            + ". Use --confirm-duplicate to assess another."
        )


class LateFeeAuditInvariantError(RuntimeError):
    pass


@dataclass(frozen=True)
class LateFeeCharge:
    id: int
    rent_obligation_id: int
    amount_cents: int
    assessed_on: str
    reason: str
    created_at: str
    voided_at: str | None


@dataclass(frozen=True)
class LateFeeVoid:
    id: int
    late_fee_charge_id: int
    reason: str
    created_at: str


@dataclass(frozen=True)
class LateFeeHistory:
    charge: LateFeeCharge
    void: LateFeeVoid | None
    period: str
    rent_account_id: int
    account_display_name: str
    unit_label: str


class SQLiteLateFeeRepository:
    """No schema initialization; reads use mode=ro, writes use checked transactions."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        mode = "rw" if write else "ro"
        connection = sqlite3.connect(
            self.database_path.resolve().as_uri() + f"?mode={mode}",
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def assess_checked(
        self,
        obligation_id: int,
        amount_cents: int,
        assessed_on: str,
        reason: str,
        *,
        confirm_duplicate: bool = False,
    ) -> LateFeeHistory:
        with self._connect(write=True) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM rent_obligations WHERE id = ?", (obligation_id,)
                ).fetchone()
                is None
            ):
                raise LateFeeObligationNotFoundError(f"Obligation {obligation_id} does not exist.")
            matches = connection.execute(
                """SELECT id FROM late_fee_charges
                   WHERE rent_obligation_id = ? AND amount_cents = ?
                     AND assessed_on = ? AND voided_at IS NULL ORDER BY id""",
                (obligation_id, amount_cents, assessed_on),
            ).fetchall()
            if matches and not confirm_duplicate:
                raise LateFeeDuplicateError(tuple(row["id"] for row in matches))
            cursor = connection.execute(
                """INSERT INTO late_fee_charges
                   (rent_obligation_id, amount_cents, assessed_on, reason, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (obligation_id, amount_cents, assessed_on, reason, datetime.now(UTC).isoformat()),
            )
            return self._history(connection, int(cursor.lastrowid))

    def void_checked(self, fee_id: int, reason: str) -> LateFeeHistory:
        with self._connect(write=True) as connection:
            history = self._history(connection, fee_id)
            if history.charge.voided_at is not None:
                raise LateFeeAlreadyVoidedError(f"Late fee {fee_id} is already voided.")
            timestamp = datetime.now(UTC).isoformat()
            connection.execute(
                """INSERT INTO late_fee_voids (late_fee_charge_id, reason, created_at)
                   VALUES (?, ?, ?)""",
                (fee_id, reason, timestamp),
            )
            connection.execute(
                "UPDATE late_fee_charges SET voided_at = ? WHERE id = ?",
                (timestamp, fee_id),
            )
            return self._history(connection, fee_id)

    def get_history(self, fee_id: int) -> LateFeeHistory:
        with self._connect() as connection:
            return self._history(connection, fee_id)

    def list_histories(
        self,
        *,
        period: str | None = None,
        account_id: int | None = None,
        active_only: bool = False,
    ) -> tuple[LateFeeHistory, ...]:
        clauses = []
        values: list[str | int] = []
        if period is not None:
            clauses.append("obligation.period = ?")
            values.append(period)
        if account_id is not None:
            clauses.append("obligation.rent_account_id = ?")
            values.append(account_id)
        if active_only:
            clauses.append("charge.voided_at IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT charge.id FROM late_fee_charges AS charge "
                "JOIN rent_obligations AS obligation ON obligation.id = charge.rent_obligation_id"
                + where
                + " ORDER BY charge.assessed_on, charge.id",
                values,
            ).fetchall()
            return tuple(self._history(connection, row["id"]) for row in rows)

    @staticmethod
    def _history(connection: sqlite3.Connection, fee_id: int) -> LateFeeHistory:
        row = connection.execute(
            """SELECT charge.*, obligation.period, obligation.rent_account_id,
                      account.display_name AS account_display_name, unit.label AS unit_label
               FROM late_fee_charges AS charge
               JOIN rent_obligations AS obligation ON obligation.id = charge.rent_obligation_id
               JOIN rent_accounts AS account ON account.id = obligation.rent_account_id
               JOIN units AS unit ON unit.id = account.unit_id
               WHERE charge.id = ?""",
            (fee_id,),
        ).fetchone()
        if row is None:
            raise LateFeeNotFoundError(f"Late fee {fee_id} does not exist.")
        charge = LateFeeCharge(
            row["id"],
            row["rent_obligation_id"],
            row["amount_cents"],
            row["assessed_on"],
            row["reason"],
            row["created_at"],
            row["voided_at"],
        )
        audit = connection.execute(
            "SELECT * FROM late_fee_voids WHERE late_fee_charge_id = ?", (fee_id,)
        ).fetchone()
        void = LateFeeVoid(**dict(audit)) if audit is not None else None
        if (charge.voided_at is None) != (void is None) or (
            void is not None and charge.voided_at != void.created_at
        ):
            raise LateFeeAuditInvariantError("Late-fee audit state is inconsistent.")
        return LateFeeHistory(
            charge,
            void,
            row["period"],
            row["rent_account_id"],
            row["account_display_name"],
            row["unit_label"],
        )
