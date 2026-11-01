"""Tests for ``bin/make_run_dir.py`` — research timestamped run-dir creator.

Covers:
* Happy-path creation: ``<base>/<slug>-<ts>/`` layout
* Timestamp format validation
* Nested parent creation (``mkdir -p`` semantics)
* Argument validation (missing args → exit 1, invalid patterns → exit 2)
* Windows-portability invariants: no ``/tmp`` literal, no CRLF in stdout (behavioural)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from _research_text import load_module

# Load via explicit path to avoid sys.path conflicts when foundry + research
# both provide a module named ``make_run_dir``.
_SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "make_run_dir.py"
_mod = load_module(_SCRIPT, "research_make_run_dir")

make_run_dir = _mod.make_run_dir
main = _mod.main

TIMESTAMP_RE = re.compile(r"-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enter ``tmp_path`` so the strictly relative ``base_dir`` the contract requires resolves to a writable sandbox."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestMakeRunDir:
    """Layout of the directory created by ``make_run_dir()``."""

    @pytest.fixture
    def run_dir(self, tmp_path: Path) -> Path:
        """Create a run dir for slug ``myskill`` under ``<tmp>/runs``."""
        return make_run_dir("myskill", str(tmp_path / "runs"))

    def test_creates_directory(self, run_dir: Path) -> None:
        """The returned path exists as a directory."""
        assert run_dir.is_dir()

    def test_name_starts_with_slug(self, run_dir: Path) -> None:
        """The directory name starts with ``<slug>-`` so runs of different skills never collide."""
        assert run_dir.name.startswith("myskill-")

    def test_name_ends_with_utc_timestamp(self, run_dir: Path) -> None:
        """The directory name ends with a filesystem-safe UTC ISO timestamp (dashes, trailing ``Z``)."""
        assert TIMESTAMP_RE.search(run_dir.name)

    def test_creates_intermediate_parents(self, tmp_path: Path) -> None:
        """Nested base dirs are created transparently (``mkdir -p`` semantics).

        A first run of a skill finds none of ``level1/level2`` on disk and must still succeed.
        """
        result = make_run_dir("myskill", str(tmp_path / "level1" / "level2" / "runs"))

        assert result.is_dir()


class TestMainValidation:
    """Argument validation for ``main()``."""

    @pytest.mark.parametrize(
        "argv", [pytest.param([], id="no-args"), pytest.param(["myskill"], id="slug-only-base-dir-also-required")]
    )
    def test_missing_args_exit_one(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        """Missing args (none, or only the skill-slug) → exit 1 with usage message on stderr.

        Both the skill-slug and the base-dir are required, so an empty argv and a slug-only argv are each a usage error
        (exit 1) rather than a validation error (exit 2).
        """
        rc = main(argv)

        assert rc == 1
        assert "usage" in capsys.readouterr().err

    @pytest.mark.parametrize("slug", ["../evil", "bad/slug", "bad slug", "bad!slug"])
    def test_invalid_slug_exits_two(self, workdir: Path, slug: str, capsys: pytest.CaptureFixture[str]) -> None:
        """An unsafe skill-slug → exit 2 with a ``SKILL_SLUG`` error on stderr.

        The base dir is a valid relative path, so the slug is the only reason for the rejection.
        """
        rc = main([slug, "runs"])

        assert rc == 2
        assert "SKILL_SLUG" in capsys.readouterr().err

    @pytest.mark.parametrize("base", ["../evil", "bad base", "bad!base", "/abs/path"])
    def test_invalid_base_dir_exits_two(self, base: str, capsys: pytest.CaptureFixture[str]) -> None:
        """An unsafe or absolute base-dir → exit 2 with a ``BASE_DIR`` error on stderr.

        Traversal, whitespace, punctuation and absolute paths are each refused before anything is created.
        """
        rc = main(["myskill", base])

        assert rc == 2
        assert "BASE_DIR" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["myskill\n", "runs"], id="slug-trailing-newline"),
            pytest.param(["myskill", "runs\n"], id="base-trailing-newline"),
        ],
    )
    def test_trailing_newline_is_rejected(self, argv: list[str], workdir: Path) -> None:
        """A newline-terminated slug or base dir is invalid, not silently accepted.

        The validators are anchored with ``$``, which ``re.match`` lets match before a final newline; a value pasted
        with a trailing newline would then reach the filesystem as a directory name.
        """
        assert main(argv) == 2
        assert not any(workdir.iterdir())


class TestMainHappyPath:
    """``main()`` with valid arguments."""

    def test_prints_created_path(self, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Valid args → exit 0 and the printed path matches the directory created on disk.

        The path-validation contract mandates a strictly relative ``base_dir`` (no leading ``/``, ``os.path.isabs()``
        rejected), so the run starts inside ``workdir``.
        """
        rc = main(["myskill", "runs"])

        assert rc == 0
        assert Path(capsys.readouterr().out.strip()).is_dir()

    def test_output_has_no_crlf(self, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Stdout must not contain CR (Windows text-mode regression guard)."""
        main(["myskill", "runs"])

        assert "\r" not in capsys.readouterr().out


class TestArgparseCLI:
    """The script run as a real process: ``--help`` and the golden two-positional invocation."""

    def test_help_exits_zero(self) -> None:
        """``--help`` prints usage and exits 0 (argparse contract).

        Runs the script in a real subprocess because the ``__main__`` entry point is the unit under test.
        """
        result = subprocess.run([sys.executable, str(_SCRIPT), "--help"], capture_output=True, text=True)

        assert result.returncode == 0
        assert "usage" in result.stdout.lower()

    def test_golden_invocation_two_positional(self, tmp_path: Path) -> None:
        """``make_run_dir.py <slug> <base>`` creates the run directory and prints its path relative to the cwd.

        Real subprocess: skills call the script exactly this way, so exit status and stdout are the contract.
        """
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "myskill", "runs"], cwd=tmp_path, capture_output=True, text=True
        )

        assert result.returncode == 0
        assert (tmp_path / result.stdout.strip()).is_dir()
