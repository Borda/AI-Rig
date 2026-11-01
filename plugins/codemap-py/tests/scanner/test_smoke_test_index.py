"""Tests for ``bin/smoke_test_index.py`` — codemap index smoke-test and staleness check."""

from __future__ import annotations

import doctest
import json
import os
import tempfile
from pathlib import Path

import pytest
import smoke_test_index as _smoke_module
from smoke_test_index import SmokeResult, compute_age_hours, main, smoke_test_index


@pytest.fixture(name="index_in_allowed_root")
def _index_in_allowed_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Index file the CLI path validator accepts: ``tmp_path`` is made the OS temp dir, mtime pinned to the epoch."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    index = tmp_path / "idx.json"
    index.write_text(json.dumps({"a": 1}))
    os.utime(index, (0, 0))
    return index


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
        """The age is the mtime-to-now gap in hours: rounded to 2 dp and never negative.

        Clock skew can put a file's mtime in the future; the reported age must clamp to zero instead of going negative.
        """
        assert compute_age_hours(mtime, now) == expected_hours


class TestSmokeResultToJson:
    """SmokeResult.to_json: field ordering and optional error field."""

    def test_ok_result_omits_error_field(self) -> None:
        """A successful result serializes without an ``error`` key.

        Consumers test for the key's presence, so an ok result must not carry a null placeholder.
        """
        d = json.loads(SmokeResult(ok=True, stale=False, age_hours=1.5, path="/x.json").to_json())

        assert "error" not in d
        assert d["ok"] is True
        assert d["stale"] is False
        assert d["age_hours"] == 1.5

    def test_failed_result_includes_error(self) -> None:
        """A failed result serializes the error message and a null age.

        The caller needs the diagnostic to tell the user why the index was rejected.
        """
        d = json.loads(SmokeResult(ok=False, stale=False, age_hours=None, path="/x.json", error="missing").to_json())

        assert d["ok"] is False
        assert d["age_hours"] is None
        assert d["error"] == "missing"

    def test_output_is_single_line(self) -> None:
        """to_json produces a single JSON line with no embedded newlines.

        The skill reads the result line-wise from stdout, so a pretty-printed payload would break the parse.
        """
        assert "\n" not in SmokeResult(ok=True, stale=False, age_hours=0.5, path="/p.json").to_json()


class TestSmokeTestIndex:
    """smoke_test_index: all outcome branches."""

    def test_missing_file_returns_not_ok(self, tmp_path: Path) -> None:
        """A non-existent path yields ok=False with a descriptive error.

        The first scan has not run yet; the caller must get a clear "not found" instead of an exception.
        """
        result = smoke_test_index(tmp_path / "missing.json", max_age_hours=24)

        assert result.ok is False
        assert result.age_hours is None
        assert "not found" in (result.error or "")

    @pytest.mark.parametrize(
        ("age_seconds", "stale"),
        [
            pytest.param(10, False, id="fresh-index"),
            pytest.param(25 * 3600, True, id="older-than-max-age"),
        ],
    )
    def test_valid_index_reports_staleness(self, tmp_path: Path, age_seconds: int, stale: bool) -> None:
        """A valid index is ok either way; ``stale`` is set only when older than ``max_age_hours``.

        The clock is injected through ``now`` (10 s versus 25 h after the write, against a 24 h limit), so the check is
        deterministic.
        """
        p = tmp_path / "idx.json"
        p.write_text(json.dumps({"modules": []}))

        result = smoke_test_index(p, max_age_hours=24, now=p.stat().st_mtime + age_seconds)

        assert result.ok is True
        assert result.stale is stale
        assert result.error is None

    @pytest.mark.parametrize(
        ("content", "error_fragment"),
        [
            # Corrupted JSON → ok=False with error describing the problem.
            pytest.param("not json {{{", "unreadable", id="invalid-json"),
            # Empty JSON object → ok=False (payload must be non-empty).
            pytest.param("{}", "empty", id="empty-dict"),
            # A top-level array is valid JSON but not an index object.
            pytest.param("[1, 2, 3]", "not a JSON object", id="non-dict-json"),
        ],
    )
    def test_unusable_payload_returns_not_ok(self, tmp_path: Path, content: str, error_fragment: str) -> None:
        """A corrupted, empty or non-object index payload gives ok=False with an error describing the problem.

        An index that cannot be served must be reported as broken so the caller rebuilds it.
        """
        p = tmp_path / "idx.json"
        p.write_text(content)

        result = smoke_test_index(p, max_age_hours=24)

        assert result.ok is False
        assert error_fragment in (result.error or "")

    def test_result_path_is_absolute(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """result.path is always an absolute path string, even for a relative input.

        The index is given as a bare filename from the working directory; the report must carry the resolved path so the
        user can find the file.
        """
        (tmp_path / "idx.json").write_text(json.dumps({"k": "v"}))
        monkeypatch.chdir(tmp_path)

        result = smoke_test_index(Path("idx.json"), max_age_hours=24)

        assert Path(result.path).is_absolute()


class TestMain:
    """Verify command-line exit codes and JSON output."""

    @pytest.mark.parametrize(
        ("index_name", "max_age_hours", "exit_code", "ok", "stale"),
        [
            # Epoch mtime against a huge limit: still fresh.
            pytest.param("idx.json", "1000000000", 0, True, False, id="fresh-index-exits-zero"),
            # Epoch mtime against a one-hour limit: valid but stale.
            pytest.param("idx.json", "1", 1, True, True, id="stale-index-exits-one"),
            # The named index does not exist.
            pytest.param("nope.json", "1", 1, False, False, id="missing-index-exits-one"),
        ],
    )
    def test_exit_code_and_json_report(
        self,
        index_in_allowed_root: Path,
        capsys: pytest.CaptureFixture[str],
        index_name: str,
        max_age_hours: str,
        exit_code: int,
        ok: bool,
        stale: bool,
    ) -> None:
        """Exit 0 only for a valid, fresh index; stdout carries the matching ok/stale flags.

        The index mtime is pinned to the epoch and the age limit varied, so freshness never depends on the wall clock.
        Asserting the JSON flags as well as the exit code keeps a path-validation rejection from passing as "stale".
        """
        rc = main(["--index-path", str(index_in_allowed_root.with_name(index_name)), "--max-age-hours", max_age_hours])

        report = json.loads(capsys.readouterr().out)
        assert rc == exit_code
        assert (report["ok"], report["stale"]) == (ok, stale)


def test_doctests_pass() -> None:
    """Doctest examples embedded in smoke_test_index.py must not regress.

    The examples document the JSON shape and age rounding that consumers rely on.
    """
    results = doctest.testmod(_smoke_module, verbose=False)

    assert results.failed == 0, f"{results.failed} doctest(s) failed"
