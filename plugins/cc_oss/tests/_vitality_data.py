"""Fixed-clock constants and record builders shared by the vitality extract, scoring and assemble tests."""

from __future__ import annotations

from datetime import datetime, timezone

import vitality_extract as vx

#: ANALYSIS_NOW used by every vitality test, in Unix seconds.
NOW = 1_800_000_000

#: Seconds in one day.
DAY = 86400


def iso(days_ago: float) -> str:
    """Return an ISO-8601 UTC timestamp ``days_ago`` days before ANALYSIS_NOW."""
    return datetime.fromtimestamp(NOW - days_ago * DAY, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def data_view(*records: dict) -> vx.DataView:
    """Build a DataView from ``{"type", "data", ...}`` records at the fixed ANALYSIS_NOW."""
    return vx.DataView(records={r["type"]: r for r in records}, now=NOW)
