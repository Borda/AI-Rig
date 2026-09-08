"""Tests for ``bin/make_run_dir.py`` — research timestamped run-dir creator.

Covers:
* Happy-path creation: ``<base>/<slug>-<ts>/`` layout
* Timestamp format validation
* Nested parent creation (``mkdir -p`` semantics)
* Argument validation (missing args → exit 1, invalid patterns → exit 2)
* Windows-portability invariants: no ``/tmp`` literal, no CRLF in stdout (behavioural)
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Load via explicit path to avoid sys.path conflicts when foundry + research
# both provide a module named ``make_run_dir``.
_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "make_run_dir.py"
_spec = importlib.util.spec_from_file_location("research_make_run_dir", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

make_run_dir = _mod.make_run_dir
main = _mod.main

TIMESTAMP_RE = re.compile(r"-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")


class TestMakeRunDir:
    """Unit tests for ``make_run_dir()``."""

    def test_creates_directory(self, tmp_path: Path) -> None:
        """Created path exists as a directory."""
        result = make_run_dir("myskill", str(tmp_path / "runs"))
        assert result.is_dir()

    def test_name_starts_with_slug(self, tmp_path: Path) -> None:
        """Directory name starts with ``<slug>-``."""
        result = make_run_dir("myskill", str(tmp_path / "runs"))
        assert result.name.startswith("myskill-")

    def test_timestamp_in_name(self, tmp_path: Path) -> None:
        """Directory name ends with a UTC ISO timestamp."""
        result = make_run_dir("myskill", str(tmp_path / "runs"))
        assert TIMESTAMP_RE.search(result.name)

    def test_creates_intermediate_parents(self, tmp_path: Path) -> None:
        """Nested base dirs are created transparently (``mkdir -p`` semantics)."""
        base = tmp_path / "level1" / "level2" / "runs"
        result = make_run_dir("myskill", str(base))
        assert result.is_dir()


class TestMainValidation:
    """Argument validation tests for ``main()``."""

    def test_no_args_exit_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No args → exit 1 with usage message on stderr."""
        rc = main([])
        assert rc == 1
        assert "usage" in capsys.readouterr().err

    def test_one_arg_exit_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Only skill-slug provided → exit 1 (base-dir also required)."""
        rc = main(["myskill"])
        assert rc == 1
        assert "usage" in capsys.readouterr().err

    @pytest.mark.parametrize("slug", ["../evil", "bad/slug", "bad slug", "bad!slug"])
    def test_invalid_slug_exit_two(
        self, tmp_path: Path, slug: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid skill-slug → exit 2 with SKILL_SLUG error on stderr."""
        # Use a relative base-dir from inside tmp_path — the path-validation contract requires base_dir
        # to be strictly relative; chdir into tmp_path so the relative ``runs``
        # path resolves to a writable sandbox location.
        monkeypatch.chdir(tmp_path)
        rc = main([slug, "runs"])
        assert rc == 2
        assert "SKILL_SLUG" in capsys.readouterr().err

    @pytest.mark.parametrize("base", ["../evil", "bad base", "bad!base", "/abs/path"])
    def test_invalid_base_dir_exit_two(self, tmp_path: Path, base: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Invalid base-dir → exit 2 with BASE_DIR error on stderr."""
        rc = main(["myskill", base])
        assert rc == 2
        assert "BASE_DIR" in capsys.readouterr().err


class TestMainHappyPath:
    """Integration tests for ``main()`` happy path."""

    def test_exit_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid args → exit 0.

        The path-validation contract mandates a strictly relative ``base_dir`` (no leading ``/``, ``os.path.isabs()``
        rejected); chdir into ``tmp_path`` so a relative ``runs`` base resolves to a writable sandbox location.
        """
        monkeypatch.chdir(tmp_path)
        rc = main(["myskill", "runs"])
        assert rc == 0

    def test_prints_created_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Printed path matches directory created on disk."""
        monkeypatch.chdir(tmp_path)
        rc = main(["myskill", "runs"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert Path(out).is_dir()

    def test_output_has_no_crlf(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stdout must not contain CRLF (Windows text-mode regression guard)."""
        monkeypatch.chdir(tmp_path)
        main(["myskill", "runs"])
        out = capsys.readouterr().out
        assert "\r" not in out


class TestArgparseCLI:
    """Argparse-surface tests: ``--help`` and the golden 2-positional invocation."""

    def test_help_exits_zero(self) -> None:
        """Print usage and exit 0 (argparse contract)."""
        result = subprocess.run([sys.executable, str(_SCRIPT), "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "usage" in result.stdout.lower()

    def test_golden_invocation_two_positional(self, tmp_path: Path) -> None:
        """Preserve the positional skill invocation contract and report the created run directory.

        dir.
        """
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "myskill", "runs"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert (tmp_path / result.stdout.strip()).is_dir()
