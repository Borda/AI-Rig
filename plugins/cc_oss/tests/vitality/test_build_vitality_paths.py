"""Tests for ``bin/build_vitality_paths.py``.

Path construction, manifest reading and the agents-list assembly are pure; the git and bridge lookups mock
``subprocess.run`` so no real subprocess runs. One real-subprocess test pins the ``_run`` wrapper itself.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

import build_vitality_paths as bvp
import pytest


def _answer(git_out: str, bridge_out: str, returncode: int = 0) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a ``subprocess.run`` stand-in answering the git lookup with ``git_out`` and the bridge probe otherwise."""

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Answer one command from the canned outputs."""
        return subprocess.CompletedProcess(cmd, returncode, stdout=git_out if cmd[0] == "git" else bridge_out)

    return fake_run


@pytest.fixture
def tmp_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point sentinel writes at an isolated temp directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def run() -> Iterator[mock.Mock]:
    """Mock ``subprocess.run`` to report commit ``abc1234`` and an available bridge; tests may reset ``side_effect``."""
    with mock.patch(
        "build_vitality_paths.subprocess.run", autospec=True, side_effect=_answer("abc1234", "available")
    ) as m:
        yield m


def _install_cached_version(home: Path, version: str) -> None:
    """Create an installed-plugin cache entry for ``version`` under ``home``."""
    target = home / ".claude/plugins/cache/borda-ai-rig/oss" / version / ".claude-plugin"
    target.mkdir(parents=True)
    (target / "plugin.json").write_text(json.dumps({"version": version}), encoding="utf-8")


class TestBuildReportPath:
    """The vitality report path."""

    def test_embeds_owner_repo_and_timestamp(self) -> None:
        """The vitality report path embeds owner, repo and the UTC timestamp.

        The path is what the Stop hook later checks for, so its shape is part of the skill contract.
        """
        got = bvp.build_report_path("owner", "repo", "2026-09-11T10-00-00Z")
        assert got == ".reports/analyse/vitality/output-analyse-vitality-owner-repo-2026-09-11T10-00-00Z.md"


class TestReadVersion:
    """Reading the plugin version out of a manifest."""

    def test_returns_version_string(self, tmp_path: Path) -> None:
        """A well-formed manifest yields its version string.

        The version is stamped into the report header so a reader knows which skill release produced it.
        """
        manifest = tmp_path / "plugin.json"
        manifest.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
        assert bvp.read_version(manifest) == "1.2.3"

    @pytest.mark.parametrize("payload", ["not json", "{}", "[]"])
    def test_falls_back_to_unknown(self, tmp_path: Path, payload: str) -> None:
        """Malformed or non-object manifests report ``unknown`` rather than failing.

        A damaged manifest must never abort report generation; ``{}`` is valid JSON with no version, ``[]`` is not an
        object.
        """
        manifest = tmp_path / "plugin.json"
        manifest.write_text(payload, encoding="utf-8")
        assert bvp.read_version(manifest) == "unknown"

    def test_missing_file_reports_unknown(self, tmp_path: Path) -> None:
        """An absent manifest reports ``unknown``.

        A source checkout run before any install has no manifest at the resolved path.
        """
        assert bvp.read_version(tmp_path / "nope.json") == "unknown"


class TestResolveVersionFile:
    """Locating the manifest: newest cache entry, else the source tree."""

    def test_prefers_lexically_last_cache_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The lexically last cache entry wins, matching the ``ls … | sort | tail -1`` pipeline it replaces.

        The choice is lexical, not version-aware: ``0.9.0`` sorts after ``0.10.0``. Pinned so a "smarter" sort is a
        deliberate change.
        """
        _install_cached_version(tmp_path, "0.9.0")
        _install_cached_version(tmp_path, "0.10.0")
        monkeypatch.setattr(bvp.Path, "home", staticmethod(lambda: tmp_path))
        assert bvp.resolve_version_file().parts[-3] == "0.9.0"

    def test_falls_back_to_source_tree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no installed cache, the source-tree manifest is used.

        An empty home directory stands for a user who runs the skill straight from a repository checkout.
        """
        monkeypatch.setattr(bvp.Path, "home", staticmethod(lambda: tmp_path))
        assert bvp.resolve_version_file() == Path("plugins/cc_oss/.claude-plugin/plugin.json")


class TestAgentsYaml:
    """The agents list reflects which passes actually ran."""

    @pytest.mark.parametrize(
        ("quick", "codex", "expected_lines"),
        [
            pytest.param(True, True, 1, id="quick-drops-review-agents"),
            pytest.param(False, False, 2, id="full-without-bridge"),
            pytest.param(False, True, 3, id="full-with-bridge"),
        ],
    )
    def test_line_count_follows_passes_run(self, quick: bool, codex: bool, expected_lines: int) -> None:
        """One line per agent that ran: quick mode lists one, a full run two, and the bridge adds a third.

        The list ends up in the report header, so it must not claim review passes that were skipped.
        """
        assert len(bvp.agents_yaml(quick, codex).splitlines()) == expected_lines

    @pytest.mark.parametrize(
        ("codex", "listed"),
        [pytest.param(True, True, id="bridge-available"), pytest.param(False, False, id="bridge-absent")],
    )
    def test_names_bridge_only_when_available(self, codex: bool, listed: bool) -> None:
        """``bridge:review`` is listed only when the Codex bridge answered ``available``.

        Listing it for a run that never used the bridge would misattribute the review.
        """
        assert ("bridge:review" in bvp.agents_yaml(False, codex)) is listed


class TestRunHelper:
    """``_run`` against a real subprocess — the one place the wrapper meets an actual process."""

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            pytest.param("print('  ok  ')", "ok", id="stdout-stripped"),
            pytest.param("raise SystemExit(1)", "fallback", id="nonzero-exit-falls-back"),
            pytest.param("pass", "fallback", id="empty-output-falls-back"),
        ],
    )
    def test_returns_stripped_stdout_or_fallback(self, code: str, expected: str) -> None:
        """A successful command yields its stripped stdout; failure or silence yields the fallback.

        Uses the running interpreter as the command so the test needs no external tool.
        """
        assert bvp._run([sys.executable, "-c", code], 10, "fallback") == expected


class TestMain:
    """The CLI: report sentinel, metadata lines, and degradation when a lookup fails."""

    def test_writes_report_sentinel_and_emits_metadata(
        self, tmp_sentinels: Path, run: mock.Mock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The report path is persisted before the report exists, and provenance is printed for the header.

        The bridge answers ``available`` and git reports a commit, so both appear in the printed metadata.
        """
        assert bvp.main(["--owner", "owner", "--repo", "repo", "--quick", "false"]) == 0
        sentinel = (tmp_sentinels / "analyse-report-file-shared").read_text(encoding="utf-8")
        assert sentinel.startswith(".reports/analyse/vitality/output-analyse-vitality-owner-repo-")
        assert sentinel.endswith(".md\n")
        out = capsys.readouterr().out
        assert "REPORT_COMMIT=abc1234" in out
        assert "CODEX_AVAILABLE=1" in out
        assert "bridge:review" in out

    def test_marks_codex_absent(self, tmp_sentinels: Path, run: mock.Mock, capsys: pytest.CaptureFixture[str]) -> None:
        """A bridge that is not available is reported as ``CODEX_AVAILABLE=0``.

        ``--quick true`` is used as well, so the quick-mode explanation line is also expected in the output.
        """
        run.side_effect = _answer("abc1234", "absent")
        assert bvp.main(["--owner", "owner", "--repo", "repo", "--quick", "true"]) == 0
        out = capsys.readouterr().out
        assert "CODEX_AVAILABLE=0" in out
        assert "--quick: core scoring only" in out

    def test_reports_unknown_commit_when_git_fails(
        self, tmp_sentinels: Path, run: mock.Mock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failing git call degrades to ``unknown`` instead of aborting the report.

        Running outside a repository, or with git missing, must still produce a report header.
        """
        run.side_effect = _answer("", "", returncode=1)
        assert bvp.main(["--owner", "owner", "--repo", "repo"]) == 0
        assert "REPORT_COMMIT=unknown" in capsys.readouterr().out


@pytest.mark.usefixtures("run")
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
        self, tmp_sentinels: Path, extra_args: list[str], expect_written: bool
    ) -> None:
        """--dry-run leaves the sentinel directory empty and exits 0.

        Without the flag the real skill path is unchanged and writes its sentinels.
        """
        assert bvp.main(["--owner", "owner", "--repo", "repo", "--quick", "false", *extra_args]) == 0
        assert (list(tmp_sentinels.glob("*")) != []) is expect_written

    def test_announces_what_it_would_write(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Each suppressed write is still reported, so a verifier sees the computed value.

        A dry run that printed nothing would hide the report path it computed.
        """
        assert bvp.main(["--owner", "owner", "--repo", "repo", "--quick", "false", "--dry-run"]) == 0
        assert "[dry-run] would write " in capsys.readouterr().out
