"""Tests for ``bin/make_run_dir.py`` — foundry timestamped run-dir creator.

Covers:
* Happy-path directory creation and output
* Timestamp format validation
* Nested parent creation (``mkdir -p`` semantics)
* Argument validation (wrong count → exit 1)
* Path validation (traversal, system prefixes, instruction-source links → exit 2)
* Windows-portability invariant: no CRLF in stdout
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

# Load via explicit path to avoid sys.path conflicts when foundry + research
# both provide a module named ``make_run_dir``.
_SCRIPT = Path(__file__).resolve().parents[3] / "bin" / "make_run_dir.py"
_spec = importlib.util.spec_from_file_location("foundry_make_run_dir", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

make_run_dir = _mod.make_run_dir
main = _mod.main

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")
FROZEN_NAME = "2026-01-02T03-04-05Z"

_skip_symlinks_windows = pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs POSIX permissions")


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty sandbox made the current directory, so relative base dirs resolve to a writable location.

    The path-validation contract requires base dirs to resolve under the current working directory or ``~/.claude``.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> str:
    """Freeze the script's clock at 2026-01-02 03:04:05 UTC and return the run-dir name that clock produces."""

    class _Frozen(_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 2, 3, 4, 5, tzinfo=tz)

    monkeypatch.setattr(_mod, "datetime", _Frozen)
    return FROZEN_NAME


@pytest.fixture
def rules_dir(workdir: Path) -> Path:
    """Instruction-source directory ``plugins/cc_demo/rules`` that no run directory may be created in."""
    path = workdir / "plugins" / "cc_demo" / "rules"
    path.mkdir(parents=True)
    return path


class TestMakeRunDir:
    """``make_run_dir()`` creates a UTC-timestamped directory under a validated base dir."""

    @pytest.mark.parametrize(
        "base_dir",
        [
            pytest.param("runs", id="single-level"),
            # Nested base dirs are created transparently (``mkdir -p`` semantics).
            pytest.param("level1/level2/runs", id="intermediate-parents"),
        ],
    )
    def test_creates_directory(self, workdir: Path, base_dir: str) -> None:
        """Created path exists as a directory, with any missing intermediate parents created too.

        Skills pass a base such as ``.reports/audit`` that may not exist on the first run; the call must build the whole
        chain instead of failing.
        """
        result = make_run_dir(base_dir)

        assert result.is_dir()

    def test_returns_path_under_base(self, workdir: Path) -> None:
        """Returned path is a direct child of *base_dir*.

        Callers append their own file names to the returned path, so it must sit exactly one level below the base.
        """
        result = make_run_dir("runs")

        assert result.parent == Path("runs")

    def test_timestamp_format(self, workdir: Path) -> None:
        """Directory name matches the ``YYYY-MM-DDTHH-MM-SSZ`` UTC pattern.

        The name is filesystem-safe (dashes instead of colons) and sorts chronologically, which tooling relies on.
        """
        result = make_run_dir("runs")

        assert TIMESTAMP_RE.match(result.name)


class TestMain:
    """``main()`` command-line entry point: positional base dir in, run-dir path out."""

    def test_golden_positional_invocation(self, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Legacy ``make_run_dir.py <base-dir>`` invocation exits 0 and prints a path under the base.

        Regression guard for the original positional shape that every skill call site still uses.
        """
        rc = main(["runs"])

        out = capsys.readouterr().out.strip()
        assert rc == 0
        assert Path(out).parent == Path("runs")
        assert Path(out).is_dir()

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--help`` prints usage to stdout and exits 0 (argparse contract).

        Users discover the tool through its help text; it must not be treated as an argument-count error.
        """
        with pytest.raises(SystemExit) as exc:
            main(["--help"])

        assert exc.value.code == 0
        assert "make_run_dir.py" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-args"),
            pytest.param(["runs", "extra"], id="extra-arg"),
        ],
    )
    def test_wrong_arg_count_exits_one(
        self, workdir: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        """Anything other than exactly one positional argument exits 1 with a usage message on stderr.

        Argparse would exit 2 for a missing positional; the legacy contract reserves 2 for path-validation failures and
        1 for argument-count errors, so callers can tell the two apart.
        """
        rc = main(argv)

        assert rc == 1
        assert "usage" in capsys.readouterr().err

    def test_output_has_no_crlf(self, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Stdout contains no carriage return (Windows text-mode regression guard).

        A trailing ``\\r`` would end up inside the path when a skill captures the output in a shell variable.
        """
        main(["runs"])

        assert "\r" not in capsys.readouterr().out


class TestPathValidation:
    """Unsafe base dirs are refused with exit 2 before anything is created."""

    @pytest.mark.parametrize(
        "base_dir",
        [
            pytest.param("../escape", id="parent-directory-traversal"),
            pytest.param("/etc/evil", id="system-prefix"),
            pytest.param("/tmp/evil", id="tmp-prefix-cwe-22"),
        ],
    )
    def test_rejects_unsafe_base_dir(self, capsys: pytest.CaptureFixture[str], base_dir: str) -> None:
        """Parent-directory traversal and absolute paths under ``/etc`` or ``/tmp`` exit 2 with an explanatory error.

        A run directory outside the working tree or ``~/.claude`` would let a crafted base dir write anywhere (CWE-22).
        """
        rc = main([base_dir])

        assert rc == 2
        assert "make_run_dir:" in capsys.readouterr().err


class TestInstructionSourceLinks:
    """A run-directory base that is, or links into, the instruction sources of the working tree is refused."""

    @_skip_symlinks_windows
    @pytest.mark.parametrize(
        "base_dir",
        [
            pytest.param(".reports/audit", id="linked-base"),
            pytest.param("plugins/cc_demo/rules", id="direct-base"),
            pytest.param(".claude/agents", id="claude-dir"),
        ],
    )
    def test_base_resolving_into_sources_creates_nothing(
        self, workdir: Path, rules_dir: Path, capsys: pytest.CaptureFixture[str], base_dir: str
    ) -> None:
        """A base reaching `plugins/`, `.claude/` or `.codex/` exits 2 and leaves no timestamped directory behind.

        The cases cover a symlinked report directory pointing at plugin rules, the rules directory itself, and
        `.claude/`; a run directory created there would plant files among the instructions the host loads.
        """
        agents = workdir / ".claude" / "agents"
        agents.mkdir(parents=True)
        (workdir / ".reports").mkdir()
        (workdir / ".reports" / "audit").symlink_to(rules_dir)

        rc = main([base_dir])

        assert rc == 2
        assert "instruction source" in capsys.readouterr().err
        assert list(rules_dir.iterdir()) == []
        assert list(agents.iterdir()) == []

    @_skip_symlinks_windows
    def test_planted_run_dir_link_is_refused(self, workdir: Path, rules_dir: Path, frozen_clock: str) -> None:
        """A link already standing at the timestamped name is refused instead of reused as the run directory.

        With the clock frozen, the name is predictable; an attacker-planted link there would redirect later writes into
        the instruction sources.
        """
        (workdir / "runs").mkdir()
        (workdir / "runs" / frozen_clock).symlink_to(rules_dir)

        with pytest.raises(ValueError, match="already exists"):
            make_run_dir("runs")

        assert list(rules_dir.iterdir()) == []

    def test_existing_run_dir_is_never_reused(self, workdir: Path, frozen_clock: str) -> None:
        """A directory already standing at the timestamped name is refused, so nothing planted inside it is inherited.

        A pre-made run directory could hold a link such as ``consolidate/proposals`` that later steps would write
        through; a run directory is therefore always created fresh.
        """
        (workdir / "runs" / frozen_clock).mkdir(parents=True)

        with pytest.raises(ValueError, match="already exists"):
            make_run_dir("runs")
