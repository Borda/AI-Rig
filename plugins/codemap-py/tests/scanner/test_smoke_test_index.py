"""Tests for ``bin/smoke_test_index.py`` — codemap index smoke-test and staleness check."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from smoke_test_index import SmokeResult, compute_age_hours, main, smoke_test_index

# ---------------------------------------------------------------------------
# compute_age_hours — pure function
# ---------------------------------------------------------------------------


class TestComputeAgeHours:
    """compute_age_hours: rounding, clamping, and boundary values."""

    @pytest.mark.parametrize(
        ("mtime", "now", "expected_hours"),
        [
            # 3600-second gap returns exactly 1.0.
            pytest.param(0.0, 3600.0, 1.0, id="exactly-one-hour"),
            # Result is rounded to 2 dp.
            pytest.param(0.0, 8190.0, 2.27, id="rounds-to-two-decimal-places"),
            # Mtime > now (future file) returns 0.0, never negative.
            pytest.param(100.0, 50.0, 0.0, id="clamped-at-zero-when-mtime-in-future"),
            # Report zero age when the modification and current times match.
            pytest.param(42.0, 42.0, 0.0, id="zero-age"),
        ],
    )
    def test_age_in_hours(self, mtime: float, now: float, expected_hours: float) -> None:
        """The age is the mtime-to-now gap in hours: rounded to 2 dp and never negative."""
        assert compute_age_hours(mtime, now) == expected_hours


# ---------------------------------------------------------------------------
# SmokeResult.to_json — pure serialisation
# ---------------------------------------------------------------------------


class TestSmokeResultToJson:
    """SmokeResult.to_json: field ordering and optional error field."""

    def test_ok_result_no_error_field(self) -> None:
        """Successful result must omit the error key entirely."""
        r = SmokeResult(ok=True, stale=False, age_hours=1.5, path="/x.json")
        d = json.loads(r.to_json())
        assert "error" not in d
        assert d["ok"] is True
        assert d["stale"] is False
        assert d["age_hours"] == 1.5

    def test_failed_result_includes_error(self) -> None:
        """Failed result must include the error field."""
        r = SmokeResult(ok=False, stale=False, age_hours=None, path="/x.json", error="missing")
        d = json.loads(r.to_json())
        assert d["ok"] is False
        assert d["age_hours"] is None
        assert d["error"] == "missing"

    def test_output_is_single_line(self) -> None:
        """to_json must produce a single JSON line with no embedded newlines."""
        r = SmokeResult(ok=True, stale=False, age_hours=0.5, path="/p.json")
        assert "\n" not in r.to_json()


# ---------------------------------------------------------------------------
# smoke_test_index — main logic unit
# ---------------------------------------------------------------------------


class TestSmokeTestIndex:
    """smoke_test_index: all outcome branches."""

    def test_missing_file_returns_not_ok(self, tmp_path: Path) -> None:
        """Non-existent path → ok=False with descriptive error."""
        result = smoke_test_index(tmp_path / "missing.json", max_age_hours=24)
        assert result.ok is False
        assert result.age_hours is None
        assert "not found" in (result.error or "")

    def test_valid_fresh_index_is_ok(self, tmp_path: Path) -> None:
        """Valid index written moments ago → ok=True, stale=False."""
        p = tmp_path / "idx.json"
        p.write_text(json.dumps({"modules": []}))
        result = smoke_test_index(p, max_age_hours=24, now=p.stat().st_mtime + 10)
        assert result.ok is True
        assert result.stale is False
        assert result.error is None

    def test_stale_index_sets_stale_flag(self, tmp_path: Path) -> None:
        """Index older than max_age_hours → ok=True, stale=True."""
        p = tmp_path / "idx.json"
        p.write_text(json.dumps({"modules": []}))
        old_now = p.stat().st_mtime + 25 * 3600
        result = smoke_test_index(p, max_age_hours=24, now=old_now)
        assert result.ok is True
        assert result.stale is True

    @pytest.mark.parametrize(
        ("filename", "content", "error_fragment"),
        [
            # Corrupted JSON → ok=False with error describing the problem.
            pytest.param("bad.json", "not json {{{", "unreadable", id="invalid-json"),
            # Empty JSON object → ok=False (payload must be non-empty).
            pytest.param("empty.json", "{}", "empty", id="empty-dict"),
        ],
    )
    def test_unusable_payload_returns_not_ok(
        self, tmp_path: Path, filename: str, content: str, error_fragment: str
    ) -> None:
        """A corrupted or empty index payload → ok=False with an error describing the problem."""
        p = tmp_path / filename
        p.write_text(content)
        result = smoke_test_index(p, max_age_hours=24)
        assert result.ok is False
        assert error_fragment in (result.error or "")

    def test_non_dict_json_returns_not_ok(self, tmp_path: Path) -> None:
        """JSON array at top level → ok=False."""
        p = tmp_path / "list.json"
        p.write_text("[1, 2, 3]")
        result = smoke_test_index(p, max_age_hours=24)
        assert result.ok is False

    def test_path_in_result_is_absolute(self, tmp_path: Path) -> None:
        """result.path is always an absolute path string."""
        p = tmp_path / "idx.json"
        p.write_text(json.dumps({"k": "v"}))
        result = smoke_test_index(p, max_age_hours=24, now=p.stat().st_mtime + 1)
        assert Path(result.path).is_absolute()


# ---------------------------------------------------------------------------
# main() — CLI entry point
# ---------------------------------------------------------------------------


class TestMain:
    """Verify command-line exit codes and JSON output."""

    def test_ok_fresh_index_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh valid index → exit code 0 and ok=true in stdout."""
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        p = tmp_path / "idx.json"
        p.write_text(json.dumps({"modules": []}))
        rc = main(["--index-path", str(p), "--max-age-hours", "9999"])
        out = capsys.readouterr().out
        assert rc == 0
        assert json.loads(out)["ok"] is True

    def test_missing_file_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Missing index → exit code 1 and ok=false in stdout."""
        rc = main(["--index-path", str(tmp_path / "nope.json")])
        out = capsys.readouterr().out
        assert rc == 1
        assert json.loads(out)["ok"] is False

    def test_stale_index_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Valid but stale index → exit code 1."""
        p = tmp_path / "idx.json"
        p.write_text(json.dumps({"a": 1}))
        # Fabricate a very old mtime via utime
        import os

        os.utime(p, (0, 0))
        rc = main(["--index-path", str(p), "--max-age-hours", "1"])
        assert rc == 1


# ---------------------------------------------------------------------------
# Doctest hookup
# ---------------------------------------------------------------------------


def test_doctests_pass() -> None:
    """Doctest examples embedded in smoke_test_index.py must not regress."""
    import doctest

    import smoke_test_index as _mod

    results = doctest.testmod(_mod, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"
