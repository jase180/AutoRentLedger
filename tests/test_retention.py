from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autorentledger.retention import (
    BackupRetentionError,
    BackupRetentionResult,
    daily_backup_destination,
    prune_daily_backups,
)


def create_daily_backups(directory: Path, count: int) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    paths = []
    for offset in range(count):
        timestamp = (start + timedelta(days=offset)).strftime("%Y-%m-%dT%H%M%SZ")
        path = directory / f"autorentledger-daily-{timestamp}.db"
        path.write_bytes(f"synthetic backup {offset}".encode())
        paths.append(path)
    return paths


def test_daily_backup_destination_is_utc_deterministic_and_collision_safe(tmp_path):
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    now = datetime(2026, 8, 27, 14, 36, tzinfo=UTC)
    first = daily_backup_destination(backup_directory, now=now)
    assert first.name == "autorentledger-daily-2026-08-27T143600Z.db"
    first.touch()
    second = daily_backup_destination(backup_directory, now=now)
    assert second.name == "autorentledger-daily-2026-08-27T143600Z-1.db"


def test_retention_keeps_newest_thirty_and_deletes_only_older_eligible_files(tmp_path):
    backup_directory = tmp_path / "backups"
    backups = create_daily_backups(backup_directory, 35)
    unrelated = {
        backup_directory / "manual-before-change.db",
        backup_directory / "autorentledger-2026-01-01T000000Z.db",
        backup_directory / "autorentledger-daily-not-a-timestamp.db",
        backup_directory / "README.txt",
    }
    for path in unrelated:
        path.write_text("leave me alone", encoding="utf-8")
    nested = backup_directory / "nested"
    nested.mkdir()
    nested_backup = nested / "autorentledger-daily-2020-01-01T000000Z.db"
    nested_backup.write_text("nested", encoding="utf-8")

    result = prune_daily_backups(backup_directory, 30, backups[-1])

    assert result == BackupRetentionResult(kept_count=30, deleted_count=5)
    assert all(not path.exists() for path in backups[:5])
    assert all(path.exists() for path in backups[5:])
    assert all(path.exists() for path in unrelated)
    assert nested_backup.exists()


def test_current_backup_is_preserved_even_with_future_dated_candidates(tmp_path):
    backup_directory = tmp_path / "backups"
    backups = create_daily_backups(backup_directory, 4)
    current = backups[0]

    result = prune_daily_backups(backup_directory, 2, current)

    assert result == BackupRetentionResult(kept_count=2, deleted_count=2)
    assert current.exists()
    assert backups[-1].exists()
    assert not backups[1].exists()
    assert not backups[2].exists()


def test_fewer_than_keep_count_causes_no_deletion(tmp_path):
    backup_directory = tmp_path / "backups"
    backups = create_daily_backups(backup_directory, 4)

    result = prune_daily_backups(backup_directory, 30, backups[-1])

    assert result == BackupRetentionResult(kept_count=4, deleted_count=0)
    assert all(path.exists() for path in backups)


def test_symlink_candidates_are_ignored(tmp_path, monkeypatch):
    backup_directory = tmp_path / "backups"
    backups = create_daily_backups(backup_directory, 3)
    unsafe_candidate = backups[0]
    original_is_symlink = Path.is_symlink

    def synthetic_symlink(path):
        return path == unsafe_candidate or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", synthetic_symlink)
    result = prune_daily_backups(backup_directory, 1, backups[-1])

    assert result == BackupRetentionResult(kept_count=1, deleted_count=1)
    assert unsafe_candidate.exists()
    assert not backups[1].exists()
    assert backups[-1].exists()


def test_retention_rejects_current_backup_outside_directory(tmp_path):
    backup_directory = tmp_path / "backups"
    create_daily_backups(backup_directory, 2)
    outside = tmp_path / "autorentledger-daily-2026-01-03T000000Z.db"
    outside.touch()

    with pytest.raises(BackupRetentionError, match="outside"):
        prune_daily_backups(backup_directory, 1, outside)
    assert outside.exists()


def test_deletion_failure_is_reported_without_touching_current_backup(tmp_path):
    backup_directory = tmp_path / "backups"
    backups = create_daily_backups(backup_directory, 3)

    def fail_delete(path):
        raise PermissionError("PRIVATE_SYNTHETIC_PATH_SENTINEL")

    with pytest.raises(BackupRetentionError, match="eligible old daily backup"):
        prune_daily_backups(
            backup_directory,
            1,
            backups[-1],
            delete_file=fail_delete,
        )
    assert backups[-1].exists()
    assert all(path.exists() for path in backups)


@pytest.mark.parametrize("keep", [0, -1])
def test_retention_rejects_nonpositive_counts(tmp_path, keep):
    backup_directory = tmp_path / "backups"
    current = create_daily_backups(backup_directory, 1)[0]
    with pytest.raises(ValueError, match="positive integer"):
        prune_daily_backups(backup_directory, keep, current)
