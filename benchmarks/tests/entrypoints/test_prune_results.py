"""Tests for the benchmark-results retention script.

``benchmarks/results/`` is gitignored, so everything the script deletes is unrecoverable. These tests pin the two
properties that matter for that: the default never deletes, and the retention boundary selects exactly what it claims
to.

The fixtures build a results tree with explicitly stamped mtimes rather than sleeping, so the boundary is asserted
against a known clock instead of wall time.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

_BENCH = Path(__file__).resolve().parent.parent.parent
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

import prune_results  # noqa: E402  (needs the sys.path insert above)

#: Bound before the autouse stub can replace the attribute, so the one test that must
#: exercise the real git lookup still reaches it.
_REAL_CITED_NAMES = prune_results.cited_names

_SECONDS_PER_DAY = 86400
#: Captured once at import so fixture stamps and boundary assertions share one clock,
#: while still sitting close enough to the real clock that ``main``'s own ``time.time()``
#: classifies the same entries. A hardcoded epoch drifts out of that agreement.
_NOW = time.time()


def _age(path: Path, days: float) -> None:
    """Stamp *path*'s mtime to *days* before the fixed reference clock."""
    when = _NOW - days * _SECONDS_PER_DAY
    os.utime(path, (when, when))


def _remaining(root: Path) -> set[str]:
    """Return the names still present in the results directory under *root*."""
    return {path.name for path in prune_results.results_dir(root).iterdir()}


def _cites_ancient(root: Path) -> set[str]:
    """Stand in for the git lookup with a tracked write-up that cites only the ``ancient`` run."""
    return {"ancient"}


def _citations_unavailable(root: Path) -> set[str]:
    """Stand in for a git lookup that cannot run, as when git is missing."""
    raise prune_results.CitationsUnavailableError("git missing")


@pytest.fixture(autouse=True)
def _no_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the fixture tree as citing nothing unless a test says otherwise.

    ``tmp_path`` is not a git repository, so the real lookup fails there and the script correctly refuses to delete.
    Tests exercising retention need that guard neutral; the tests that exercise the guard itself override this in their
    own body.
    """
    monkeypatch.setattr(prune_results, "cited_names", lambda root: set())


@pytest.fixture(name="results")
def _results(tmp_path: Path) -> Path:
    """Build a results tree holding runs at 5, 29, 31, and 400 days old."""
    directory = prune_results.results_dir(tmp_path)
    directory.mkdir(parents=True)
    for name, days in (("fresh", 5), ("just-inside", 29), ("just-outside", 31), ("ancient", 400)):
        run = directory / name
        run.mkdir()
        (run / "result.jsonl").write_text('{"ok": true}\n', encoding="utf-8")
        _age(run, days)
    return tmp_path


@pytest.fixture(name="linked_results")
def _linked_results(results: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Add a 400-day-old symlink to a 4 KiB file outside the results tree and return the link.

    Windows may not permit setting a symlink's timestamp, so only the link's ``lstat`` metadata is simulated. That still
    catches a regression from ``lstat()`` to the target-following ``stat()``.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"x" * 4096)
    link = prune_results.results_dir(results) / "linked"
    link.symlink_to(outside, target_is_directory=True)
    real_lstat = Path.lstat
    old_link_mtime = _NOW - 400 * _SECONDS_PER_DAY

    def _lstat_with_old_link_mtime(path: Path) -> os.stat_result:
        """Report the test link's old metadata without changing its target."""
        metadata = real_lstat(path)
        if path != link:
            return metadata
        fields = list(metadata)
        fields[8] = old_link_mtime
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", _lstat_with_old_link_mtime)
    return link


def test_age_stamps_regular_entry_when_follow_symlinks_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regular fixture entries can be aged when the optional symlink capability is absent.

    Some platforms reject ``os.utime(..., follow_symlinks=False)``. The fixture helper must still stamp ordinary
    directories there, otherwise every retention test would fail for a reason unrelated to the script.
    """
    entry = tmp_path / "entry"
    entry.mkdir()
    requested_days = 5
    real_utime = os.utime

    def _utime_without_no_follow(*args, **kwargs):
        """Reject only the unavailable symlink-specific timestamp option."""
        if kwargs.get("follow_symlinks") is False:
            raise NotImplementedError("follow_symlinks unavailable")
        return real_utime(*args, **kwargs)

    monkeypatch.setattr(os, "utime", _utime_without_no_follow)

    _age(entry, requested_days)

    assert entry.stat().st_mtime == pytest.approx(_NOW - requested_days * _SECONDS_PER_DAY, rel=0, abs=1e-6)


class TestStaleEntries:
    """``stale_entries`` selects the runs older than the retention window."""

    def test_selects_only_entries_past_the_window_oldest_first(self, results: Path) -> None:
        """The window boundary is exclusive on the keep side (29 days stays, 31 goes) and matches come oldest first.

        An off-by-one here silently deletes a run someone is still comparing against, so the boundary is asserted on
        both sides. Oldest-first order means a truncated listing shows the safest deletions.
        """
        stale = prune_results.stale_entries(prune_results.results_dir(results), days=30, now=_NOW)

        assert [path.name for path in stale] == ["ancient", "just-outside"]

    def test_missing_results_directory_yields_nothing(self, tmp_path: Path) -> None:
        """A repository that has never run a benchmark has no stale entries.

        The results directory is created lazily by the first benchmark run, so its absence is a normal state.
        """
        assert prune_results.stale_entries(prune_results.results_dir(tmp_path), days=30, now=_NOW) == []


class TestMain:
    """The command line never deletes without ``--apply`` and refuses unsafe windows."""

    def test_dry_run_is_the_default_and_deletes_nothing(self, results: Path, capsys: pytest.CaptureFixture) -> None:
        """Without ``--apply`` nothing is removed, because the data is not recoverable from git.

        A run with no flags reports what it would remove and leaves all four fixture runs on disk.
        """
        exit_code = prune_results.main(["--root", str(results)])

        assert exit_code == 0
        assert _remaining(results) == {"fresh", "just-inside", "just-outside", "ancient"}
        assert "would remove" in capsys.readouterr().out

    def test_apply_removes_only_the_selected_entries(self, results: Path) -> None:
        """``--apply`` deletes the aged-out runs and leaves the rest intact.

        Of the four fixture runs, only the 31- and 400-day-old ones lie past the 30-day default window.
        """
        prune_results.main(["--root", str(results), "--apply"])

        assert _remaining(results) == {"fresh", "just-inside"}

    def test_apply_is_idempotent(self, results: Path, capsys: pytest.CaptureFixture) -> None:
        """A second run finds nothing to do rather than failing or re-reporting.

        Cron-style repeated invocation must be safe: the second pass sees an already-pruned tree.
        """
        prune_results.main(["--root", str(results), "--apply"])
        capsys.readouterr()

        assert prune_results.main(["--root", str(results), "--apply"]) == 0
        assert "nothing older than" in capsys.readouterr().out

    def test_missing_results_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """A repository that has never run a benchmark exits successfully instead of failing.

        The command is run from hooks and scripts that cannot know whether any benchmark ever ran.
        """
        assert prune_results.main(["--root", str(tmp_path)]) == 0

    @pytest.mark.parametrize("days", ["0", "-1"])
    def test_rejects_a_zero_or_negative_window(self, results: Path, days: str) -> None:
        """``--days 0`` would delete everything including the run in progress; a non-positive window is refused.

        The refusal exits with status 2 before any deletion, so all four fixture runs survive even with ``--apply``.
        """
        assert prune_results.main(["--root", str(results), "--days", days, "--apply"]) == 2
        assert len(_remaining(results)) == 4


class TestSymlinkedEntry:
    """A symlink out of the results tree is handled as a link, never followed."""

    def test_size_is_measured_on_the_link_not_its_target(self, linked_results: Path) -> None:
        """Sizing a link must not count the bytes of the directory it points to.

        Following the link would inflate the reported total by the 4 KiB file stored outside the results tree.
        """
        assert prune_results.entry_size_bytes(linked_results) < 4096

    def test_apply_removes_the_link_and_keeps_its_target(self, results: Path, linked_results: Path) -> None:
        """Deleting an aged-out link leaves the directory it points to untouched.

        Following the link on delete would reach outside the directory the script is scoped to.
        """
        prune_results.main(["--root", str(results), "--apply"])

        assert not linked_results.exists()
        assert (results / "outside" / "big.bin").exists()


class TestCitationGuard:
    """Runs named by tracked documentation are kept unless the guard is overridden."""

    @mock.patch("prune_results.cited_names", new=_cites_ancient)
    def test_cited_run_is_kept_whatever_its_age(self, results: Path, capsys: pytest.CaptureFixture) -> None:
        """A run named by tracked documentation survives the prune.

        This is the regression for the incident that motivated the guard: an age-based prune removed runs that a
        tracked write-up cites as frozen evidence, and because the directory is gitignored nothing could restore them.
        """
        prune_results.main(["--root", str(results), "--apply"])

        remaining = _remaining(results)
        assert "ancient" in remaining
        assert "just-outside" not in remaining
        assert "keeping 1 cited by tracked docs" in capsys.readouterr().out

    @mock.patch("prune_results.cited_names", new=_cites_ancient)
    def test_ignore_citations_overrides_the_guard(self, results: Path) -> None:
        """The escape hatch still deletes a cited run, for the case where that is intended.

        ``--ignore-citations`` is the explicit opt-out, so the cited 400-day-old run goes with the other stale ones.
        """
        prune_results.main(["--root", str(results), "--apply", "--ignore-citations"])

        assert _remaining(results) == {"fresh", "just-inside"}

    @mock.patch("prune_results.cited_names", new=_citations_unavailable)
    def test_refuses_to_delete_when_citations_cannot_be_determined(
        self, results: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """An unreadable citation set fails closed rather than assuming nothing is cited.

        An empty set from a failed search is indistinguishable from a genuinely uncited tree, and acting on it would
        authorise deleting everything.
        """
        assert prune_results.main(["--root", str(results), "--apply"]) == 2
        assert len(_remaining(results)) == 4
        assert "refusing to delete" in capsys.readouterr().err

    def test_real_lookup_reads_tracked_files_of_this_repository(self) -> None:
        """The real lookup finds run names cited by this repository's own tracked docs.

        Pinning the mechanism, not a specific run: the mocked tests above would pass even if the git invocation were
        wrong, so this one stays on the real ``git`` call.
        """
        names = _REAL_CITED_NAMES(Path(__file__).resolve().parent.parent.parent.parent)

        assert any(name.startswith(("claude-", "codex-")) for name in names)
