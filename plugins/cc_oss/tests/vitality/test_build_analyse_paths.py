"""Tests for ``bin/build_analyse_paths.py``.

Path construction and slug sanitisation are pure; the repository lookup mocks ``subprocess.run`` so no real ``gh``
invocation occurs. Sentinel assertions read back the files the script writes under an isolated ``TMPDIR``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import build_analyse_paths as bap
import pytest
from _text_io import read_shared_sentinel

_TODAY = "2026-09-11"
_THREAD_REPORT = ".reports/analyse/thread/output-analyse-thread-owner-repo-42-2026-09-11.md"
_THREAD_MODE = Path(__file__).resolve().parents[2] / "skills/analyse/modes/thread.md"


def _gh_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Build the result ``gh repo view`` would return."""
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout)


def _bash_blocks(markdown: str) -> list[str]:
    """Return the body of every fenced ``bash`` block, found line by line so prose wrapping cannot matter."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in markdown.splitlines():
        if current is None and line.strip() == "```bash":
            current = []
        elif current is not None and line.strip().startswith("```"):
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    return ["\n".join(block) for block in blocks]


@pytest.fixture
def tmp_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in an isolated directory that is also ``TMPDIR``, so sentinels, reports and cache land inside it."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def gh() -> Iterator[mock.Mock]:
    """Make every subprocess call answer as ``gh repo view`` for ``owner/repo``; tests may reset ``return_value``."""
    with mock.patch("build_analyse_paths.subprocess.run", autospec=True, return_value=_gh_result("owner/repo")) as run:
        yield run


class TestSlugs:
    """Slug and argument sanitisation — report and cache modes deliberately differ."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("owner/repo", "owner-repo", id="plain"),
            pytest.param("Owner.AI/repo_x", "OwnerAI-repox", id="strips-dots-and-underscores"),
            pytest.param("", "local", id="unresolved-falls-back"),
        ],
    )
    def test_report_slug(self, raw: str, expected: str) -> None:
        """Report slugs keep only alphanumerics and dashes, falling back to ``local``.

        The slug becomes part of a file name, so dots and underscores are dropped and an unresolved repository still
        yields a usable name.
        """
        assert bap.report_slug(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("owner/repo", "owner-repo", id="plain"),
            pytest.param("Owner.AI/repo_x", "Owner.AI-repo_x", id="keeps-dots-and-underscores"),
            pytest.param("", "", id="unresolved-stays-empty"),
        ],
    )
    def test_cache_slug_keeps_more_than_report_slug(self, raw: str, expected: str) -> None:
        """Cache keys only replace slashes — the stricter report sanitisation does not apply here.

        Keeping dots and underscores keeps distinct repositories distinct; an unresolved one stays empty so caching can
        be disabled.
        """
        assert bap.cache_slug(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("42", "42", id="plain-number"),
            pytest.param("../../etc", "etc", id="strips-traversal"),
            pytest.param("a/b", "ab", id="strips-slash"),
            pytest.param("vitality", "vitality", id="keyword-unchanged"),
        ],
    )
    def test_sanitize_clean_args(self, raw: str, expected: str) -> None:
        """Only alphanumerics and dashes survive sanitisation, mirroring ``report_slug``'s allowlist.

        Real arguments (a number or the ``vitality`` keyword) pass through unchanged; path separators and dots do not.
        """
        assert bap.sanitize_clean_args(raw) == expected


class TestPaths:
    """Report and cache path construction."""

    @pytest.mark.parametrize(
        ("clean_args", "expected"),
        [
            pytest.param("42", _THREAD_REPORT, id="embeds-subdir-slug-argument-and-date"),
            pytest.param(
                "vitality",
                ".reports/analyse/thread/output-analyse-thread-owner-repo-vitality-2026-09-11.md",
                id="keyword-argument-unchanged",
            ),
            pytest.param(
                "../../etc",
                ".reports/analyse/thread/output-analyse-thread-owner-repo-etc-2026-09-11.md",
                id="traversal-shaped-clean-args-cannot-escape-subdir",
            ),
        ],
    )
    def test_build_report_path(self, clean_args: str, expected: str) -> None:
        """The report path embeds subdir, slug, argument and date; a traversal-shaped ``clean_args`` cannot escape it.

        The numeric and keyword arguments are the shapes ``oss:analyse`` actually passes (a PR or issue id, or the bare
        ``vitality``/``ecosystem`` keyword); both are already pure ``[A-Za-z0-9-]`` and must come out unchanged.
        """
        assert bap.build_report_path("thread", "owner-repo", clean_args, _TODAY) == expected

    def test_build_cache_path_disabled_without_slug(self) -> None:
        """An unresolvable repository disables caching rather than risking a cross-repo collision.

        With no slug two different repositories would share one cache key, so the path is empty instead.
        """
        assert bap.build_cache_path("", "42", _TODAY) == ""

    def test_build_cache_path_sanitizes_clean_args(self) -> None:
        """A traversal-shaped ``clean_args`` cannot escape the cache subdirectory.

        ``../../etc`` is reduced to ``etc`` before it joins the file name, so the cache file stays under ``.cache/gh``.
        """
        assert bap.build_cache_path("owner-repo", "../../etc", _TODAY) == ".cache/gh/owner-repo-etc-2026-09-11.json"


@pytest.mark.usefixtures("gh")
class TestReportMode:
    """The default mode: decide whether an existing report arms the reply fast path."""

    def test_without_existing_report(self, tmp_sentinels: Path) -> None:
        """A missing report leaves the fast path off and the mtime at zero.

        With nothing on disk for the resolved repository and argument, every fast-path sentinel records "no report".
        """
        assert bap.main(["--clean-args", "42", "--today", _TODAY]) == 0
        assert read_shared_sentinel(tmp_sentinels, "analyse-fast-path-tentative") == "false"
        assert read_shared_sentinel(tmp_sentinels, "analyse-report-mtime") == "0"
        assert read_shared_sentinel(tmp_sentinels, "analyse-drift") == "false"
        assert read_shared_sentinel(tmp_sentinels, "analyse-fast-path") == "false"

    def test_with_existing_report(self, tmp_sentinels: Path) -> None:
        """An existing report arms the tentative fast path and records its mtime.

        The report is created at the exact path the script derives for ``owner/repo`` and argument 42.
        """
        report = tmp_sentinels / bap.build_report_path("thread", "owner-repo", "42", _TODAY)
        report.parent.mkdir(parents=True)
        report.write_text("x", encoding="utf-8")
        assert bap.main(["--clean-args", "42", "--today", _TODAY]) == 0
        assert read_shared_sentinel(tmp_sentinels, "analyse-fast-path-tentative") == "true"
        assert int(read_shared_sentinel(tmp_sentinels, "analyse-report-mtime")) > 0

    def test_sentinel_has_trailing_newline(self, tmp_sentinels: Path) -> None:
        """Sentinels end in a newline — ``IFS= read -r`` fails on a file without one.

        The skill reads these back with ``read -r``, so a missing final newline would make the read report failure.
        """
        bap.main(["--clean-args", "42", "--today", _TODAY])
        assert (tmp_sentinels / "analyse-drift-shared").read_text(encoding="utf-8") == "false\n"

    def test_pins_report_file_only_when_report_exists(self, tmp_sentinels: Path) -> None:
        """A tentative fast-path hit pins the report sentinel; a missing report must not forge one.

        A confirmed hit jumps to Step 7 without thread mode, so nothing else would name the report to the Stop hook. A
        path to a report that was never produced is the state that once made the hook deny an unrelated question.
        """
        assert bap.main(["--clean-args", "42", "--today", _TODAY]) == 0
        assert not (tmp_sentinels / "analyse-report-file-shared").exists()

        report = tmp_sentinels / bap.build_report_path("thread", "owner-repo", "42", _TODAY)
        report.parent.mkdir(parents=True)
        report.write_text("report", encoding="utf-8")
        assert bap.main(["--clean-args", "42", "--today", _TODAY]) == 0

        assert (
            read_shared_sentinel(tmp_sentinels, "analyse-report-file") == report.relative_to(tmp_sentinels).as_posix()
        )


class TestCacheMode:
    """``--mode cache``: record the cache key path and create the cache directory."""

    def test_creates_dir_and_sentinel(self, tmp_sentinels: Path, gh: mock.Mock) -> None:
        """Cache mode records the key path and creates the cache directory.

        The key embeds the resolved ``owner-repo`` slug, the argument and the pinned date.
        """
        assert bap.main(["--mode", "cache", "--clean-args", "42", "--today", _TODAY]) == 0
        assert read_shared_sentinel(tmp_sentinels, "analyse-cache-file") == ".cache/gh/owner-repo-42-2026-09-11.json"
        assert (tmp_sentinels / ".cache/gh").is_dir()

    def test_stops_thread_without_repo_context(
        self, tmp_sentinels: Path, gh: mock.Mock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A numeric argument outside a GitHub repo prints the stop notice — after the sentinel and mkdir.

        ``gh repo view`` fails here, so the repository is unresolved: the cache sentinel is empty, the directory still
        exists, and the user is told thread mode needs a repository.
        """
        gh.return_value = _gh_result("", returncode=1)
        assert bap.main(["--mode", "cache", "--clean-args", "42", "--today", _TODAY]) == 0
        assert read_shared_sentinel(tmp_sentinels, "analyse-cache-file") == ""
        assert (tmp_sentinels / ".cache/gh").is_dir()
        assert "No GitHub repository context" in capsys.readouterr().out

    def test_non_numeric_without_repo_is_quiet(self, gh: mock.Mock, capsys: pytest.CaptureFixture[str]) -> None:
        """The thread-mode guard only fires for numeric arguments.

        A keyword such as ``ecosystem`` needs no repository, so an unresolved one must not print the stop notice.
        """
        gh.return_value = _gh_result("", returncode=1)
        assert bap.main(["--mode", "cache", "--clean-args", "ecosystem", "--today", _TODAY]) == 0
        assert "No GitHub repository context" not in capsys.readouterr().out


@pytest.mark.usefixtures("gh")
class TestReportFileMode:
    """``--mode report-file``: the path thread mode writes is the path report mode probes."""

    def test_pins_path_and_sentinel(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Report-file mode creates the report directory, pins the hook sentinel and prints the path.

        Thread mode runs this before it writes the report, so the Step 6a hook has a path to gate on.
        """
        assert bap.main(["--mode", "report-file", "--clean-args", "42", "--today", _TODAY]) == 0
        assert read_shared_sentinel(tmp_sentinels, "analyse-report-file") == _THREAD_REPORT
        assert (tmp_sentinels / ".reports/analyse/thread").is_dir()
        assert f"[analyse] report → {_THREAD_REPORT}" in capsys.readouterr().out

    def test_leaves_fast_path_sentinels_alone(self, tmp_sentinels: Path) -> None:
        """Report-file mode must not reset the drift state Step 2 and Step 4 already decided.

        Thread mode runs after those steps; re-running report mode there would rewrite ``analyse-drift`` and the fast-
        path flags mid-run.
        """
        assert bap.main(["--mode", "report-file", "--clean-args", "42", "--today", _TODAY]) == 0
        assert sorted(path.name for path in tmp_sentinels.glob("analyse-*")) == ["analyse-report-file-shared"]

    def test_written_report_found_by_fast_path(self, tmp_sentinels: Path) -> None:
        """A report written at the path thread mode was given arms the reply fast path on the next run.

        The two paths once diverged: thread mode wrote ``output-analyse-thread-<N>-<date>.md`` while the fast path
        probed ``output-analyse-thread-<repo-slug>-<N>-<date>.md``, so a thread report this skill wrote was never
        reused.
        """
        assert bap.main(["--mode", "report-file", "--clean-args", "42", "--today", _TODAY]) == 0
        (tmp_sentinels / read_shared_sentinel(tmp_sentinels, "analyse-report-file")).write_text(
            "report", encoding="utf-8"
        )

        assert bap.main(["--clean-args", "42", "--today", _TODAY]) == 0

        assert read_shared_sentinel(tmp_sentinels, "analyse-fast-path-tentative") == "true"

    def test_thread_mode_takes_report_path_from_builder(self) -> None:
        """The thread-mode report block calls the builder instead of spelling the report path out itself.

        Reads the shipped ``thread.md`` line by line (no regex over wrapped prose) and checks the one bash block that
        sets ``analyse-today``.
        """
        block = next(
            block for block in _bash_blocks(_THREAD_MODE.read_text(encoding="utf-8")) if "analyse-today" in block
        )

        assert 'build_analyse_paths.py" --mode report-file' in block
        assert "REPORT_FILE=" not in block


@pytest.mark.usefixtures("tmp_sentinels")
class TestDryRun:
    """Covers --dry-run: compute and print, write no session state.

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
        self, tmp_sentinels: Path, gh: mock.Mock, extra_args: list[str], expect_written: bool
    ) -> None:
        """--dry-run leaves the sentinel directory empty and exits 0.

        Without the flag the real skill path is unchanged and writes its sentinels.
        """
        assert bap.main(["--clean-args", "42", "--today", _TODAY, *extra_args]) == 0
        assert (list(tmp_sentinels.glob("*")) != []) is expect_written

    def test_announces_what_it_would_write(self, gh: mock.Mock, capsys: pytest.CaptureFixture[str]) -> None:
        """Each suppressed write is still reported, so a verifier sees the computed value.

        A dry run that printed nothing would hide the sentinel values it computed.
        """
        assert bap.main(["--clean-args", "42", "--today", _TODAY, "--dry-run"]) == 0
        assert "[dry-run] would write " in capsys.readouterr().out
