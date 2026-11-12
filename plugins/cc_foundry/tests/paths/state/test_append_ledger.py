"""Tests for the append_ledger bin script.

Covers the append-only contract: staged ``.rec`` files land at the end of the ledger, earlier records are never
rewritten, staged files are consumed, and ``--from`` sources are appended but kept.
"""

from __future__ import annotations

from pathlib import Path

import append_ledger as al
import pytest
from _hook_env import _symlinks_supported

_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="symlink creation unavailable")


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Ledger holding one earlier record, as a run would have after its first iteration."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".reports" / "experiments.jsonl"
    path.parent.mkdir()
    path.write_bytes(b'{"iteration":0}\n')
    return path


class TestStagedRecords:
    """Covers the ``<ledger>.rec`` staging path that skills use for model-valued records."""

    def test_appends_after_existing_records(self, ledger: Path) -> None:
        """A staged record lands after the earlier ones, which stay byte-identical.

        This is the whole point of the ledger contract: history is never re-emitted, so it cannot be dropped or
        reworded.
        """
        ledger.with_name("experiments.jsonl.rec").write_bytes(b'{"iteration":1}\n')
        assert al.main([str(ledger)]) == 0
        assert ledger.read_bytes() == b'{"iteration":0}\n{"iteration":1}\n'

    def test_consumes_staged_file(self, ledger: Path) -> None:
        """The staged file is deleted, so the next Write creates it fresh instead of overwriting an unread file."""
        staged = ledger.with_name("experiments.jsonl.rec")
        staged.write_bytes(b'{"iteration":1}\n')
        al.main([str(ledger), "--run-root", str(ledger.parent)])
        assert not staged.exists()

    def test_terminates_record_missing_newline(self, ledger: Path) -> None:
        """A record saved without a trailing newline cannot glue the next record onto its line.

        The Write tool does not guarantee a final newline; two flushes in a row must still yield two JSONL lines.
        """
        staged = ledger.with_name("experiments.jsonl.rec")
        staged.write_bytes(b'{"iteration":1}')
        al.main([str(ledger), "--run-root", str(ledger.parent)])
        staged.write_bytes(b'{"iteration":2}')
        al.main([str(ledger), "--run-root", str(ledger.parent)])
        assert ledger.read_bytes().splitlines() == [b'{"iteration":0}', b'{"iteration":1}', b'{"iteration":2}']

    def test_sweeps_parallel_writer_files_in_name_order(self, ledger: Path) -> None:
        """Each parallel writer stages its own file; one call appends all of them, sorted by writer name.

        Writers `a` and `b` stage in reverse order on disk; the ledger must still show `a` before `b`, so the result
        never depends on directory listing order.
        """
        ledger.with_name("experiments.jsonl.b.rec").write_bytes(b'{"w":"b"}\n')
        ledger.with_name("experiments.jsonl.a.rec").write_bytes(b'{"w":"a"}\n')
        al.main([str(ledger), "--run-root", str(ledger.parent)])
        assert ledger.read_bytes().splitlines()[1:] == [b'{"w":"a"}', b'{"w":"b"}']

    def test_creates_absent_ledger(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The first staged record creates the ledger, so callers need no separate initial Write.

        The ledger path does not exist yet, only its parent directory does; the flush must create the file itself.
        """
        monkeypatch.chdir(tmp_path)
        ledger = tmp_path / ".reports" / "nested" / "log.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.with_name("log.jsonl.rec").write_bytes(b"x\n")
        assert al.main([str(ledger), "--run-root", str(ledger.parent)]) == 0
        assert ledger.read_bytes() == b"x\n"

    def test_nothing_staged_is_a_clean_no_op(self, ledger: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A flush with no staged record exits 0 and leaves the ledger untouched.

        A skill may flush on every iteration even when a step staged nothing; that must be a harmless no-op that reports
        it, not an error or an empty-line append.
        """
        assert al.main([str(ledger), "--run-root", str(ledger.parent)]) == 0
        assert ledger.read_bytes() == b'{"iteration":0}\n'
        assert "nothing staged" in capsys.readouterr().out


class TestFromSources:
    """Covers ``--from``, used when the records already exist as files on disk."""

    def test_appends_sources_and_keeps_them(self, ledger: Path) -> None:
        """Source files are appended in argument order and left in place for their own run directory.

        The first source lacks a trailing newline; its record must still end up on its own line before the second.
        """
        first, second = ledger.parent / "a.jsonl", ledger.parent / "b.jsonl"
        first.write_bytes(b'{"t":"a"}')
        second.write_bytes(b'{"t":"b"}\n')
        assert al.main([str(ledger), "--run-root", str(ledger.parent), "--from", str(first), str(second)]) == 0
        assert ledger.read_bytes().splitlines()[1:] == [b'{"t":"a"}', b'{"t":"b"}']
        assert first.exists()

    def test_skips_missing_source(self, ledger: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unmatched shell glob arrives as a literal path; it is reported, not fatal.

        When no run produced a result file the shell passes the pattern through unexpanded; the flush must warn on
        stderr and still exit 0 so an empty batch does not abort the skill.
        """
        assert (
            al.main(
                [str(ledger), "--run-root", str(ledger.parent), "--from", str(ledger.parent / "*" / "result.jsonl")]
            )
            == 0
        )
        assert "skipped missing source" in capsys.readouterr().err

    def test_leaves_staged_records_alone(self, ledger: Path) -> None:
        """A ``--from`` call never consumes a ``.rec`` another step staged for its own flush.

        Only the plain staged-flush form owns `.rec` files; mixing the two forms must not eat a pending record.
        """
        staged = ledger.with_name("experiments.jsonl.rec")
        staged.write_bytes(b"pending\n")
        source = ledger.parent / "r.jsonl"
        source.write_bytes(b"r\n")
        al.main([str(ledger), "--run-root", str(ledger.parent), "--from", str(source)])
        assert staged.exists()


@_symlinks
@pytest.mark.parametrize("linked", ["ledger", "source", "parent"])
def test_linked_paths_are_refused(linked: str, tmp_path: Path) -> None:
    """Linked ledgers, sources and ledger directories never copy or append another file's bytes."""
    target = tmp_path / "target"
    target.mkdir()
    ledger = target / "ledger.jsonl"
    source = target / "source.jsonl"
    ledger.write_bytes(b"before\n")
    source.write_bytes(b"secret\n")
    paths = {"ledger": ledger, "source": source, "parent": target}
    link = tmp_path / "linked"
    link.symlink_to(paths[linked], target_is_directory=linked == "parent")
    ledger_arg = {"ledger": link, "source": ledger, "parent": link / ledger.name}[linked]
    source_arg = link if linked == "source" else source
    assert al.main(["--run-root", str(tmp_path), str(ledger_arg), "--from", str(source_arg)]) == 1
    assert ledger.read_bytes() == b"before\n"


def test_missing_ledger_parent_is_not_created(tmp_path: Path) -> None:
    """A mistaken ledger path cannot create directories outside an already prepared run."""
    ledger = tmp_path / "missing" / "ledger.jsonl"
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"record\n")
    assert al.main(["--run-root", str(ledger.parent), str(ledger), "--from", str(source)]) == 1
    assert not ledger.parent.exists()


def test_instruction_source_is_not_a_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ledger variable pointing into plugin sources cannot append a staged record there."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(al.tempfile, "gettempdir", lambda: str(tmp_path / "system-temp"))
    ledger = tmp_path / "plugins" / "cc_demo" / "bin" / "helper.py"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"instructions\n")
    staged = ledger.with_name(ledger.name + ".rec")
    staged.write_bytes(b"record\n")
    assert al.main(["--run-root", str(ledger.parent), str(ledger)]) == 1
    assert ledger.read_bytes() == b"instructions\n"
    assert staged.exists()


@pytest.mark.parametrize(
    "directory", [".reports/run", ".temp/state", ".notes/logs", ".experiments/run", ".developments/run"]
)
def test_project_artifact_ledgers_remain_appendable(
    directory: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run ledgers and cross-run calibration history retain the same append-only behavior."""
    monkeypatch.chdir(tmp_path)
    ledger = tmp_path / directory / "events.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.with_name(ledger.name + ".rec").write_bytes(b"record\n")
    assert al.main([str(ledger)]) == 0
    assert ledger.read_bytes() == b"record\n"


@pytest.mark.parametrize("cwd_relative", [".experiments/run/worktrees/variant", "src/package"])
def test_artifact_ledger_above_cwd_remains_appendable(
    cwd_relative: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute artifact ledger remains appendable from a worktree or project subdirectory."""
    project = tmp_path / "project"
    cwd = project / cwd_relative
    cwd.mkdir(parents=True)
    (project / ".git").mkdir()
    ledger = project / ".experiments" / "run" / "metric-runs.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.with_name(ledger.name + ".rec").write_bytes(b"record\n")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(al.tempfile, "gettempdir", lambda: str(tmp_path / "system-temp"))

    assert al.main([str(ledger)]) == 0
    assert ledger.read_bytes() == b"record\n"


def test_nonartifact_ledger_outside_cwd_and_temp_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing cwd cannot admit an ordinary project file as a ledger."""
    project = tmp_path / "project"
    cwd = project / "src"
    cwd.mkdir(parents=True)
    ledger = project / "events.jsonl"
    staged = ledger.with_name(ledger.name + ".rec")
    staged.write_bytes(b"record\n")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(al.tempfile, "gettempdir", lambda: str(tmp_path / "system-temp"))

    assert al.main(["--run-root", str(ledger.parent), str(ledger)]) == 1
    assert staged.exists()
