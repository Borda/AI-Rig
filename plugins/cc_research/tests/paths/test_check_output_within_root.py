"""Tests for ``bin/check_output_within_root.py`` — output-path containment and ``--out`` parsing.

Path containment and ``--parse-out`` logic run in-process; a few tests keep the real subprocess so the script entry
point, its exit codes and its argparse surface are still proven end to end.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import check_output_within_root as cow
import pytest

BIN = str(Path(__file__).resolve().parents[2] / "bin" / "check_output_within_root.py")
_SENTINEL = "sweep-out-path"
_SESSION = "testsess"


def _run_script(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None):
    """Run the script in a real subprocess and return the completed process."""
    return subprocess.run([sys.executable, BIN, *args], capture_output=True, text=True, cwd=cwd, env=env)


@pytest.fixture
def sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Private sentinel directory and fixed session token, with the working directory at ``tmp_path``."""
    directory = tmp_path / "sentinels"
    directory.mkdir()
    monkeypatch.setenv("TMPDIR", str(directory))
    monkeypatch.setenv("CSID", _SESSION)
    monkeypatch.chdir(tmp_path)
    return directory


def _parse_out(arguments: str) -> int:
    """Run the in-process ``--parse-out`` mode for ``arguments`` against the sweep sentinel."""
    return cow.main([f"--parse-out={arguments}", "--sentinel", _SENTINEL])


class TestContainment:
    """``<candidate> <root>`` positional mode decides whether a path stays inside the project root."""

    @pytest.mark.parametrize(
        "candidate",
        [
            pytest.param("{root}/sub/dir", id="missing-subdirectory"),
            pytest.param("{root}", id="equal-to-root"),
        ],
    )
    def test_inside_root_exits_zero(self, tmp_path: Path, candidate: str) -> None:
        """A candidate below or equal to the root is accepted.

        The candidate need not exist yet: skills check the path before creating it, and the root itself is allowed.
        """
        assert cow.main([candidate.format(root=tmp_path), str(tmp_path)]) == 0

    def test_existing_subdirectory_exits_zero(self, tmp_path: Path) -> None:
        """The documented SKILL call shape ``<candidate> <root>`` accepts an existing subdirectory.

        Guards the golden invocation against regression: two positionals, candidate already on disk below the root.
        """
        (tmp_path / "sub").mkdir()

        assert cow.main([str(tmp_path / "sub"), str(tmp_path)]) == 0

    @pytest.mark.parametrize(
        "candidate",
        [
            pytest.param("/tmp/evil", id="absolute-elsewhere"),
            pytest.param("{root}-evil", id="sibling-sharing-prefix"),
            pytest.param("{root}/../sibling", id="relative-parent"),
            pytest.param("{root}/../../etc", id="traversal-above-root"),
        ],
    )
    def test_outside_root_exits_one(self, tmp_path: Path, candidate: str) -> None:
        """A candidate outside the root is rejected, including a sibling that merely shares the root's name prefix.

        String-prefix checks wrongly accept ``<root>-evil``; resolving ``..`` segments first is what these cases pin.
        """
        assert cow.main([candidate.format(root=tmp_path), str(tmp_path)]) == 1

    def test_one_positional_is_argument_error(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Supplying only the candidate, without a root, is an argparse error (exit 2)."""
        with pytest.raises(SystemExit) as excinfo:
            cow.main([str(tmp_path)])

        assert excinfo.value.code == 2
        assert "required" in capsys.readouterr().err


class TestParseOut:
    """``--parse-out`` extracts ``--out`` from a raw ``$ARGUMENTS`` string and persists it to a sentinel."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("goal text --out docs/program.md --team", "docs/program.md\n", id="relative-path-persisted"),
            pytest.param("some optimization goal", "program.md\n", id="absent-writes-default"),
            pytest.param("--team --out out.md", "out.md\n", id="leading-dash-payload"),
        ],
    )
    @mock.patch("check_output_within_root.project_root", autospec=True)
    def test_persists_sentinel(
        self, mock_root: mock.MagicMock, arguments: str, expected: str, sentinels: Path, tmp_path: Path
    ) -> None:
        """The requested output path, or ``program.md`` when ``--out`` is absent, lands in the sentinel.

        ``$ARGUMENTS`` often starts with a flag; the attached ``--parse-out=`` form must absorb it. The git lookup of
        the project root is mocked to ``tmp_path`` (the real one is covered by the subprocess test below).
        """
        mock_root.return_value = str(tmp_path)

        assert _parse_out(arguments) == 0
        assert (sentinels / f"{_SENTINEL}-{_SESSION}").read_text(encoding="utf-8") == expected

    @pytest.mark.parametrize("candidate", ["../evil.md", "sub/../../evil.md"])
    def test_rejects_traversal(self, sentinels: Path, capsys: pytest.CaptureFixture[str], candidate: str) -> None:
        """An ``--out`` path containing ``..`` is rejected with the labelled traversal error.

        Rejection happens before any git lookup, so no sentinel is written and the message starts with the label.
        """
        assert _parse_out(f"goal --out {candidate}") == 2

        stderr = capsys.readouterr().err
        assert "invalid --out path (path traversal not allowed)" in stderr
        assert stderr.startswith("sweep:")

    @mock.patch("check_output_within_root.project_root", autospec=True)
    def test_rejects_escape_without_traversal(
        self, mock_root: mock.MagicMock, sentinels: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An absolute path outside the project root escapes without containing ``..``."""
        mock_root.return_value = str(tmp_path)
        outside = tmp_path.parent / "outside-root.md"

        assert _parse_out(f"goal --out {outside}") == 2
        assert "--out path escapes project root" in capsys.readouterr().err

    def test_requires_sentinel(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--parse-out`` without ``--sentinel`` is an argparse error naming the missing flag."""
        with pytest.raises(SystemExit) as excinfo:
            cow.main(["--parse-out=goal --out x.md"])

        assert excinfo.value.code == 2
        assert "--sentinel" in capsys.readouterr().err


class TestScriptEntryPoint:
    """The shipped script run as a real subprocess: exit codes, usage text and the real git root lookup."""

    @pytest.mark.parametrize(
        ("candidate", "code"),
        [pytest.param("{root}/sub", 0, id="inside-root"), pytest.param("{root}-evil", 1, id="sibling-prefix")],
    )
    def test_exit_code(self, tmp_path: Path, candidate: str, code: int) -> None:
        """The process exits 0 for a contained path and 1 for an escaping one.

        This is the contract hooks and skills depend on, so it is exercised through the real interpreter once.
        """
        assert _run_script(candidate.format(root=tmp_path), str(tmp_path)).returncode == code

    def test_help_exits_zero(self) -> None:
        """Print usage and exit 0 (argparse contract)."""
        result = _run_script("--help")

        assert result.returncode == 0
        assert "usage" in result.stdout.lower()

    def test_parse_out_persists_with_real_git_lookup(self, tmp_path: Path) -> None:
        """``--parse-out`` end to end with the real project-root lookup and an isolated sentinel directory.

        Run from a directory outside any repository, the root falls back to the working directory, so the relative
        ``--out`` path is accepted and persisted.
        """
        sentinels = tmp_path / "sentinels"
        sentinels.mkdir()
        env = {**os.environ, "TMPDIR": str(sentinels), "CSID": _SESSION}

        result = _run_script("--parse-out=goal --out docs/program.md", "--sentinel", _SENTINEL, cwd=tmp_path, env=env)

        assert result.returncode == 0, result.stderr
        assert (sentinels / f"{_SENTINEL}-{_SESSION}").read_text(encoding="utf-8") == "docs/program.md\n"
