"""Exercise default artifact authorization through the ledger CLI entrypoint."""

from pathlib import Path

import append_ledger as al
import pytest
from _hook_env import _symlinks_supported

_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="symlink creation unavailable")


@pytest.mark.parametrize("directory", [".temp", ".reports", ".experiments", ".developments", ".notes"])
@pytest.mark.parametrize("relative", [True, False])
def test_default_artifact_roots_append_without_git(
    directory: str, relative: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a Git marker, only artifact directories directly under cwd authorize staged appends."""
    monkeypatch.chdir(tmp_path)
    ledger = tmp_path / directory / "run/events.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"earlier\n")
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record")
    argument = ledger.relative_to(tmp_path) if relative else ledger

    assert al.main([str(argument)]) == 0
    assert ledger.read_bytes() == b"earlier\nrecord\n"
    assert not stage.exists()


@pytest.mark.parametrize("marker_file", [True, False])
def test_git_project_is_discovered_from_ledger_outside_cwd(
    marker_file: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both repository and worktree Git markers locate artifact roots from an absolute ledger path."""
    project = tmp_path / "project"
    project.mkdir()
    marker = project / ".git"
    marker.write_text("gitdir: elsewhere\n", encoding="utf-8") if marker_file else marker.mkdir()
    cwd = tmp_path / "elsewhere/src"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    ledger = project / ".reports/run/events.jsonl"
    ledger.parent.mkdir(parents=True)
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record\n")

    assert al.main([str(ledger)]) == 0
    assert ledger.read_bytes() == b"record\n"
    assert not stage.exists()


@pytest.mark.parametrize("directory", [".", ".claude", "plugins", "plugins/.reports", "nested/.temp"])
def test_default_roots_refuse_other_project_paths(
    directory: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A familiar artifact basename nested inside another directory grants no default authority."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    ledger = tmp_path / directory / "events.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"earlier\n")
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record\n")

    assert al.main([str(ledger)]) == 1
    assert "outside authorized roots" in capsys.readouterr().err
    assert ledger.read_bytes() == b"earlier\n"
    assert stage.read_bytes() == b"record\n"


def test_cwd_fallback_does_not_authorize_external_artifact_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unrelated .reports directory without a Git ancestor is outside the cwd project."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    ledger = tmp_path / ".reports/events.jsonl"
    ledger.parent.mkdir()
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record\n")

    assert al.main([str(ledger)]) == 1
    assert "outside authorized roots" in capsys.readouterr().err
    assert not ledger.exists()
    assert stage.read_bytes() == b"record\n"


@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("relative", [".reports/nested/../events.jsonl", ".reports/../events.jsonl"])
def test_parent_traversal_ledger_is_refused(
    explicit: bool, relative: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parent traversal is refused even when normalization would leave the ledger inside a permitted root."""
    monkeypatch.chdir(tmp_path)
    run = tmp_path / ".reports"
    (run / "nested").mkdir(parents=True)
    argument = tmp_path / relative
    ledger = argument.resolve()
    ledger.write_bytes(b"earlier\n")
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record\n")
    roots = ["--run-root", str(run)] if explicit else []

    assert al.main([str(argument), *roots]) == 1
    assert ledger.read_bytes() == b"earlier\n"
    assert stage.read_bytes() == b"record\n"


@_symlinks
@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("linked", ["ledger", "parent", "source"])
def test_linked_paths_are_refused_in_both_authorization_modes(
    explicit: bool, linked: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linked ledger files, parent directories and read-only sources cannot bypass either mode."""
    monkeypatch.chdir(tmp_path)
    run = tmp_path / ".reports"
    run.mkdir()
    ledger = run / "events.jsonl"
    ledger.write_bytes(b"earlier\n")
    source = run / "source.jsonl"
    source.write_bytes(b"record\n")
    link = run / "linked"
    link.symlink_to({"ledger": ledger, "parent": run, "source": source}[linked], target_is_directory=linked == "parent")
    ledger_arg = {"ledger": link, "parent": link / ledger.name, "source": ledger}[linked]
    source_arg = link if linked == "source" else source
    roots = ["--run-root", str(run)] if explicit else []

    assert al.main([str(ledger_arg), *roots, "--from", str(source_arg)]) == 1
    assert ledger.read_bytes() == b"earlier\n"
    assert source.read_bytes() == b"record\n"


@_symlinks
def test_symlinked_default_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A default artifact root cannot redirect appends through a directory link."""
    monkeypatch.chdir(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    ledger = outside / "events.jsonl"
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record\n")
    (tmp_path / ".reports").symlink_to(outside, target_is_directory=True)

    assert al.main([str(tmp_path / ".reports" / ledger.name)]) == 1
    assert not ledger.exists()
    assert stage.read_bytes() == b"record\n"


def test_default_roots_allow_cross_artifact_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing result files in another default root append in argument order and remain available."""
    monkeypatch.chdir(tmp_path)
    ledger = tmp_path / ".notes/history.jsonl"
    ledger.parent.mkdir()
    source = tmp_path / ".reports/run/result.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"result")

    assert al.main([str(ledger), "--from", str(source)]) == 0
    assert ledger.read_bytes() == b"result\n"
    assert source.read_bytes() == b"result"


@pytest.mark.parametrize("authorize_source", [True, False])
def test_default_mode_requires_source_authority_outside_artifacts(
    authorize_source: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration source outside default roots is read only with explicit source authorization."""
    monkeypatch.chdir(tmp_path)
    ledger = tmp_path / ".reports/events.jsonl"
    ledger.parent.mkdir()
    source = tmp_path / "legacy.jsonl"
    source.write_bytes(b"legacy\n")
    roots = ["--source-root", str(tmp_path)] if authorize_source else []

    assert al.main([str(ledger), *roots, "--from", str(source)]) == (0 if authorize_source else 1)
    assert ledger.exists() == authorize_source
    assert source.read_bytes() == b"legacy\n"


def test_explicit_run_root_replaces_default_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Explicit authorization cannot inherit another default directory for writes or staged deletion."""
    monkeypatch.chdir(tmp_path)
    run = tmp_path / ".reports/run"
    run.mkdir(parents=True)
    ledger = tmp_path / ".notes/events.jsonl"
    ledger.parent.mkdir()
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record\n")

    assert al.main([str(ledger), "--run-root", str(run)]) == 1
    assert "outside authorized roots" in capsys.readouterr().err
    assert not ledger.exists()
    assert stage.read_bytes() == b"record\n"


def test_default_mode_preserves_shared_read_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default authorization retains the input cap before appending or deleting stages."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(al, "MAX_INPUT_BYTES", 8)
    ledger = tmp_path / ".reports/events.jsonl"
    ledger.parent.mkdir()
    ledger.write_bytes(b"earlier\n")
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"123456789")

    assert al.main([str(ledger)]) == 1
    assert "E_INPUT_TOO_LARGE" in capsys.readouterr().err
    assert ledger.read_bytes() == b"earlier\n"
    assert stage.read_bytes() == b"123456789"


@_symlinks
@pytest.mark.parametrize("linked_target", ["ledger", "source"])
def test_default_root_link_cannot_authorize_its_target(
    linked_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving an unused default root link cannot authorize its external target for reads or writes."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".reports").symlink_to(outside, target_is_directory=True)
    notes = project / ".notes"
    notes.mkdir()
    ledger = (outside if linked_target == "ledger" else notes) / "events.jsonl"
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record\n")
    source = outside / "source.jsonl"
    source.write_bytes(b"secret\n")
    args = ["--from", str(source)] if linked_target == "source" else []

    assert al.main([str(ledger), *args]) == 1
    assert not ledger.exists()
    assert stage.read_bytes() == b"record\n"
    assert source.read_bytes() == b"secret\n"


def test_default_artifact_root_must_be_a_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ordinary file named after an artifact directory is not an authorized ledger."""
    monkeypatch.chdir(tmp_path)
    ledger = tmp_path / ".reports"
    ledger.write_bytes(b"earlier\n")
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record\n")

    assert al.main([str(ledger)]) == 1
    assert ledger.read_bytes() == b"earlier\n"
    assert stage.read_bytes() == b"record\n"
