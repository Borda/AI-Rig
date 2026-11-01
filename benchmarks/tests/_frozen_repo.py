"""Location, pinned commit and skip marker for the frozen benchmark checkout used by the fix-stage tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _launcher_capability import _pinned_frozen_checkout_is_available

BENCHMARKS = Path(__file__).resolve().parents[1]
#: Clean checkout of the frozen benchmark repository, overridable through ``PL_REPO_PATH``.
FROZEN_REPO = Path(os.environ.get("PL_REPO_PATH", str(BENCHMARKS.parent / ".sandbox" / "pytorch-lightning")))
#: Commit the frozen checkout must be at for its oracle results to be comparable.
FROZEN_REPO_COMMIT = "be98784a1a03581b7051a355ae1084fd352d7cea"

requires_frozen_repo = pytest.mark.skipif(
    not _pinned_frozen_checkout_is_available(FROZEN_REPO, FROZEN_REPO_COMMIT),
    reason=f"pinned frozen benchmark checkout is unavailable at {FROZEN_REPO}",
)
