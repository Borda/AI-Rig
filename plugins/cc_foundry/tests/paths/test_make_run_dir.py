"""Tests for ``bin/make_run_dir.py`` — foundry timestamped run-dir creator.

Covers:
* Happy-path directory creation and output
* Timestamp format validation
* Nested parent creation (``mkdir -p`` semantics)
* Argument validation (wrong count → exit 1)
* Windows-portability invariant: no ``/tmp`` literal
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

# Load via explicit path to avoid sys.path conflicts when foundry + research
# both provide a module named ``make_run_dir``.
_SCRIPT = Path(__file__).resolve().parent.parent.parent / "bin" / "make_run_dir.py"
_spec = importlib.util.spec_from_file_location("foundry_make_run_dir", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

make_run_dir = _mod.make_run_dir
main = _mod.main

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")


class TestMakeRunDir:
    """Unit tests for ``make_run_dir()``.

    The path-validation contract requires absolute ``base_dir`` values to resolve under the current working directory or
    ``~/.claude``; chdir into ``tmp_path`` so the relative ``runs`` base resolves to a writable sandbox location.
    """

    def test_creates_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Created path exists as a directory."""
        monkeypatch.chdir(tmp_path)
        result = make_run_dir("runs")
        assert result.is_dir()

    def test_returns_path_under_base(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returned path is a direct child of *base_dir*."""
        monkeypatch.chdir(tmp_path)
        result = make_run_dir("runs")
        assert result.parent == Path("runs")

    def test_timestamp_format(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Directory name matches ``YYYY-MM-DDTHH-MM-SSZ`` UTC pattern."""
        monkeypatch.chdir(tmp_path)
        result = make_run_dir("runs")
        assert TIMESTAMP_RE.match(result.name)

    def test_creates_intermediate_parents(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nested base dirs are created transparently (``mkdir -p`` semantics)."""
        monkeypatch.chdir(tmp_path)
        result = make_run_dir("level1/level2/runs")
        assert result.is_dir()


class TestMain:
    """Integration tests for ``main()``."""

    def test_happy_path_exit_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single valid arg → exit 0, prints path to stdout."""
        monkeypatch.chdir(tmp_path)
        rc = main(["runs"])
        assert rc == 0

    def test_happy_path_prints_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Printed path matches directory created on disk."""
        monkeypatch.chdir(tmp_path)
        rc = main(["runs"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert Path(out).is_dir()

    def test_golden_positional_invocation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: legacy ``make_run_dir.py <base-dir>`` positional shape → exit 0, path under base."""
        monkeypatch.chdir(tmp_path)
        rc = main(["runs"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert Path(out).parent == Path("runs")
        assert Path(out).is_dir()

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage to stdout and exit 0 (argparse contract)."""
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        assert "make_run_dir.py" in capsys.readouterr().out

    def test_no_args_exit_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No args → exit 1 with usage message on stderr."""
        rc = main([])
        assert rc == 1
        assert "usage" in capsys.readouterr().err

    def test_extra_args_exit_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Extra positional arg → exit 1 (only exactly one arg accepted)."""
        monkeypatch.chdir(tmp_path)
        rc = main(["runs", "extra"])
        assert rc == 1

    def test_output_has_no_crlf(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stdout must not contain CRLF (Windows text-mode regression guard)."""
        monkeypatch.chdir(tmp_path)
        main(["runs"])
        out = capsys.readouterr().out
        assert "\r" not in out


class TestSecurity:
    """Path-validation tests."""

    def test_rejects_traversal(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Reject parent-directory traversal with an explanatory error."""
        rc = main(["../escape"])
        assert rc == 2
        assert "make_run_dir:" in capsys.readouterr().err

    def test_rejects_system_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Absolute path under ``/etc`` → exit 2."""
        rc = main(["/etc/evil"])
        assert rc == 2
        assert "make_run_dir:" in capsys.readouterr().err

    def test_rejects_tmp_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Absolute path under ``/tmp`` → exit 2 (CWE-22)."""
        rc = main(["/tmp/evil"])
        assert rc == 2
        assert "make_run_dir:" in capsys.readouterr().err
