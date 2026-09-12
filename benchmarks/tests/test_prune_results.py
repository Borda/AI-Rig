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

import pytest

_BENCH = Path(__file__).resolve().parent.parent
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
    # follow_symlinks=False: the default stamps a symlink's target, leaving the link itself
    # fresh — and the link's own mtime is what the retention scan reads.
    os.utime(path, (when, when), follow_symlinks=False)


@pytest.fixture(autouse=True)
def _no_citations(monkeypatch) -> None:
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


def test_selects_only_entries_past_the_retention_window(results: Path) -> None:
    """The window boundary is exclusive on the keep side: 29 days stays, 31 days goes.

    An off-by-one here silently deletes a run someone is still comparing against, so the boundary is asserted on both
    sides rather than only on the delete side.
    """
    stale = prune_results.stale_entries(prune_results.results_dir(results), days=30, now=_NOW)

    assert [path.name for path in stale] == ["ancient", "just-outside"]


def test_orders_matches_oldest_first(results: Path) -> None:
    """Matches come back oldest first, so a truncated listing shows the safest deletions."""
    stale = prune_results.stale_entries(prune_results.results_dir(results), days=30, now=_NOW)

    assert [path.name for path in stale] == sorted(
        [path.name for path in stale], key=lambda name: {"ancient": 0, "just-outside": 1}[name]
    )


def test_dry_run_is_the_default_and_deletes_nothing(results: Path, capsys) -> None:
    """Without ``--apply`` nothing is removed — the data is not recoverable from git."""
    exit_code = prune_results.main(["--root", str(results)])

    assert exit_code == 0
    assert {path.name for path in prune_results.results_dir(results).iterdir()} == {
        "fresh",
        "just-inside",
        "just-outside",
        "ancient",
    }
    assert "would remove" in capsys.readouterr().out


def test_apply_removes_only_the_selected_entries(results: Path) -> None:
    """``--apply`` deletes the aged-out runs and leaves the rest intact."""
    prune_results.main(["--root", str(results), "--apply"])

    assert {path.name for path in prune_results.results_dir(results).iterdir()} == {"fresh", "just-inside"}


def test_apply_is_idempotent(results: Path, capsys) -> None:
    """A second run finds nothing to do rather than failing or re-reporting."""
    prune_results.main(["--root", str(results), "--apply"])
    capsys.readouterr()

    assert prune_results.main(["--root", str(results), "--apply"]) == 0
    assert "nothing older than" in capsys.readouterr().out


def test_missing_results_directory_is_not_an_error(tmp_path: Path) -> None:
    """A repository that has never run a benchmark is a normal state, not a failure."""
    assert prune_results.stale_entries(prune_results.results_dir(tmp_path), days=30, now=_NOW) == []
    assert prune_results.main(["--root", str(tmp_path)]) == 0


def test_rejects_a_zero_or_negative_window(results: Path) -> None:
    """``--days 0`` would delete everything including the run in progress; refuse it."""
    assert prune_results.main(["--root", str(results), "--days", "0", "--apply"]) == 2
    assert len(list(prune_results.results_dir(results).iterdir())) == 4


def test_symlinked_entry_is_measured_as_a_link_not_its_target(results: Path, tmp_path: Path) -> None:
    """A symlink out of the results tree is never followed when sizing or deleting.

    Following one would both inflate the reported total and, on delete, reach outside the directory the script is scoped
    to.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"x" * 4096)
    link = prune_results.results_dir(results) / "linked"
    link.symlink_to(outside, target_is_directory=True)
    _age(link, 400)

    assert prune_results.entry_size_bytes(link) < 4096

    prune_results.main(["--root", str(results), "--apply"])

    assert not link.exists()
    assert (outside / "big.bin").exists()


def test_a_cited_run_is_kept_whatever_its_age(results: Path, monkeypatch, capsys) -> None:
    """A run named by tracked documentation survives the prune.

    This is the regression for the incident that motivated the guard: an age-based prune
    removed runs that a tracked write-up cites as frozen evidence, and because the
    directory is gitignored nothing could restore them.
    """
    monkeypatch.setattr(prune_results, "cited_names", lambda root: {"ancient"})

    prune_results.main(["--root", str(results), "--apply"])

    remaining = {path.name for path in prune_results.results_dir(results).iterdir()}
    assert "ancient" in remaining
    assert "just-outside" not in remaining
    assert "keeping 1 cited by tracked docs" in capsys.readouterr().out


def test_ignore_citations_overrides_the_guard(results: Path, monkeypatch) -> None:
    """The escape hatch still deletes a cited run, for the case where that is intended."""
    monkeypatch.setattr(prune_results, "cited_names", lambda root: {"ancient"})

    prune_results.main(["--root", str(results), "--apply", "--ignore-citations"])

    assert {path.name for path in prune_results.results_dir(results).iterdir()} == {"fresh", "just-inside"}


def test_refuses_to_delete_when_citations_cannot_be_determined(results: Path, monkeypatch, capsys) -> None:
    """An unreadable citation set fails closed rather than assuming nothing is cited.

    An empty set from a failed search is indistinguishable from a genuinely uncited tree, and acting on it would
    authorise deleting everything.
    """

    def _raise(root):
        raise prune_results.CitationsUnavailableError("git missing")

    monkeypatch.setattr(prune_results, "cited_names", _raise)

    assert prune_results.main(["--root", str(results), "--apply"]) == 2
    assert len(list(prune_results.results_dir(results).iterdir())) == 4
    assert "refusing to delete" in capsys.readouterr().err


def test_cited_names_reads_tracked_files_of_this_repository() -> None:
    """The real lookup finds run names cited by this repository's own tracked docs.

    Pinning the mechanism, not a specific run: monkeypatched tests above would pass even if the git invocation were
    wrong.
    """
    names = _REAL_CITED_NAMES(Path(__file__).resolve().parent.parent.parent)

    assert any(name.startswith(("claude-", "codex-")) for name in names)
