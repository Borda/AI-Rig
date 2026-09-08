"""Tests for ``bin/check_index_smoke.py`` — wrapper around smoke_test_index.py.

The wrapper script invokes ``bin/smoke_test_index.py`` as a subprocess, projects the result down to
``{"ok":bool,"stale":bool,"age_hours":N}`` (plus an ``error`` field on failure), and derives the exit code:

0 — ok+fresh 1 — invalid / stale / empty smoke output 2 — invalid arguments

Strategy — exercise ``main()`` and the pure projection helpers directly, mocking ``subprocess.run`` so tests don't
depend on a real Python interpreter on PATH and don't write index fixtures to disk.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

import check_index_smoke  # noqa: E402 — bin/ on sys.path via conftest.py
from check_index_smoke import (  # noqa: E402
    derive_exit_code,
    main,
    project_smoke_result,
)


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Build a ``CompletedProcess`` stand-in for mocked ``subprocess.run``."""
    return subprocess.CompletedProcess(
        args=["python", "smoke_test_index.py"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


@pytest.fixture(name="fake_smoke")
def _fake_smoke(monkeypatch: pytest.MonkeyPatch):
    """Patch ``subprocess.run`` and ``Path.is_file`` inside ``check_index_smoke``.

    Yields a setter that tests use to declare the raw stdout the upstream
    ``smoke_test_index.py`` should appear to emit on the next call.
    ``Path.is_file`` is stubbed to ``True`` so the secondary file-existence guard does not fire before the mocked
    subprocess call.
    """
    state: dict[str, Any] = {"stdout": ""}

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Return a completed process containing the configured upstream output."""
        return _completed(state["stdout"])

    monkeypatch.setattr(check_index_smoke.subprocess, "run", _fake_run)
    monkeypatch.setattr(check_index_smoke.Path, "is_file", lambda self: True)

    def _set(stdout: str) -> None:
        """Set the mocked upstream stdout consumed by the next wrapper call."""
        state["stdout"] = stdout

    return _set


# ---------------------------------------------------------------------------
# Pure helper coverage — project_smoke_result + derive_exit_code
# ---------------------------------------------------------------------------


class TestProjectSmokeResult:
    """Projection drops upstream-only keys, preserves error on failure paths."""

    def test_success_path_strips_extra_keys(self) -> None:
        """Ok+fresh result must surface only ok/stale/age_hours."""
        out = project_smoke_result(
            json.dumps({"ok": True, "stale": False, "age_hours": 1.5, "path": "/x"}),
        )
        assert out == {"ok": True, "stale": False, "age_hours": 1.5}

    def test_error_field_is_preserved(self) -> None:
        """Failed result must keep the upstream ``error`` message intact."""
        out = project_smoke_result(
            json.dumps(
                {"ok": False, "stale": False, "age_hours": None, "error": "index file not found"},
            ),
        )
        assert out == {
            "ok": False,
            "stale": False,
            "age_hours": None,
            "error": "index file not found",
        }

    @pytest.mark.parametrize("raw", ["", "   \n"])
    def test_empty_input_returns_no_output_error(self, raw: str) -> None:
        """Empty / whitespace-only stdin must yield the canonical no-output error."""
        out = project_smoke_result(raw)
        assert out["ok"] is False
        assert out["stale"] is False
        assert out["age_hours"] is None
        assert out["error"] == "smoke_test_index.py produced no output"

    def test_invalid_json_returns_error_payload(self) -> None:
        """Malformed JSON must yield ok=false with a parse-error message."""
        out = project_smoke_result("not-json{")
        assert out["ok"] is False
        assert out["stale"] is False
        assert out["age_hours"] is None
        assert "invalid JSON" in out["error"]

    def test_non_object_json_returns_error_payload(self) -> None:
        """List or scalar top-level JSON must be rejected."""
        out = project_smoke_result(json.dumps([1, 2, 3]))
        assert out["ok"] is False
        assert "non-object" in out["error"]


class TestDeriveExitCode:
    """Exit code derivation: 0 only when ok=true AND stale=false."""

    @pytest.mark.parametrize(
        ("projected", "expected"),
        [
            pytest.param({"ok": True, "stale": False, "age_hours": 0.1}, 0, id="ok_fresh"),
            pytest.param({"ok": True, "stale": True, "age_hours": 999.0}, 1, id="ok_stale"),
            pytest.param({"ok": False, "stale": False, "age_hours": None, "error": "x"}, 1, id="failed"),
            pytest.param({"ok": False, "stale": True, "age_hours": None, "error": "x"}, 1, id="failed_stale"),
        ],
    )
    def test_exit_code_matches_contract(self, projected: dict[str, Any], expected: int) -> None:
        """Truth table from the legacy bash script must be preserved."""
        assert derive_exit_code(projected) == expected


# ---------------------------------------------------------------------------
# main() — argv + stdout + exit code surface
# ---------------------------------------------------------------------------


class TestMainArgumentValidation:
    """Argument-parsing branch — missing/unknown flags exit 2."""

    def test_missing_index_path_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No ``--index-path`` → argparse exits 2 with diagnostic on stderr."""
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "--index-path" in err

    def test_unknown_argument_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Unknown flag → argparse exits 2 with diagnostic on stderr."""
        with pytest.raises(SystemExit) as excinfo:
            main(["--index-path", "/tmp/idx.json", "--bogus", "1"])
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "unrecognized arguments" in err or "--bogus" in err


class TestMainHappyPath:
    """Fresh valid smoke result → exit 0 + projected JSON shape."""

    def test_ok_and_fresh_exits_zero(self, fake_smoke, capsys: pytest.CaptureFixture[str]) -> None:
        """Return minimal successful output for a current healthy index."""
        fake_smoke(json.dumps({"ok": True, "stale": False, "age_hours": 2.31, "path": "/x"}))
        rc = main(["--index-path", "/tmp/idx.json"])
        assert rc == 0

        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["stale"] is False
        assert payload["age_hours"] == 2.31
        assert "error" not in payload
        assert "path" not in payload


class TestMainStaleIndex:
    """Valid index past freshness threshold → exit 1, stale=true, no error key."""

    def test_stale_index_exits_one(self, fake_smoke, capsys: pytest.CaptureFixture[str]) -> None:
        """Aged-but-valid index → ok=true, stale=true, exit 1."""
        fake_smoke(json.dumps({"ok": True, "stale": True, "age_hours": 48.0}))
        rc = main(["--index-path", "/tmp/idx.json", "--max-age-hours", "1"])
        assert rc == 1

        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["stale"] is True
        assert payload["age_hours"] == 48.0
        assert "error" not in payload


class TestMainInvalidIndex:
    """Preserve an upstream error while returning the failure exit code."""

    def test_ok_false_propagates_error(self, fake_smoke, capsys: pytest.CaptureFixture[str]) -> None:
        """Upstream-reported failure surfaces the ``error`` message verbatim."""
        fake_smoke(
            json.dumps(
                {"ok": False, "stale": False, "age_hours": None, "error": "index file not found"},
            ),
        )
        rc = main(["--index-path", "/tmp/idx.json"])
        assert rc == 1

        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["stale"] is False
        assert payload["age_hours"] is None
        assert payload["error"] == "index file not found"


class TestMainEmptySmokeOutput:
    """Empty upstream stdout → exit 1, canonical no-output error message."""

    def test_empty_output_exits_one(self, fake_smoke, capsys: pytest.CaptureFixture[str]) -> None:
        """Upstream emitting nothing must produce the standard no-output payload."""
        fake_smoke("")
        rc = main(["--index-path", "/tmp/idx.json"])
        assert rc == 1

        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["stale"] is False
        assert payload["age_hours"] is None
        assert payload["error"] == "smoke_test_index.py produced no output"


class TestRunSmokeOSError:
    """Convert subprocess launch errors into structured output."""

    def test_oserror_yields_invocation_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """OS-level failure to spawn the upstream script must be reported, not raised."""

        def _boom(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            """Raise the expected spawn error for the wrapper's failure path."""
            raise OSError("exec failed")

        monkeypatch.setattr(check_index_smoke.subprocess, "run", _boom)
        monkeypatch.setattr(check_index_smoke.Path, "is_file", lambda self: True)
        rc = main(["--index-path", "/tmp/idx.json"])
        assert rc == 1

        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "failed to invoke" in payload["error"]
