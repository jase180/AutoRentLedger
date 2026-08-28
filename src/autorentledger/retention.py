"""Conservative retention for verified daily-operation backups."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DAILY_BACKUP_PREFIX = "autorentledger-daily-"
_DAILY_BACKUP_PATTERN = re.compile(
    r"^autorentledger-daily-(\d{4}-\d{2}-\d{2}T\d{6}Z)(?:-([1-9]\d*))?\.db$"
)


class BackupRetentionError(RuntimeError):
    """Daily backup retention could not be completed safely."""


@dataclass(frozen=True)
class BackupRetentionResult:
    kept_count: int
    deleted_count: int


def daily_backup_destination(
    backup_directory: Path,
    *,
    now: datetime | None = None,
) -> Path:
    """Return a collision-safe path using the retention-eligible daily naming scheme."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    base = backup_directory / f"{DAILY_BACKUP_PREFIX}{timestamp}.db"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.stem}-{suffix}{base.suffix}")
        suffix += 1
    return candidate


def prune_daily_backups(
    backup_directory: Path,
    keep: int,
    current_backup: Path,
    *,
    delete_file: Callable[[Path], None] = Path.unlink,
) -> BackupRetentionResult:
    """Keep the newest eligible daily backups while always preserving the current one."""
    if keep <= 0:
        raise ValueError("Backup retention count must be a positive integer.")

    directory = backup_directory.resolve(strict=False)
    current = current_backup.resolve(strict=False)
    if current.parent != directory:
        raise BackupRetentionError("Current daily backup is outside the backup directory.")

    eligible = _eligible_daily_backups(backup_directory)
    current_entry = next(
        (entry for entry in eligible if entry[0].resolve(strict=False) == current),
        None,
    )
    if current_entry is None:
        raise BackupRetentionError("Current daily backup is not retention eligible.")

    other_entries = [entry for entry in eligible if entry is not current_entry]
    other_entries.sort(key=lambda entry: (entry[1], entry[2], entry[0].name), reverse=True)
    keep_paths = {current_entry[0]}
    keep_paths.update(entry[0] for entry in other_entries[: keep - 1])
    delete_paths = [entry[0] for entry in other_entries if entry[0] not in keep_paths]

    deleted_count = 0
    try:
        for path in delete_paths:
            if path.is_symlink() or path.parent.resolve(strict=False) != directory:
                continue
            delete_file(path)
            deleted_count += 1
    except OSError as error:
        raise BackupRetentionError("Unable to delete an eligible old daily backup.") from error

    remaining_count = len(_eligible_daily_backups(backup_directory))
    return BackupRetentionResult(
        kept_count=remaining_count,
        deleted_count=deleted_count,
    )


def _eligible_daily_backups(
    backup_directory: Path,
) -> list[tuple[Path, datetime, int]]:
    if not backup_directory.exists() or not backup_directory.is_dir():
        return []
    eligible: list[tuple[Path, datetime, int]] = []
    for candidate in backup_directory.iterdir():
        if candidate.is_symlink() or not candidate.is_file():
            continue
        match = _DAILY_BACKUP_PATTERN.fullmatch(candidate.name)
        if match is None:
            continue
        try:
            timestamp = datetime.strptime(match.group(1), "%Y-%m-%dT%H%M%SZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            continue
        collision_suffix = int(match.group(2) or 0)
        eligible.append((candidate, timestamp, collision_suffix))
    return eligible
