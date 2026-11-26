"""Real-Git acceptance checks for optional gate source binding.

Acceptance tests drive ``shared/run_gates.py`` against real temporary Git repositories, because the contract is the
observable receipt, exit status and side effects of that runner. Controlled Git-output boundary checks additionally
exercise filename framing and inspection failures without depending on host filename capabilities.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from _gate_runner_support import (
    CODEX_TRAILER,
    PLUGIN_ROOT,
    RUN_GATES,
    _foreign_file,
    _gate_check,
    _git,
    _git_steps,
    _initialize_repository,
    _interrupt_before_install,
    _listing,
    _load_run_gates,
    _load_validator,
    _marker_command,
    _passing_proof,
    _PendingPublication,
    _python_command,
    _receipt_text,
    _rewrite_after_manifest_install,
    _run,
    _run_lint_gate,
    _skip_gates,
    _swap_directory,
)
from _modules import load_module
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, FILE_SYMLINKS_AVAILABLE, HARD_LINKS_AVAILABLE

SOURCE_DIRTY_STATUS = "source-dirty"
HEAD_MISMATCH = "source-head-mismatch"
INVALID_FAIL_EXIT_CODE = "gate-check-invalid-fail-exit-code"


def test_free_form_retry_preserves_unreferenced_inventories(tmp_path: Path) -> None:
    """Inventory-shaped files outside the prior receipt keep their public names and stay out of its archive."""
    output = tmp_path / "review"
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('first')")).returncode == 0
    matched = hashlib.sha256(b"other owner").hexdigest()
    foreign = [output / "checks" / f"tracked-source-{digest}.bin" for digest in ("a" * 64, matched)]
    for path in foreign:
        path.write_bytes(b"other owner")
    identities = [path.stat() for path in foreign]
    retry = _run_lint_gate(tmp_path, output, None, _python_command("print('second')"))
    assert retry.returncode == 0, retry.stderr
    archive = output / "gate-attempts/001"
    for path, identity in zip(foreign, identities):
        assert path.is_file()
        assert os.path.samestat(identity, path.stat())
        assert path.read_bytes() == b"other owner"
        assert not (archive / "checks" / path.name).exists()
    assert not (archive / "tracked-source-errors.json").exists()
    assert _load_validator()._validate_gates(archive)["checks_failed"] == []


def test_candidate_ignores_inventory_files_absent_from_its_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreferenced staged files cannot install new inputs or displace an intact public input."""
    output = tmp_path / "review"
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('first')")).returncode == 0
    public_bytes = b"retained input"
    name = f"tracked-source-{hashlib.sha256(public_bytes).hexdigest()}.bin"
    public = output / "checks" / name
    public.write_bytes(public_bytes)
    identity = public.stat()
    extra = f"tracked-source-{hashlib.sha256(b'new input').hexdigest()}.bin"
    gates = _load_run_gates(monkeypatch)
    original = gates.run_check

    def capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
        record = original(*args, **kwargs)
        if record["id"] == "review":
            staged_checks = args[4] / "checks"
            (staged_checks / name).write_bytes(b"different staged bytes")
            (staged_checks / extra).write_bytes(b"new input")
        return record

    monkeypatch.setattr(gates, "run_check", capture)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(RUN_GATES), "--out", str(output), "--lint", _python_command("print('second')"), *_skip_gates("lint")],
    )
    assert gates.main() == 0
    assert public.read_bytes() == public_bytes
    assert os.path.samestat(identity, public.stat())
    assert not (output / "checks" / extra).exists()
    assert _load_validator()._validate_gates(output / "gate-attempts/001")["checks_failed"] == []


@pytest.mark.parametrize("failure", ["interrupted", "rejected"])
def test_unpublished_structured_attempt_keeps_inventory_private(
    repository: Path, output: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    """A stopped or invalid candidate leaves the previous report intact and no new public inventory."""
    (repository / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "test_example.py").write_text("import example\ndef test_value(): assert example.VALUE == 1\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", f"test fixture\n\n{CODEX_TRAILER}")
    assert _run_lint_gate(repository, output, None, _python_command("print('prior')")).returncode == 0
    previous = (output / "gates.json").read_bytes()
    command = TestPytestGateRecovery._tests_gate_command(
        output,
        repository,
        _git(repository, "rev-parse", "HEAD"),
        sys.executable,
        "--pytest-args-json",
        json.dumps(["-q", "-p", "no:xdist", "-p", "no:cacheprovider"]),
    )
    gates = _load_run_gates(monkeypatch)
    validate = gates._validate_staged_gates
    visited = []

    def stop_candidate(stage: Path, *args: Any) -> dict[str, Any]:
        if stage.parent == output:
            assert _gate_check(stage, "tests")["python_imports"]["status"] == "pass"
            inventory = next((stage / "checks").glob("tracked-source-*.bin"))
            assert not (output / "checks" / inventory.name).exists()
            visited.append(inventory.name)
            if failure == "interrupted":
                raise InterruptedError("candidate validation interrupted")
            inventory.write_bytes(b"invalid candidate")
        return validate(stage, *args)

    with monkeypatch.context() as patch:
        patch.setattr(sys, "argv", command[1:])
        patch.setattr(gates, "_validate_staged_gates", stop_candidate)
        assert gates.main() == 2
    diagnostic = "candidate validation interrupted" if failure == "interrupted" else "gate-evidence-digest-mismatch"
    assert diagnostic in capsys.readouterr().err
    assert len(visited) == 1
    public = output / "checks" / visited[0]
    assert not public.exists()
    assert (output / "gates.json").read_bytes() == previous
    assert not (output / "gate-attempts/001").exists()
    validator = _load_validator()
    assert validator._validate_gates(output)["checks_failed"] == []
    retry = _run(command)
    assert retry.returncode == 0, retry.stderr
    current = validator._validate_gates(output)
    check = next(row for row in current["checks"] if row["id"] == "tests")
    assert check["source"]["artifacts"][f"checks/{public.name}"] == hashlib.sha256(public.read_bytes()).hexdigest()
    assert validator._validate_gates(output / "gate-attempts/001")["checks_failed"] == []
    assert (output / "gate-attempts/001/gates.json").read_bytes() == previous


@pytest.mark.parametrize(
    "missing",
    [
        pytest.param("gates.txt", id="gate-statuses"),
        pytest.param("failed.txt", id="failure-list"),
        pytest.param("gates.checks.jsonl", id="gate-records"),
    ],
)
def test_retry_requires_all_prior_summaries(tmp_path: Path, missing: str) -> None:
    """A missing summary blocks commands and archive publication until the complete previous attempt is available."""
    output = tmp_path / "review"
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('first')")).returncode == 0
    summaries = {name: (output / name).read_bytes() for name in ("gates.txt", "failed.txt", "gates.checks.jsonl")}
    prior = (output / "gates.json").read_bytes()
    removed = output / missing
    removed.unlink()
    marker = tmp_path / "second-ran"
    retry = _run_lint_gate(tmp_path, output, None, _marker_command(marker))
    assert retry.returncode == 2, retry.stderr
    assert missing in retry.stderr
    assert not marker.exists()
    assert (output / "gates.json").read_bytes() == prior
    assert not (output / "gate-attempts/001").exists()
    removed.write_bytes(summaries[missing])
    retry = _run_lint_gate(tmp_path, output, None, _marker_command(marker))
    assert retry.returncode == 0, retry.stderr
    assert marker.is_file()
    archive = output / "gate-attempts/001"
    assert (archive / "gates.json").read_bytes() == prior
    assert {name: (archive / name).read_bytes() for name in summaries} == summaries


@pytest.mark.parametrize("reason", [pytest.param("", id="empty"), pytest.param(" \t ", id="whitespace")])
def test_explicit_blank_skip_reason_is_rejected_before_execution(tmp_path: Path, reason: str) -> None:
    """A supplied blank reason rejects the command line before another gate can have side effects."""
    output = tmp_path / "review"
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('first')")).returncode == 0
    prior = (output / "gates.json").read_bytes()
    marker = tmp_path / "lint-ran"
    args = [
        sys.executable,
        str(RUN_GATES),
        "--out",
        str(output),
        "--lint",
        _marker_command(marker),
        "--format",
        _python_command("print('format')"),
        *_skip_gates("lint"),
    ]
    args[args.index("--skip-format") + 1] = reason
    rejected = _run(args, cwd=tmp_path)
    assert not marker.exists()
    assert rejected.returncode == 2, rejected.stderr
    assert "--skip-format" in rejected.stderr
    assert "invalid-skip-reason" in rejected.stderr
    assert (output / "gates.json").read_bytes() == prior
    assert not (output / "gate-attempts/001").exists()
    args[args.index("--skip-format") + 1] = "explicitly skipped"
    assert _run(args, cwd=tmp_path).returncode == 0
    assert marker.exists()
    assert (output / "gate-attempts/001/gates.json").read_bytes() == prior


def test_running_retry_keeps_previous_report_complete(tmp_path: Path) -> None:
    """The second command can read all of the first receipt before the second attempt is published."""
    output = tmp_path / "review"
    first = _run_lint_gate(tmp_path, output, None, _python_command("print('previous')"))
    assert first.returncode == 0, first.stderr
    check = _gate_check(output, "lint")
    paths = [output / name for name in ("gates.json", check["command_path"], check["stdout"], check["stderr"])]
    expected = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    competing = [sys.executable, str(RUN_GATES), "--out", str(output), "--skip-lint", "unused", *_skip_gates("lint")]
    probe = _python_command(
        "import hashlib, subprocess; from pathlib import Path; "
        f"other = subprocess.run({competing!r}, capture_output=True); "
        "assert other.returncode == 2 and b'gate-writer-busy' in other.stderr; "
        f"saved = {expected!r}; assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==s for p,s in saved.items()); "
        f"assert not Path({str(output / '.gate-publication.json')!r}).exists(); print('next')"
    )
    second = _run_lint_gate(tmp_path, output, None, probe)
    assert second.returncode == 0, second.stderr
    validator = _load_validator()
    assert validator._validate_gates(output)["checks_failed"] == []
    assert validator._validate_gates(output / "gate-attempts/001")["checks_failed"] == []


def test_damage_marker_created_during_gate_execution_blocks_publication(tmp_path: Path) -> None:
    """A marker appearing after preflight keeps its bytes and prevents a successful unreadable publication."""
    output = tmp_path / "review"
    marker = output / "gate-log-errors.json"
    result = _run_lint_gate(tmp_path, output, None, _marker_command(marker))
    assert result.returncode == 2, result.stderr
    assert marker.read_bytes() == b"ran"
    assert not (output / "gates.json").exists()
    assert not (output / ".gate-publication.json").exists()
    with pytest.raises(SystemExit, match="gate-evidence-damaged-gate-log"):
        _load_validator()._validate_gates(output)


def test_damage_marker_appearing_during_install_keeps_journal_until_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public reader refuses a candidate interrupted by a new damage marker during manifest installation."""
    output = tmp_path / "review"
    marker = output / "gate-log-errors.json"
    runner = _load_run_gates(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(RUN_GATES), "--out", str(output), "--lint", _python_command("print('first')"), *_skip_gates("lint")],
    )
    replace = os.replace

    def mark_installed_candidate(source: str | Path, destination: str | Path) -> None:
        """Model an independent marker writer at the installation boundary."""
        replace(source, destination)
        if Path(destination) == output / "gates.json":
            marker.write_bytes(b"independent damage report")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", mark_installed_candidate)
        assert runner.main() == 2
    journal = output / ".gate-publication.json"
    assert journal.exists()
    assert marker.read_bytes() == b"independent damage report"
    validator = _load_validator()
    with pytest.raises(SystemExit, match="gate-publication-in-progress"):
        validator._validate_gates(output)
    second = tmp_path / "second-command"
    assert _run_lint_gate(tmp_path, output, None, _marker_command(second)).returncode == 2
    assert not second.exists()
    assert marker.read_bytes() == b"independent damage report"
    marker.rename(tmp_path / "retained-damage-report")
    retry = _run_lint_gate(tmp_path, output, None, _marker_command(second))
    assert retry.returncode == 0, retry.stderr
    assert second.exists()
    assert not journal.exists()
    assert validator._validate_gates(output / "gate-attempts/001")["status"] == "pass"
    assert validator._validate_gates(output)["status"] == "pass"


def test_interrupted_publication_recovers_without_mixed_completed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed file install withdraws the old manifest and the next writer finishes the authenticated candidate."""
    output = tmp_path / "review"
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('old')")).returncode == 0
    prior = (output / "gates.json").read_bytes()
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(RUN_GATES), "--out", str(output), "--lint", _python_command("print('second')"), *_skip_gates("lint")],
    )
    replace = os.replace

    def interrupt(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == output / "checks/lint.stdout.txt":
            raise OSError("interrupted-publication")
        replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt)
        assert gates.main() == 2
    assert (output / ".gate-publication.json").is_file()
    assert not (output / "gates.json").exists()
    assert (output / "gate-attempts/001/gates.json").read_bytes() == prior
    validator = _load_validator()
    with pytest.raises(SystemExit):
        validator._validate_gates(output)
    assert validator._validate_gates(output / "gate-attempts/001")["checks_failed"] == []
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('third')")).returncode == 0
    assert not (output / ".gate-publication.json").exists()
    assert validator._validate_gates(output / "gate-attempts/002")["checks_failed"] == []
    assert (output / "gate-attempts/002/checks/lint.stdout.txt").read_text().strip() == "second"
    assert validator._validate_gates(output)["checks_failed"] == []


def test_resume_rechecks_inventory_membership_from_the_candidate_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An older sealed plan containing an extra inventory must stop before it changes the previous attempt."""
    output = tmp_path / "review"
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('first')")).returncode == 0
    prior = (output / "gates.json").read_bytes()
    content = b"older public input"
    name = f"checks/tracked-source-{hashlib.sha256(content).hexdigest()}.bin"
    public = output / name
    public.write_bytes(content)
    identity = public.stat()
    gates = _load_run_gates(monkeypatch)
    finish = gates._finish_publication
    marker = output / ".gate-publication.json"

    def stop_before_install(root: Path) -> None:
        if marker.exists():
            raise OSError("simulated interruption")
        finish(root)

    monkeypatch.setattr(
        sys,
        "argv",
        [str(RUN_GATES), "--out", str(output), "--lint", _python_command("print('second')"), *_skip_gates("lint")],
    )
    with monkeypatch.context() as patch:
        patch.setattr(gates, "_finish_publication", stop_before_install)
        assert gates.main() == 2
    owner = json.loads(marker.read_bytes())
    stage = output / owner["stage"]
    plan_path = stage / "publication.json"
    plan = json.loads(plan_path.read_bytes())
    (stage / name).write_bytes(b"unreferenced bytes")
    plan["before"][name] = gates._file_state(public)
    plan["after"][name] = hashlib.sha256((stage / name).read_bytes()).hexdigest()
    plan_path.write_bytes(json.dumps(plan).encode())
    owner["sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    marker.write_bytes(json.dumps(owner).encode())
    capsys.readouterr()
    assert gates.main() == 2
    assert "gate-recovery-blocked:gate-publication-unowned-inventory" in capsys.readouterr().err
    assert marker.is_file()
    assert (output / "gates.json").read_bytes() == prior
    assert os.path.samestat(identity, public.stat())
    assert public.read_bytes() == content


def _occupy_archive_slot(pending: _PendingPublication, outside: Path) -> Callable[[], object]:
    """Fill the numbered history slot sealed into the journal with another owner's file."""
    return _foreign_file(pending.output / "gate-attempts/001/occupant")


def _change_staged_log(pending: _PendingPublication, outside: Path) -> Callable[[], object]:
    """Rewrite a staged log the receipt does not authenticate, after the journal sealed its digest."""
    (pending.stage / "checks/lint.stdout.txt").write_bytes(b"changed candidate\n")
    return (pending.output / "checks/lint.stdout.txt").read_bytes


def _occupy_withdrawn_manifest(pending: _PendingPublication, outside: Path) -> Callable[[], object]:
    """Place another owner's file at the private name that receives the withdrawn live manifest."""
    return _foreign_file(pending.stage / "previous-manifest")


def _redirect_history_root(pending: _PendingPublication, outside: Path) -> Callable[[], object]:
    """Replace the empty history root created for this archive by a link to an outside directory."""
    history = pending.output / "gate-attempts"
    history.rmdir()
    history.symlink_to(outside, target_is_directory=True)
    return _listing(outside)


def _redirect_archive_slot(pending: _PendingPublication, outside: Path) -> Callable[[], object]:
    """Place a link to an outside directory at the numbered history slot sealed into the journal."""
    (pending.output / "gate-attempts/001").symlink_to(outside, target_is_directory=True)
    return _listing(outside)


_NEEDS_DIRECTORY_LINKS = pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="host cannot link directories")


@pytest.mark.parametrize(
    ("arrange", "code"),
    [
        pytest.param(_occupy_archive_slot, "gate-publication-archive-occupied", id="archive-slot-occupied"),
        pytest.param(_change_staged_log, "gate-publication-candidate-changed", id="staged-member-changed"),
        pytest.param(
            _occupy_withdrawn_manifest, "gate-publication-previous-manifest-changed", id="withdrawn-manifest-occupied"
        ),
        pytest.param(
            _redirect_history_root,
            "gate-attempts-directory-redirected",
            marks=_NEEDS_DIRECTORY_LINKS,
            id="history-root-redirected",
        ),
        pytest.param(
            _redirect_archive_slot,
            "gate-attempts-directory-redirected",
            marks=_NEEDS_DIRECTORY_LINKS,
            id="archive-slot-redirected",
        ),
    ],
)
def test_resumed_publication_refuses_changed_journal_state(
    pending_lint_retry: _PendingPublication,
    outside_directory: Path,
    arrange: Callable[[_PendingPublication, Path], Callable[[], object]],
    code: str,
) -> None:
    """Each resume-time guard stops with its own code before the live receipt or another owner's state changes.

    The sealed journal names a candidate, an archive slot and private staging names. Recovery that finds any of them
    changed after sealing must refuse to install, keep the journal for inspection, and leave foreign files and
    directories, including the target of a redirected history path, exactly as it found them.
    """
    pending = pending_lint_retry
    observe = arrange(pending, outside_directory)
    expected = observe()
    with pytest.raises(ValueError, match=f"^{re.escape(code)}$"):
        pending.gates._finish_publication(pending.output)
    assert observe() == expected
    assert (pending.output / "gates.json").read_bytes() == pending.prior
    assert (pending.output / ".gate-publication.json").is_file()


@_NEEDS_DIRECTORY_LINKS
def test_resumed_publication_refuses_redirected_relocated_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside_directory: Path
) -> None:
    """An archive already moved into history and then replaced by a link stops recovery without reading through it.

    Installation stopped after the previous attempt was relocated and the old manifest withdrawn. If the relocated
    archive path then becomes a link, resuming must refuse it by name instead of verifying or moving its target.
    """
    output = tmp_path / "review"
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('first')")).returncode == 0
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(RUN_GATES), "--out", str(output), "--lint", _python_command("print('second')"), *_skip_gates("lint")],
    )
    replace = os.replace

    def interrupt(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == output / "checks/lint.stdout.txt":
            raise OSError("interrupted-publication")
        replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt)
        assert gates.main() == 2
    archived = output / "gate-attempts/001"
    archived.rename(outside_directory / "001")
    archived.symlink_to(outside_directory / "001", target_is_directory=True)
    relocated = _listing(outside_directory / "001")
    expected = relocated()
    with pytest.raises(ValueError, match=r"^gate-attempts-directory-redirected$"):
        gates._finish_publication(output)
    assert relocated() == expected
    assert (output / ".gate-publication.json").is_file()


@_NEEDS_DIRECTORY_LINKS
def test_retry_refuses_redirected_history_root_before_execution(tmp_path: Path, outside_directory: Path) -> None:
    """A linked history root stops the retry before any command runs or the previous receipt moves through it."""
    output = tmp_path / "review"
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('first')")).returncode == 0
    prior = (output / "gates.json").read_bytes()
    (output / "gate-attempts").symlink_to(outside_directory, target_is_directory=True)
    marker = tmp_path / "second-ran"
    retry = _run_lint_gate(tmp_path, output, None, _marker_command(marker))
    assert retry.returncode == 2, retry.stderr
    assert "gate-recovery-blocked:gate-attempts-directory-redirected" in retry.stderr
    assert not marker.exists()
    assert _listing(outside_directory)() == ["sentinel"]
    assert (output / "gates.json").read_bytes() == prior


def test_resumed_publication_keeps_occupied_retirement_name(
    repository: Path, head: str, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another owner's file at a retired member's private name stops recovery instead of being overwritten.

    A lint-only retry after a structured pytest attempt retires the published import proof into the stage. The
    retirement name must be free; an occupant there is foreign evidence and the live proof stays published.
    """
    (repository / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "test_example.py").write_text("import example\ndef test_value(): assert example.VALUE == 1\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", f"test fixture\n\n{CODEX_TRAILER}")
    command = TestPytestGateRecovery._tests_gate_command(
        output,
        repository,
        _git(repository, "rev-parse", "HEAD"),
        sys.executable,
        "--pytest-args-json",
        json.dumps(["-q", "-p", "no:xdist", "-p", "no:cacheprovider"]),
    )
    first = _run(command)
    assert first.returncode == 0, first.stderr
    proof = output / "checks/tests.python-imports.json"
    published = proof.read_bytes()
    gates = _load_run_gates(monkeypatch)
    argv = [str(RUN_GATES), "--out", str(output), "--lint", _python_command("print('lint')"), *_skip_gates("lint")]
    stage = _interrupt_before_install(gates, monkeypatch, output, argv)
    occupant = _foreign_file(stage / f"retired-{proof.name}")
    with pytest.raises(ValueError, match=r"^gate-publication-retired-member-occupied$"):
        gates._finish_publication(output)
    assert occupant() == b"foreign owner"
    assert proof.read_bytes() == published
    assert (output / ".gate-publication.json").is_file()


def test_publication_stops_when_its_journal_changes_during_installation(
    pending_lint_retry: _PendingPublication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal rewritten while members install is not this writer's journal, so it is never removed."""
    pending = pending_lint_retry
    marker = pending.output / ".gate-publication.json"
    _rewrite_after_manifest_install(pending.output, marker, monkeypatch)
    with pytest.raises(ValueError, match=r"^gate-publication-marker-changed$"):
        pending.gates._finish_publication(pending.output)
    assert marker.read_bytes().endswith(b"\n")


def test_local_publication_stops_when_the_live_collector_receipt_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collector receipt changed during installation no longer matches the staged copy the candidate was bound to."""
    gate = _local_gate_inputs(tmp_path)
    gates = _load_run_gates(monkeypatch)
    _interrupt_before_install(gates, monkeypatch, gate.output, gate.args[1:])
    receipt = gate.output / "local-source/review-worktree.json"
    _rewrite_after_manifest_install(gate.output, receipt, monkeypatch)
    with pytest.raises(ValueError, match=r"^gate-publication-collector-changed$"):
        gates._finish_publication(gate.output)
    assert (gate.output / ".gate-publication.json").is_file()


def _child_proof(output: Path) -> Path:
    """Read the exact child submission path recorded by a structured pytest invocation."""
    command = _receipt_text(output, _gate_check(output, "tests"), "command_path")
    return Path(json.loads(command.removeprefix("argv-json:"))[5])


def _artifact_hashes(output: Path, check: dict[str, Any], *additional: str) -> dict[str, str]:
    """Calculate expected digests from the actual command, captured streams and any import proof."""
    paths = (check["command_path"], check["stdout"], check["stderr"], *additional)
    return {path: hashlib.sha256((output / path).read_bytes()).hexdigest() for path in paths}


def _edit_gates_json(output: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    """Rewrite ``gates.json`` after applying ``edit`` to its parsed payload."""
    path = output / "gates.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    edit(payload)
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


@pytest.fixture
def linked_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    """Return a committed ``original`` checkout, a detached ``selected`` worktree of the same commit, and that id."""
    original = tmp_path / "original"
    original.mkdir()
    expected_head = _initialize_repository(original)
    selected = tmp_path / "selected"
    _git(original, "worktree", "add", "--detach", str(selected), expected_head)
    return original, selected, expected_head


class TestPytestGateRecovery:
    """A source-bound test gate that cannot run pytest keeps its failure and says how review should continue."""

    @staticmethod
    def _tests_gate_command(
        output: Path, repository: Path, expected_head: str, interpreter: Path | str, *extra: str
    ) -> list[str]:
        """Build a source-bound ``--pytest-python`` invocation with every other gate declared out of scope."""
        return [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--expected-head",
            expected_head,
            "--worktree",
            str(repository),
            "--pytest-python",
            str(interpreter),
            "--pytest-import",
            "example",
            *extra,
            *_skip_gates("tests"),
        ]

    @staticmethod
    def _assert_failure_with_recovery(result: subprocess.CompletedProcess[str], output: Path, stderr_text: str) -> None:
        """Assert the tests gate failed, kept ``stderr_text`` and printed the dependency recovery guidance."""
        assert result.returncode == 1
        assert stderr_text in (output / "checks/tests.stderr.txt").read_text(encoding="utf-8")
        assert json.loads((output / "gates.json").read_text(encoding="utf-8"))["checks_failed"] == ["tests"]
        assert "dependency preparation" in result.stderr
        assert "continue independent review" in result.stderr

    def test_missing_pytest_keeps_failure_and_directs_recovery(
        self, tmp_path: Path, repository: Path, head: str, output: Path
    ) -> None:
        """A gate interpreter without pytest fails the tests gate and tells review work to continue.

        The runner is pointed at a fresh virtual environment that has no pytest installed; the import error must stay in
        the tests receipt instead of being masked by the recovery message.
        """
        environment = tmp_path / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True, capture_output=True
        )
        interpreter = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

        result = _run(self._tests_gate_command(output, repository, head, interpreter))

        self._assert_failure_with_recovery(result, output, "ModuleNotFoundError: No module named 'pytest'")

    def test_collection_import_failure_keeps_failure_and_directs_recovery(self, repository: Path, output: Path) -> None:
        """An import failure while pytest collects also keeps its failure and the recovery guidance.

        The committed ``conftest.py`` imports a module that does not exist, so pytest itself starts but cannot collect.
        """
        (repository / "conftest.py").write_text("import nonexistent_review_fixture_dependency\n", encoding="utf-8")
        _git(repository, "add", "conftest.py")
        _git(repository, "commit", "-m", f"collection fixture\n\n{CODEX_TRAILER}")
        head = _git(repository, "rev-parse", "HEAD")

        result = _run(
            self._tests_gate_command(
                output, repository, head, sys.executable, "--pytest-args-json", '["-p", "no:xdist"]'
            )
        )

        self._assert_failure_with_recovery(result, output, "nonexistent_review_fixture_dependency")


class TestProjectEnvironment:
    """The optional ``--project-env`` selects the checker environment without falling back to ``PATH``."""

    def test_checker_runs_with_project_env_first_on_path_in_selected_worktree(
        self, tmp_path: Path, repository: Path, head: str, output: Path
    ) -> None:
        """A declared checker sees the project environment first on ``PATH`` and runs in the selected worktree.

        The environment is a minimal fake with ``pyvenv.cfg`` and a placeholder interpreter; the checker asserts its own
        ``PATH`` and records its working directory so the receipt can be compared against the worktree.
        """
        environment = tmp_path / "project-env"
        environment.mkdir()
        (environment / "pyvenv.cfg").write_text("home = fixture\n", encoding="utf-8")
        scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
        scripts.mkdir(parents=True)
        (scripts / ("python.exe" if sys.platform == "win32" else "python")).write_bytes(b"placeholder")
        marker = tmp_path / "checker-cwd.txt"
        command = _python_command(
            f"import os; from pathlib import Path; "
            f"assert os.environ['PATH'].split(os.pathsep)[0] == {str(scripts)!r}; "
            f"Path({str(marker)!r}).write_text(str(Path.cwd()), encoding='utf-8')"
        )
        arguments = [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--expected-head",
            head,
            "--worktree",
            str(repository),
            "--project-env",
            str(environment),
            "--types",
            command,
            *_skip_gates("types"),
        ]

        result = _run(arguments, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert marker.read_text(encoding="utf-8") == str(repository)
        payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
        assert _gate_check(output, "types")["status"] == "pass"
        assert payload["source"]["project_env"] == environment.as_posix()

    def test_missing_environment_is_rejected_before_gates(
        self, tmp_path: Path, repository: Path, head: str, output: Path
    ) -> None:
        """A ``--project-env`` that does not exist is a prerequisite error, not a silent ``PATH`` fallback.

        The runner must exit with the usage status and write no receipts at all.
        """
        arguments = [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--expected-head",
            head,
            "--worktree",
            str(repository),
            "--project-env",
            str(tmp_path / "missing"),
        ]

        result = _run(arguments, cwd=tmp_path)

        assert result.returncode == 2
        assert "invalid-project-env" in result.stderr
        assert not output.exists()


class TestPythonCommand:
    """The test-local ``_python_command`` helper builds commands that run under every supported shell."""

    @pytest.mark.parametrize(
        ("executable", "expected_prefix"),
        [
            pytest.param(
                r"C:\Program Files\Python\python.exe",
                "& 'C:\\Program Files\\Python\\python.exe' -c ",
                id="powershell-spaced-executable",
            ),
            pytest.param(
                r"C:\Tools\Python's runtime\python.exe",
                "& 'C:\\Tools\\Python''s runtime\\python.exe' -c ",
                id="powershell-literal-apostrophe",
            ),
        ],
    )
    def test_windows_interpreter_path_uses_powershell_invocation(
        self, monkeypatch: pytest.MonkeyPatch, executable: str, expected_prefix: str
    ) -> None:
        """Interpreter paths with spaces or apostrophes are quoted for PowerShell on Windows.

        The command is built on any host by faking the platform and executable, so the Windows quoting stays covered on
        POSIX runners too.
        """
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "executable", executable)

        command = _python_command("pass")

        assert command.startswith(expected_prefix)
        assert "cGFzcw==" in command


class TestSourceGuard:
    """``--expected-head`` binds a gate command to the exact committed source before and after it runs."""

    @pytest.mark.parametrize("expected_head", [pytest.param("0" * 40, id="sha1"), pytest.param("0" * 64, id="sha256")])
    def test_wrong_expected_head_blocks_command_and_records_preblocked_receipt(
        self, tmp_path: Path, repository: Path, head: str, output: Path, expected_head: str
    ) -> None:
        """A checkout whose head differs from the expected one never runs the command and is recorded as pre-blocked.

        Both SHA-1 and SHA-256 length ids are accepted; the runner must fail the gate and keep the actual head in the
        receipt instead of executing a command against unreviewed source.
        """
        marker = repository / "ran.txt"

        result = _run_lint_gate(repository, output, expected_head, _marker_command(marker))

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert not marker.exists()
        assert check["status"] == "refused"
        assert check["source"] == {
            "expected_head": expected_head,
            "before": {"head": head, "status": "", "ignored": "[]"},
            "after": None,
        }
        assert HEAD_MISMATCH in _receipt_text(output, check)

    @pytest.mark.parametrize("exit_code", [0, 1, 7])
    def test_matching_head_runs_command_and_keeps_its_exit_status(
        self, repository: Path, head: str, output: Path, exit_code: int
    ) -> None:
        """On matching source the command runs and its native exit status is recorded unchanged.

        A zero exit passes the gate; any nonzero exit fails it while the exact code, and clean before and after source
        receipts, are retained.
        """
        result = _run_lint_gate(repository, output, head, _python_command(f"raise SystemExit({exit_code})"))

        check = _gate_check(output, "lint")
        assert result.returncode == (1 if exit_code else 0)
        assert check["status"] == ("fail" if exit_code else "pass")
        assert check["exit_code"] == exit_code
        assert check["source"] == {
            "expected_head": head,
            "before": {"head": head, "status": "", "ignored": "[]"},
            "after": {"head": head, "status": "", "ignored": "[]"},
            "artifacts": _artifact_hashes(output, check),
        }

    @pytest.mark.parametrize(
        ("mutation", "expected_status"),
        [
            pytest.param("tracked.txt", " M tracked.txt", id="tracked"),
            pytest.param("untracked.txt", "?? untracked.txt", id="untracked"),
        ],
    )
    def test_dirty_source_blocks_command_before_execution(
        self, repository: Path, head: str, output: Path, mutation: str, expected_status: str
    ) -> None:
        """A tracked edit or an untracked file blocks the gate before its command starts.

        The porcelain status of the dirty checkout is recorded as the ``before`` receipt and there is no ``after``.
        """
        (repository / mutation).write_text("changed\n", encoding="utf-8", newline="\n")
        marker = repository / "ran.txt"

        result = _run_lint_gate(repository, output, head, _marker_command(marker))

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert not marker.exists()
        assert check["status"] == "refused"
        assert check["source"] == {
            "expected_head": head,
            "before": {"head": head, "status": expected_status, "ignored": "[]"},
            "after": None,
        }
        assert SOURCE_DIRTY_STATUS in _receipt_text(output, check)

    def test_command_that_moves_head_fails_an_otherwise_passing_gate(
        self, repository: Path, head: str, output: Path
    ) -> None:
        """A command that commits during the run fails the gate even though it exits zero.

        The after-run receipt records the new head, so a gate cannot certify source it changed itself.
        """
        command = _python_command(
            "from pathlib import Path\n"
            "import subprocess\n"
            "Path('drift.txt').write_text('drift\\n', encoding='utf-8', newline='\\n')\n"
            "subprocess.run(['git', 'add', 'drift.txt'], check=True)\n"
            f"subprocess.run(['git', 'commit', '-m', 'drift\\n\\n{CODEX_TRAILER}'], check=True)\n"
        )

        result = _run_lint_gate(repository, output, head, command)

        actual_head = _git(repository, "rev-parse", "HEAD")
        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert actual_head != head
        assert check["status"] == "fail"
        assert check["exit_code"] == 0
        assert check["source"] == {
            "expected_head": head,
            "before": {"head": head, "status": "", "ignored": "[]"},
            "after": {"head": actual_head, "status": "", "ignored": "[]"},
            "artifacts": _artifact_hashes(output, check),
        }
        assert HEAD_MISMATCH in _receipt_text(output, check)

    def test_command_that_leaves_untracked_file_fails_gate_and_validator_rejects_edit(
        self, repository: Path, head: str, output: Path
    ) -> None:
        """A successful command that leaves an untracked file fails the gate, and the receipt cannot be repaired.

        The artifact validator accepts the failed record, then rejects the same record once its ``after`` source receipt
        is rewritten to look clean, because a failed gate with exit code zero needs the dirty evidence.
        """
        command = _python_command(
            "from pathlib import Path\nPath('during-gate.txt').write_text('changed\\n', encoding='utf-8', newline='\\n')"
        )

        result = _run_lint_gate(repository, output, head, command)

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert check["exit_code"] == 0
        assert check["source"] == {
            "expected_head": head,
            "before": {"head": head, "status": "", "ignored": "[]"},
            "after": {"head": head, "status": "?? during-gate.txt", "ignored": "[]"},
            "artifacts": _artifact_hashes(output, check),
        }
        assert SOURCE_DIRTY_STATUS in _receipt_text(output, check)
        validator = _load_validator()
        assert validator._validate_gates(output)["status"] == "fail"
        _edit_gates_json(
            output,
            lambda payload: payload["checks"][0]["source"].update(after=payload["checks"][0]["source"]["before"]),
        )
        with pytest.raises(SystemExit, match=INVALID_FAIL_EXIT_CODE):
            validator._validate_gates(output)

    def test_non_git_directory_fails_guard_without_running_command(self, tmp_path: Path, output: Path) -> None:
        """Unavailable Git provenance fails the guard instead of being treated as a clean source.

        The invocation directory is not a repository, so the head cannot be read; the command must not run and the
        receipt must carry the inspection error.
        """
        marker = tmp_path / "ran.txt"

        result = _run_lint_gate(tmp_path, output, "a" * 40, _marker_command(marker))

        check = _gate_check(output, "lint")
        source = check["source"]
        assert result.returncode == 1
        assert not marker.exists()
        assert source["expected_head"] == "a" * 40
        assert source["before"]["head"] == ""
        assert source["before"]["status"] == ""
        assert source["before"]["error"]
        assert source["after"] is None
        assert "source-inspection-failed" in _receipt_text(output, check)

    def test_unflagged_run_records_execution_directory_for_unstaged_remediation(
        self, repository: Path, head: str, output: Path
    ) -> None:
        """An ordinary gate runs in the dirty collected checkout and reports where it observed the unstaged bytes."""
        tracked = repository / "tracked.txt"
        tracked.write_bytes(b"unstaged change\n")
        digest = hashlib.sha256(tracked.read_bytes()).hexdigest()
        command = _python_command(
            "from pathlib import Path\nimport hashlib\nprint(Path.cwd().as_posix())\n"
            "print(hashlib.sha256(Path('tracked.txt').read_bytes()).hexdigest())"
        )
        result = _run_lint_gate(repository, output, None, command)
        check = _gate_check(output, "lint")
        assert result.returncode == 0, result.stderr
        assert check["status"] == "pass"
        assert "source" not in check
        receipt = _load_validator()._validate_gates(output)
        assert "source" not in receipt
        assert receipt["execution_cwd"] == repository.resolve().as_posix()
        assert _receipt_text(output, check, "stdout").splitlines() == [receipt["execution_cwd"], digest]
        assert _git(repository, "diff", "--cached") == ""
        pr = output / "pr"
        pr.mkdir()
        (pr / "local-checkout.json").write_text(json.dumps({"worktree": repository.resolve().as_posix()}))
        _load_validator()._validate_code_remediate_target_integration(
            pr,
            {"target_oid": head, "pre_merge_head": head, "post_merge_head": head, "status": "not-needed"},
            current_checkout=True,
        )

    @pytest.mark.parametrize(
        ("changed_name", "dependency_git_steps"),
        [
            pytest.param("tracked.txt", [], id="tracked"),
            pytest.param("extra.txt", [], id="untracked"),
            pytest.param(
                "tracked.txt",
                [("add", "tracked.txt"), ("commit", "-m", f"new dependency state\n\n{CODEX_TRAILER}")],
                id="commit",
            ),
        ],
    )
    def test_ignored_submodule_change_blocks_guarded_execution(
        self,
        tmp_path: Path,
        repository: Path,
        output: Path,
        changed_name: str,
        dependency_git_steps: list[tuple[str, ...]],
    ) -> None:
        """A submodule edit, new file or new commit blocks the gate even when ordinary Git status hides it.

        The submodule is configured with ``ignore = all``, so plain ``git status`` is clean; the runner must still see
        the change through ``--ignore-submodules=none`` and refuse to execute the command.
        """
        dependency = repository / "dependency"
        dependency.mkdir()
        _initialize_repository(dependency)
        (repository / ".gitmodules").write_text(
            '[submodule "dependency"]\n\tpath = dependency\n\turl = ../dependency\n\tignore = all\n',
            encoding="utf-8",
            newline="\n",
        )
        _git(repository, "add", ".gitmodules", "dependency")
        _git(repository, "commit", "-m", f"dependency\n\n{CODEX_TRAILER}")
        head = _git(repository, "rev-parse", "HEAD")
        (dependency / changed_name).write_text("changed\n", encoding="utf-8", newline="\n")
        _git_steps(dependency, dependency_git_steps)
        assert _git(repository, "status", "--porcelain=v1", "--untracked-files=all") == ""
        assert _git(repository, "status", "--porcelain=v1", "--ignore-submodules=none")
        marker = tmp_path / "ran.txt"

        result = _run_lint_gate(repository, output, head, _marker_command(marker))

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert not marker.exists()
        assert check["status"] == "refused"
        assert check["source"]["before"]["head"] == head
        assert "dependency" in check["source"]["before"]["status"]
        assert check["source"]["after"] is None
        assert SOURCE_DIRTY_STATUS in _receipt_text(output, check)


class TestSelectedWorktree:
    """``--worktree`` selects the checkout that is guarded and used as the command's working directory."""

    def test_wrong_head_in_selected_worktree_blocks_command(self, tmp_path: Path, output: Path) -> None:
        """The selected checkout is checked, not the invocation checkout, before any command runs.

        The invocation repository matches the expected head, but the selected one has a later commit; the gate must fail
        and record the selected path next to the head it actually found.
        """
        original = tmp_path / "original"
        original.mkdir()
        expected_head = _initialize_repository(original)
        selected = tmp_path / "selected"
        selected.mkdir()
        _initialize_repository(selected)
        (selected / "tracked.txt").write_text("different\n", encoding="utf-8", newline="\n")
        _git(selected, "add", "tracked.txt")
        _git(selected, "commit", "-m", f"different\n\n{CODEX_TRAILER}")
        actual_head = _git(selected, "rev-parse", "HEAD")
        assert actual_head != expected_head
        marker = tmp_path / "ran.txt"

        result = _run_lint_gate(original, output, expected_head, _marker_command(marker), worktree=selected)

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert not marker.exists()
        assert check["source"] == {
            "expected_head": expected_head,
            "worktree": selected.resolve().as_posix(),
            "before": {"head": actual_head, "status": "", "ignored": "[]"},
            "after": None,
        }
        assert HEAD_MISMATCH in _receipt_text(output, check)

    def test_same_head_in_other_worktree_retains_checkout_identity(
        self, linked_worktree: tuple[Path, Path, str], output: Path
    ) -> None:
        """The receipt names the selected path even when another checkout has the same Git head.

        Two linked checkouts at one commit are indistinguishable by head alone, so the worktree path is what ties the
        receipt to the reviewed source.
        """
        original, selected, expected_head = linked_worktree

        result = _run_lint_gate(original, output, expected_head, _python_command("pass"), worktree=selected)

        payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
        source = _gate_check(output, "lint")["source"]
        assert result.returncode == 0
        assert payload["source"] == {"expected_head": expected_head, "worktree": selected.resolve().as_posix()}
        assert source["worktree"] == selected.resolve().as_posix()
        assert source["before"] == {"head": expected_head, "status": "", "ignored": "[]"}
        assert source["after"] == {"head": expected_head, "status": "", "ignored": "[]"}
        assert original.resolve().as_posix() != source["worktree"]

    def test_command_runs_in_selected_worktree(self, linked_worktree: tuple[Path, Path, str], output: Path) -> None:
        """The gate command starts in the selected checkout while the runner keeps its own working directory.

        The command prints its cwd; the stdout receipt must equal the selected path, and no ``gates`` directory may
        appear inside the invocation checkout.
        """
        original, selected, expected_head = linked_worktree
        command = _python_command("from pathlib import Path; print(Path.cwd().resolve().as_posix())")

        result = _run_lint_gate(original, output, expected_head, command, worktree=selected)

        check = _gate_check(output, "lint")
        assert result.returncode == 0
        assert _receipt_text(output, check, "stdout").strip() == selected.resolve().as_posix()
        assert not (original / "gates").exists()

    def test_requires_expected_head_before_writing_receipts(self, repository: Path, output: Path) -> None:
        """Selecting a worktree without ``--expected-head`` is a usage error that writes no receipts.

        A checkout selection without an exact source guard would give a false sense of binding.
        """
        result = _run_lint_gate(repository, output, None, _python_command("pass"), worktree=repository)

        assert result.returncode == 2
        assert "invalid-worktree: --expected-head is required" in result.stderr
        assert not output.exists()

    def test_subdirectory_is_rejected_as_checkout_root(self, repository: Path, head: str, output: Path) -> None:
        """A repository subdirectory is rejected as the selected checkout because it is a misleading identity.

        The gate fails with a dedicated error in the ``before`` receipt and the command never runs.
        """
        nested = repository / "nested"
        nested.mkdir()
        marker = repository.parent / "ran.txt"

        result = _run_lint_gate(repository, output, head, _marker_command(marker), worktree=nested)

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert not marker.exists()
        assert check["source"]["worktree"] == nested.resolve().as_posix()
        assert check["source"]["before"]["error"] == "selected-worktree-is-not-checkout-root"


@dataclass
class _EditableCheckout:
    """A committed checkout paired with a detached worktree, wired for editable-install provenance checks."""

    original: Path
    selected: Path
    expected_head: str
    editable_path: Path
    environment: dict[str, str]

    def link_editable(self, root: Path) -> None:
        """Point the simulated editable install at the ``src`` directory of one checkout."""
        self.editable_path.write_text(f"{(root / 'src').as_posix()}\n", encoding="utf-8", newline="\n")

    def run_tests(
        self, output: Path, imported_module: str = "demo_package", pytest_arguments: list[str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the structured test gate from the original checkout."""
        arguments = [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--worktree",
            str(self.selected),
            "--expected-head",
            self.expected_head,
            "--pytest-python",
            sys.executable,
            "--pytest-import",
            imported_module,
            "--pytest-args-json",
            json.dumps(pytest_arguments or ["-q", "-o", "addopts=", "tests/test_demo.py"]),
            *_skip_gates("tests"),
        ]
        return _run(arguments, cwd=self.original, env=self.environment)


def _package_origin(root: Path) -> str:
    """Return the resolved ``demo_package`` module path a given checkout provides."""
    return (root / "src" / "demo_package" / "__init__.py").resolve().as_posix()


def _demo_test_path(root: Path) -> str:
    """Return the resolved path of the demo test file inside a given checkout."""
    return (root / "tests" / "test_demo.py").resolve().as_posix()


@pytest.fixture
def editable_checkout(tmp_path: Path, request: pytest.FixtureRequest) -> _EditableCheckout:
    """Build a committed checkout with a demo package plus a detached worktree of the same commit.

    Indirect parametrization may set a project ``addopts`` string, or ``"drop-proof"`` to install a conftest that
    removes the worker source receipt before it is delivered.
    """
    original = tmp_path / "original"
    original.mkdir()
    _initialize_repository(original)
    package = original / "src" / "demo_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 'same in both checkouts'\n", encoding="utf-8", newline="\n")
    tests = original / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text(
        "from demo_package import VALUE\n"
        "\ndef test_value():\n"
        "    from pathlib import Path\n"
        "    marker = Path(__file__).resolve().parents[2] / 'selected-test-ran.txt'\n"
        "    marker.write_text('executed', encoding='utf-8')\n"
        "    assert VALUE == 'same in both checkouts'\n",
        encoding="utf-8",
        newline="\n",
    )
    project_addopts = getattr(request, "param", None)
    if project_addopts == "drop-proof":
        (tests / "conftest.py").write_text(
            "import pytest\n\n"
            "@pytest.hookimpl(hookwrapper=True, trylast=True)\n"
            "def pytest_sessionfinish(session, exitstatus):\n"
            "    yield\n"
            "    if hasattr(session.config, 'workeroutput'):\n"
            "        session.config.workeroutput.pop('codex_rig_source_proof', None)\n",
            encoding="utf-8",
            newline="\n",
        )
    elif project_addopts is not None:
        (original / "pytest.ini").write_text(f"[pytest]\naddopts = {project_addopts}\n", encoding="utf-8", newline="\n")
    (original / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8", newline="\n")
    _git(original, "add", "src", "tests", ".gitignore")
    if project_addopts not in (None, "drop-proof"):
        _git(original, "add", "pytest.ini")
    _git(original, "commit", "-m", f"test package\n\n{CODEX_TRAILER}")
    expected_head = _git(original, "rev-parse", "HEAD")
    selected = tmp_path / "selected"
    _git(original, "worktree", "add", "--detach", str(selected), expected_head)

    # A .pth file is the import-path effect of a conventional editable install.
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    editable_site = tmp_path / "editable-site"
    editable_site.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        f"import site\nsite.addsitedir({str(editable_site)!r})\n", encoding="utf-8", newline="\n"
    )
    editable_path = editable_site / "editable.pth"
    environment = {**os.environ, "PYTHONPATH": str(bootstrap)}

    return _EditableCheckout(original, selected, expected_head, editable_path, environment)


class TestEditableSourceProvenance:
    """The tests gate proves, per import and per worker, that executed source comes from the selected worktree."""

    def test_unstartable_interpreter_retains_failed_attempt_and_allows_retry(
        self, editable_checkout: _EditableCheckout, output: Path, tmp_path: Path
    ) -> None:
        """A launch refusal between two passing structured runs preserves three independently readable receipts."""
        checkout = editable_checkout
        checkout.link_editable(checkout.selected)
        first = checkout.run_tests(output)
        assert first.returncode == 0, first.stderr
        prior = (output / "gates.json").read_bytes()
        proof = "checks/tests.python-imports.json"
        prior_proof = (output / proof).read_bytes()
        invalid = tmp_path / "unstartable.exe"
        invalid.write_bytes(b"fixture")
        invalid.chmod(0o600)
        invocation = list(first.args)
        invocation[invocation.index("--pytest-python") + 1] = str(invalid)
        failed = _run(invocation, cwd=checkout.original, env=checkout.environment)
        assert failed.returncode == 1, failed.stderr
        check = _gate_check(output, "tests")
        assert check["status"] == "fail"
        assert check["exit_code"] == 126
        assert check["execution_route"]["mode"] == "not-started"
        assert "standard" in check["execution_route"]["reason"]
        assert str(invalid) in _receipt_text(output, check)
        assert _receipt_text(output, check, "stdout") == ""
        assert "python_imports" not in check
        assert not (output / proof).exists()
        validator = _load_validator()
        assert validator._validate_gates(output)["checks_failed"] == ["tests"]
        archive = output / "gate-attempts/001"
        assert (archive / "gates.json").read_bytes() == prior
        assert (archive / proof).read_bytes() == prior_proof
        assert validator._validate_gates(archive)["checks_failed"] == []
        failed_receipt = (output / "gates.json").read_bytes()
        retry = checkout.run_tests(output)
        assert retry.returncode == 0, retry.stderr
        assert validator._validate_gates(output)["checks_failed"] == []
        archived_failure = output / "gate-attempts/002"
        assert (archived_failure / "gates.json").read_bytes() == failed_receipt
        assert validator._validate_gates(archived_failure)["checks_failed"] == ["tests"]

    @pytest.mark.parametrize("middle", ["free-form", "skipped", "failed"])
    def test_mode_change_archives_each_attempts_own_proof(
        self, editable_checkout: _EditableCheckout, output: Path, middle: str
    ) -> None:
        """Switch away from structured pytest and back, validating each attempt against its own proof and logs."""
        checkout = editable_checkout
        checkout.link_editable(checkout.selected)
        first = checkout.run_tests(output)
        assert first.returncode == 0, first.stderr
        first_receipt = (output / "gates.json").read_bytes()
        previous_scratch = _child_proof(output).parent
        middle_args = (
            ["--skip-tests", "out of scope"]
            if middle == "skipped"
            else ["--tests", _python_command(f"raise SystemExit({int(middle == 'failed')})")]
        )
        second = _run(
            [
                sys.executable,
                str(RUN_GATES),
                "--out",
                str(output),
                "--worktree",
                str(checkout.selected),
                "--expected-head",
                checkout.expected_head,
                *middle_args,
                *_skip_gates("tests"),
            ]
        )
        assert second.returncode == int(middle == "failed"), second.stderr
        validator = _load_validator()
        failures = ["tests"] if middle == "failed" else []
        assert validator._validate_gates(output)["checks_failed"] == failures
        assert not (output / "checks/tests.python-imports.json").exists()
        assert previous_scratch.is_dir()
        third = checkout.run_tests(output)
        assert third.returncode == 0, third.stderr
        archive = output / "gate-attempts/001"
        assert (archive / "gates.json").read_bytes() == first_receipt
        assert validator._validate_gates(archive)["checks_failed"] == []
        assert validator._validate_gates(output / "gate-attempts/002")["checks_failed"] == failures
        assert validator._validate_gates(output)["checks_failed"] == []

    def test_retry_preserves_replacement_scratch_directory(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """An unrelated directory at the old scratch path survives the retry and stays outside the archive."""
        checkout = editable_checkout
        checkout.link_editable(checkout.selected)
        assert checkout.run_tests(output).returncode == 0
        old_scratch = _child_proof(output).parent
        old_scratch.rename(output / "moved-first-directory")
        old_scratch.mkdir()
        sentinel = old_scratch / "user-file"
        sentinel.write_bytes(b"retain")
        identity = old_scratch.stat()
        retry = checkout.run_tests(output)
        assert retry.returncode == 0, retry.stderr
        assert _child_proof(output).parent != old_scratch
        assert os.path.samestat(identity, old_scratch.stat())
        assert sentinel.read_bytes() == b"retain"
        archive = output / "gate-attempts/001"
        assert not (archive / "checks" / old_scratch.name).exists()
        assert _load_validator()._validate_gates(archive)["checks_failed"] == []
        assert _load_validator()._validate_gates(output)["checks_failed"] == []

    def test_mode_change_preserves_replaced_proof(self, editable_checkout: _EditableCheckout, output: Path) -> None:
        """An archived receipt cannot authorize retiring different bytes at the canonical proof path."""
        checkout = editable_checkout
        checkout.link_editable(checkout.selected)
        assert checkout.run_tests(output).returncode == 0
        prior = (output / "gates.json").read_bytes()
        proof = output / "checks/tests.python-imports.json"
        proof.write_bytes(b"unowned")
        skipped = _run(
            [sys.executable, str(RUN_GATES), "--out", str(output), "--skip-tests", "unused", *_skip_gates("tests")]
        )
        assert skipped.returncode == 2
        assert "source-gate-previous-proof-unowned" in skipped.stderr
        assert (output / "gates.json").read_bytes() == prior
        assert proof.read_bytes() == b"unowned"

    @pytest.mark.parametrize(
        ("rejection", "diagnostic"),
        [
            pytest.param("without-skip", "test-skip-approval-without-skip", id="approval-without-skip"),
            pytest.param("invalid", "test-skip-approval-invalid", id="invalid-decision"),
            pytest.param("missing", "test-skip-approval-unreadable", id="unreadable-decision"),
            pytest.param("destination", "test-skip-approval-invalid-path", id="unwritable-decision-destination"),
        ],
    )
    def test_rejected_skip_decision_leaves_prior_receipt_reusable(
        self, editable_checkout: _EditableCheckout, output: Path, rejection: str, diagnostic: str
    ) -> None:
        """A rejected invocation preserves the prior structured receipt until a valid retry can archive it."""
        checkout = editable_checkout
        checkout.link_editable(checkout.selected)
        first = checkout.run_tests(output)
        assert first.returncode == 0, first.stderr
        previous = (output / "gates.json").read_bytes()
        decision = output / "decision.json"
        if rejection == "invalid":
            decision.write_text("{}", encoding="utf-8")
        if rejection == "destination":
            decision.write_text(
                '{"decision":"skip-tests","reason":"unused","evidence":"user decision"}', encoding="utf-8"
            )
            (output / "checks/tests-skip-approval.json").mkdir()
        extra = [] if rejection == "without-skip" else ["--skip-tests", "unused"]
        rejected = _run(
            [
                sys.executable,
                str(RUN_GATES),
                "--out",
                str(output),
                "--test-skip-approval",
                str(decision),
                *extra,
                *_skip_gates("tests"),
            ]
        )
        assert rejected.returncode != 0
        assert diagnostic in rejected.stderr
        assert (output / "gates.json").read_bytes() == previous
        assert _load_validator()._validate_gates(output)["checks_failed"] == []
        assert not (output / "gate-attempts").exists()
        retry = checkout.run_tests(output)
        assert retry.returncode == 0, retry.stderr
        assert (output / "gate-attempts/001/gates.json").read_bytes() == previous
        assert _load_validator()._validate_gates(output / "gate-attempts/001")["checks_failed"] == []

    def test_editable_package_from_invoking_checkout_fails_gate(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A passing test must not certify an editable package that is imported from the invoking checkout.

        The simulated editable install points at ``original/src`` while the gate reviews ``selected``; the test still
        passes, but the import proof must fail and name the wrong origin.
        """
        editable_checkout.link_editable(editable_checkout.original)

        result = editable_checkout.run_tests(output)

        check = _gate_check(output, "tests")
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert check["python_imports"]["status"] == "fail"
        assert check["python_imports"]["modules"]["demo_package"]["origin"] == _package_origin(
            editable_checkout.original
        )
        assert "1 passed" in _receipt_text(output, check, "stdout")

    def test_editable_package_from_selected_worktree_passes_gate(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A package and test file that resolve inside the selected worktree certify the gate.

        Both the imported module and the executed test file are tracked and carry a passing proof with no reason.
        """
        editable_checkout.link_editable(editable_checkout.selected)

        result = editable_checkout.run_tests(output)

        check = _gate_check(output, "tests")
        selected_test = _demo_test_path(editable_checkout.selected)
        assert result.returncode == 0, result.stderr
        assert check["status"] == "pass"
        assert check["python_imports"]["status"] == "pass"
        child_argv = json.loads(_receipt_text(output, check, "command_path").removeprefix("argv-json:"))
        child_proof = Path(child_argv[5])
        assert child_proof.name == "tests.python-imports.json"
        private_checks = child_proof.parent.parent
        assert private_checks.name == "checks"
        assert private_checks.parent.parent == output
        assert re.fullmatch(r"\.gate-stage-[0-9a-f]{32}", private_checks.parent.name)
        assert re.fullmatch(r"pytest-scratch-[0-9a-f]{32}", child_proof.parent.name)
        assert child_proof.read_bytes() == (output / "checks/tests.python-imports.json").read_bytes()
        binding = json.loads(child_argv[-1])["tracked_inventory"]
        inventory = Path(binding["path"])
        assert inventory.parent == private_checks
        assert hashlib.sha256(inventory.read_bytes()).hexdigest() == binding["sha256"]
        assert (output / "checks" / inventory.name).read_bytes() == inventory.read_bytes()
        assert check["source"]["artifacts"] == _artifact_hashes(
            output, check, "checks/tests.python-imports.json", f"checks/{inventory.name}"
        )
        # The recorded command replays against its retained execution inputs after the runner exits.
        replay = _run(child_argv, cwd=editable_checkout.selected, env=editable_checkout.environment)
        assert replay.returncode == 0, replay.stderr
        assert check["python_imports"]["modules"]["demo_package"] == {
            "origin": _package_origin(editable_checkout.selected),
            "tracked": True,
            "status": "pass",
            "reason": None,
        }
        assert check["python_imports"]["tests"][selected_test] == {
            "origin": selected_test,
            "tracked": True,
            "status": "pass",
            "reason": None,
        }

    def test_bytecode_is_disabled_so_overlay_recovery_leaves_no_pyc(
        self, editable_checkout: _EditableCheckout, tmp_path: Path, output: Path
    ) -> None:
        """A failed source-bound gate can be recovered with an overlay while its failed attempt is preserved.

        The first run fails because ``PYTEST_ADDOPTS`` names a plugin that is not importable. After the plugin is added
        on an overlay path the same output directory passes, runs Python with ``-B`` and writes no ``.pyc`` into the
        overlay or the reviewed worktree, and the first attempt stays archived.
        """
        editable_checkout.link_editable(editable_checkout.selected)
        editable_checkout.environment["PYTEST_ADDOPTS"] = (
            f"{editable_checkout.environment.get('PYTEST_ADDOPTS', '')} -p overlay_probe"
        ).strip()
        editable_checkout.environment["PYTHONDONTWRITEBYTECODE"] = "1"
        failed = editable_checkout.run_tests(output)
        assert failed.returncode == 1
        assert _load_validator()._validate_gates(output)["checks_failed"] == ["tests"]
        assert "overlay_probe" in (output / "checks/tests.stderr.txt").read_text(encoding="utf-8")
        stale = _child_proof(output).parent / "prior-run.cache"
        stale.write_bytes(b"first attempt only")
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        (overlay / "overlay_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
        bootstrap = editable_checkout.environment["PYTHONPATH"]
        editable_checkout.environment["PYTHONPATH"] = os.pathsep.join((str(overlay), bootstrap))

        completed = editable_checkout.run_tests(output)

        invocation = (output / "checks/tests.command.txt").read_text(encoding="utf-8").removeprefix("argv-json:")
        prior = json.loads((output / "gate-attempts/001/gates.json").read_text(encoding="utf-8"))
        assert completed.returncode == 0, completed.stderr
        assert _gate_check(output, "tests")["python_imports"]["status"] == "pass"
        assert json.loads(invocation)[1] == "-B"
        assert not list(overlay.rglob("*.pyc"))
        assert not list(editable_checkout.selected.rglob("*.pyc"))
        assert prior["checks_failed"] == ["tests"]
        assert stale.read_bytes() == b"first attempt only"
        assert not (_child_proof(output).parent / stale.name).exists()
        assert not (output / "gate-attempts/001/checks" / stale.parent.name).exists()
        binding = json.loads(json.loads(invocation)[-1])["tracked_inventory"]
        current_input = Path(binding["path"])
        archived_input = output / "gate-attempts/001/checks" / current_input.name
        assert archived_input.read_bytes() == current_input.read_bytes()
        assert hashlib.sha256(archived_input.read_bytes()).hexdigest() == binding["sha256"]

    @pytest.mark.parametrize("editable_checkout", ["-n 2"], indirect=True)
    @pytest.mark.parametrize(
        ("extra_environment", "pytest_arguments"),
        [
            pytest.param({}, ["-q", "tests/test_demo.py", "-o", "addopts=", "-n", "2"], id="cli"),
            pytest.param({}, ["-q", "tests/test_demo.py"], id="project"),
            pytest.param({"PYTEST_ADDOPTS": "-n 2"}, ["-q", "tests/test_demo.py", "-o", "addopts="], id="environment"),
        ],
    )
    def test_parallel_run_proves_worker_sources(
        self,
        editable_checkout: _EditableCheckout,
        tmp_path: Path,
        output: Path,
        extra_environment: dict[str, str],
        pytest_arguments: list[str],
    ) -> None:
        """Selected tests and imports are certified from real xdist workers whichever source enables ``-n 2``.

        The worker count comes from the command line, the project ``pytest.ini`` or ``PYTEST_ADDOPTS``; in each case
        both workers must report a passing source proof and the test must really execute in the selected worktree.
        """
        editable_checkout.link_editable(editable_checkout.selected)
        marker = tmp_path / "selected-test-ran.txt"
        editable_checkout.environment["PYTEST_ADDOPTS"] = (
            f"{editable_checkout.environment.get('PYTEST_ADDOPTS', '')} {extra_environment.get('PYTEST_ADDOPTS', '')}"
        ).strip()

        result = editable_checkout.run_tests(output, pytest_arguments=pytest_arguments)

        proof = _gate_check(output, "tests")["python_imports"]
        selected_test = _demo_test_path(editable_checkout.selected)
        assert result.returncode == 0, result.stderr
        assert marker.read_text(encoding="utf-8") == "executed"
        assert _gate_check(output, "tests")["status"] == "pass"
        assert proof["status"] == "pass"
        assert len(proof["workers"]) == 2
        assert all(worker["status"] != "fail" for worker in proof["workers"].values())
        assert proof["tests"][selected_test]["status"] == "pass"
        assert proof["modules"]["demo_package"]["origin"] == _package_origin(editable_checkout.selected)
        assert any(selected_test in worker["tests"] for worker in proof["workers"].values())
        assert any(worker["modules"]["demo_package"]["status"] == "pass" for worker in proof["workers"].values())

    @pytest.mark.parametrize(
        "pytest_arguments",
        [
            pytest.param(["-q", "-o", "addopts=", "tests/test_demo.py"], id="no-addopts"),
            pytest.param(["-q", "-n", "0", "tests/test_demo.py"], id="explicit-zero-workers"),
        ],
    )
    def test_serial_run_retains_source_proof(
        self, editable_checkout: _EditableCheckout, tmp_path: Path, output: Path, pytest_arguments: list[str]
    ) -> None:
        """Serial pytest, with or without an explicit ``-n 0``, still yields a valid proof and runs the selected test.

        Without xdist workers the proof comes from the single process, so it must pass without a ``workers`` section.
        """
        editable_checkout.link_editable(editable_checkout.selected)
        marker = tmp_path / "selected-test-ran.txt"

        result = editable_checkout.run_tests(output, pytest_arguments=pytest_arguments)

        assert result.returncode == 0, result.stderr
        assert marker.read_text(encoding="utf-8") == "executed"
        assert _gate_check(output, "tests")["python_imports"]["status"] == "pass"

    def test_parallel_worker_with_wrong_editable_origin_fails_proof(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A worker that imports the package from the invoking checkout cannot certify the reviewed source.

        Two xdist workers run against an editable install pointing at ``original/src``; the gate and the module proof
        must both fail and record the wrong origin.
        """
        editable_checkout.link_editable(editable_checkout.original)

        result = editable_checkout.run_tests(output, pytest_arguments=["-q", "-n", "2", "tests/test_demo.py"])

        check = _gate_check(output, "tests")
        proof = check["python_imports"]
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert len(proof["workers"]) == 2
        assert proof["modules"]["demo_package"]["origin"] == _package_origin(editable_checkout.original)
        assert proof["modules"]["demo_package"]["status"] == "fail"

    def test_parallel_external_test_file_fails_proof(self, editable_checkout: _EditableCheckout, output: Path) -> None:
        """Workers that collect a test file outside the selected worktree fail the selected-test provenance.

        The package resolves correctly, but the test path is the invoking checkout's copy, so its proof reason must be
        ``test-outside-worktree``.
        """
        editable_checkout.link_editable(editable_checkout.selected)
        external_test = editable_checkout.original / "tests" / "test_demo.py"

        result = editable_checkout.run_tests(output, pytest_arguments=["-q", "-n", "2", str(external_test)])

        check = _gate_check(output, "tests")
        proof = check["python_imports"]
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert len(proof["workers"]) == 2
        assert proof["tests"][external_test.resolve().as_posix()]["reason"] == "test-outside-worktree"

    @pytest.mark.parametrize("editable_checkout", ["drop-proof"], indirect=True)
    def test_parallel_missing_worker_receipts_fail_closed(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A passing worker test whose source receipt is removed before delivery fails the gate.

        A project conftest strips the proof from each worker's output, so the runner must report ``worker-proof-
        missing`` rather than assume the sources were fine.
        """
        editable_checkout.link_editable(editable_checkout.selected)

        result = editable_checkout.run_tests(output, pytest_arguments=["-q", "-n", "2", "tests/test_demo.py"])

        check = _gate_check(output, "tests")
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert check["python_imports"]["status"] == "fail"
        assert any(error.startswith("worker-proof-missing:") for error in check["python_imports"]["errors"])

    def test_serial_external_test_file_cannot_certify_selected_worktree(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A passing serial test taken from outside the reviewed checkout fails the source proof.

        The test file is recorded as untracked with the reason ``test-outside-worktree`` even though pytest reports it
        as passed.
        """
        editable_checkout.link_editable(editable_checkout.selected)
        external_test = editable_checkout.original / "tests" / "test_demo.py"

        result = editable_checkout.run_tests(output, pytest_arguments=["-q", "-o", "addopts=", str(external_test)])

        check = _gate_check(output, "tests")
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert check["python_imports"]["status"] == "fail"
        assert check["python_imports"]["tests"][external_test.resolve().as_posix()] == {
            "origin": external_test.resolve().as_posix(),
            "tracked": False,
            "status": "fail",
            "reason": "test-outside-worktree",
        }
        assert "1 passed" in _receipt_text(output, check, "stdout")

    def test_unimported_declared_module_is_inconclusive(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A declared import module that the test run never loads yields inconclusive provenance.

        The gate asks about ``unloaded_package``, which no test imports, so no origin can be proven and the gate fails.
        """
        result = editable_checkout.run_tests(output, "unloaded_package")

        check = _gate_check(output, "tests")
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert check["python_imports"]["status"] == "inconclusive"
        assert check["python_imports"]["modules"]["unloaded_package"]["reason"] == "module-not-imported"


@dataclass
class _LocalGate:
    """A dirty local repository, its reviewed mirror, the receipts directory and an import-bound gate invocation."""

    repository: Path
    output: Path
    mirror: Path
    args: list[str]


def _local_gate_inputs(
    tmp_path: Path,
    *,
    committed: bool = True,
    gitlink: bool = False,
    conftest: str = "",
    pytest_arguments: list[str] | None = None,
) -> _LocalGate:
    """Build a real collector mirror and its explicit import-bound gate invocation.

    ``committed`` selects whether ``localmod.py`` and its test were committed before the working-tree edit or stay
    untracked, ``gitlink`` adds a committed gitlink entry named ``dependency``, and ``conftest`` is written uncommitted.
    """
    repository = tmp_path / "repository"
    repository.mkdir()
    head = _initialize_repository(repository)
    (repository / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(repository, "add", ".gitignore")
    _git(repository, "commit", "-m", f"test caches\n\n{CODEX_TRAILER}")
    module = repository / "localmod.py"
    test = repository / "test_localmod.py"
    module.write_text("value = 1\n", encoding="utf-8")
    test.write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
        "import localmod\ndef test_value():\n    assert localmod.value == 2\n",
        encoding="utf-8",
    )
    if committed:
        _git(repository, "add", "localmod.py", "test_localmod.py")
        if gitlink:
            _git(repository, "update-index", "--add", "--cacheinfo", f"160000,{head},dependency")
            (repository / "dependency").mkdir()
        _git(repository, "commit", "-m", f"runtime files\n\n{CODEX_TRAILER}")
    module.write_text("value = 2\n", encoding="utf-8")
    if conftest:
        (repository / "conftest.py").write_text(conftest, encoding="utf-8", newline="\n")
    output = tmp_path / "review"
    collected = _run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/collect_diff.py"),
            "--review-worktree",
            "--repository",
            str(repository),
            "--out",
            str(output / "local-source"),
        ]
    )
    assert collected.returncode == 0, collected.stderr
    mirror = Path(json.loads((output / "local-source/review-worktree.json").read_bytes())["review_worktree"])
    if gitlink:
        (mirror / "dependency").mkdir(exist_ok=True)
    args = [
        sys.executable,
        str(RUN_GATES),
        "--out",
        str(output),
        "--worktree",
        str(mirror),
        "--pytest-python",
        sys.executable,
        "--pytest-import",
        "localmod",
        "--pytest-args-json",
        json.dumps(pytest_arguments or ["-q", "-p", "no:xdist", "-o", "addopts=", "test_localmod.py"]),
        *_skip_gates("tests"),
    ]
    return _LocalGate(repository, output, mirror, args)


def _move_gitlink_pointer(gate: _LocalGate) -> Path:
    """Alter the mirror's recorded gitlink identity and return the mirror as the worktree to run in."""
    _git(gate.mirror, "update-index", "--cacheinfo", f"160000,{_git(gate.mirror, 'rev-parse', 'HEAD')},dependency")
    return gate.mirror


def _use_unreceipted_checkout(gate: _LocalGate) -> Path:
    """Return the original repository, which has no collector receipt, as the worktree to run in."""
    return gate.repository


def _expect_validator_rejects_stripped_guard_error(
    validator: Any, output: Path, check: dict[str, Any], stderr: bytes, guard_error: str
) -> None:
    """Remove the guard error from the stderr receipt and require the validator to reject the inconsistent record."""
    (output / str(check["stderr"])).write_bytes(stderr.replace(guard_error.encode("utf-8"), b""))
    with pytest.raises(SystemExit, match=INVALID_FAIL_EXIT_CODE):
        validator._validate_gates(output)


def _accept_validator_state(
    validator: Any, output: Path, check: dict[str, Any], stderr: bytes, guard_error: str
) -> None:
    """No extra check: a nonzero command exit already explains the failed gate."""


@dataclass(frozen=True)
class _GuardCase:
    """One child behaviour whose raw output and failure disposition must survive the post-run source guard."""

    body: str
    raw_prefix: bytes
    gate_status: str
    exit_code: int
    runner_exit: int
    validator_status: str = "fail"
    timeout_seconds: str = "20"
    reason_pattern: str = ".*"
    tamper_check: Callable[..., None] = _accept_validator_state


class TestLocalMirror:
    """Gates run against a local review mirror bind their results to the exact mirror bytes and receipts."""

    @pytest.mark.parametrize(
        ("committed", "gitlink"),
        [
            pytest.param(True, False, id="tracked"),
            pytest.param(False, False, id="added"),
            pytest.param(True, True, id="gitlink"),
        ],
    )
    def test_import_bound_gate_preserves_admitted_source(self, tmp_path: Path, committed: bool, gitlink: bool) -> None:
        """The mirror's dirty bytes, added files included, are imported and unchanged gitlinks are excluded.

        The test imports a module edited in the working tree, so passing proves the gate ran the mirror and not the
        committed copy; the sidecar proof must match ``gates.json`` and its hash must be recorded.
        """
        gate = _local_gate_inputs(tmp_path, committed=committed, gitlink=gitlink)

        result = _run(gate.args)

        check = _gate_check(gate.output, "tests")
        proof = check["python_imports"]
        sidecar = (gate.output / "checks/tests.python-imports.json").read_bytes()
        snapshot = json.loads((gate.output / "checks/local-source-snapshot.json").read_bytes())
        assert result.returncode == 0, result.stderr
        assert check["source"]["mode"] == "local-review-mirror"
        assert check["source"]["before"] == check["source"]["after"]
        assert json.loads(sidecar) == proof
        assert check["source"]["artifacts"]["checks/tests.python-imports.json"] == hashlib.sha256(sidecar).hexdigest()
        assert proof["modules"]["localmod"]["tracked"] is committed
        assert proof["modules"]["localmod"]["snapshot_member"]["path"] == "localmod.py"
        assert proof["tests"][(gate.mirror / "test_localmod.py").as_posix()]["tracked"] is committed
        assert all(record["path"] != "dependency" for record in snapshot["files"])
        assert bool(snapshot["gitlinks"]) is gitlink

    def test_local_retry_preserves_unowned_scratch(self, tmp_path: Path) -> None:
        """A local mirror submits into fresh scratch and leaves an unrelated sibling directory untouched."""
        gate = _local_gate_inputs(tmp_path)
        first = _run(gate.args)
        assert first.returncode == 0, first.stderr
        foreign = gate.output / "checks/pytest-scratch/user-file"
        foreign.parent.mkdir()
        foreign.write_bytes(b"retain")
        retry = _run(gate.args)
        assert retry.returncode == 0, retry.stderr
        assert foreign.read_bytes() == b"retain"
        assert _load_validator()._validate_gates(gate.output)["checks_failed"] == []
        archive = gate.output / "gate-attempts/001"
        validator = _load_validator()
        assert validator._validate_gates(archive)["checks_failed"] == []
        relocated = tmp_path / "unrelated/gate-attempts/001"
        shutil.copytree(archive, relocated)
        with pytest.raises(SystemExit, match="gate-evidence-local-source-mismatch"):
            validator._validate_gates(relocated)
        malformed = gate.output / "gate-attempts/01"
        shutil.copytree(archive, malformed)
        with pytest.raises(SystemExit, match="gate-evidence-local-source-mismatch"):
            validator._validate_gates(malformed)
        (archive / "local-source/review-worktree.json").write_bytes(b"changed")
        with pytest.raises(SystemExit, match="gate-evidence-digest-mismatch"):
            validator._validate_gates(archive)

    def test_mirror_to_ordinary_retry_retires_only_the_archived_snapshot(self, tmp_path: Path) -> None:
        """An ordinary gate following a mirror keeps the mirror evidence in its original attempt."""
        gate = _local_gate_inputs(tmp_path)
        assert _run(gate.args).returncode == 0
        snapshot = Path("checks/local-source-snapshot.json")
        original = (gate.output / snapshot).read_bytes()
        collector = gate.output / "local-source/review-worktree.json"
        receipt = collector.read_bytes()
        ordinary = _run_lint_gate(gate.repository, gate.output, None, _python_command("print('ok')"))
        assert ordinary.returncode == 0, ordinary.stderr
        validator = _load_validator()
        assert validator._validate_gates(gate.output)["checks_failed"] == []
        archived = gate.output / "gate-attempts/001"
        assert (archived / snapshot).read_bytes() == original
        assert validator._validate_gates(archived)["checks_failed"] == []
        assert not (gate.output / snapshot).exists()
        assert collector.read_bytes() == receipt
        assert _run(gate.args).returncode == 0
        assert validator._validate_gates(gate.output / "gate-attempts/002")["checks_failed"] == []
        assert validator._validate_gates(gate.output)["checks_failed"] == []

    @pytest.mark.skipif(not HARD_LINKS_AVAILABLE, reason="host cannot hardlink files")
    def test_mirror_to_ordinary_retry_preserves_shared_snapshot(self, tmp_path: Path) -> None:
        """A shared snapshot cannot be retired, even if its content still matches the previous receipt."""
        gate = _local_gate_inputs(tmp_path)
        assert _run(gate.args).returncode == 0
        current = gate.output / "gates.json"
        prior = current.read_bytes()
        snapshot = gate.output / "checks/local-source-snapshot.json"
        shared = tmp_path / "other-owner"
        os.link(snapshot, shared)
        marker = tmp_path / "ordinary-ran"
        ordinary = _run_lint_gate(gate.repository, gate.output, None, _marker_command(marker))
        assert ordinary.returncode == 2, ordinary.stderr
        assert "local-gate-previous-snapshot-invalid-file" in ordinary.stderr
        assert snapshot.samefile(shared)
        assert not marker.exists()
        assert current.read_bytes() == prior
        assert _load_validator()._validate_gates(gate.output)["checks_failed"] == []
        history = gate.output / "gate-attempts"
        assert not list(history.glob("[0-9]*"))
        shared.unlink()
        assert _run_lint_gate(gate.repository, gate.output, None, _marker_command(marker)).returncode == 0
        assert [path.name for path in history.glob("[0-9]*")] == ["001"]
        assert _load_validator()._validate_gates(history / "001")["checks_failed"] == []

    def test_partial_mirror_submission_is_retained_without_blocking_retry(self, tmp_path: Path) -> None:
        """An interrupted child submission stays private; the parent records and archives its own observation."""
        trigger = tmp_path / "truncate"
        hook = (
            "import json, os\nfrom pathlib import Path\n"
            "def pytest_unconfigure(config):\n"
            f"    if Path({str(trigger)!r}).exists():\n"
            "        task = json.loads(os.environ['CODEX_RIG_PYTEST_SOURCE_PROOF'])\n"
            "        Path(task['proof_path']).write_bytes(b'{partial')\n"
        )
        gate = _local_gate_inputs(tmp_path, conftest=hook)
        assert _run(gate.args).returncode == 0
        trigger.touch()
        failed = _run(gate.args)
        assert failed.returncode == 1, failed.stderr
        validator = _load_validator()
        assert validator._validate_gates(gate.output)["checks_failed"] == ["tests"]
        result = _gate_check(gate.output, "tests")
        assert result["python_imports"]["status"] == "inconclusive"
        submission = _child_proof(gate.output)
        assert submission.read_bytes() == b"{partial"
        assert submission != gate.output / "checks/tests.python-imports.json"
        prior = (gate.output / "gates.json").read_bytes()
        trigger.unlink()
        assert _run(gate.args).returncode == 0
        assert submission.read_bytes() == b"{partial"
        archive = gate.output / "gate-attempts/002"
        assert (archive / "gates.json").read_bytes() == prior
        assert validator._validate_gates(archive)["checks_failed"] == ["tests"]
        assert validator._validate_gates(gate.output)["checks_failed"] == []

    def test_failed_mirror_process_without_proof_retains_verifiable_history(self, tmp_path: Path) -> None:
        """A started interpreter that cannot import pytest retains its failure and an independently readable archive."""
        gate = _local_gate_inputs(tmp_path)
        assert _run(gate.args).returncode == 0
        environment = tmp_path / "python-without-pytest"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True, capture_output=True
        )
        interpreter = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        args = list(gate.args)
        args[args.index("--pytest-python") + 1] = str(interpreter)
        failed = _run(args)
        assert failed.returncode == 1, failed.stderr
        check = _gate_check(gate.output, "tests")
        assert check["exit_code"] == 1
        assert check["execution_route"]["mode"] == "standard"
        assert "ModuleNotFoundError: No module named 'pytest'" in _receipt_text(gate.output, check)
        assert check["python_imports"]["status"] == "inconclusive"
        assert check["python_imports"]["modules"] == {}
        validator = _load_validator()
        assert validator._validate_gates(gate.output)["checks_failed"] == ["tests"]
        prior = (gate.output / "gates.json").read_bytes()
        proof_path = "checks/tests.python-imports.json"
        proof = (gate.output / proof_path).read_bytes()
        assert json.loads(proof) == check["python_imports"]
        assert hashlib.sha256(proof).hexdigest() == check["source"]["artifacts"][proof_path]
        retry = _run(gate.args)
        assert retry.returncode == 0, retry.stderr
        archive = gate.output / "gate-attempts/002"
        assert (archive / "gates.json").read_bytes() == prior
        assert (archive / proof_path).read_bytes() == proof
        assert validator._validate_gates(archive)["checks_failed"] == ["tests"]
        assert validator._validate_gates(gate.output / "gate-attempts/001")["checks_failed"] == []
        assert validator._validate_gates(gate.output)["checks_failed"] == []

    def test_unstartable_local_interpreter_preserves_mirror_evidence(self, tmp_path: Path) -> None:
        """A mirror can prove its source observations when the target interpreter never starts."""
        gate = _local_gate_inputs(tmp_path)
        invalid = tmp_path / "unstartable.exe"
        invalid.write_bytes(b"fixture")
        invalid.chmod(0o600)
        gate.args[gate.args.index("--pytest-python") + 1] = str(invalid)
        run = _run(gate.args)
        assert run.returncode == 1, run.stderr
        check = _gate_check(gate.output, "tests")
        assert check["exit_code"] == 126
        assert check["execution_route"]["mode"] == "not-started"
        assert check["source"]["before"] == check["source"]["after"]
        assert "python_imports" not in check
        assert not (gate.output / "checks/tests.python-imports.json").exists()
        assert _load_validator()._validate_gates(gate.output)["checks_failed"] == ["tests"]

    @pytest.mark.skipif(not HARD_LINKS_AVAILABLE, reason="host cannot hardlink files")
    def test_rejected_mirror_destination_preserves_current_proof(self, tmp_path: Path) -> None:
        """A mirror's unusable snapshot path is diagnosed before its previous structured receipt is retired."""
        gate = _local_gate_inputs(tmp_path)
        first = _run(gate.args)
        assert first.returncode == 0, first.stderr
        prior = (gate.output / "gates.json").read_bytes()
        snapshot = gate.output / "checks/local-source-snapshot.json"
        os.link(snapshot, tmp_path / "kept-snapshot.json")
        assert _load_validator()._validate_gates(gate.output)["checks_failed"] == []
        rejected = _run(gate.args)
        assert rejected.returncode != 0
        assert "local-gate-artifact-escape" in rejected.stderr
        assert (gate.output / "gates.json").read_bytes() == prior
        assert _load_validator()._validate_gates(gate.output)["checks_failed"] == []
        assert not (gate.output / "gate-attempts").exists()

    def test_rejects_source_changed_during_run(self, tmp_path: Path) -> None:
        """A command that edits the mirror while it runs fails the gate and the failure cannot be edited away.

        The command does run (its marker exists) but the post-run guard fails the gate; deleting ``guard_error`` from
        the record afterwards makes the validator reject it, so the evidence cannot be silently removed.
        """
        gate = _local_gate_inputs(tmp_path, committed=False)
        marker = tmp_path / "executed.txt"
        command = _python_command(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran'); "
            f"Path({str(gate.mirror / 'localmod.py')!r}).write_text('value = 99\\n')"
        )

        result = _run_lint_gate(gate.repository, gate.output, None, command, gate.mirror)

        validator = _load_validator()
        assert result.returncode != 0
        assert marker.exists()
        assert _gate_check(gate.output, "lint")["status"] == "fail"
        assert validator._validate_gates(gate.output)["status"] == "fail"
        _edit_gates_json(gate.output, lambda payload: payload["checks"][0]["source"].pop("guard_error"))
        with pytest.raises(SystemExit, match=INVALID_FAIL_EXIT_CODE):
            validator._validate_gates(gate.output)

    @pytest.mark.parametrize(
        ("gitlink", "choose_worktree"),
        [
            pytest.param(True, _move_gitlink_pointer, id="gitlink-pointer"),
            pytest.param(False, _use_unreceipted_checkout, id="unreceipted-root"),
        ],
    )
    def test_rejects_unadmitted_source_before_running(
        self, tmp_path: Path, gitlink: bool, choose_worktree: Callable[[_LocalGate], Path]
    ) -> None:
        """A mirror with an altered gitlink identity, or a checkout with no collector receipt, never runs the command.

        Local source execution only trusts the mirror that the collector produced; any other worktree must fail closed
        with a nonzero status before the command can leave its marker.
        """
        gate = _local_gate_inputs(tmp_path, committed=gitlink, gitlink=gitlink)
        marker = tmp_path / "executed.txt"
        worktree = choose_worktree(gate)

        result = _run_lint_gate(gate.repository, gate.output, None, _marker_command(marker), worktree)

        assert result.returncode != 0
        assert not marker.exists()

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                _GuardCase(
                    "sys.stderr.buffer.write(b'\\xff')",
                    b"\xff",
                    "fail",
                    0,
                    1,
                    tamper_check=_expect_validator_rejects_stripped_guard_error,
                ),
                id="invalid-utf8",
            ),
            pytest.param(
                _GuardCase(
                    "sys.stderr.buffer.write(b'x' * (524288 - 1) + b'\\xe2\\x82\\xac' + b'z' * (524288 - 1))",
                    b"x" * (524288 - 1) + b"\xe2\n[output truncated, omitted bytes: 1]\n\xac" + b"z" * (524288 - 1),
                    "fail",
                    0,
                    1,
                    tamper_check=_expect_validator_rejects_stripped_guard_error,
                ),
                id="truncated-utf8",
            ),
            pytest.param(_GuardCase("sys.exit(1)", b"", "fail", 1, 1), id="command-failed"),
            pytest.param(_GuardCase("sys.exit(126)", b"", "fail", 126, 1), id="launched-command-exits-126"),
            pytest.param(
                _GuardCase("sys.exit(127)", b"", "missing-command", 127, 1, reason_pattern=".+"),
                id="missing-command",
            ),
            pytest.param(
                _GuardCase(
                    "time.sleep(20)",
                    b"",
                    "timeout",
                    124,
                    124,
                    validator_status="timeout",
                    timeout_seconds="1",
                    reason_pattern="timeout after 1 seconds",
                ),
                id="timeout",
            ),
        ],
    )
    def test_source_guard_preserves_external_output_and_failure(self, tmp_path: Path, case: _GuardCase) -> None:
        """Raw child bytes and the command's failure disposition survive alongside the post-run source guard.

        Each child edits the mirror and then misbehaves in one way (invalid or truncated UTF-8, nonzero exit, missing
        command, timeout). The stderr receipt must keep the child's bytes verbatim before appending the guard error, its
        hash must be recorded, and the validator must agree with the runner on the overall status.
        """
        gate = _local_gate_inputs(tmp_path, committed=False)
        command = _python_command(
            "import sys,time;from pathlib import Path;"
            f"Path({str(gate.mirror / 'localmod.py')!r}).write_bytes(b'value = 99\\n');{case.body}"
        )
        arguments = [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(gate.output),
            "--worktree",
            str(gate.mirror),
            "--timeout-seconds",
            case.timeout_seconds,
            "--lint",
            command,
            *_skip_gates("lint"),
        ]

        completed = _run(arguments, cwd=gate.repository)

        check = _gate_check(gate.output, "lint")
        guard_error = check["source"]["guard_error"]
        stderr = (gate.output / str(check["stderr"])).read_bytes()
        validator = _load_validator()
        gates = validator._validate_gates(gate.output)
        assert completed.returncode == case.runner_exit, completed.stderr
        assert check["status"] == case.gate_status
        assert check["exit_code"] == case.exit_code
        assert check["execution_route"]["mode"] == "standard"
        assert guard_error
        assert stderr.startswith(case.raw_prefix)
        assert guard_error.encode("utf-8") in stderr
        assert check["source"]["artifacts"][check["stderr"]] == hashlib.sha256(stderr).hexdigest()
        assert re.fullmatch(case.reason_pattern, str(check.get("reason") or ""))
        assert gates["status"] == case.validator_status
        assert gates["checks_failed"] == ["lint"]
        case.tamper_check(validator, gate.output, check, stderr, guard_error)

    def test_parallel_workers_share_admitted_source_proof(self, tmp_path: Path) -> None:
        """Real xdist workers each retain the same admitted untracked module and test byte membership.

        Both workers must report the same ``local_source`` receipt as the aggregate, and the untracked files must be
        recorded as untracked for every worker.
        """
        gate = _local_gate_inputs(
            tmp_path,
            committed=False,
            pytest_arguments=["-q", "-n", "2", "-p", "no:cacheprovider", "-o", "addopts=", "test_localmod.py"],
        )

        result = _run(gate.args)

        proof = _gate_check(gate.output, "tests")["python_imports"]
        assert result.returncode == 0, result.stderr
        assert set(proof["workers"]) == {"gw0", "gw1"}
        assert all(worker["local_source"] == proof["local_source"] for worker in proof["workers"].values())
        assert proof["modules"]["localmod"]["tracked"] is False
        assert all(record["tracked"] is False for record in proof["tests"].values())

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda path, content: path.unlink(), id="deleted"),
            pytest.param(lambda path, content: path.write_bytes(content + b" "), id="changed"),
        ],
    )
    @pytest.mark.parametrize(
        ("conftest", "expected_status", "expected_exit", "timeout_seconds", "reason_pattern"),
        [
            pytest.param("", "fail", 0, 60, ".*", id="pass"),
            pytest.param(
                "def pytest_sessionfinish(session):\n    session.exitstatus = 127\n",
                "missing-command",
                127,
                60,
                ".+",
                id="missing-command",
            ),
            pytest.param(
                "import time\nfrom pathlib import Path\n"
                "def pytest_unconfigure(config):\n    Path({ready!r}).touch()\n    time.sleep(90)\n",
                "timeout",
                124,
                1,
                "timeout after 1 seconds",
                id="timeout",
            ),
        ],
    )
    def test_rejects_child_sidecar_drift(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mutate: Callable[[Path, bytes], None],
        conftest: str,
        expected_status: str,
        expected_exit: int,
        timeout_seconds: int,
        reason_pattern: str,
    ) -> None:
        """Detect a parent-published sidecar changed before the final mirror guard while preserving child exit."""
        ready = tmp_path / "pytest-teardown"
        gate = _local_gate_inputs(tmp_path, committed=False, conftest=conftest.format(ready=str(ready)))
        gates = _load_run_gates(monkeypatch)
        local_source = _local_source_receipt(gates, gate.output, gate.mirror)
        if expected_exit == 124:
            # Let the real capture deadline run against a live child that has already closed its proof file.
            # A missing teardown signal still reaches timeout cleanup after the bounded startup allowance.
            def capture_clock() -> Iterator[float]:
                yield 0.0
                startup_deadline = time.monotonic() + 60
                while not ready.is_file() and time.monotonic() < startup_deadline:
                    time.sleep(0.05)
                start = time.monotonic()
                while True:
                    yield time.monotonic() - start

            monkeypatch.setattr(gates, "time", mock.Mock(wraps=time, monotonic=capture_clock().__next__))

        publish = gates._publish_gate_proof

        def change_published_proof(path: Path, content: bytes, identity: os.stat_result) -> None:
            publish(path, content, identity)
            assert json.loads(content)["status"] == "pass"
            mutate(path, content)

        monkeypatch.setattr(gates, "_publish_gate_proof", change_published_proof)

        result = gates.run_check(
            "tests",
            "",
            "",
            timeout_seconds,
            gate.output,
            None,
            gate.mirror,
            gates.PythonTestSpec(
                Path(sys.executable), ("localmod",), ("-q", "-p", "no:xdist", "-o", "addopts=", "test_localmod.py")
            ),
            {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            local_source,
        )

        proof = result["python_imports"]
        assert proof["status"] == "pass"
        assert proof["modules"]["localmod"]["status"] == "pass"
        assert proof["tests"][(gate.mirror / "test_localmod.py").as_posix()]["status"] == "pass"
        if expected_exit == 124:
            assert ready.is_file()
            assert result["duration_seconds"] >= timeout_seconds
            assert "timeout: exceeded 1 seconds" in _receipt_text(gate.output, result)
        assert result["source"]["before"] == result["source"]["after"]
        assert result["status"] == expected_status
        assert result["exit_code"] == expected_exit
        assert re.fullmatch(reason_pattern, str(result.get("reason") or ""))
        assert "local-gate-proof-artifact-changed" in (gate.output / "checks/tests.stderr.txt").read_text(
            encoding="utf-8"
        )


def _local_source_receipt(gates: Any, output: Path, mirror: Path) -> dict[str, str]:
    """Write the local source snapshot for ``mirror`` and return the receipt describing it for ``run_check``."""
    (output / "checks").mkdir()
    content = gates._local_snapshot_bytes(gates.validate_local_gate_source(output, mirror))
    (output / "checks/local-source-snapshot.json").write_bytes(content)
    return {
        "mode": "local-review-mirror",
        "worktree": mirror.as_posix(),
        "receipt_path": "local-source/review-worktree.json",
        "receipt_sha256": hashlib.sha256((output / "local-source/review-worktree.json").read_bytes()).hexdigest(),
        "snapshot_path": "checks/local-source-snapshot.json",
        "snapshot_sha256": hashlib.sha256(content).hexdigest(),
    }


def test_gate_timeout_default_and_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give long gates two hours while preserving explicit timeout precedence."""
    monkeypatch.syspath_prepend(str(RUN_GATES.parent))
    runner = load_module("long_gate_runner", RUN_GATES)
    monkeypatch.delenv("GATE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(sys, "argv", [str(RUN_GATES), "--out", str(tmp_path)])
    assert runner.parse_args().timeout_seconds == 7200
    monkeypatch.setenv("GATE_TIMEOUT_SECONDS", "1800")
    assert runner.parse_args().timeout_seconds == 1800
    monkeypatch.setattr(sys, "argv", [str(RUN_GATES), "--out", str(tmp_path), "--timeout-seconds", "60"])
    assert runner.parse_args().timeout_seconds == 60


def test_project_environment_binds_nested_uv_run(tmp_path: Path) -> None:
    """A nested uv hook must use the selected disposable environment instead of a worktree default."""
    repository = tmp_path / "project"
    repository.mkdir()
    _initialize_repository(repository)
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "gate-environment-probe"\nversion = "0.0.0"\nrequires-python = ">=3.10"\n',
        encoding="utf-8",
    )
    _git(repository, "add", "pyproject.toml")
    _git(repository, "commit", "-m", f"project fixture\n\n{CODEX_TRAILER}")
    head = _git(repository, "rev-parse", "HEAD")
    (repository / ".git/info/exclude").write_text(".venv/\n", encoding="utf-8")
    environment = tmp_path / "selected-env"
    created = _run([sys.executable, "-m", "venv", "--without-pip", str(environment)])
    assert created.returncode == 0, created.stderr
    check = f"import sys; from pathlib import Path; assert Path(sys.prefix) == Path({str(environment)!r})"
    command = _python_command(
        f"import subprocess; subprocess.run(['uv', '--cache-dir', {str(tmp_path / 'uv-cache')!r}, 'run', '--offline', '--no-sync', "
        f"'--python', {sys.executable!r}, 'python', '-c', {check!r}], check=True)"
    )
    result = _run(
        [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(tmp_path / "gates"),
            "--worktree",
            str(repository),
            "--expected-head",
            head,
            "--project-env",
            str(environment),
            "--types",
            command,
            *_skip_gates("types"),
        ],
        cwd=repository,
    )
    assert result.returncode == 0, result.stderr
    assert not (repository / ".venv").exists()


@pytest.mark.integration
@pytest.mark.parametrize("parallel", [pytest.param(False, id="serial"), pytest.param(True, id="xdist")])
def test_clean_source_proof_needs_no_git_in_pytest_processes(
    tmp_path: Path, repository: Path, output: Path, parallel: bool
) -> None:
    """The parent supplies index membership so confined controllers and workers never need to invoke Git."""
    audit_directory = tmp_path / "audit"
    audit_directory.mkdir()
    (repository / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "test_first.py").write_text("import example\ndef test_value(): assert example.VALUE == 1\n")
    (repository / "test_žluťoučký.py").write_text("def test_value(): assert True\n", encoding="utf-8")
    (repository / "test_third.py").write_text("def test_value(): assert True\n")
    (repository / "conftest.py").write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        f"AUDIT = Path({str(audit_directory)!r})\n"
        "def audit(event, args):\n"
        "    if event == 'subprocess.Popen' and 'ls-files' in args[1]:\n"
        "        with (AUDIT / (str(os.getpid()) + '.jsonl')).open('a', encoding='utf-8') as stream:\n"
        "            stream.write(json.dumps(args[1]) + '\\n')\n"
        "sys.addaudithook(audit)\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", f"inventory fixture\n\n{CODEX_TRAILER}")
    arguments = ["-q", "-p", "no:cacheprovider", "-o", "addopts="] + (["-n", "2"] if parallel else ["-p", "no:xdist"])

    result = _run(
        TestPytestGateRecovery._tests_gate_command(
            output,
            repository,
            _git(repository, "rev-parse", "HEAD"),
            sys.executable,
            "--pytest-args-json",
            json.dumps(arguments),
        )
    )

    assert result.returncode == 0, result.stderr
    proof = _gate_check(output, "tests")["python_imports"]
    inventories = [path.read_text(encoding="utf-8").splitlines() for path in audit_directory.glob("*.jsonl")]
    assert proof["status"] == "pass"
    assert len(proof["tests"]) == 3
    assert proof["modules"]["example"]["tracked"] is True
    assert inventories == []


@pytest.mark.parametrize(
    ("filename", "tracked"),
    [
        pytest.param("test_line\nbreak.py", True, id="newline"),
        pytest.param("test_žluťoučký.py", True, id="non-ascii"),
        pytest.param("test_line", False, id="partial-line"),
        pytest.param("test_*.py", False, id="literal-pathspec"),
        pytest.param(".", False, id="worktree-root"),
    ],
)
def test_source_inventory_matches_exact_nul_delimited_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str, tracked: bool
) -> None:
    """Git's raw NUL framing preserves unusual filenames and never interprets an origin as a pathspec."""
    gates = _load_run_gates(monkeypatch)
    inventory = b"test_line\nbreak.py\0" + os.fsencode("test_žluťoučký.py") + b"\0"
    git = mock.Mock(spec=subprocess.run, return_value=subprocess.CompletedProcess([], 0, stdout=inventory))
    monkeypatch.setattr(gates.subprocess, "run", git)

    result = gates.inspect_collected_test(tmp_path / filename, tmp_path)

    assert result["tracked"] is tracked
    assert result["status"] == ("pass" if tracked else "fail")
    git.assert_called_once_with(
        ["git", "ls-files", "-z", "--", f":(literal){filename}"],
        cwd=tmp_path,
        env=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=gates.SOURCE_INSPECTION_TIMEOUT_SECONDS,
    )


@pytest.mark.parametrize(
    "git_result",
    [
        pytest.param(subprocess.CompletedProcess([], 1, stdout=b"test_file.py\0"), id="partial-output-failure"),
        pytest.param(OSError("git unavailable"), id="unavailable"),
        pytest.param(subprocess.TimeoutExpired("git", 1), id="timeout"),
    ],
)
def test_source_inventory_failure_rejects_clean_tracking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, git_result: subprocess.CompletedProcess[bytes] | Exception
) -> None:
    """An unsuccessful Git inventory cannot certify a collected file even when it emitted that file's name."""
    gates = _load_run_gates(monkeypatch)
    git = mock.Mock(spec=subprocess.run, side_effect=[git_result])
    monkeypatch.setattr(gates.subprocess, "run", git)

    result = gates.inspect_collected_test(tmp_path / "test_file.py", tmp_path)

    assert result == {
        "origin": (tmp_path / "test_file.py").resolve().as_posix(),
        "tracked": False,
        "status": "fail",
        "reason": "test-not-tracked",
    }
    git.assert_called_once()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        pytest.param("content", "tracked-source-inventory-changed", id="replaced-inventory"),
        pytest.param("worktree", "tracked-source-inventory-identity-mismatch", id="different-worktree"),
        pytest.param("head", "tracked-source-inventory-identity-mismatch", id="different-head"),
    ],
)
def test_pytest_rejects_mismatched_tracked_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, error: str
) -> None:
    """Reject changed inventory bytes and cross-checkout or cross-commit bindings before collecting tests."""
    gates = _load_run_gates(monkeypatch)
    head = "a" * 40
    content = b"\0".join((os.fsencode(tmp_path.as_posix()), head.encode("ascii"), b"test_source.py"))
    inventory = tmp_path / "tracked-paths"
    inventory.write_bytes(content + (b"extra" if change == "content" else b""))
    task = {
        "worktree": (tmp_path / "other" if change == "worktree" else tmp_path).as_posix(),
        "modules": ["demo"],
        "tracked_inventory": {
            "path": str(inventory),
            "head": "b" * 40 if change == "head" else head,
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    }
    monkeypatch.setenv("CODEX_RIG_PYTEST_SOURCE_PROOF", json.dumps(task))
    with pytest.raises(ValueError, match=error):
        gates.pytest_configure(None)


def test_parent_rejects_changed_index_membership_after_passing_pytest(
    editable_checkout: _EditableCheckout, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a passing child receipt when the parent cannot reproduce the original tracked-file set."""
    checkout = editable_checkout
    checkout.link_editable(checkout.selected)
    gates = _load_run_gates(monkeypatch)
    index = ["git", "ls-files", "-z", "--"]
    run = subprocess.run
    original = run(index, cwd=checkout.selected, capture_output=True, check=True).stdout
    test_marker = checkout.selected.parent / "selected-test-ran.txt"
    review_marker = checkout.selected.parent / "review-started"
    plugin = Path(checkout.environment["PYTHONPATH"]) / "fixture_progress.py"
    plugin.write_text(
        "import sys\n\ndef pytest_sessionfinish(session, exitstatus):\n    sys.stderr.write('progress')\n",
        encoding="utf-8",
    )
    changes = []

    def observe_postrun_index(arguments, *args, **kwargs):
        """Report one changed live membership after the real pytest fixture records that it ran."""
        if list(arguments) == index and kwargs.get("cwd") == checkout.selected and test_marker.exists() and not changes:
            changes.append(True)
            return subprocess.CompletedProcess(arguments, 0, stdout=original + b"fixture-notes.txt\0")
        return run(arguments, *args, **kwargs)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RUN_GATES),
            "--out",
            str(output),
            "--worktree",
            str(checkout.selected),
            "--expected-head",
            checkout.expected_head,
            "--pytest-python",
            sys.executable,
            "--pytest-import",
            "demo_package",
            "--pytest-args-json",
            json.dumps(["-q", "-s", "-p", "fixture_progress", "-p", "no:xdist", "-o", "addopts="]),
            "--review",
            _marker_command(review_marker),
            "--skip-lint",
            "not selected",
            "--skip-format",
            "not selected",
            "--skip-types",
            "not selected",
        ],
    )
    for key, value in checkout.environment.items():
        monkeypatch.setenv(key, value)
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", observe_postrun_index)
        assert gates.main() == 1
    assert changes == [True]
    assert test_marker.exists()
    assert not review_marker.exists()
    result = _gate_check(output, "tests")
    assert result["python_imports"]["status"] == "pass"
    assert result["status"] == "fail"
    assert result["exit_code"] == 0
    assert (output / result["stderr"]).read_bytes() == b"progress\ntracked-source-inventory-changed\n"
    validator = _load_validator()
    assert validator._validate_gates(output)["checks_failed"] == ["tests", "review"]
    assert result["source"]["guard_error"] == "tracked-source-inventory-changed"
    assert _gate_check(output, "review")["status"] == "fail"
    assert _gate_check(output, "review")["source"]["after"] is None
    inventory = next((output / "checks").glob("tracked-source-*.bin"))
    admitted = inventory.read_bytes()
    root, head, *names = admitted.split(b"\0")
    assert names == sorted(original.split(b"\0")[:-1])
    changed = b"\0".join([root, head, *sorted([*names, b"fixture-notes.txt"])])
    assert result["source"]["after"]["expected_inventory_sha256"] == hashlib.sha256(admitted).hexdigest()
    assert result["source"]["after"]["observed_inventory_sha256"] == hashlib.sha256(changed).hexdigest()
    retry = _run([sys.executable, *sys.argv], cwd=checkout.original, env=checkout.environment)
    assert retry.returncode == 0, retry.stderr
    assert review_marker.exists()
    assert validator._validate_gates(output / "gate-attempts/001")["checks_failed"] == ["tests", "review"]
    assert validator._validate_gates(output)["checks_failed"] == []


@pytest.mark.parametrize(
    "redirect",
    [
        pytest.param(False, id="missing-input"),
        pytest.param(
            True, marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="needs symlinks"), id="symlink"
        ),
    ],
)
def test_retry_recovers_missing_inventory_and_refuses_redirect_before_archiving(
    editable_checkout: _EditableCheckout, output: Path, tmp_path: Path, redirect: bool
) -> None:
    """Preserve a missing input as a failed observation while refusing to follow a redirected index."""
    editable_checkout.link_editable(editable_checkout.selected)
    first = editable_checkout.run_tests(output)
    assert first.returncode == 0, first.stderr
    source = next((output / "checks").glob("tracked-source-*.bin"))
    outside = tmp_path / "outside"
    source.rename(outside)
    content = outside.read_bytes()
    if redirect:
        source.symlink_to(outside)
    retry = editable_checkout.run_tests(output)
    if redirect:
        assert retry.returncode == 2
        assert "tracked-source-inventory-invalid-file" in retry.stderr
        assert not (output / "gate-attempts").exists()
    else:
        assert retry.returncode == 0, retry.stderr
        errors = json.loads((output / "gate-diagnostics/001/tracked-source-errors.json").read_text(encoding="utf-8"))
        assert errors == {source.relative_to(output).as_posix(): None}
        assert source.read_bytes() == content
        assert not (output / "gate-attempts").exists()
    assert outside.read_bytes() == content


def test_retry_preserves_corrupt_inventory_and_recreates_admitted_bytes(
    editable_checkout: _EditableCheckout, output: Path
) -> None:
    """Recover the executable check while retaining the old receipt together with its actual mismatched input."""
    editable_checkout.link_editable(editable_checkout.selected)
    first = editable_checkout.run_tests(output)
    assert first.returncode == 0, first.stderr
    inventory = next((output / "checks").glob("tracked-source-*.bin"))
    original = inventory.read_bytes()
    prior = (output / "gates.json").read_bytes()
    inventory.write_bytes(b"corrupt previous attempt")
    original_error = output / "checks/tests.stderr.txt"
    saved_error = original_error.read_bytes()
    original_error.write_bytes(saved_error + b"also changed")
    blocked = editable_checkout.run_tests(output)
    assert blocked.returncode == 2, blocked.stderr
    assert "gate-evidence-digest-mismatch" in blocked.stderr
    assert not list((output / "gate-attempts").glob("[0-9]*"))
    assert (output / "gates.json").read_bytes() == prior
    original_error.write_bytes(saved_error)
    second = editable_checkout.run_tests(output)
    assert second.returncode == 0, second.stderr
    archive = output / "gate-diagnostics/001"
    assert (archive / "gates.json").read_bytes() == prior
    assert (archive / "checks" / inventory.name).read_bytes() == b"corrupt previous attempt"
    errors = json.loads((archive / "tracked-source-errors.json").read_text(encoding="utf-8"))
    assert errors == {inventory.relative_to(output).as_posix(): hashlib.sha256(b"corrupt previous attempt").hexdigest()}
    assert inventory.read_bytes() == original
    assert _gate_check(output, "tests")["status"] == "pass"
    with pytest.raises(SystemExit, match="gate-evidence-digest-mismatch"):
        _load_validator()._validate_gates(archive)
    assert not (output / "gate-attempts").exists()
    assert editable_checkout.run_tests(output).returncode == 0
    assert _load_validator()._validate_gates(output / "gate-attempts/001")["checks_failed"] == []


@pytest.mark.parametrize(
    ("history", "log_suffix"),
    [pytest.param("gate-attempts", b"", id="complete"), pytest.param("gate-diagnostics", b"modified", id="damaged")],
)
def test_retry_refuses_occupied_history_root_before_execution(
    repository: Path, head: str, output: Path, tmp_path: Path, history: str, log_suffix: bytes
) -> None:
    """An ordinary file at the selected history root blocks execution and remains intact until its owner moves it."""
    first = _run_lint_gate(repository, output, head, _python_command("print('original')"), worktree=repository)
    assert first.returncode == 0, first.stderr
    prior = (output / "gates.json").read_bytes()
    stream = output / "checks/lint.stdout.txt"
    prior_stdout = stream.read_bytes() + log_suffix
    stream.write_bytes(prior_stdout)
    history_root = output / history
    history_root.write_bytes(b"independent history note")
    identity = history_root.stat()
    marker = tmp_path / "lint-started"

    blocked = _run_lint_gate(repository, output, head, _marker_command(marker), worktree=repository)
    assert blocked.returncode == 2, blocked.stderr
    assert f"gate-recovery-blocked:gate-history-root-occupied:{history}\n" in blocked.stderr
    assert not marker.exists()
    assert os.path.samestat(identity, history_root.stat())
    assert history_root.read_bytes() == b"independent history note"
    assert (output / "gates.json").read_bytes() == prior
    assert stream.read_bytes() == prior_stdout
    assert not (output / ".gate-publication.json").exists()

    history_root.rename(tmp_path / "retained-history-note")
    retry = _run_lint_gate(repository, output, head, _marker_command(marker), worktree=repository)
    assert retry.returncode == 0, retry.stderr
    assert marker.exists()
    assert (history_root / "001/gates.json").read_bytes() == prior
    assert (history_root / "001/checks/lint.stdout.txt").read_bytes() == prior_stdout
    assert _load_validator()._validate_gates(output)["checks_failed"] == []


@pytest.fixture
def structured_inventory_retry(
    editable_checkout: _EditableCheckout, output: Path
) -> tuple[list[str], Path, Path, Path]:
    """Prepare a free-form current receipt and a structured retry whose selected input belongs to an older attempt."""
    editable_checkout.link_editable(editable_checkout.selected)
    pytest_arguments = ["-q", "-p", "no:xdist", "-o", "addopts=", "tests/test_demo.py"]
    initial = editable_checkout.run_tests(output, pytest_arguments=pytest_arguments)
    assert initial.returncode == 0, initial.stderr
    test_marker = editable_checkout.selected.parent / "selected-test-ran.txt"
    test_marker.unlink()
    inventory = next((output / "checks").glob("tracked-source-*.bin"))
    assert _run_lint_gate(editable_checkout.original, output, None, _python_command("print('lint')")).returncode == 0
    # Lint precedes structured pytest, so both markers must stay absent when its future inventory is occupied.
    lint_marker = editable_checkout.selected.parent / "lint-started"
    command = list(initial.args)
    lint = command.index("--skip-lint")
    command[lint : lint + 2] = ["--lint", _marker_command(lint_marker)]
    return command, inventory, lint_marker, test_marker


@pytest.mark.parametrize(
    ("failed_reads", "error", "attempts"),
    [
        pytest.param(0, "tracked-source-inventory-changed", 1, id="readable-index"),
        pytest.param(1, "tracked-source-inventory-changed", 2, id="transient-git-failure"),
        pytest.param(2, "tracked-source-inventory-unavailable", 2, id="unavailable-index"),
    ],
)
def test_structured_retry_preserves_colliding_inventory_from_an_older_attempt(
    editable_checkout: _EditableCheckout,
    output: Path,
    structured_inventory_retry: tuple[list[str], Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed_reads: int,
    error: str,
    attempts: int,
) -> None:
    """A current free-form receipt cannot authorize repairing a file last used by an older structured attempt."""
    command, inventory, lint_marker, test_marker = structured_inventory_retry
    original = inventory.read_bytes()
    prior = (output / "gates.json").read_bytes()
    inventory.write_bytes(b"other owner's replacement")
    identity = inventory.stat()
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    monkeypatch.setattr(sys, "argv", command[1:])
    run = subprocess.run
    prelaunch_reads = []

    def read_committed_paths(arguments, *args, **kwargs):
        """Fault only the requested commit's membership read, leaving child-side index checks intact."""
        if (
            "ls-tree" in arguments
            and f"{editable_checkout.expected_head}^{{commit}}" in arguments
            and kwargs.get("cwd") == editable_checkout.selected
            and not lint_marker.exists()
        ):
            prelaunch_reads.append(arguments)
            if len(prelaunch_reads) <= failed_reads:
                return subprocess.CompletedProcess(arguments, 128, stdout=b"")
        return run(arguments, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", read_committed_paths)
        assert gates.main() == 2
    assert error in capsys.readouterr().err
    assert not lint_marker.exists()
    assert not test_marker.exists()
    assert len(prelaunch_reads) == attempts
    assert os.path.samestat(identity, inventory.stat())
    assert inventory.read_bytes() == b"other owner's replacement"
    assert (output / "gates.json").read_bytes() == prior
    assert not (output / "gate-attempts/002").exists()
    inventory.write_bytes(original)
    prelaunch_reads.clear()
    failed_reads = min(failed_reads, 1)
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", read_committed_paths)
        assert gates.main() == 0, capsys.readouterr().err
    assert len(prelaunch_reads) == failed_reads + 1
    assert lint_marker.exists()
    assert test_marker.exists()
    assert (output / "gate-attempts/002/gates.json").read_bytes() == prior
    validator = _load_validator()
    assert validator._validate_gates(output / "gate-attempts/002")["status"] == "pass"
    assert validator._validate_gates(output)["status"] == "pass"


@pytest.mark.parametrize(
    ("failed_reads", "diagnostic"),
    [
        pytest.param(1, "tracked-source-inventory-changed", id="inspection-recovers"),
        pytest.param(2, "source-inspection-failed", id="inspection-remains-unavailable"),
    ],
)
def test_inventory_collision_with_unavailable_checkout_never_launches_gates(
    editable_checkout, output, structured_inventory_retry, monkeypatch, capsys, failed_reads, diagnostic
):
    """Retry the separate checkout inspection once and keep the current receipt while the input conflict remains."""
    command, inventory, lint_marker, test_marker = structured_inventory_retry
    inventory.write_bytes(b"separate input")
    identity = inventory.stat()
    manifest = (output / "gates.json").read_bytes()
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    monkeypatch.setattr(sys, "argv", command[1:])
    run = subprocess.run
    status_reads = []

    def inspect_status(arguments, *args, **kwargs):
        """Fault the checkout status read alone, leaving all membership reads and later Git operations intact."""
        if "status" in arguments and kwargs.get("cwd") == editable_checkout.selected:
            status_reads.append(arguments)
            if len(status_reads) <= failed_reads:
                return subprocess.CompletedProcess(arguments, 128, stdout=b"", stderr=b"temporary status failure")
        return run(arguments, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", inspect_status)
        assert gates.main() == 2
    assert diagnostic in capsys.readouterr().err
    assert not lint_marker.exists()
    assert not test_marker.exists()
    assert len(status_reads) == 2
    assert inventory.read_bytes() == b"separate input"
    assert os.path.samestat(identity, inventory.stat())
    assert (output / "gates.json").read_bytes() == manifest
    assert not (output / "gate-attempts/002").exists()
    assert _load_validator()._validate_gates(output)["checks_failed"] == []


@pytest.fixture(params=["dirty", "different-head", "dirty-then-clean"])
def mismatched_inventory_checkout(request, editable_checkout, structured_inventory_retry):
    """Create a genuine checkout mismatch after preparing the original clean receipt and its retry arguments."""
    checkout = editable_checkout.selected
    tracked = checkout / "tracked.txt"
    original = tracked.read_bytes()
    if request.param == "different-head":
        _git(checkout, "commit", "--allow-empty", "-m", f"different checkout\n\n{CODEX_TRAILER}")
        reason = HEAD_MISMATCH
    else:
        tracked.write_bytes(b"modified checkout contents\n")
        reason = SOURCE_DIRTY_STATUS
    return request.param, original, reason


def test_confirmed_checkout_mismatch_records_every_applicable_gate_without_execution(
    editable_checkout, output, structured_inventory_retry, mismatched_inventory_checkout, monkeypatch
):
    """A confirmed mismatch remains the before observation in the failed receipt, including after external cleanup."""
    state, original, reason = mismatched_inventory_checkout
    command, inventory, lint_marker, test_marker = structured_inventory_retry
    checkout = editable_checkout.selected
    observed_head = _git(checkout, "rev-parse", "HEAD")
    prior = (output / "gates.json").read_bytes()
    inventory.write_bytes(b"separate input")
    identity = inventory.stat()
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    monkeypatch.setattr(sys, "argv", command[1:])
    run = subprocess.run

    def read_then_clean(arguments, *args, **kwargs):
        """Restore the fixture only after Git has observed the dirty checkout in the cleanup case."""
        result = run(arguments, *args, **kwargs)
        if state == "dirty-then-clean" and "status" in arguments and kwargs.get("cwd") == checkout and result.stdout:
            (checkout / "tracked.txt").write_bytes(original)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", read_then_clean)
        assert gates.main() == 1
    assert not lint_marker.exists()
    assert not test_marker.exists()
    assert bool(_git(checkout, "status", "--porcelain")) == (state == "dirty")
    assert inventory.read_bytes() == b"separate input"
    assert os.path.samestat(identity, inventory.stat())
    validator = _load_validator()
    archived = output / "gate-attempts/002"
    assert (archived / "gates.json").read_bytes() == prior
    assert validator._validate_gates(archived)["status"] == "pass"
    receipt = validator._validate_gates(output)
    assert (receipt["checks_failed"], receipt["checks_refused"]) == ([], ["lint", "tests"])
    assert receipt["checks_not_applicable"] == ["format", "types", "review"]
    lint = _gate_check(output, "lint")
    tests = _gate_check(output, "tests")
    assert lint["source"]["before"] == tests["source"]["before"]
    assert lint["source"]["before"]["head"] == observed_head
    assert lint["source"]["after"] is None
    assert tests["source"]["after"] is None
    assert reason in _receipt_text(output, lint)
    assert reason in _receipt_text(output, tests)


@pytest.mark.parametrize("later_membership", ["staged", "clean"])
def test_conflicting_committed_inventory_prevents_dispatch_after_staged_membership(
    editable_checkout, output, structured_inventory_retry, later_membership, monkeypatch
):
    """Keep the first staged observation when the clean commit's inventory is occupied, including after cleanup."""
    command, inventory, lint_marker, test_marker = structured_inventory_retry
    checkout = editable_checkout.selected
    prior = (output / "gates.json").read_bytes()
    inventory.write_bytes(b"independent retained input")
    identity = inventory.stat()
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    monkeypatch.setattr(sys, "argv", command[1:])
    index = ("git", "ls-files", "-z", "--")
    status = (*gates.SOURCE_GIT, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")
    run = subprocess.run
    clean = {args: run(args, cwd=checkout, capture_output=True, check=True) for args in (index, status)}
    note = checkout / "fixture-notes.txt"
    note.write_bytes(b"fixture notes\n")
    _git(checkout, "add", "--", note.name)
    staged = {args: run(args, cwd=checkout, capture_output=True, check=True) for args in (index, status)}
    assert set(staged[index].stdout.split(b"\0")) - set(clean[index].stdout.split(b"\0")) == {note.name.encode()}
    assert staged[status].stdout.strip() == b"A  fixture-notes.txt"
    assert clean[status].stdout == b""
    observations = []

    def replay_observation(arguments, *args, **kwargs):
        """Return captured Git results without changing the fixture checkout while the runner is inspecting it."""
        key = tuple(arguments)
        if kwargs.get("cwd") == checkout and key in staged:
            snapshot = clean if observations and later_membership == "clean" else staged
            if key == status:
                observations.append(snapshot[status].stdout)
            return snapshot[key]
        return run(arguments, *args, **kwargs)

    def unexpected_workload(*args, **kwargs):
        """Record a dispatch decision at the executor boundary without starting the workload."""
        pytest.fail("workload requested after observing conflicting membership")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", replay_observation)
        patch.setattr(gates, "execute_command", unexpected_workload)
        assert gates.main() == 1
    assert observations[0] == staged[status].stdout
    assert not lint_marker.exists()
    assert not test_marker.exists()
    assert inventory.read_bytes() == b"independent retained input"
    assert os.path.samestat(identity, inventory.stat())
    validator = _load_validator()
    archive = output / "gate-attempts/002"
    assert (archive / "gates.json").read_bytes() == prior
    assert validator._validate_gates(archive)["status"] == "pass"
    assert validator._validate_gates(output)["checks_refused"] == ["lint", "tests"]
    lint = _gate_check(output, "lint")
    tests = _gate_check(output, "tests")
    assert lint["source"]["before"] == tests["source"]["before"]
    assert lint["source"]["before"]["status"] == "A  fixture-notes.txt"
    assert lint["source"]["after"] is None
    assert tests["source"]["after"] is None


@pytest.mark.parametrize(
    ("observation", "diagnostic"),
    [
        pytest.param("membership", "tracked-source-inventory-changed", id="live-membership"),
        pytest.param("checkout", SOURCE_DIRTY_STATUS, id="checkout-status"),
    ],
)
def test_post_command_drift_preserves_completed_gate_and_stops_remaining_workloads(
    editable_checkout, output, structured_inventory_retry, tmp_path, monkeypatch, observation, diagnostic
):
    """Preserve completed lint and format outcomes when an independently observed checkout change later clears."""
    command, inventory, lint_marker, test_marker = structured_inventory_retry
    checkout = editable_checkout.selected
    prior = (output / "gates.json").read_bytes()
    admitted = inventory.read_bytes()
    format_marker = tmp_path / "format-ran"
    review_marker = tmp_path / "review-ran"
    position = command.index("--skip-format")
    command[position : position + 2] = ["--format", _marker_command(format_marker)]
    position = command.index("--skip-review")
    command[position : position + 2] = ["--review", _marker_command(review_marker)]
    gates = _load_run_gates(monkeypatch)
    index = ["git", "ls-files", "-z", "--"]
    status = [*gates.SOURCE_GIT, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"]
    selected = {"membership": index, "checkout": status}[observation]
    run = subprocess.run
    # Capture a genuine local change and restore the fixture before starting any gate.
    note = checkout / "fixture-notes.txt"
    note.write_bytes(b"fixture notes\n")
    _git(checkout, "add", "--", note.name)
    changed = run(selected, cwd=checkout, capture_output=True, check=True)
    assert note.name.encode() in changed.stdout
    _git(checkout, "restore", "--staged", "--", note.name)
    note.rename(tmp_path / "retained-fixture-notes.txt")
    assert not _git(checkout, "status", "--porcelain")
    delivered = []

    def observe_after_format(arguments, *args, **kwargs):
        """Return the changed observation once after format executes, then resume real reads of the clean fixture."""
        if list(arguments) == selected and kwargs.get("cwd") == checkout and format_marker.exists() and not delivered:
            delivered.append(True)
            return changed
        return run(arguments, *args, **kwargs)

    execute = gates.execute_command

    def observe_workload(command, timeout, stdout, stderr, *args, **kwargs):
        """Reject a later dispatch at the executor boundary without starting another fixture process."""
        assert stdout.name not in {"tests.stdout.txt", "review.stdout.txt"}, "later workload requested"
        return execute(command, timeout, stdout, stderr, *args, **kwargs)

    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    monkeypatch.setattr(sys, "argv", command[1:])
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", observe_after_format)
        patch.setattr(gates, "execute_command", observe_workload)
        assert gates.main() == 1
    assert lint_marker.exists()
    assert format_marker.exists()
    assert delivered == [True]
    assert not test_marker.exists()
    assert not review_marker.exists()
    assert inventory.read_bytes() == admitted
    assert list((output / "checks").glob("tracked-source-*.bin")) == [inventory]
    validator = _load_validator()
    receipt = validator._validate_gates(output)
    assert receipt["checks_failed"] == ["format", "tests", "review"]
    assert receipt["checks_not_applicable"] == ["types"]
    by_id = {row["id"]: row for row in receipt["checks"]}
    assert by_id["lint"]["status"] == "pass"
    assert by_id["lint"]["exit_code"] == 0
    assert by_id["lint"]["source"]["after"]["head"] == editable_checkout.expected_head
    assert by_id["format"]["exit_code"] == 0
    assert by_id["format"]["source"]["after"] == by_id["tests"]["source"]["before"]
    assert by_id["format"]["source"]["after"] == by_id["review"]["source"]["before"]
    assert diagnostic in _receipt_text(output, by_id["format"])
    assert diagnostic in _receipt_text(output, by_id["tests"])
    assert diagnostic in _receipt_text(output, by_id["review"])
    assert by_id["tests"]["source"]["after"] is None
    assert by_id["review"]["source"]["after"] is None
    archive = output / "gate-attempts/002"
    assert (archive / "gates.json").read_bytes() == prior
    assert validator._validate_gates(archive)["status"] == "pass"


@pytest.mark.parametrize("occupied_membership", ["live", "pinned"])
def test_clean_status_with_different_index_membership_stops_before_lint(
    editable_checkout, output, structured_inventory_retry, monkeypatch, occupied_membership
):
    """A confirmed membership mismatch fails before dispatch and preserves either occupied inventory."""
    command, admitted_inventory, lint_marker, test_marker = structured_inventory_retry
    checkout = editable_checkout.selected
    tree = admitted_inventory.read_bytes()
    root, head, *names = tree.split(b"\0")
    assert root == os.fsencode(checkout.as_posix())
    assert head == editable_checkout.expected_head.encode()
    alternate_names = sorted([*names, b"other-tracked-note.txt"])
    alternate = b"\0".join([root, head, *alternate_names])
    name = f"tracked-source-{hashlib.sha256(alternate).hexdigest()}.bin"
    foreign = admitted_inventory if occupied_membership == "pinned" else admitted_inventory.with_name(name)
    foreign.write_bytes(b"other report's input")
    identity = foreign.stat()
    public_inventories = {admitted_inventory: tree, foreign: b"other report's input"}
    prior = (output / "gates.json").read_bytes()
    assert not _git(checkout, "status", "--porcelain")
    gates = _load_run_gates(monkeypatch)
    run = subprocess.run

    def read_alternate_index(arguments, *args, **kwargs):
        """Model the alternate live membership while the commit lookup and checkout observation remain real."""
        if list(arguments) == ["git", "ls-files", "-z", "--"] and kwargs.get("cwd") == checkout:
            return subprocess.CompletedProcess(arguments, 0, stdout=b"\0".join(alternate_names) + b"\0")
        return run(arguments, *args, **kwargs)

    def unexpected_workload(*args, **kwargs):
        pytest.fail("workload requested with an unadmitted file list")

    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    monkeypatch.setattr(sys, "argv", command[1:])
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", read_alternate_index)
        patch.setattr(gates, "execute_command", unexpected_workload)
        assert gates.main() == 1
    assert not lint_marker.exists()
    assert not test_marker.exists()
    assert {path: path.read_bytes() for path in foreign.parent.glob("tracked-source-*.bin")} == public_inventories
    assert os.path.samestat(identity, foreign.stat())
    validator = _load_validator()
    assert validator._validate_gates(output)["checks_refused"] == ["lint", "tests"]
    before = _gate_check(output, "lint")["source"]["before"]
    assert before["expected_inventory_sha256"] == hashlib.sha256(tree).hexdigest()
    assert before["observed_inventory_sha256"] == hashlib.sha256(alternate).hexdigest()
    assert "tracked-source-inventory-changed" in _receipt_text(output, _gate_check(output, "lint"))
    assert "tracked-source-inventory-changed" in _receipt_text(output, _gate_check(output, "tests"))
    archive = output / "gate-attempts/002"
    assert (archive / "gates.json").read_bytes() == prior
    assert validator._validate_gates(archive)["status"] == "pass"


def test_postrun_checkout_and_index_reads_recover_independently(
    editable_checkout, output, structured_inventory_retry, monkeypatch
):
    """A transient checkout read and a later transient membership read each recover without rerunning lint."""
    command, inventory, lint_marker, test_marker = structured_inventory_retry
    command[command.index("--lint") + 1] = _python_command(
        f"from pathlib import Path\nwith Path({str(lint_marker)!r}).open('ab') as stream:\n    stream.write(b'lint')"
    )
    admitted = inventory.read_bytes()
    prior = (output / "gates.json").read_bytes()
    checkout = editable_checkout.selected
    gates = _load_run_gates(monkeypatch)
    reads = []
    run = subprocess.run
    status = [*gates.SOURCE_GIT, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"]
    index = ["git", "ls-files", "-z", "--"]

    def read_after_lint(arguments, *args, **kwargs):
        """Fail the first read at each Git boundary after lint has really run."""
        if kwargs.get("cwd") == checkout and lint_marker.exists() and list(arguments) in (status, index):
            kind = "status" if list(arguments) == status else "membership"
            if (kind, "unavailable") not in reads:
                reads.append((kind, "unavailable"))
                return subprocess.CompletedProcess(arguments, 128, stdout=b"", stderr=b"temporary read failure")
            reads.append((kind, "read"))
        return run(arguments, *args, **kwargs)

    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    monkeypatch.setattr(sys, "argv", command[1:])
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", read_after_lint)
        assert gates.main() == 0
    assert reads[:4] == [
        ("status", "unavailable"),
        ("status", "read"),
        ("membership", "unavailable"),
        ("membership", "read"),
    ]
    assert lint_marker.read_bytes() == b"lint"
    assert test_marker.exists()
    assert inventory.read_bytes() == admitted
    validator = _load_validator()
    assert validator._validate_gates(output)["checks_failed"] == []
    archive = output / "gate-attempts/002"
    assert (archive / "gates.json").read_bytes() == prior
    assert validator._validate_gates(archive)["checks_failed"] == []


def test_structured_retry_preserves_other_inventory_while_publishing_its_selected_input(
    editable_checkout, output, structured_inventory_retry
):
    """A different digest-named public input stays in place while a clean retry publishes the selected inventory."""
    command, inventory, lint_marker, test_marker = structured_inventory_retry
    selected = inventory.read_bytes()
    prior = (output / "gates.json").read_bytes()
    other_content = b"different retained input"
    other = inventory.with_name(f"tracked-source-{hashlib.sha256(other_content).hexdigest()}.bin")
    assert other != inventory
    inventory.rename(other)
    other.write_bytes(other_content)
    identity = other.stat()
    retry = _run(command, cwd=editable_checkout.original, env=editable_checkout.environment)
    assert retry.returncode == 0, retry.stderr
    assert lint_marker.exists()
    assert test_marker.exists()
    assert other.read_bytes() == other_content
    assert os.path.samestat(identity, other.stat())
    assert inventory.read_bytes() == selected
    validator = _load_validator()
    archive = output / "gate-attempts/002"
    assert (archive / "gates.json").read_bytes() == prior
    assert not (archive / "checks" / other.name).exists()
    assert validator._validate_gates(archive)["checks_failed"] == []
    assert validator._validate_gates(output)["checks_failed"] == []


@pytest.fixture
def tracked_file_comparison(request, editable_checkout, structured_inventory_retry, monkeypatch):
    """Supply read-only index observations for local text files and record their real content comparisons."""
    state, unavailable_reads = request.param
    checkout = editable_checkout.selected
    mode = {"executable": "100755", "link": "120000"}.get(state, "100644")
    indexed_file = ".gitignore" if state in {"content", "two-file-comparison"} else "tracked.txt"
    indexed_hash = _git(checkout, "rev-parse", f"HEAD:{indexed_file}")
    assert not (checkout / "tracked.txt").is_symlink()
    assert _git(checkout, "hash-object", "tracked.txt") != _git(checkout, "rev-parse", "HEAD:.gitignore")
    if state == "dirty-with-unavailable-comparison":
        (checkout / "notes.txt").write_bytes(b"working notes")
    _git(checkout, "config", "core.filemode", "true")
    _git(checkout, "config", "core.symlinks", "true")
    names = [".gitignore", "tracked.txt"] if state == "two-file-comparison" else ["tracked.txt"]
    recorded_flags = b"".join(f"h {name}\0".encode() for name in names)
    recorded_index = b"".join(f"{mode} {indexed_hash} 0\t{name}\0".encode() for name in names)
    inspections, comparisons = [], []
    run = subprocess.run

    def read_tracked_file(arguments, *args, **kwargs):
        """Replay the selected index rows while using the filesystem and Git for all other observations."""
        if kwargs.get("cwd") == checkout:
            if arguments[-3:] == ["ls-files", "-v", "-z"]:
                inspections.append("inspected")
                return subprocess.CompletedProcess(arguments, 0, stdout=recorded_flags)
            if arguments[-3:] == ["ls-files", "-s", "-z"]:
                return subprocess.CompletedProcess(arguments, 0, stdout=recorded_index)
            if arguments[-3:-1] == ["hash-object", "--"] and arguments[-1] in names:
                comparisons.append("compared")
                if arguments[-1] == ".gitignore" or len(comparisons) <= unavailable_reads:
                    failed = subprocess.CompletedProcess(arguments, 1, stdout=b"", stderr=b"comparison unavailable")
                    if kwargs.get("check"):
                        failed.check_returncode()
                    return failed
        return run(arguments, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", read_tracked_file)
    diagnostic = (
        SOURCE_DIRTY_STATUS
        if state == "dirty-with-unavailable-comparison"
        else "git-index-concealed-entries:count=1:paths=['tracked.txt']"
    )
    return inspections, comparisons, diagnostic


@pytest.mark.parametrize(
    ("tracked_file_comparison", "expected_inspections", "expected_comparisons"),
    [
        pytest.param(("content", 0), 1, 1, id="content-difference"),
        pytest.param(("executable", 0), 1, 0, id="executable-mode-difference"),
        pytest.param(("link", 0), 1, 0, id="file-kind-difference"),
        pytest.param(("content", 1), 2, 2, id="recovered-comparison-proves-difference"),
        pytest.param(("dirty-with-unavailable-comparison", 2), 1, 1, id="observed-dirty-checkout"),
        pytest.param(("two-file-comparison", 0), 1, 2, id="unavailable-file-and-confirmed-file"),
    ],
    indirect=["tracked_file_comparison"],
)
def test_inventory_collision_publishes_confirmed_tracked_file_failure(
    editable_checkout,
    output,
    structured_inventory_retry,
    tracked_file_comparison,
    expected_inspections,
    expected_comparisons,
    monkeypatch,
):
    """Keep the prior attempt and publish the confirmed source failure without starting an applicable command."""
    command, inventory, lint_marker, test_marker = structured_inventory_retry
    prior = (output / "gates.json").read_bytes()
    inventory.write_bytes(b"independent retained input")
    identity = inventory.stat()
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    monkeypatch.setattr(sys, "argv", command[1:])
    assert gates.main() == 1
    inspections, comparisons, diagnostic = tracked_file_comparison
    assert len(inspections) == expected_inspections
    assert len(comparisons) == expected_comparisons
    assert not lint_marker.exists()
    assert not test_marker.exists()
    assert os.path.samestat(identity, inventory.stat())
    assert inventory.read_bytes() == b"independent retained input"
    validator = _load_validator()
    archive = output / "gate-attempts/002"
    assert (archive / "gates.json").read_bytes() == prior
    assert validator._validate_gates(archive)["status"] == "pass"
    assert validator._validate_gates(output)["checks_refused"] == ["lint", "tests"]
    lint = _gate_check(output, "lint")
    tests = _gate_check(output, "tests")
    assert lint["source"]["before"] == tests["source"]["before"]
    assert lint["source"]["before"]["head"] == editable_checkout.expected_head
    assert lint["source"]["after"] is None
    assert tests["source"]["after"] is None
    assert diagnostic in _receipt_text(output, lint)
    assert diagnostic in _receipt_text(output, tests)


@pytest.mark.parametrize(
    ("tracked_file_comparison", "diagnostic"),
    [
        pytest.param(("unchanged", 1), "tracked-source-inventory-changed", id="retry-establishes-clean-source"),
        pytest.param(("unchanged", 2), "source-inspection-failed", id="comparison-still-unavailable"),
    ],
    indirect=["tracked_file_comparison"],
)
def test_inventory_collision_retains_prior_receipt_after_unavailable_content_comparison(
    editable_checkout, output, structured_inventory_retry, tracked_file_comparison, diagnostic, monkeypatch, capsys
):
    """Retry an inconclusive comparison once and retain the prior report when the source is unresolved or clean."""
    command, inventory, lint_marker, test_marker = structured_inventory_retry
    prior = (output / "gates.json").read_bytes()
    inventory.write_bytes(b"independent retained input")
    identity = inventory.stat()
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    monkeypatch.setattr(sys, "argv", command[1:])
    assert gates.main() == 2
    error = capsys.readouterr().err
    assert diagnostic in error
    assert "git-index-concealed-entries" not in error
    inspections, comparisons, _ = tracked_file_comparison
    assert len(inspections) == 2
    assert len(comparisons) == 2
    assert not lint_marker.exists()
    assert not test_marker.exists()
    assert os.path.samestat(identity, inventory.stat())
    assert inventory.read_bytes() == b"independent retained input"
    assert (output / "gates.json").read_bytes() == prior
    assert not (output / "gate-attempts/002").exists()
    assert _load_validator()._validate_gates(output)["status"] == "pass"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(f"tracked-source-{'a' * 64}.bin", id="possible-inventory"),
        pytest.param("other-check-notes.txt", id="ordinary-file"),
    ],
)
def test_successful_empty_git_listing_retains_ordinary_failed_tests_receipt(
    tmp_path: Path, output: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """An empty committed checkout runs lint and retains the tests guard's failure without retrying a valid listing."""
    repository = tmp_path / "empty-checkout"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Gate Test")
    _git(repository, "config", "user.email", "gate@example.invalid")
    _git(repository, "commit", "--allow-empty", "-m", f"empty checkout\n\n{CODEX_TRAILER}")
    command = TestPytestGateRecovery._tests_gate_command(
        output, repository, _git(repository, "rev-parse", "HEAD"), sys.executable
    )
    lint = tmp_path / "lint-started"
    position = command.index("--skip-lint")
    command[position : position + 2] = ["--lint", _marker_command(lint)]
    checks = output / "checks"
    checks.mkdir(parents=True)
    other = checks / name
    other.write_bytes(b"unrelated retained file")
    identity = other.stat()
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setattr(sys, "argv", command[1:])
    run = subprocess.run
    observed = []

    def observe_membership(arguments, *args, **kwargs):
        """Record empty committed trees separately from empty live indexes at execution."""
        result = run(arguments, *args, **kwargs)
        if kwargs.get("cwd") == repository:
            if "ls-tree" in arguments:
                observed.append(("commit", lint.exists(), result.returncode, result.stdout))
            elif list(arguments) == ["git", "ls-files", "-z", "--"]:
                observed.append(("index", lint.exists(), result.returncode, result.stdout))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", observe_membership)
        assert gates.main() == 1
    assert lint.exists()
    assert observed == [
        ("commit", False, 0, b""),  # Pin once, including when the output contains only an unrelated file.
        ("index", False, 0, b""),  # Before lint.
        ("index", True, 0, b""),  # After lint.
        ("index", True, 0, b""),  # Before the selected tests, which cannot collect from an empty checkout.
    ]
    assert _load_validator()._validate_gates(output)["checks_failed"] == ["tests"]
    assert "tracked-source-inventory-empty-or-unavailable" in _receipt_text(output, _gate_check(output, "tests"))
    assert os.path.samestat(identity, other.stat())
    assert other.read_bytes() == b"unrelated retained file"


def test_interrupted_inventory_repair_recovers_its_former_archive_location(
    editable_checkout: _EditableCheckout, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume an older journal whose authenticated damaged attempt already moved into numbered history."""
    editable_checkout.link_editable(editable_checkout.selected)
    first = editable_checkout.run_tests(output)
    assert first.returncode == 0, first.stderr
    inventory = next((output / "checks").glob("tracked-source-*.bin"))
    inventory.write_bytes(b"previously damaged")
    gates = _load_run_gates(monkeypatch)
    for key, value in editable_checkout.environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", first.args[1:])
    replace = os.replace

    def interrupt(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == output / "gates.json":
            raise OSError("interrupted manifest install")
        replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt)
        assert gates.main() == 2
    assert (output / ".gate-publication.json").is_file()
    diagnostic = output / "gate-diagnostics/001"
    numbered = output / "gate-attempts/001"
    numbered.parent.mkdir(exist_ok=True)
    diagnostic.rename(numbered)
    identity = numbered.stat()
    retry = editable_checkout.run_tests(output)
    assert retry.returncode == 0, retry.stderr
    assert os.path.samestat(identity, diagnostic.stat())
    assert (diagnostic / "checks" / inventory.name).read_bytes() == b"previously damaged"
    validator = _load_validator()
    assert validator._validate_gates(numbered)["checks_failed"] == []
    assert validator._validate_gates(output)["checks_failed"] == []


def test_relocated_diagnostic_recovers_with_file_at_other_history_root(
    editable_checkout: _EditableCheckout, output: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recovery finishes the damaged attempt even when an independent file blocks the following ordinary retry."""
    editable_checkout.link_editable(editable_checkout.selected)
    pytest_arguments = ["-q", "-p", "no:xdist", "-o", "addopts=", "tests/test_demo.py"]
    first = editable_checkout.run_tests(output, pytest_arguments=pytest_arguments)
    assert first.returncode == 0, first.stderr
    manifest = (output / "gates.json").read_bytes()
    inventory = next((output / "checks").glob("tracked-source-*.bin"))
    inventory.write_bytes(b"historical damaged input")
    alternate = output / "gate-attempts"
    alternate.write_bytes(b"separate history note")
    identity = alternate.stat()
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    monkeypatch.setattr(sys, "argv", first.args[1:])
    replace = os.replace

    def interrupt(source: str | Path, destination: str | Path) -> None:
        """Stop after the damaged archive moves and before the replacement manifest reaches the reader."""
        if Path(destination) == output / "gates.json":
            raise OSError("interrupted after relocation")
        replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt)
        assert gates.main() == 2
    journal = output / ".gate-publication.json"
    moved = output / "gate-diagnostics/001"
    moved_identity = moved.stat()
    assert journal.exists()
    validator = _load_validator()
    with pytest.raises(SystemExit, match="gate-publication-in-progress"):
        validator._validate_gates(output)

    lint = tmp_path / "lint-started"
    retry = _run_lint_gate(tmp_path, output, None, _marker_command(lint))
    assert retry.returncode == 2, retry.stderr
    assert not journal.exists()
    assert validator._validate_gates(output)["status"] == "pass"
    assert not lint.exists()
    assert os.path.samestat(identity, alternate.stat())
    assert alternate.read_bytes() == b"separate history note"
    assert os.path.samestat(moved_identity, moved.stat())
    assert (moved / "gates.json").read_bytes() == manifest
    assert (moved / "checks" / inventory.name).read_bytes() == b"historical damaged input"

    recovered = (output / "gates.json").read_bytes()
    alternate.rename(tmp_path / "retained-history-note")
    retry = _run_lint_gate(tmp_path, output, None, _marker_command(lint))
    assert retry.returncode == 0, retry.stderr
    assert lint.exists()
    ordinary = alternate / "001"
    assert (ordinary / "gates.json").read_bytes() == recovered
    assert validator._validate_gates(ordinary)["status"] == "pass"
    assert validator._validate_gates(output)["status"] == "pass"


@pytest.mark.parametrize("prior_location", ["stage", "gate-attempts"])
def test_interrupted_inventory_repair_preserves_occupied_diagnostic_across_restarts(
    editable_checkout: _EditableCheckout, output: Path, monkeypatch: pytest.MonkeyPatch, prior_location: str
) -> None:
    """Restart an older damaged publication on either side of relocation while retaining an occupied diagnostic."""
    editable_checkout.link_editable(editable_checkout.selected)
    pytest_arguments = ["-q", "-p", "no:xdist", "-o", "addopts=", "tests/test_demo.py"]
    first = editable_checkout.run_tests(output, pytest_arguments=pytest_arguments)
    assert first.returncode == 0, first.stderr
    inventory = next((output / "checks").glob("tracked-source-*.bin"))
    inventory.write_bytes(b"previously damaged")
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setattr(sys, "argv", first.args[1:])
    monkeypatch.setenv("PYTHONPATH", editable_checkout.environment["PYTHONPATH"])
    occupied = output / "gate-diagnostics/001"
    former = output / "gate-attempts/001"
    rename = Path.rename

    def interrupt_initial_move(source: str | Path, destination: str | Path) -> None:
        """Leave the older archive in one of its two historical recovery locations."""
        if Path(destination) == occupied:
            if prior_location == "gate-attempts":
                former.parent.mkdir(exist_ok=True)
                rename(source, former)
            raise OSError("interrupted older publication")
        rename(source, destination)

    with monkeypatch.context() as patch:
        # Path.rename: Python 3.10's pathlib binds os.rename at import, so patching os.rename never reaches the runner.
        patch.setattr(Path, "rename", interrupt_initial_move)
        assert gates.main() == 2
    marker = output / ".gate-publication.json"
    stage = output / json.loads(marker.read_bytes())["stage"]
    archive = {"stage": stage / "previous", "gate-attempts": former}[prior_location]
    identity = archive.stat()
    occupied.mkdir()
    (occupied / "note.txt").write_bytes(b"independent diagnostic")
    occupied_identity = occupied.stat()
    replace = os.replace

    def interrupt_manifest_install(source: str | Path, destination: str | Path) -> None:
        """Interrupt a second time once the recovered archive has reached its diagnostic number."""
        if Path(destination) == output / "gates.json":
            raise OSError("interrupted manifest install")
        replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt_manifest_install)
        assert gates.main() == 2
    moved = output / "gate-diagnostics/002"
    assert os.path.samestat(identity, moved.stat())
    assert (moved / "checks" / inventory.name).read_bytes() == b"previously damaged"
    assert not former.exists()
    assert marker.exists()

    retry = editable_checkout.run_tests(output, pytest_arguments=pytest_arguments)
    assert retry.returncode == 0, retry.stderr
    assert not marker.exists()
    assert os.path.samestat(identity, moved.stat())
    assert os.path.samestat(occupied_identity, occupied.stat())
    assert (occupied / "note.txt").read_bytes() == b"independent diagnostic"
    assert set(occupied.parent.iterdir()) == {occupied, moved}
    validator = _load_validator()
    assert validator._validate_gates(former)["checks_failed"] == []
    assert validator._validate_gates(output)["checks_failed"] == []


@pytest.mark.parametrize(
    "replacement",
    [
        pytest.param("missing", id="missing-proof"),
        pytest.param("oversized", id="oversized-proof"),
        pytest.param("wrong-worktree", id="cross-worktree-proof"),
        pytest.param("prepublished", id="preexisting-canonical-proof"),
        pytest.param("prior-collision", id="canonical-file-present-before-launch"),
        pytest.param(
            "symlink", marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="needs symlinks"), id="redirect"
        ),
    ],
)
def test_invalid_pytest_scratch_proof_cannot_certify_gate(
    repository: Path, head: str, output: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    """Retain a validator-readable failed observation while preserving any canonical-file collision."""
    gates = _load_run_gates(monkeypatch)
    sentinel = repository.parent / "outside.json"
    sentinel.write_text("untouched", encoding="utf-8")
    checks = output / "checks"
    checks.mkdir(parents=True)
    canonical = checks / "tests.python-imports.json"
    if replacement == "prior-collision":
        canonical.write_text("unexpected", encoding="utf-8")

    def submit(argv: list[str], timeout: int, stdout: Path, stderr: Path, *settings: Any) -> tuple[int, float]:
        stdout.write_bytes(b"")
        stderr.write_bytes(b"")
        submitted = Path(argv[5])
        assert submitted.parent == settings[-1]
        assert submitted.parent.parent == checks
        if replacement in {"missing", "prior-collision"}:
            return 0, 0.0
        if replacement == "symlink":
            submitted.symlink_to(sentinel)
        elif replacement == "oversized":
            with submitted.open("wb") as stream:
                stream.truncate(gates.MAX_TEST_PROOF_BYTES + 1)
        else:
            document = {
                "mode": "in-process-pytest",
                "status": "pass",
                "worktree": (repository if replacement == "prepublished" else repository.parent).as_posix(),
                "invoked_interpreter": Path(sys.executable).as_posix(),
                "modules": {"example": {"status": "pass"}},
                "tests": {"test.py": {"status": "pass"}},
                "workers": {},
                "errors": [],
            }
            submitted.write_text(json.dumps(document), encoding="utf-8")
            if replacement == "prepublished":
                canonical.write_text("unexpected", encoding="utf-8")
        return 0, 0.0

    monkeypatch.setattr(gates, "execute_command", submit)
    spec = gates.PythonTestSpec(Path(sys.executable), ("example",), ())
    result = gates.run_check("tests", "", "", 30, output, head, repository, spec)
    assert result["status"] == "fail"
    assert result["python_imports"]["reason"] == "proof-missing-or-invalid"
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    validator = _load_validator()
    if replacement in {"prepublished", "prior-collision"}:
        assert canonical.read_text(encoding="utf-8") == "unexpected"
        assert "source-gate-parent-proof-unavailable" in _receipt_text(output, result)
        assert "checks/tests.python-imports.json" not in result["source"]["artifacts"]
        with pytest.raises((SystemExit, ValueError)):
            validator._validate_gate_artifacts(output, result, None)
    else:
        assert json.loads(canonical.read_bytes()) == result["python_imports"]
        assert (
            result["source"]["artifacts"]["checks/tests.python-imports.json"]
            == hashlib.sha256(canonical.read_bytes()).hexdigest()
        )
        validator._validate_gate_artifacts(output, result, None)


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="host cannot link directories")
def test_redirected_pytest_scratch_root_prevents_launch(
    repository: Path, head: str, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a redirected scratch root before touching its target or running any project test."""
    gates = _load_run_gates(monkeypatch)
    checks = output / "checks"
    checks.mkdir(parents=True)
    token = "a" * 32
    monkeypatch.setattr(gates.secrets, "token_hex", lambda size: token)
    (checks / f"pytest-scratch-{token}").symlink_to(repository, target_is_directory=True)
    launch = mock.Mock(side_effect=AssertionError("unexpected launch"))
    monkeypatch.setattr(gates, "execute_command", launch)
    spec = gates.PythonTestSpec(Path(sys.executable), ("example",), ())
    result = gates.run_check("tests", "", "", 30, output, head, repository, spec)
    assert result["status"] == "fail"
    assert "source-gate-pytest-scratch-redirected" in _receipt_text(output, result)
    assert not (repository / "tests.python-imports.json").exists()
    launch.assert_not_called()


def _swap_before_publication(monkeypatch: pytest.MonkeyPatch, checks: Path) -> Callable[[], None]:
    """Swap the checks directory while the child runs and force the path-checked publication branch."""
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    return lambda: _swap_directory(checks)


def _swap_at_directory_open(monkeypatch: pytest.MonkeyPatch, checks: Path) -> Callable[[], None]:
    """Swap the checks directory after its path check passes, as publication opens it by descriptor."""
    real_open = os.open
    swapped: list[Path] = []

    def open_after_swap(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if not swapped and not isinstance(path, int) and Path(os.fspath(path)) == checks:
            swapped.append(checks)
            _swap_directory(checks)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_after_swap)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, open_after_swap})
    return lambda: None


@pytest.mark.parametrize(
    "arrange",
    [
        pytest.param(_swap_before_publication, id="swapped-before-publication"),
        pytest.param(
            _swap_at_directory_open,
            marks=pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="host has no directory-relative open"),
            id="swapped-at-directory-open",
        ),
    ],
)
def test_passing_proof_is_never_published_into_a_replaced_checks_directory(
    repository: Path,
    head: str,
    output: Path,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Callable[[pytest.MonkeyPatch, Path], Callable[[], None]],
) -> None:
    """A checks directory replaced after admission receives no canonical proof, even from a valid submission.

    The parent admits the checks directory before launch. Whether it is swapped before the path check or between that
    check and the descriptor open, publication must refuse with the directory-changed code and fail the gate.
    """
    gates = _load_run_gates(monkeypatch)
    checks = output / "checks"
    checks.mkdir(parents=True)
    swap = arrange(monkeypatch, checks)

    def submit(argv: list[str], timeout: int, stdout: Path, stderr: Path, *settings: Any) -> tuple[int, float]:
        stdout.write_bytes(b"")
        stderr.write_bytes(b"")
        Path(argv[5]).write_bytes(_passing_proof(repository))
        swap()
        return 0, 0.0

    monkeypatch.setattr(gates, "execute_command", submit)
    spec = gates.PythonTestSpec(Path(sys.executable), ("example",), ())
    result = gates.run_check("tests", "", "", 30, output, head, repository, spec)
    assert result["status"] == "fail"
    assert "source-gate-proof-rejected:source-gate-checks-directory-changed" in _receipt_text(output, result)
    assert not (checks / "tests.python-imports.json").exists()


def test_proof_in_a_replaced_pytest_scratch_cannot_certify_gate(
    repository: Path, head: str, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid proof inside a directory that replaced the attempt's scratch is not that attempt's submission.

    The child may rename its scratch away and create a new one at the same path. The parent compares the admitted
    scratch identity before reading, so the replacement's proof is rejected and the gate fails.
    """
    gates = _load_run_gates(monkeypatch)
    (output / "checks").mkdir(parents=True)

    def submit(argv: list[str], timeout: int, stdout: Path, stderr: Path, *settings: Any) -> tuple[int, float]:
        stdout.write_bytes(b"")
        stderr.write_bytes(b"")
        scratch = Path(argv[5]).parent
        scratch.rename(scratch.with_name("displaced-scratch"))
        scratch.mkdir()
        Path(argv[5]).write_bytes(_passing_proof(repository))
        return 0, 0.0

    monkeypatch.setattr(gates, "execute_command", submit)
    spec = gates.PythonTestSpec(Path(sys.executable), ("example",), ())
    result = gates.run_check("tests", "", "", 30, output, head, repository, spec)
    assert result["status"] == "fail"
    assert result["python_imports"]["reason"] == "proof-missing-or-invalid"
    assert "source-gate-proof-rejected:source-gate-pytest-scratch-changed" in _receipt_text(output, result)


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="host cannot link directories")
def test_gate_replay_coordinates_match_when_output_ancestor_has_an_alias(
    editable_checkout: _EditableCheckout, tmp_path: Path
) -> None:
    """Bind scratch and inventory under the same canonical checks directory even when the caller supplied an alias."""
    editable_checkout.link_editable(editable_checkout.selected)
    real_parent = tmp_path / "real-output"
    real_parent.mkdir()
    alias = tmp_path / "alias-output"
    alias.symlink_to(real_parent, target_is_directory=True)
    output = alias / "run"
    run = editable_checkout.run_tests(output)
    assert run.returncode == 0, run.stderr
    check = _gate_check(output, "tests")
    argv = json.loads(_receipt_text(output, check, "command_path").removeprefix("argv-json:"))
    checks = Path(argv[5]).parent.parent
    assert checks.parent.parent == output.resolve()
    assert re.fullmatch(r"\.gate-stage-[0-9a-f]{32}", checks.parent.name)
    assert checks.name == "checks"
    assert re.fullmatch(r"pytest-scratch-[0-9a-f]{32}", Path(argv[5]).parent.name)
    assert Path(argv[5]).name == "tests.python-imports.json"
    assert Path(json.loads(argv[-1])["tracked_inventory"]["path"]).parent == checks


@pytest.mark.parametrize(
    ("decision", "status", "mode", "legacy", "reason"),
    [
        pytest.param("selected", "pass", "macos-worktree", False, "verified native Ruff", id="native-command"),
        pytest.param("failed", "fail", "macos-worktree", False, "verified native Ruff", id="failed-tool-never-replays"),
        pytest.param(
            "denied",
            "fail",
            "not-started",
            False,
            "worktree profile preparation refused or failed",
            id="denial-never-replays",
        ),
        pytest.param(
            "launcher-missing",
            "fail",
            "not-started",
            False,
            "sandbox launcher refused or failed",
            id="launcher-disappears",
        ),
        pytest.param(
            "unavailable",
            "pass",
            "standard",
            True,
            "worktree profile unavailable or incompatible before launch",
            id="prelaunch-ineligible",
        ),
        pytest.param(
            "declined",
            "pass",
            "standard",
            True,
            "worktree sandbox declined: Ruff input includes private, linked or nested paths",
            id="contributor-input-declines-confinement",
        ),
    ],
)
def test_contributor_gate_routes_only_once(
    linked_worktree: tuple[Path, Path, str],
    output: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    status: str,
    mode: str,
    legacy: bool,
    reason: str,
) -> None:
    """The public CLI records the command actually launched and never replays a refused or failed sandbox.

    A profile the checkout's own inputs decline is recorded as a named standard route: the original command runs once,
    unchanged, and the record never claims confinement it did not have.
    """
    gates = _load_run_gates(monkeypatch)
    _, selected, head = linked_worktree
    # An existing protected contributor interval activates the optional native profiles.
    output.mkdir()
    (output / "integrity.json").write_text("{}", encoding="utf-8")
    invocation = [
        "run_gates.py",
        "--out",
        str(output),
        "--worktree",
        str(selected),
        "--expected-head",
        head,
        "--format",
        "ruff format --check .",
        *_skip_gates("format"),
    ]
    monkeypatch.setattr(sys, "argv", invocation)
    monkeypatch.setattr(gates, "sys", mock.Mock(wraps=sys, platform="darwin"))
    monkeypatch.setattr(gates.shutil, "which", lambda *args, **kwargs: sys.executable)
    monkeypatch.setattr(gates, "_ruff_configuration_reason", lambda *args: None)
    legacy_invocations = []
    prepared = []

    def shell(command: str, platform: str) -> list[str]:
        legacy_invocations.append(command)
        return [sys.executable, "-c", "print('standard')"]

    def prepare(executable: Path, args: list[str], work: Path, scratch: Path, env: dict) -> tuple | None:
        prepared.append(args)
        assert work == selected
        assert scratch.is_dir()
        if decision == "unavailable":
            return None
        if decision == "declined":
            return "Ruff input includes private, linked or nested paths"
        if decision == "denied":
            raise PermissionError("execution denied")
        if decision == "launcher-missing":
            return [str(output / "missing-launcher")], env
        return [sys.executable, "-c", f"print('confined'); raise SystemExit({int(decision == 'failed')})"], env

    monkeypatch.setattr(gates, "command_argv", shell)
    monkeypatch.setattr(gates.worktree_sandbox, "native_available", lambda: True)
    monkeypatch.setattr(gates.worktree_sandbox, "prepare_ruff", prepare)
    assert gates.main() == int(status == "fail")
    result = _gate_check(output, "format")
    assert result["status"] == status
    assert result["execution_route"] == {"mode": mode, "reason": reason}
    assert prepared == [["format", "--check", "."]]
    assert legacy_invocations == (["ruff format --check ."] if legacy else [])
    if decision not in {"denied", "launcher-missing"}:
        assert _receipt_text(output, result, "stdout").strip() == ("standard" if legacy else "confined")


@pytest.mark.parametrize(
    ("error", "status", "exit_code", "diagnostic"),
    [
        pytest.param(
            FileNotFoundError(errno.ENOENT, "missing shell"),
            "missing-command",
            127,
            "missing-command:",
            id="missing-shell",
        ),
        pytest.param(OSError(errno.EAGAIN, "process limit"), "fail", 126, "process limit", id="process-limit"),
    ],
)
def test_free_form_gate_records_unstarted_shell(
    output: Path, monkeypatch: pytest.MonkeyPatch, error: OSError, status: str, exit_code: int, diagnostic: str
) -> None:
    """An OS refusal to launch the shell differs from a successfully launched shell returning a failure."""
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["run_gates.py", "--out", str(output), "--lint", "exit 0", *_skip_gates("lint")])
    launch = mock.Mock(side_effect=error)
    monkeypatch.setattr(gates.subprocess, "Popen", launch)
    assert gates.main() == 1
    check = _gate_check(output, "lint")
    assert check["status"] == status
    assert check["exit_code"] == exit_code
    assert check["execution_route"]["mode"] == "not-started"
    assert diagnostic in _receipt_text(output, check)
    assert _load_validator()._validate_gates(output)["checks_failed"] == ["lint"]
    launch.assert_called_once()


def test_single_process_declaration_requires_clean_import_bound_tests(repository: Path, output: Path) -> None:
    """A compatibility declaration cannot attach to a free-form shell gate."""
    result = _run(
        [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--single-process-pytest",
            "--tests",
            _python_command("pass"),
            *_skip_gates("tests"),
        ],
        cwd=repository,
    )
    assert result.returncode == 2
    assert "requires clean import-bound pytest" in result.stderr


@pytest.fixture
def simulated_system_git(monkeypatch: pytest.MonkeyPatch) -> list:
    """Run the fixed macOS system Git probe with this host's Git, checking the parent's command and PATH."""
    executable = shutil.which("git")
    original = subprocess.run
    probes = []

    def system_git(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert argv[0] == "/usr/bin/git"
        assert kwargs["env"]["PATH"] == os.defpath
        probes.append(argv)
        return original([executable, *argv[1:]], **kwargs)

    monkeypatch.setattr(subprocess, "run", system_git)
    return probes


@pytest.mark.parametrize(
    ("setting", "lint", "plain_lint", "reason"),
    [
        pytest.param("fix = true", True, True, "enables fixes", id="original-default-can-fix"),
        pytest.param("fix = true", True, False, None, id="explicit-no-fix"),
        pytest.param("fix-only = true", True, False, "enables fixes", id="fix-only-overrides-no-fix"),
        pytest.param("fix-only = true", False, False, None, id="format-check"),
        pytest.param('extend = "../other.toml"', True, False, "inheritance", id="external-config"),
        pytest.param("line-length = 120", True, True, None, id="read-only-default"),
    ],
)
def test_worktree_ruff_config_controls_route(
    linked_worktree: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    setting: str,
    lint: bool,
    plain_lint: bool,
    reason: str | None,
    simulated_system_git: list,
) -> None:
    """Inspect actual source configs while keeping the project environment out of parent Git resolution."""
    _, work, _ = linked_worktree
    (work / "ruff.toml").write_text(setting, encoding="utf-8")
    gates = _load_run_gates(monkeypatch)
    env = {"HOME": str(tmp_path), "PATH": str(work)}
    observed = gates._ruff_configuration_reason(work, env, lint, plain_lint)
    assert simulated_system_git
    if reason is None:
        assert observed is None
    else:
        assert reason in observed


@pytest.mark.parametrize(
    ("declaration", "worker_settings", "selected"),
    [
        pytest.param(False, "serial", False, id="absence-of-parallel-options-is-insufficient"),
        pytest.param(True, "serial", True, id="declared-exact-selection"),
        pytest.param(True, "cli", False, id="explicit-distribution-conflict"),
        pytest.param(True, "nested-config", False, id="nested-project-workers"),
        pytest.param(True, "override", False, id="override-configured-workers"),
    ],
)
def test_pytest_profile_preserves_explicit_selection(
    editable_checkout: _EditableCheckout,
    output: Path,
    monkeypatch: pytest.MonkeyPatch,
    declaration: bool,
    worker_settings: str,
    selected: bool,
) -> None:
    """Drive the public gate and keep argv, import receipts and the validator's proof path through routing."""
    gates = _load_run_gates(monkeypatch)
    checkout = editable_checkout
    checkout.link_editable(checkout.selected)
    options = ["-q", "tests/test_demo.py::test_value"]
    if worker_settings == "nested-config":
        (checkout.selected / "tests/pytest.ini").write_text("[pytest]\naddopts = -n 2\n", encoding="utf-8")
        _git(checkout.selected, "add", "tests/pytest.ini")
        _git(checkout.selected, "commit", "-qm", "nested pytest project")
        checkout.expected_head = _git(checkout.selected, "rev-parse", "HEAD")
    elif worker_settings == "override":
        options.extend(["-o", "addopts=-n 2"])
    else:
        options.extend(["-o", "addopts=", *(["-n", "1"] if worker_settings == "cli" else ["-p", "no:xdist"])])
    output.mkdir()
    (output / "integrity.json").write_text("{}", encoding="utf-8")
    invocation = [
        "run_gates.py",
        "--out",
        str(output),
        "--worktree",
        str(checkout.selected),
        "--expected-head",
        checkout.expected_head,
        "--pytest-python",
        sys.executable,
        "--pytest-import",
        "demo_package",
        "--pytest-args-json",
        json.dumps(options),
        *_skip_gates("tests"),
    ]
    if declaration:
        invocation.append("--single-process-pytest")
    monkeypatch.setattr(sys, "argv", invocation)
    monkeypatch.setattr(gates, "sys", mock.Mock(wraps=sys, platform="darwin"))
    monkeypatch.setenv("PYTHONPATH", checkout.environment["PYTHONPATH"])
    launches = []

    def prepare(argv: list[str], work: Path, scratch: Path, env: dict) -> tuple:
        launches.append(argv)
        assert json.loads(argv[7]) == options
        assert Path(argv[5]).parent == scratch
        assert work == checkout.selected
        return argv, env

    monkeypatch.setattr(gates.worktree_sandbox, "native_available", lambda: True)
    monkeypatch.setattr(gates.worktree_sandbox, "prepare_pytest", prepare)
    assert gates.main() == 0
    result = _gate_check(output, "tests")
    assert bool(launches) == selected
    assert result["execution_route"]["mode"] == ("macos-worktree" if selected else "standard")
    assert result["python_imports"]["status"] == "pass"
    assert list(result["python_imports"]["tests"]) == [_demo_test_path(checkout.selected)]
    recorded = json.loads(_receipt_text(output, result, "command_path").removeprefix("argv-json:"))
    assert json.loads(recorded[7]) == options
    if worker_settings == "nested-config":
        assert result["execution_route"]["reason"] == "pytest target may select nested configuration"
    if worker_settings in {"nested-config", "override"}:
        assert len(result["python_imports"]["workers"]) == 2


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(["-o", "addopts=-n 2"], id="short"),
        pytest.param(["-oaddopts=-n 2"], id="short-joined"),
        pytest.param(["-o=addopts=-n 2"], id="short-equals"),
        pytest.param(["--override-ini", "addopts=-n 2"], id="long"),
        pytest.param(["--override-ini=addopts=-n 2"], id="long-equals"),
    ],
)
def test_pytest_nonempty_addopts_override_preserves_the_standard_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: list[str]
) -> None:
    """An ini override can configure workers indirectly before pytest parses its normal command line."""
    gates = _load_run_gates(monkeypatch)
    assert gates._pytest_configuration_reason(tmp_path, options) == "pytest addopts override needs the standard route"


@pytest.mark.parametrize(
    ("ignore", "reason"),
    [
        pytest.param("some-other-path.py", None, id="external-rule-misses-source"),
        pytest.param("tracked.txt", "external Git exclusions", id="external-rule-selects-source"),
    ],
)
def test_worktree_ruff_checks_external_ignore_against_real_inputs(
    linked_worktree: tuple[Path, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore: str,
    reason: str | None,
    simulated_system_git: list,
) -> None:
    """Permit unrelated user ignores while keeping the original file set when an external pattern matches."""
    _, work, _ = linked_worktree
    home = tmp_path / "home"
    config = home / ".config"
    (config / "git").mkdir(parents=True)
    (config / "git/ignore").write_text(ignore, encoding="utf-8")
    gates = _load_run_gates(monkeypatch)
    observed = gates._ruff_configuration_reason(work, {"HOME": str(home), "XDG_CONFIG_HOME": str(config)}, False, False)
    assert simulated_system_git
    if reason is None:
        assert observed is None
    else:
        assert reason in observed


def _nest_repository(work: Path) -> None:
    """Create an untracked nested Git repository whose files Ruff would discover separately."""
    _git(work, "init", "-q", "nested")


def _ancestor_ignore(work: Path) -> None:
    """Write an ignore file above the worktree that Ruff reads during file discovery."""
    (work.parent / ".ignore").write_text("*.py\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        pytest.param(_nest_repository, "nested repository needs separate Ruff discovery", id="nested-repository"),
        pytest.param(_ancestor_ignore, "Ruff may inherit an ancestor .ignore file", id="ancestor-ignore"),
    ],
)
def test_worktree_ruff_discovery_outside_the_profile_keeps_standard_route(
    linked_worktree: tuple[Path, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    arrange: Callable[[Path], None],
    reason: str,
) -> None:
    """Input discovery the confined Ruff cannot reproduce keeps the original route under its named reason."""
    _, work, _ = linked_worktree
    arrange(work)
    # Simulate the fixed system Git only after the arrangement has used the host's Git.
    request.getfixturevalue("simulated_system_git")
    gates = _load_run_gates(monkeypatch)
    assert gates._ruff_configuration_reason(work, {"HOME": str(tmp_path / "home")}, True, True) == reason


def test_root_ruff_config_can_inherit_external_python_version(
    linked_worktree: tuple[Path, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    simulated_system_git: list,
) -> None:
    """An enclosing project's Python version matters even with a separate root Ruff config."""
    _, work, _ = linked_worktree
    (work.parent / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.12"\n', encoding="utf-8")
    config = work / "ruff.toml"
    config.write_text("line-length = 100\n", encoding="utf-8")
    gates = _load_run_gates(monkeypatch)
    env = {"HOME": str(tmp_path)}
    assert "target Python version" in gates._ruff_configuration_reason(work, env, True, True)
    config.write_text('target-version = "py310"\n', encoding="utf-8")
    assert gates._ruff_configuration_reason(work, env, True, True) is None


def test_pytest_profile_rejects_external_and_worker_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve exterior pytest configuration and declared project worker selection before launching a child."""
    gates = _load_run_gates(monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -k external\n", encoding="utf-8")
    assert "outside the worktree" in gates._pytest_configuration_reason(work, [])
    (work / "pytest.ini").write_text("[pytest]\naddopts = -k local\n", encoding="utf-8")
    assert gates._pytest_configuration_reason(work, []) is None
    (work / "pytest.ini").write_text("[pytest]\naddopts = -n 2\n", encoding="utf-8")
    assert (
        gates._pytest_configuration_reason(work, [])
        == "pytest arguments or project configuration request a distribution option"
    )
    (tmp_path / "conftest.py").touch()
    assert "conftest" in gates._pytest_configuration_reason(work, [])


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("subproject/tests", id="directory"),
        pytest.param("subproject/tests/test_sample.py::test_case", id="node-selector"),
        pytest.param("sibling.py", id="unrelated-config"),
    ],
)
def test_pytest_positional_target_preserves_nested_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """Only a target under a nested project can select its worker settings ahead of the root configuration."""
    gates = _load_run_gates(monkeypatch)
    root = tmp_path / "work"
    nested = root / "subproject"
    (nested / "tests").mkdir(parents=True)
    (nested / "tests/test_sample.py").touch()
    (root / "sibling.py").touch()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (nested / "pytest.ini").write_text("[pytest]\naddopts = -n 2\n", encoding="utf-8")
    reason = gates._pytest_configuration_reason(root, ["-q", "-k", "test_case", target])
    assert reason == (None if target == "sibling.py" else "pytest target may select nested configuration")


@pytest.mark.parametrize(
    ("filename", "document"),
    [
        pytest.param("pyproject.toml", "[tool]\npytest = 1", id="scalar-pytest-table"),
        pytest.param("pyproject.toml", "[tool.pytest]\nini_options = 1", id="scalar-ini-options"),
        pytest.param("pytest.toml", "pytest = 1", id="scalar-native-table"),
    ],
)
def test_malformed_pytest_table_keeps_standard_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, document: str
) -> None:
    """A TOML scalar in place of a pytest table must return a routing reason without aborting the gate."""
    gates = _load_run_gates(monkeypatch)
    (tmp_path / filename).write_text(document, encoding="utf-8")
    assert gates._pytest_configuration_reason(tmp_path, []) == "pytest configuration cannot be verified"
