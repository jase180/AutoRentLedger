from autorentledger import storage
from autorentledger.storage import rent_operations, rentals, schedules, tenancy_setup


def test_guided_tenancy_setup_persistence_has_focused_ownership():
    assert "SQLiteTenancySetupRepository" not in rentals.__dict__
    assert tenancy_setup.SQLiteTenancySetupRepository.__module__ == (
        "autorentledger.storage.tenancy_setup"
    )
    assert storage.SQLiteTenancySetupRepository is (
        tenancy_setup.SQLiteTenancySetupRepository
    )
    assert storage.TenancySetupStorageResult is tenancy_setup.TenancySetupStorageResult


def test_rent_lifecycle_persistence_has_focused_ownership():
    assert "RentChangeStorageResult" not in schedules.__dict__
    assert "TenancyEndStorageResult" not in schedules.__dict__
    assert rent_operations.SQLiteRentOperationRepository.__module__ == (
        "autorentledger.storage.rent_operations"
    )
    assert storage.SQLiteRentOperationRepository is (
        rent_operations.SQLiteRentOperationRepository
    )
    assert storage.RentChangeStorageResult is rent_operations.RentChangeStorageResult
    assert storage.TenancyEndStorageResult is rent_operations.TenancyEndStorageResult


def test_schedule_repository_retains_generation_ownership():
    assert schedules.SQLiteRentScheduleRepository.__module__ == (
        "autorentledger.storage.schedules"
    )
    assert hasattr(schedules.SQLiteRentScheduleRepository, "generation_transaction")
    assert hasattr(schedules.SQLiteRentScheduleRepository, "list_generation_sources")
