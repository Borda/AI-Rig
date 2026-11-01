"""Tests for bin/codemap_cache.py — review→resolve pre-flight cache.

Covers: write splitting a batch into per-module artifacts, read freshness verdicts (fresh, cold miss, index-rebuilt,
git-sha mismatch, index-stamp mismatch, content-hash mismatch), and the reuse_ratio report metric.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import codemap_cache  # type: ignore[import-not-found]
import pytest

# Pinned so two identical _write_index calls produce an identical file stamp; the
# real clock would make every rewrite look like a new index and every reuse test flaky.
_FIXED_MTIME_NS = 1_700_000_000_000_000_000


def _write_index(
    tmp_path: Path,
    git_sha: str = "abc123",
    scanned_at: str = "2026-07-10T00:00:00+00:00",
    mtime_ns: int = _FIXED_MTIME_NS,
) -> Path:
    """Create a deterministic codemap index fixture with a pinned file timestamp.

    Examples:
        >>> tmp_path = getfixture("tmp_path")
        >>> path = _write_index(tmp_path, git_sha="demo")
        >>> json.loads(path.read_text())["git_sha"]
        'demo'
    """
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps({"git_sha": git_sha, "scanned_at": scanned_at, "modules": {}}))
    os.utime(idx, ns=(mtime_ns, mtime_ns))
    return idx


def _write_batch(tmp_path: Path) -> Path:
    """Create a batch fixture covering module, qname, failed, and uncovered results.

    Examples:
        >>> tmp_path = getfixture("tmp_path")
        >>> path = _write_batch(tmp_path)
        >>> json.loads(path.read_text())["count"]
        5
    """
    batch = {
        "batch": [
            {"ok": True, "index": 0, "cmd": "central", "result": {"central": []}},
            {"ok": True, "index": 1, "cmd": "rdeps", "result": {"module": "pkg.mod", "importers": ["a"]}},
            {"ok": True, "index": 2, "cmd": "fn-rdeps", "result": {"qname": "pkg.mod::fn", "called_by": []}},
            {"ok": True, "index": 3, "cmd": "uncovered", "result": {"query": "pkg.other", "symbols": ["f"]}},
            {"ok": False, "index": 4, "cmd": "rdeps", "result": {"error": "boom"}},
        ],
        "count": 5,
        "index": {"query_complete": True},
    }
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch))
    return path


def _write_cache(tmp_path: Path) -> tuple[int, Path]:
    """Write the standard batch under the standard index into ``tmp_path/cache``; return ``(exit_code, cache_dir)``."""
    cache = tmp_path / "cache"
    argv = ["write", "--batch", str(_write_batch(tmp_path)), "--index", str(_write_index(tmp_path))]
    return codemap_cache.main([*argv, "--cache-dir", str(cache)]), cache


def _read_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], cache: Path, module: str = "pkg.mod", index: Path | None = None
) -> dict:
    """Run ``read`` for ``module`` against ``index`` (default: the standard one) and return its JSON verdict."""
    index = index or _write_index(tmp_path)
    rc = codemap_cache.main(["read", "--module", module, "--index", str(index), "--cache-dir", str(cache)])
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def _edit_artifact(cache: Path, mutate: Callable[[dict], object]) -> None:
    """Load ``pkg.mod``'s cached artifact, apply ``mutate`` to it, and write it back."""
    path = cache / "pkg.mod.json"
    artifact = json.loads(path.read_text())
    mutate(artifact)
    path.write_text(json.dumps(artifact))


def _drop_prefix(field: str) -> Callable[[dict], object]:
    """Return a mutation removing ``field`` from an artifact's freshness prefix."""
    return lambda artifact: artifact["prefix"].pop(field)


def _set_prefix(field: str, value: object) -> Callable[[dict], object]:
    """Return a mutation overwriting ``field`` in an artifact's freshness prefix."""
    return lambda artifact: artifact["prefix"].update({field: value})


def _drop_index_sha(metadata: dict) -> object:
    """Remove the git sha from index metadata."""
    return metadata.pop("git_sha")


def _set_index_sha(value: object) -> Callable[[dict], object]:
    """Return a mutation overwriting the git sha in index metadata."""
    return lambda metadata: metadata.update({"git_sha": value})


@pytest.fixture(name="seeded_cache")
def _seeded_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """Write the standard batch into a cache and drain the write's stdout so later reads parse cleanly."""
    rc, cache = _write_cache(tmp_path)
    assert rc == 0
    capsys.readouterr()
    return cache


class TestWrite:
    """Write subcommand: batch to per-module artifacts."""

    def test_splits_by_module(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """One artifact is written per module named by the batch's successful results.

        The batch mixes module-keyed, qname-keyed (``pkg.mod::fn``), global and failed results; only the two modules
        ``pkg.mod`` and ``pkg.other`` get a cache file, and the summary reports that count.
        """
        rc, cache = _write_cache(tmp_path)
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["modules_written"] == 2
        assert (cache / "pkg.mod.json").exists()
        assert (cache / "pkg.other.json").exists()

    def test_prefix_delta_shape(self, seeded_cache: Path, tmp_path: Path) -> None:
        """An artifact carries the index's freshness prefix, grouped answers, and an empty delta.

        The prefix records the git sha, scan time, and file stamp that later reads compare against, so a reused answer
        can be proven current. A qname-keyed ``fn-rdeps`` result is grouped under its module ``pkg.mod``.
        """
        art = json.loads((seeded_cache / "pkg.mod.json").read_text())
        assert art["prefix"]["git_sha"] == "abc123"
        assert art["prefix"]["scanned_at"] == "2026-07-10T00:00:00+00:00"
        assert art["prefix"]["index_stamp"] == codemap_cache._file_stamp(_write_index(tmp_path))
        assert set(art["prefix"]["answers"]) == {"rdeps", "fn-rdeps"}
        assert art["delta"] == {"touched_files": [], "exhausted_queries": [], "notes": []}

    def test_skips_failed_items(self, seeded_cache: Path) -> None:
        """A failed batch result never overwrites the valid answer for the same query.

        The batch holds a successful ``rdeps`` for ``pkg.mod`` and a later ``ok: false`` ``rdeps`` item; only the valid
        importer list may reach the cache.
        """
        art = json.loads((seeded_cache / "pkg.mod.json").read_text())
        assert art["prefix"]["answers"]["rdeps"]["importers"] == ["a"]


class TestRead:
    """Read subcommand: freshness verdicts."""

    def test_fresh_reuse(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], seeded_cache: Path) -> None:
        """An untouched index lets the seeded answers be reused.

        Reading right after the write, with the same index bytes, stamp, and identity, is the happy path of the
        review-to-resolve hand-off: the cached ``rdeps`` answer comes back and the reason is ``fresh``.
        """
        verdict = _read_verdict(tmp_path, capsys, seeded_cache)
        assert (verdict["reuse"], verdict["reason"]) == (True, "fresh")
        assert verdict["answers"]["rdeps"]["importers"] == ["a"]

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(_drop_prefix("git_sha"), id="git-sha-absent"),
            pytest.param(_set_prefix("git_sha", ""), id="git-sha-empty"),
            pytest.param(_set_prefix("git_sha", "not-a-timestamp"), id="git-sha-mismatched-text"),
            pytest.param(_drop_prefix("scanned_at"), id="scanned-at-absent"),
            pytest.param(_set_prefix("scanned_at", ""), id="scanned-at-empty"),
            pytest.param(_set_prefix("scanned_at", "not-a-timestamp"), id="scanned-at-malformed"),
        ],
    )
    def test_invalid_freshness_coordinate_requeries(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], seeded_cache: Path, mutate: Callable[[dict], object]
    ) -> None:
        """Absent or malformed provenance in an artifact is re-queried even when the file stamps match.

        The index stamp alone must not make an answer reusable: the artifact's recorded git sha and scan time have to be
        present and consistent with the index too.
        """
        _edit_artifact(seeded_cache, mutate)
        assert _read_verdict(tmp_path, capsys, seeded_cache)["reuse"] is False

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(_drop_index_sha, id="absent"),
            pytest.param(_set_index_sha(None), id="null"),
            pytest.param(_set_index_sha(""), id="empty"),
            pytest.param(_set_index_sha("   "), id="blank"),
            pytest.param(_set_index_sha(123), id="integer"),
            pytest.param(_set_index_sha(False), id="boolean"),
        ],
    )
    def test_malformed_index_sha_rejects_roundtrip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], mutate: Callable[[dict], object]
    ) -> None:
        """Writing from an index with no usable git sha never manufactures reusable provenance.

        The write itself succeeds, but a later read of that cache reports missing freshness metadata and returns no
        answers, whatever shape the bad sha takes.
        """
        index = _write_index(tmp_path)
        metadata = json.loads(index.read_text())
        mutate(metadata)
        index.write_text(json.dumps(metadata))
        cache = tmp_path / "cache"
        batch = ["write", "--batch", str(_write_batch(tmp_path)), "--index", str(index), "--cache-dir", str(cache)]
        assert codemap_cache.main(batch) == 0
        capsys.readouterr()
        verdict = _read_verdict(tmp_path, capsys, cache, index=index)
        assert verdict == {"reuse": False, "reason": "missing_freshness_metadata", "answers": {}}

    def test_same_stamp_in_another_project_is_not_reusable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], seeded_cache: Path
    ) -> None:
        """Equal bytes and timestamps from a different project's index never substitute for index identity.

        A second project directory holds a byte-identical index with the same pinned mtime; reading the first project's
        cache against it must still re-query.
        """
        other = tmp_path / "other-project"
        other.mkdir()
        verdict = _read_verdict(tmp_path, capsys, seeded_cache, index=_write_index(other))
        assert verdict["reuse"] is False

    def test_cold_miss(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], seeded_cache: Path) -> None:
        """A module with no cached artifact reports a cold miss.

        ``pkg.absent`` was never part of the seeded batch, so there is nothing to reuse and the caller must query.
        """
        verdict = _read_verdict(tmp_path, capsys, seeded_cache, module="pkg.absent")
        assert (verdict["reuse"], verdict["reason"]) == (False, "cold_miss")

    @pytest.mark.parametrize(
        ("index_kwargs", "reason"),
        [
            pytest.param(
                {"git_sha": "abc123", "scanned_at": "2026-07-11T00:00:00+00:00"}, "index_rebuilt", id="index-rebuilt"
            ),
            pytest.param({"git_sha": "def456"}, "git_sha_mismatch", id="git-sha-moved"),
            pytest.param({"mtime_ns": _FIXED_MTIME_NS + 5_000_000_000}, "index_stamp_mismatch", id="in-place-rewrite"),
        ],
    )
    def test_changed_index_invalidates(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        seeded_cache: Path,
        index_kwargs: dict[str, object],
        reason: str,
    ) -> None:
        """A seeded artifact is not reused once the index was rebuilt, moved to another git sha, or rewritten in place.

        An in-place rewrite keeps identical metadata but gets a new mtime: ``git_sha`` and ``scanned_at`` are the
        index's own declared fields, so an incremental scan or a restored backup that leaves them untouched was
        indistinguishable from no change at all, and stale answers were served as fresh.
        """
        verdict = _read_verdict(tmp_path, capsys, seeded_cache, index=_write_index(tmp_path, **index_kwargs))
        assert (verdict["reuse"], verdict["reason"]) == (False, reason)

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            pytest.param(_drop_prefix("index_stamp"), "index_stamp_mismatch", id="artifact-without-stamp"),
            pytest.param(
                lambda artifact: artifact["prefix"]["answers"]["rdeps"].update(importers=["tampered"]),
                "content_hash_mismatch",
                id="tampered-content",
            ),
        ],
    )
    def test_damaged_artifact_is_requeried(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        seeded_cache: Path,
        mutate: Callable[[dict], object],
        reason: str,
    ) -> None:
        """A pre-stamp artifact or one whose cached answers were edited is re-queried, never trusted.

        Both fail closed: a missing index stamp reads as a stamp mismatch, and answers that no longer hash to the
        recorded content hash are rejected.
        """
        _edit_artifact(seeded_cache, mutate)
        verdict = _read_verdict(tmp_path, capsys, seeded_cache)
        assert (verdict["reuse"], verdict["reason"]) == (False, reason)


class TestReport:
    """Report subcommand: reuse_ratio metric."""

    def test_zero_when_no_reuse_markers(self, capsys: pytest.CaptureFixture[str], seeded_cache: Path) -> None:
        """A cache nobody reused reports a zero ratio over every cached answer.

        The seeded batch cached three answers (``rdeps`` and ``fn-rdeps`` for ``pkg.mod``, ``uncovered`` for
        ``pkg.other``) and no artifact carries a reuse note.
        """
        codemap_cache.main(["report", "--cache-dir", str(seeded_cache)])
        out = json.loads(capsys.readouterr().out)
        assert (out["reuse_ratio"], out["answers_total"]) == (0.0, 3)

    def test_ratio_counts_reused_marker(self, capsys: pytest.CaptureFixture[str], seeded_cache: Path) -> None:
        """An artifact noted as reused counts all of its answers toward the ratio.

        Marking ``pkg.mod`` (two cached answers) as reused out of three total gives 2 of 3.
        """
        _edit_artifact(seeded_cache, lambda artifact: artifact["delta"]["notes"].append("reused@2026-07-10"))
        codemap_cache.main(["report", "--cache-dir", str(seeded_cache)])
        out = json.loads(capsys.readouterr().out)
        assert (out["answers_reused"], out["reuse_ratio"]) == (2, round(2 / 3, 3))


def test_bad_args_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unreadable batch input exits 1 with a JSON error on stderr.

    A missing batch or index file must surface as a failed write, not a traceback or a silent success.
    """
    argv = ["write", "--batch", str(tmp_path / "nope.json"), "--index", str(tmp_path / "nope2.json")]
    rc = codemap_cache.main([*argv, "--cache-dir", str(tmp_path)])
    assert rc == 1
    assert "error" in capsys.readouterr().err
