"""Tests for bounded index-refresh provenance emitted by the public scan entry point."""

from __future__ import annotations

import argparse

import pytest

from codemap_py import graph


@pytest.mark.parametrize(
    ("trigger", "changed", "stale", "expected"),
    [
        pytest.param("query_self_heal", "4", "true", ("query_self_heal", 4, True), id="query_self_heal"),
        pytest.param("unknown", "-1", "maybe", ("direct_cli", None, None), id="unknown"),
        pytest.param(None, "", "", ("direct_cli", None, None), id="none"),
    ],
)
def test_refresh_result_normalizes_closed_provenance(
    monkeypatch: pytest.MonkeyPatch,
    trigger: str | None,
    changed: str,
    stale: str,
    expected: tuple[str, int | None, bool | None],
) -> None:
    """Only documented triggers and observed scalar facts reach successful index telemetry."""
    if trigger is None:
        monkeypatch.delenv("CODEMAP_REFRESH_TRIGGER", raising=False)
    else:
        monkeypatch.setenv("CODEMAP_REFRESH_TRIGGER", trigger)
    monkeypatch.setenv("CODEMAP_REFRESH_CHANGED_COUNT", changed)
    monkeypatch.setenv("CODEMAP_REFRESH_STALE_BEFORE", stale)

    result = graph._refresh_result(argparse.Namespace(incremental=True))

    assert (result["trigger"], result["changed_count"], result["stale_before"]) == expected
    assert result["incremental"] is True
    assert result["result_currency"] == "current"
