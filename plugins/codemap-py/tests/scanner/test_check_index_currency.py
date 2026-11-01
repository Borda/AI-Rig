"""Tests for ``bin/check-index-currency`` — two-tier codemap index currency check.

Strategy:
- Import the module via importlib (hyphenated filename can't use plain import).
- Mock ``_git_head`` and ``_git_dirty_py_count`` so no git binary required.
- Use ``tmp_path`` for real filesystem fixtures (Tier 2 file-hash path).
- CLI entry-point tested via ``main()`` directly, not subprocess.
- Exercise the RW gate for real (a genuine writer lease held from a second thread),
  not a stubbed one — the regression being pinned is a token-free index read, which
  a mocked gate would not detect.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from codemap_py import rwgate

# ---------------------------------------------------------------------------
# Import hyphen-named bin script (no .py extension — must use SourceFileLoader)
# ---------------------------------------------------------------------------

_BIN_DIR = Path(__file__).parent.parent.parent / "bin"
_CIC_PATH = _BIN_DIR / "check-index-currency"

_loader = importlib.machinery.SourceFileLoader("check_index_currency", str(_CIC_PATH))
_spec = importlib.util.spec_from_loader("check_index_currency", _loader)
cic = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(cic)

SHA_A = "a" * 40
SHA_B = "b" * 40


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_index(path: Path, data: dict[str, Any]) -> None:
    """Write a minimal codemap index JSON to *path*.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = Path(directory) / "nested" / "index.json"
    ...     _write_index(path, {"modules": []})
    ...     json.loads(path.read_text())["modules"]
    []
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _minimal_index(**extra: Any) -> dict[str, Any]:
    """Return a minimal valid index dict, optionally extended.

    >>> _minimal_index(project="demo")["project"]
    'demo'
    """
    base: dict[str, Any] = {"scan_version": 3, "modules": [], "scanned_at": "2026-01-01T00:00:00Z"}
    base.update(extra)
    return base


def _git(root: Path, *args: str) -> None:
    """Run one git command in *root*, asserting success."""
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _write_files(root: Path, files: dict[str, str]) -> None:
    """Write each ``name -> text`` entry of *files* under *root*."""
    for name, text in files.items():
        (root / name).write_text(text)


@pytest.fixture(name="set_git")
def _set_git(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Return a setter faking the git HEAD sha and dirty ``.py`` count the Tier 1 check consults.

    ``head=None`` models an unavailable git, which forces the Tier 2 file-hash path.
    """

    def _set(head: str | None, dirty: int = 0) -> None:
        monkeypatch.setattr(cic, "_git_head", lambda root: head)
        monkeypatch.setattr(cic, "_git_dirty_py_count", lambda root: dirty)

    return _set


# ---------------------------------------------------------------------------
# _result helper
# ---------------------------------------------------------------------------


class TestResult:
    """Tests for the ``_result`` helper."""

    @pytest.mark.parametrize(
        ("status", "reason", "expected_code"),
        [
            pytest.param("current", "ok", 0, id="current-exit-0"),
            pytest.param("stale", "changed", 1, id="stale-exit-1"),
            pytest.param("no_index", "missing", 2, id="no-index-exit-2"),
        ],
    )
    def test_status_maps_to_documented_exit_code(self, status: str, reason: str, expected_code: int) -> None:
        """Each status keeps its name in the result dict and maps to its documented exit code.

        Shell callers branch on the exit code (0 current, 1 stale, 2 no index) while JSON consumers read the status.
        """
        result, code = cic._result(status, reason)

        assert (result["status"], code) == (status, expected_code)

    @pytest.mark.parametrize(
        ("args", "expected_count"),
        [
            pytest.param(("current", "ok"), 0, id="default-zero"),
            pytest.param(("stale", "x", 5), 5, id="passed-through"),
        ],
    )
    def test_changed_count(self, args: tuple, expected_count: int) -> None:
        """changed_count defaults to 0 and is stored in the result dict when given."""
        r, _ = cic._result(*args)

        assert r["changed_count"] == expected_count


# ---------------------------------------------------------------------------
# _sha256_file helper
# ---------------------------------------------------------------------------


class TestSha256File:
    """Tests for ``_sha256_file``."""

    @pytest.mark.parametrize(
        "data",
        [pytest.param(b"hello codemap", id="known-content"), pytest.param(b"", id="empty-file")],
    )
    def test_matches_hashlib_digest(self, tmp_path: Path, data: bytes) -> None:
        """The SHA-256 of a file equals the digest of its bytes, including for an empty file.

        The empty file guards the streaming read loop against returning early or hashing nothing.
        """
        f = tmp_path / "x.py"
        f.write_bytes(data)

        assert cic._sha256_file(f) == hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# _parse_scanned_at helper
# ---------------------------------------------------------------------------


class TestParseScannedAt:
    """Tests for ``_parse_scanned_at``."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(
                "2026-01-15T12:30:00Z",
                datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc).timestamp(),
                id="valid-z-suffix",
            ),
            pytest.param(
                "2026-01-15T12:30:00",
                datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc).timestamp(),
                id="valid-no-suffix-assumes-utc",
            ),
            pytest.param(
                "2026-01-15T12:30:00.123456Z",
                datetime(2026, 1, 15, 12, 30, 0, 123456, tzinfo=timezone.utc).timestamp(),
                id="valid-microseconds",
            ),
            pytest.param(
                "2026-01-15T12:30:00+02:00",
                datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc).timestamp(),
                id="valid-timezone-offset",
            ),
            pytest.param(
                " 2026-01-15T12:30:00Z \n",
                datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc).timestamp(),
                id="valid-with-whitespace",
            ),
            pytest.param("2026-01-15", 0.0, id="date-only-rejected"),
            pytest.param(None, 0.0, id="null"),
            pytest.param(123, 0.0, id="invalid-type"),
            pytest.param("", 0.0, id="empty-string"),
            pytest.param("not-a-date", 0.0, id="garbage"),
        ],
    )
    def test_parse(self, value: object, expected: float) -> None:
        """Timestamps parse to the expected POSIX value; invalid values return zero.

        A zero result means "scanned at the epoch", so every file compares as newer than the scan rather than a bad
        timestamp silently marking the index fresh.
        """
        result = cic._parse_scanned_at(value)

        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _read_index helper
# ---------------------------------------------------------------------------


class TestReadIndex:
    """Tests for ``_read_index``."""

    def test_valid_json(self, tmp_path: Path) -> None:
        """Valid JSON dict is returned as-is."""
        p = tmp_path / "idx.json"
        p.write_text('{"a": 1}')

        assert cic._read_index(p) == {"a": 1}

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("{not json}", id="corrupt-json"),
            pytest.param("[1, 2, 3]", id="json-list-not-dict"),
        ],
    )
    def test_non_dict_or_corrupt_content_returns_none(self, tmp_path: Path, content: str) -> None:
        """Corrupt JSON and a JSON array (not a dict) both return None."""
        p = tmp_path / "idx.json"
        p.write_text(content)

        assert cic._read_index(p) is None

    def test_missing_file(self, tmp_path: Path) -> None:
        """Missing file returns None."""
        assert cic._read_index(tmp_path / "missing.json") is None

    def test_oversized_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """File exceeding size limit returns None.

        The limit is forced below zero so even a two-byte file is "oversized" without writing a huge fixture.
        """
        p = tmp_path / "huge.json"
        p.write_text("{}")
        monkeypatch.setattr(cic, "MAX_INDEX_SIZE", -1)

        assert cic._read_index(p) is None


# ---------------------------------------------------------------------------
# check_currency — no index cases
# ---------------------------------------------------------------------------


class TestCheckCurrencyNoIndex:
    """Handle missing and unreadable index files."""

    @pytest.mark.parametrize(
        ("name", "files"),
        [
            pytest.param("missing.json", {}, id="missing-file"),
            pytest.param("bad.json", {"bad.json": "not json"}, id="corrupt-json"),
        ],
    )
    def test_unreadable_index_reports_no_index(self, tmp_path: Path, name: str, files: dict[str, str]) -> None:
        """A non-existent or corrupt index path returns no_index with exit code 2.

        Both cases mean "no usable index", so the caller is told to build one rather than that the index is stale.
        """
        _write_files(tmp_path, files)

        r, code = cic.check_currency(tmp_path / name, tmp_path)

        assert (r["status"], code) == ("no_index", 2)


# ---------------------------------------------------------------------------
# check_currency — Tier 1 (git-based, mocked)
# ---------------------------------------------------------------------------


class TestCheckCurrencyTier1:
    """Git-based currency checks with mocked git calls."""

    @pytest.fixture(name="index_file")
    def _index_file(self, tmp_path: Path) -> Path:
        """Write minimal index with git_sha=SHA_A."""
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(git_sha=SHA_A))
        return p

    def test_legacy_matching_head_requires_migration(self, index_file: Path, tmp_path: Path, set_git) -> None:
        """A matching legacy HEAD does not establish complete working-tree input freshness.

        The index predates the source fingerprint, so equal HEAD and a clean tree still cannot prove that consumed
        configuration inputs are unchanged; it must be reported stale for migration.
        """
        set_git(SHA_A)

        r, code = cic.check_currency(index_file, tmp_path)

        assert r["status"] == "stale"
        assert "migration" in r["reason"]
        assert code == 1

    def test_stale_when_sha_differs(self, index_file: Path, tmp_path: Path, set_git) -> None:
        """HEAD SHA differs from stored git_sha → stale, naming both short shas."""
        set_git(SHA_B)

        r, code = cic.check_currency(index_file, tmp_path)

        assert r["status"] == "stale"
        assert code == 1
        assert "a" * 8 in r["reason"]
        assert "b" * 8 in r["reason"]

    def test_stale_when_dirty_py_files(self, index_file: Path, tmp_path: Path, set_git) -> None:
        """SHA matches but uncommitted .py changes → stale with the dirty count reported."""
        set_git(SHA_A, dirty=3)

        r, _code = cic.check_currency(index_file, tmp_path)

        assert r["status"] == "stale"
        assert r["changed_count"] == 3

    def test_git_head_none_falls_to_tier2(self, tmp_path: Path, set_git) -> None:
        """When _git_head returns None, Tier 2 runs (file_shas path).

        Tier 2 ran if the result is a definite verdict; the exact one depends on mtime, so only "not a Tier 1 error" is
        pinned.
        """
        py = tmp_path / "a.py"
        py.write_text("x = 1")
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(file_shas={"a.py": cic._sha256_file(py)}))
        set_git(None)

        r, _ = cic.check_currency(p, tmp_path)

        assert r["status"] in ("current", "stale")

    def test_no_stored_git_sha_falls_to_tier2(self, tmp_path: Path, set_git) -> None:
        """Index with no git_sha field triggers Tier 2 regardless of git availability.

        With no ``file_shas`` either (old format) Tier 2 can only answer stale.
        """
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index())
        set_git(SHA_A)

        r, _code = cic.check_currency(p, tmp_path)

        assert r["status"] == "stale"


# ---------------------------------------------------------------------------
# check_currency — Tier 2 (filesystem-based)
# ---------------------------------------------------------------------------


class TestCheckCurrencyTier2:
    """Filesystem-based currency checks (no git)."""

    @pytest.fixture(autouse=True)
    def _no_git(self, set_git) -> None:
        """Force Tier 2 by making _git_head return None."""
        set_git(None)

    def _write_py(self, root: Path, relpath: str, content: str = "x = 1") -> str:
        """Write a .py file under root and return its MD5."""
        p = root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return cic._md5_file(p)

    def test_legacy_matching_hashes_require_migration(self, tmp_path: Path) -> None:
        """Legacy Python-only hashes cannot prove that all consumed configuration inputs are current.

        Every stored hash matches the file on disk, yet an index without a source fingerprint is still stale for
        migration because config inputs were never hashed.
        """
        sha = self._write_py(tmp_path, "mod.py")
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(file_shas={"mod.py": sha}, scanned_at="2025-01-01T00:00:00Z"))

        r, code = cic.check_currency(p, tmp_path)

        assert r["status"] == "stale"
        assert "migration" in r["reason"]
        assert code == 1

    def test_stale_new_py_file(self, tmp_path: Path) -> None:
        """New .py file not present in file_shas → stale.

        ``existing.py`` is in the index; ``new_module.py`` is not, so it counts as one added file.
        """
        sha_existing = self._write_py(tmp_path, "existing.py")
        self._write_py(tmp_path, "new_module.py")
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(file_shas={"existing.py": sha_existing}))

        r, _code = cic.check_currency(p, tmp_path)

        assert r["status"] == "stale"
        assert r["changed_count"] >= 1

    def test_stale_deleted_py_file(self, tmp_path: Path) -> None:
        """File in file_shas but missing on disk → stale, with the reason naming the deletion."""
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(file_shas={"ghost.py": "abc123"}))

        r, _code = cic.check_currency(p, tmp_path)

        assert r["status"] == "stale"
        assert "delete" in r["reason"].lower()

    def test_stale_content_changed(self, tmp_path: Path) -> None:
        """File content changed (SHA differs) with mtime newer than scan → stale.

        The file is overwritten after its hash is recorded, and ``scanned_at`` is far in the past, so the mtime pre-
        filter cannot exclude it.
        """
        py = tmp_path / "mod.py"
        py.write_text("original")
        old_sha = cic._md5_file(py)
        py.write_text("changed content here")
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(file_shas={"mod.py": old_sha}, scanned_at="2020-01-01T00:00:00Z"))

        r, _code = cic.check_currency(p, tmp_path)

        assert r["status"] == "stale"
        assert r["changed_count"] >= 1

    def test_legacy_mtime_prefilter_cannot_claim_freshness(self, tmp_path: Path) -> None:
        """A legacy timestamp heuristic cannot turn changed bytes into a confirmed-current result.

        The changed file's mtime is the epoch, older than ``scanned_at``, so the old pre-filter would have skipped it
        and reported current; the migration requirement must win instead.
        """
        py = tmp_path / "old.py"
        py.write_text("content A")
        old_sha = cic._md5_file(py)
        py.write_text("content B")
        os.utime(py, (0.0, 0.0))
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(file_shas={"old.py": old_sha}, scanned_at="2025-06-01T00:00:00Z"))

        r, _ = cic.check_currency(p, tmp_path)

        assert r["status"] == "stale"
        assert "migration" in r["reason"]

    def test_stale_no_file_shas_in_index(self, tmp_path: Path) -> None:
        """Index with no file_shas field at all → stale (rebuild required)."""
        self._write_py(tmp_path, "mod.py")
        p = tmp_path / "idx.json"
        _write_index(p, {"scan_version": 2, "modules": []})

        r, code = cic.check_currency(p, tmp_path)

        assert r["status"] == "stale"
        assert code == 1


class TestCheckCurrencyTier2GitBlobs:
    """Tier 2 content verification when the index stores 40-char git blob SHAs."""

    BLOB_ONE = "1" * 40
    BLOB_TWO = "2" * 40

    @pytest.fixture(autouse=True)
    def _no_git_head(self, set_git) -> None:
        """Force Tier 2 by making _git_head return None."""
        set_git(None)

    @pytest.fixture(name="index_file")
    def _index_file(self, tmp_path: Path) -> Path:
        """Write two .py files and an index recording their blob SHAs."""
        _write_files(tmp_path, {"one.py": "x = 1", "two.py": "y = 2"})
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(file_shas={"one.py": self.BLOB_ONE, "two.py": self.BLOB_TWO}))
        return p

    def test_legacy_matching_blobs_require_migration(
        self, index_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Matching staged blobs cannot prove actual working-tree freshness.

        Git blobs describe the index/HEAD, not the bytes on disk, so equal blobs alone still require migration.
        """
        blobs = {"one.py": self.BLOB_ONE, "two.py": self.BLOB_TWO}
        monkeypatch.setattr(cic, "_git_blob_shas", lambda root: blobs)

        r, code = cic.check_currency(index_file, tmp_path)

        assert (r["status"], code) == ("stale", 1)
        assert "migration" in r["reason"]

    @pytest.mark.parametrize(
        ("blobs", "expected_count"),
        [
            # One rewritten blob SHA → stale, counting only that file.
            pytest.param({"one.py": BLOB_ONE, "two.py": "3" * 40}, 1, id="blob-differs"),
            # A file git reports no blob for counts as changed.
            pytest.param({"one.py": BLOB_ONE}, 1, id="blob-absent"),
            # Handle failure by marking every stored file changed rather than assuming current.
            pytest.param(None, 2, id="git-listing-unavailable"),
        ],
    )
    def test_stale_when_blobs_do_not_match(
        self,
        index_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        blobs: dict[str, str] | None,
        expected_count: int,
    ) -> None:
        """A rewritten, missing or unlistable git blob SHA → stale, counting only the affected files.

        When git cannot list blobs at all the check must not assume the index is current: every stored file counts.
        """
        monkeypatch.setattr(cic, "_git_blob_shas", lambda root: blobs)

        r, code = cic.check_currency(index_file, tmp_path)

        assert (r["status"], code) == ("stale", 1)
        assert r["changed_count"] == expected_count


# ---------------------------------------------------------------------------
# CLI — main() and ``--field`` flag
# ---------------------------------------------------------------------------


class TestCLI:
    """Tests for the ``main()`` CLI entry point."""

    def test_field_status_current(self, tmp_path: Path, set_git, capsys: pytest.CaptureFixture) -> None:
        """``--field status`` prints only the status value and exits 0 for a current index.

        A file whose hash matches a fingerprinted index, with git unavailable, is the minimal current index.
        """
        py = tmp_path / "m.py"
        py.write_text("x = 1")
        sha = cic._md5_file(py)
        idx = tmp_path / "idx.json"
        fingerprint = cic.fingerprint_file_hashes({"m.py": sha})
        _write_index(idx, _minimal_index(file_shas={"m.py": sha}, source_fingerprint=fingerprint))
        set_git(None)
        os.utime(py, (0.0, 0.0))  # older than scanned_at

        code = cic.main(["--index-path", str(idx), "--root", str(tmp_path), "--field", "status"])

        assert capsys.readouterr().out.strip() == "current"
        assert code == 0

    def test_field_reason(self, tmp_path: Path, set_git, capsys: pytest.CaptureFixture) -> None:
        """``--field reason`` prints a non-empty reason string for a stale index."""
        idx = tmp_path / "idx.json"
        _write_index(idx, _minimal_index(git_sha=SHA_A))
        set_git(SHA_B)

        cic.main(["--index-path", str(idx), "--root", str(tmp_path), "--field", "reason"])

        assert len(capsys.readouterr().out.strip()) > 0

    def test_full_json_output(self, tmp_path: Path, set_git, capsys: pytest.CaptureFixture) -> None:
        """Without ``--field``, output is valid JSON with status/reason/changed_count."""
        idx = tmp_path / "idx.json"
        _write_index(idx, _minimal_index(git_sha=SHA_A))
        set_git(SHA_A)

        cic.main(["--index-path", str(idx), "--root", str(tmp_path)])

        data = json.loads(capsys.readouterr().out.strip())
        assert set(data) >= {"status", "reason", "changed_count"}

    def test_missing_index_path_arg(self, capsys: pytest.CaptureFixture) -> None:
        """Missing required ``--index-path`` exits non-zero."""
        with pytest.raises(SystemExit) as exc:
            cic.main([])

        assert exc.value.code != 0

    def test_root_auto_detect_via_git_toplevel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_git, capsys: pytest.CaptureFixture
    ) -> None:
        """Without --root, ``_git_toplevel`` is called to detect project root.

        The detected root is where the tracked file lives, so a current verdict proves the detected root was used.
        """
        py = tmp_path / "m.py"
        py.write_text("x = 1")
        sha = cic._md5_file(py)
        idx = tmp_path / "idx.json"
        _write_index(
            idx, _minimal_index(file_shas={"m.py": sha}, source_fingerprint=cic.fingerprint_file_hashes({"m.py": sha}))
        )
        monkeypatch.setattr(cic, "_git_toplevel", lambda: tmp_path)
        set_git(SHA_A)

        cic.main(["--index-path", str(idx)])

        assert json.loads(capsys.readouterr().out.strip())["status"] == "current"


# ---------------------------------------------------------------------------
# Index reads enter the RW gate
# ---------------------------------------------------------------------------

_WRITER_HOLD_TIMEOUT = 5.0


@pytest.fixture(name="gate_events")
def _gate_events() -> Iterator[list[str]]:
    """Collect ``rwgate`` lifecycle event names emitted during one test."""
    events: list[str] = []
    rwgate.set_instrument(lambda name, fields: events.append(name))
    yield events
    rwgate.set_instrument(None)


@pytest.fixture(name="live_writer")
def _live_writer() -> Iterator[Callable[[Path], None]]:
    """Return a callable holding a real exclusive writer lease until the test ends.

    The writer runs on a second thread and parks inside its exclusive phase, so the index it guards is genuinely
    unavailable to a reader for the whole test — the only condition under which a leased read and a token-free read give
    different answers.
    """
    release = threading.Event()
    started = threading.Event()
    threads: list[threading.Thread] = []

    def _build(target: Path) -> None:
        """Block the writer until the test releases its coordination event."""
        started.set()
        release.wait(_WRITER_HOLD_TIMEOUT)

    def _hold(index_path: Path) -> None:
        """Run the blocking writer in a daemon thread."""
        thread = threading.Thread(target=rwgate.write_index, args=(index_path, _build), daemon=True)
        thread.start()
        threads.append(thread)
        assert started.wait(_WRITER_HOLD_TIMEOUT), "writer never reached its exclusive phase"

    yield _hold
    release.set()
    for thread in threads:
        thread.join(_WRITER_HOLD_TIMEOUT)


class TestIndexReadIsLeased:
    """The index is read under a shared reader lease, never token-free."""

    def test_read_index_brackets_the_parse_with_a_reader_token(self, tmp_path: Path, gate_events: list[str]) -> None:
        """Acquire and release exactly one reader token for each index read."""
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index())

        cic._read_index(p)

        assert gate_events == ["reader_acquire", "reader_release"]

    def test_coordination_root_materialised_beside_the_index(self, tmp_path: Path) -> None:
        """Leasing creates the ``.index-rw`` coordination root next to the index."""
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index())

        cic._read_index(p)

        assert (tmp_path / ".index-rw" / "readers").is_dir()

    def test_absent_index_leases_nothing(self, tmp_path: Path) -> None:
        """A check against a project with no index never creates a coordination root."""
        r, _ = cic.check_currency(tmp_path / "missing.json", tmp_path)

        assert r["status"] == "no_index"
        assert not (tmp_path / ".index-rw").exists()

    def test_full_check_reads_the_index_through_the_gate(self, tmp_path: Path, gate_events: list[str], set_git) -> None:
        """A whole ``check_currency`` pass takes one reader lease and reports current."""
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(file_shas={}, source_fingerprint=cic.fingerprint_file_hashes({})))
        set_git(SHA_A)

        r, code = cic.check_currency(p, tmp_path)

        assert (r["status"], code) == ("current", 0)
        assert gate_events == ["reader_acquire", "reader_release"]

    def test_live_writer_reports_stale_instead_of_reading_untokened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_git, live_writer: Callable[[Path], None]
    ) -> None:
        """A held writer lease yields stale; a token-free read would have said current.

        With git agreeing the index is current, only the busy lease can explain a stale verdict, and its reason must say
        the index is busy.
        """
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(git_sha=SHA_A))
        monkeypatch.setattr(cic, "_LEASE_TIMEOUT", 0.2)
        set_git(SHA_A)
        live_writer(p)

        r, code = cic.check_currency(p, tmp_path)

        assert (r["status"], code) == ("stale", 1)
        assert "busy" in r["reason"]

    def test_unusable_coordination_root_reports_no_index(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unwritable coordination root ends the check as no_index, not a bare read."""
        p = tmp_path / "idx.json"
        _write_index(p, _minimal_index(git_sha=SHA_A))

        def _refuse(path: Path, *, timeout: float | None = None) -> None:
            """Raise the expected coordination error for an unwritable root."""
            raise rwgate.CoordinationUnavailable(f"coordination root unwritable: {path}")

        monkeypatch.setattr(rwgate, "read_lease", _refuse)

        r, code = cic.check_currency(p, tmp_path)

        assert (r["status"], code) == ("no_index", 2)
        assert "gate unavailable" in r["reason"]


@pytest.mark.integration
def test_real_dirty_scan_is_current_until_consumed_inputs_change(tmp_path: Path, scan_index: Path) -> None:
    """Currency follows scanned bytes, including dirty sources and config, while remaining strictly read-only.

    A committed module is edited (dirty) and then scanned for real. The check must report current, flip to stale when a
    consumed config file appears, report stale for a mismatched root, and never rewrite the index in any of the three.
    """
    root = tmp_path / "dirty-project"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "mod.py").write_bytes(b"VALUE = 1\n")
    _git(root, "add", "mod.py")
    _git(root, "commit", "-qm", "initial")
    (root / "mod.py").write_bytes(b"VALUE = 2\n")
    subprocess.run([sys.executable, str(scan_index), "--root", str(root)], check=True, capture_output=True)
    index_path = root / ".cache" / "codemap" / "dirty-project.json"
    before = index_path.read_bytes()

    dirty_result, dirty_code = cic.check_currency(index_path, root)
    (root / "service.yaml").write_bytes(b"target: package.module\n")
    config_result, config_code = cic.check_currency(index_path, root)
    wrong_root_result, wrong_root_code = cic.check_currency(index_path, tmp_path)

    assert (dirty_result["status"], dirty_code) == ("current", 0)
    assert (config_result["status"], config_code, config_result["changed_count"]) == ("stale", 1, 1)
    assert (wrong_root_result["status"], wrong_root_code) == ("stale", 1)
    assert "root" in wrong_root_result["reason"]
    assert index_path.read_bytes() == before
