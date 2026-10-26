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
import sys
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

    @pytest.mark.parametrize(
        "base_dir",
        [
            pytest.param("runs", id="single-level"),
            # Nested base dirs are created transparently (``mkdir -p`` semantics).
            pytest.param("level1/level2/runs", id="intermediate-parents"),
        ],
    )
    def test_creates_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_dir: str) -> None:
        """Created path exists as a directory, with any missing intermediate parents created too."""
        monkeypatch.chdir(tmp_path)
        result = make_run_dir(base_dir)
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


class TestMain:
    """Integration tests for ``main()``."""

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

    @pytest.mark.parametrize(
        "base_dir",
        [
            pytest.param("../escape", id="parent-directory-traversal"),
            pytest.param("/etc/evil", id="system-prefix"),
            pytest.param("/tmp/evil", id="tmp-prefix-cwe-22"),
        ],
    )
    def test_rejects_unsafe_base_dir(self, capsys: pytest.CaptureFixture[str], base_dir: str) -> None:
        """Parent-directory traversal and absolute paths under ``/etc`` or ``/tmp`` exit 2 with an explanatory error."""
        rc = main([base_dir])
        assert rc == 2
        assert "make_run_dir:" in capsys.readouterr().err


class TestInstructionSourceLinks:
    """Covers a run-directory base that is, or links into, the instruction sources of the working tree."""

    @pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs POSIX permissions")
    @pytest.mark.parametrize(
        "base_dir",
        [
            pytest.param(".reports/audit", id="linked-base"),
            pytest.param("plugins/cc_demo/rules", id="direct-base"),
            pytest.param(".claude/agents", id="claude-dir"),
        ],
    )
    def test_base_resolving_into_sources_creates_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], base_dir: str
    ) -> None:
        """A base reaching `plugins/`, `.claude/` or `.codex/` exits 2 and leaves no timestamped directory behind."""
        monkeypatch.chdir(tmp_path)
        rules = tmp_path / "plugins" / "cc_demo" / "rules"
        rules.mkdir(parents=True)
        (tmp_path / ".claude" / "agents").mkdir(parents=True)
        (tmp_path / ".reports").mkdir()
        (tmp_path / ".reports" / "audit").symlink_to(rules)

        rc = main([base_dir])

        assert rc == 2
        assert "instruction source" in capsys.readouterr().err
        assert list(rules.iterdir()) == []
        assert list((tmp_path / ".claude" / "agents").iterdir()) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs POSIX permissions")
    def test_planted_run_dir_link_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A link already standing at the timestamped name is refused instead of reused as the run directory."""
        monkeypatch.chdir(tmp_path)
        rules = tmp_path / "plugins" / "cc_demo" / "rules"
        rules.mkdir(parents=True)
        (tmp_path / "runs").mkdir()

        class _Frozen(_mod.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 2, 3, 4, 5, tzinfo=tz)

        monkeypatch.setattr(_mod, "datetime", _Frozen)
        (tmp_path / "runs" / "2026-01-02T03-04-05Z").symlink_to(rules)

        with pytest.raises(ValueError, match="already exists"):
            make_run_dir("runs")
        assert list(rules.iterdir()) == []

    def test_existing_run_dir_is_never_reused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A directory already standing at the timestamped name is refused, so nothing planted inside it is inherited.

        A pre-made run directory could hold a link such as ``consolidate/proposals`` that later steps would write
        through; a run directory is therefore always created fresh.
        """
        monkeypatch.chdir(tmp_path)

        class _Frozen(_mod.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 2, 3, 4, 5, tzinfo=tz)

        monkeypatch.setattr(_mod, "datetime", _Frozen)
        (tmp_path / "runs" / "2026-01-02T03-04-05Z").mkdir(parents=True)

        with pytest.raises(ValueError, match="already exists"):
            make_run_dir("runs")
