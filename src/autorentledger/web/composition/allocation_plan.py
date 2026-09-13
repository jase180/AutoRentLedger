"""Read-only composition helpers shared by local web screens."""

from __future__ import annotations

from pathlib import Path

from autorentledger.allocation_planning import AllocationPlan, build_allocation_plan
from autorentledger.storage import (
    SQLiteAllocationPlanningRepository,
)


def build_web_allocation_plan(
    database_path: Path, period_from: str, period_to: str
) -> AllocationPlan:
    """Return the exact canonical M26 allocation-plan preview."""
    return build_allocation_plan(
        SQLiteAllocationPlanningRepository(database_path), period_from, period_to
    )
