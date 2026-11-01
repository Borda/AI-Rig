"""Tests for ``bin/parse_analyse_args.py``.

Pure classification and slug parsing run without subprocesses; the repository-resolution paths patch ``subprocess.run``
so no real ``gh`` or ``git`` invocation occurs. Sentinel assertions read back the files the script writes under an
isolated ``TMPDIR`` with a pinned session token.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import parse_analyse_args as paa
import pytest
from _process import done
from _text_io import read_shared_sentinel


def _by_executable(
    responses: dict[str, subprocess.CompletedProcess[str]],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a ``subprocess.run`` stub answering by executable name; unknown executables fail with exit 1."""

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return the canned result for ``cmd[0]``."""
        return responses.get(cmd[0], done(returncode=1))

    return fake_run


class TestClassifyReportPath:
    """classify_report_path: decide whether the raw argument is a direct report path."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(".reports/analyse/thread/out.md", (True, ".reports/analyse/thread/out.md"), id="report-path"),
            pytest.param("42", (False, ""), id="thread-number"),
            pytest.param("vitality owner/repo", (False, ""), id="vitality-keyword"),
            pytest.param("ecosystem", (False, ""), id="ecosystem-keyword"),
            pytest.param("vitality-notes.md", (False, ""), id="vitality-prefixed-md-not-a-report"),
        ],
    )
    def test_detects_direct_report_path(self, raw: str, expected: tuple[bool, str]) -> None:
        """Only a plain .md path that is not a mode keyword counts as a direct report path."""
        assert paa.classify_report_path(raw) == expected

    @pytest.mark.parametrize("raw", [".plans/todo_thing.md", "notes/todo_release.md"])
    def test_rejects_plan_files(self, raw: str) -> None:
        """Plan and todo files abort with exit code 1 rather than being treated as reports.

        Analysing a plan as if it were a finished report would feed unfinished work into the analyse pipeline.
        """
        with pytest.raises(paa._Abort) as excinfo:
            paa.classify_report_path(raw)
        assert (excinfo.value.code, "not valid report paths" in excinfo.value.lines[0]) == (1, True)


class TestClassifyMode:
    """Main --mode classify: persist direct-path state and pin the analysis date."""

    def test_writes_sentinels(self, tmp_sentinels: Path) -> None:
        """Classify mode persists direct-path state and pins the analysis date."""
        rc = paa.main(["--mode", "classify", "--args", ".reports/analyse/thread/out.md"])
        assert rc == 0
        assert read_shared_sentinel(tmp_sentinels, "analyse-direct-path-mode") == "true"
        assert read_shared_sentinel(tmp_sentinels, "analyse-report-file") == ".reports/analyse/thread/out.md"
        assert len(read_shared_sentinel(tmp_sentinels, "analyse-today")) == len("2026-01-01")

    def test_keeps_existing_today(self, tmp_sentinels: Path) -> None:
        """An already-pinned date survives a second run, so paths cannot straddle midnight."""
        (tmp_sentinels / "analyse-today-shared").write_text("2020-02-02\n", encoding="utf-8")
        rc = paa.main(["--mode", "classify", "--args", "42"])
        assert (rc, read_shared_sentinel(tmp_sentinels, "analyse-today")) == (0, "2020-02-02")

    def test_invalid_path_skips_sentinel_writes(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A plan file exits 1 before any sentinel is written, matching the original block's early exit."""
        rc = paa.main(["--mode", "classify", "--args", ".plans/active/todo_x.md"])
        assert rc == 1
        assert not (tmp_sentinels / "analyse-direct-path-mode-shared").exists()
        assert "Invalid report path" in capsys.readouterr().out

    def test_sentinel_ends_with_newline(self, tmp_sentinels: Path) -> None:
        """Sentinels end in a newline — ``IFS= read -r`` fails on a file without one."""
        paa.main(["--mode", "classify", "--args", "42"])
        assert (tmp_sentinels / "analyse-direct-path-mode-shared").read_text(encoding="utf-8") == "false\n"


class TestRepoFromArgument:
    """repo_from_argument: turn an explicit vitality argument into ``owner/repo``."""

    @pytest.mark.parametrize(
        ("extra", "expected"),
        [
            pytest.param("owner/repo", "owner/repo", id="bare-slug"),
            pytest.param("https://github.com/owner/repo", "owner/repo", id="url"),
            pytest.param("https://github.com/owner/repo/tree/main", "owner/repo", id="url-with-extra-segments"),
        ],
    )
    def test_resolves_accepted_shapes(self, extra: str, expected: str) -> None:
        """An explicit argument resolves to owner/repo for both accepted shapes."""
        assert paa.repo_from_argument(extra) == expected

    @pytest.mark.parametrize(
        ("argument", "message"),
        [
            pytest.param("https://gitlab.com/owner/repo", "Not a GitHub URL", id="non-github-url"),
            pytest.param("not a repo", "Unrecognised vitality argument", id="garbage-argument-shape"),
        ],
    )
    def test_rejects_unusable_argument(self, argument: str, message: str) -> None:
        """A non-GitHub URL or an unrecognised argument shape stops the run with a soft exit code and a hint.

        Exit code 0 keeps the skill from reporting a failure for what is only a usage problem.
        """
        with pytest.raises(paa._Abort) as excinfo:
            paa.repo_from_argument(argument)
        assert (excinfo.value.code, message in excinfo.value.lines[0]) == (0, True)


class TestRepoFromContext:
    """repo_from_context: infer the repository from the working directory when no argument names one."""

    @mock.patch("parse_analyse_args.subprocess.run", new=_by_executable({"gh": done(stdout="owner/repo\n")}))
    def test_prefers_gh(self) -> None:
        """``gh repo view`` answers first when it is available."""
        assert paa.repo_from_context(5) == "owner/repo"

    @pytest.mark.parametrize(
        ("remote", "expected"),
        [
            pytest.param("git@github.com:owner/repo.git", "owner/repo", id="ssh"),
            pytest.param("https://github.com/owner/repo.git", "owner/repo", id="https"),
        ],
    )
    @mock.patch("parse_analyse_args.subprocess.run", autospec=True)
    def test_falls_back_to_origin_remote(self, run: mock.MagicMock, remote: str, expected: str) -> None:
        """With gh unavailable, the origin remote URL is parsed instead."""
        run.side_effect = _by_executable({"gh": done(returncode=1), "git": done(stdout=remote)})
        assert paa.repo_from_context(5) == expected

    @pytest.mark.parametrize(
        ("git_result", "message"),
        [
            pytest.param(
                done(stdout="https://gitlab.com/owner/repo.git"),
                "not a GitHub repository",
                id="non-github-origin-remote",
            ),
            pytest.param(done(returncode=1), "No GitHub repository detected", id="no-remote-at-all"),
        ],
    )
    @mock.patch("parse_analyse_args.subprocess.run", autospec=True)
    def test_stops_without_a_github_remote(
        self, run: mock.MagicMock, git_result: subprocess.CompletedProcess[str], message: str
    ) -> None:
        """A non-GitHub origin stops the run rather than guessing a slug; no remote at all asks for an explicit URL."""
        run.side_effect = _by_executable({"gh": done(returncode=1), "git": git_result})
        with pytest.raises(paa._Abort) as excinfo:
            paa.repo_from_context(5)
        assert message in excinfo.value.lines[0]


class TestVitalityMode:
    """Main (default mode): normalise the vitality argument and publish owner/repo sentinels."""

    def test_normalises_args(self, tmp_sentinels: Path) -> None:
        """A vitality invocation records owner/repo and collapses the argument to the bare keyword."""
        assert paa.main(["--args", "vitality owner/repo"]) == 0
        assert read_shared_sentinel(tmp_sentinels, "analyse-clean-args") == "vitality"
        assert read_shared_sentinel(tmp_sentinels, "analyse-gh-owner") == "owner"
        assert read_shared_sentinel(tmp_sentinels, "analyse-gh-repo") == "repo"

    def test_passes_thread_number_through(self, tmp_sentinels: Path) -> None:
        """A non-vitality argument is written back unchanged with an empty owner/repo pair."""
        assert paa.main(["--args", "42"]) == 0
        assert read_shared_sentinel(tmp_sentinels, "analyse-clean-args") == "42"
        assert read_shared_sentinel(tmp_sentinels, "analyse-gh-owner") == ""

    def test_soft_stop_skips_sentinel_writes(self, tmp_sentinels: Path) -> None:
        """An unrecognised vitality argument exits before rewriting any sentinel."""
        assert paa.main(["--args", "vitality not a repo"]) == 0
        assert not (tmp_sentinels / "analyse-clean-args-shared").exists()


class TestCutField:
    """cut_field: mirror ``cut -d/ -f<n>`` for the owner/repo split."""

    @pytest.mark.parametrize("field", [1, 2])
    def test_echoes_delimiter_free_value(self, field: int) -> None:
        """A delimiter-free value is echoed for every field, exactly as ``cut`` without ``-s`` does."""
        assert paa.cut_field("owner", field) == "owner"


class TestDryRun:
    """--dry-run: compute and print, write no session state.

    These sentinels are live skill state keyed by ``CSID``, read by later steps and by PreToolUse hooks. An inspection
    run of this script must not forge them — one such run wrote an ``analyse-report-file`` naming a report that was
    never produced, and the hook denial that followed blocked an unrelated question.
    """

    @pytest.mark.parametrize(
        ("extra_args", "expect_written"),
        [
            pytest.param(["--dry-run"], False, id="dry-run-writes-no-sentinels"),
            pytest.param([], True, id="default-still-writes"),
        ],
    )
    def test_sentinels_written_only_without_dry_run(
        self, tmp_sentinels: Path, extra_args: list[str], expect_written: bool
    ) -> None:
        """--dry-run leaves the sentinel directory empty and exits 0.

        Without the flag the real skill path is unchanged and writes its sentinels.
        """
        assert paa.main(["--mode", "classify", "--args", "42", *extra_args]) == 0
        assert (list(tmp_sentinels.glob("*")) != []) is expect_written

    def test_announces_what_it_would_write(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Each suppressed write is still reported, so a verifier sees the computed value."""
        assert paa.main(["--mode", "classify", "--args", "42", "--dry-run"]) == 0
        assert "[dry-run] would write " in capsys.readouterr().out
