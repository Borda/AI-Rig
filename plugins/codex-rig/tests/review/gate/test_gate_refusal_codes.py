"""Refusal codes the gate runner names when publication, recovery, launch or a child proof cannot be trusted.

Each test reaches one guard through a real sealed attempt, the public runner or the route launcher, asserts the exact
refusal code, and checks the side effect the guard prevents. Where a later check would raise a similar error, the side
effect distinguishes them, so removing the guard under test fails the test.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _gate_runner_support import (
    RUN_GATES,
    _foreign_file,
    _load_run_gates,
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
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, FILE_SYMLINKS_AVAILABLE, HARD_LINKS_AVAILABLE

JOURNAL = ".gate-publication.json"
NEEDS_DIRECTORY_LINKS = pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="host cannot link directories")
NEEDS_FILE_LINKS = pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="host cannot link files")
NEEDS_HARD_LINKS = pytest.mark.skipif(not HARD_LINKS_AVAILABLE, reason="host cannot hard-link files")


def _reseal(pending: _PendingPublication, edit: Callable[[dict[str, Any]], None]) -> None:
    """Rewrite the sealed plan through ``edit`` and re-digest the journal, as another writer's journal would read."""
    journal = pending.output / JOURNAL
    owner = json.loads(journal.read_bytes())
    plan_path = pending.stage / "publication.json"
    plan = json.loads(plan_path.read_bytes())
    edit(plan)
    raw = json.dumps(plan).encode()
    plan_path.write_bytes(raw)
    owner["sha256"] = hashlib.sha256(raw).hexdigest()
    journal.write_bytes(json.dumps(owner).encode())


def _live(pending: _PendingPublication, name: str) -> Callable[[], bytes | None]:
    """Return an observer of one live run member's bytes, or ``None`` when it is absent."""
    path = pending.output / name
    return lambda: path.read_bytes() if path.exists() else None


def _contents(directory: Path) -> Callable[[], dict[str, bytes]]:
    """Return an observer of every regular file name and its bytes directly inside ``directory``."""
    return lambda: {entry.name: entry.read_bytes() for entry in sorted(directory.iterdir()) if entry.is_file()}


def _change_sealed_plan(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Append to the sealed plan without updating the journal digest."""
    plan = pending.stage / "publication.json"
    plan.write_bytes(plan.read_bytes() + b" ")
    return _live(pending, "gates.json")


def _rename_stage(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Move the intact stage to a name outside the private stage pattern and point the journal at it."""
    pending.stage.rename(pending.output / "renamed-stage")
    journal = pending.output / JOURNAL
    owner = json.loads(journal.read_bytes())
    owner["stage"] = "renamed-stage"
    journal.write_bytes(json.dumps(owner).encode())
    return _live(pending, "gates.json")


def _replace_stage(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Replace the stage directory by another directory holding the same entries."""
    _swap_directory(pending.stage)
    return _live(pending, "gates.json")


def _add_plan_key(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Seal a plan carrying a member this writer never records."""
    _reseal(pending, lambda plan: plan.update(extra=None))
    return _live(pending, "gates.json")


def _drop_required_summary(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Seal a plan that would leave the previous text summary beside the new manifest."""

    def drop(plan: dict[str, Any]) -> None:
        del plan["before"]["gates.txt"], plan["after"]["gates.txt"]

    _reseal(pending, drop)
    return _live(pending, "gates.txt")


def _claim_first_attempt(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Seal a first-attempt plan with no archive although earlier reports exist."""

    def first_attempt(plan: dict[str, Any]) -> None:
        plan["before"]["gates.json"] = None
        plan["archive"] = None

    _reseal(pending, first_attempt)
    return _live(pending, "gates.json")


def _replace_archive(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Replace the staged previous attempt by another directory holding the same entries."""
    _swap_directory(pending.stage / "previous")
    return _live(pending, "gates.json")


def _claim_legacy_log(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Seal a historical log the archived receipt never names, with matching bytes present in the archive."""
    content = b"unreferenced history\n"
    (pending.stage / "previous/checks/extra.txt").write_bytes(content)
    _reseal(pending, lambda plan: plan["legacy_logs"].update({"checks/extra.txt": hashlib.sha256(content).hexdigest()}))
    return _live(pending, "gates.json")


def _change_archived_log(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Rewrite an archived log the receipt does not authenticate after the journal sealed its digest."""
    (pending.stage / "previous/checks/lint.stdout.txt").write_bytes(b"changed history\n")
    return _live(pending, "gates.json")


def _lose_diagnostic_archive(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Seal a diagnostic plan, then move its staged archive out of the run where no history slot holds it."""
    _reseal(pending, lambda plan: plan["damaged_logs"].update({"checks/lint.stdout.txt": "0" * 64}))
    (pending.stage / "previous").rename(outside / "moved-archive")
    return _live(pending, "gates.json")


def _change_live_manifest(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Rewrite the live manifest after sealing; no member may install around it."""
    manifest = pending.output / "gates.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    return _live(pending, "checks/lint.stdout.txt")


def _change_live_member(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Rewrite one live member after sealing; the newer bytes must stay published."""
    member = pending.output / "checks/lint.stdout.txt"
    member.write_bytes(b"independent update\n")
    return _live(pending, "checks/lint.stdout.txt")


def _change_member_during_copy(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Rewrite one live member while its replacement is being copied, after its first comparison."""
    member = pending.output / "checks/lint.stdout.txt"
    read = pending.gates._read_gate_file

    def read_then_change(path: Path, label: str, *args: Any) -> bytes:
        content = read(path, label, *args)
        if label == "gate-publication-candidate" and path.name == member.name:
            member.write_bytes(b"concurrent update\n")
        return content

    patch.setattr(pending.gates, "_read_gate_file", read_then_change)
    return lambda: None


def _change_after_install(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Rewrite an installed member once the manifest is installed, past every pre-install comparison."""
    _rewrite_after_manifest_install(pending.output, pending.output / "checks/lint.stdout.txt", patch)
    return lambda: None


def _redirect_live_checks(pending: _PendingPublication, outside: Path, patch: pytest.MonkeyPatch) -> Callable:
    """Move the live checks directory outside the run and leave a link to it in its place."""
    checks = pending.output / "checks"
    checks.rename(outside / "checks")
    checks.symlink_to(outside / "checks", target_is_directory=True)
    return _contents(outside / "checks")


@pytest.mark.parametrize(
    ("arrange", "code"),
    [
        pytest.param(_change_sealed_plan, "gate-publication-plan-changed", id="plan-bytes-changed"),
        pytest.param(_rename_stage, "gate-publication-invalid-stage", id="stage-outside-private-pattern"),
        pytest.param(_replace_stage, "gate-publication-invalid-stage", id="stage-directory-replaced"),
        pytest.param(_add_plan_key, "gate-publication-invalid-plan", id="plan-unknown-member"),
        pytest.param(_drop_required_summary, "gate-publication-invalid-plan", id="plan-omits-summary"),
        pytest.param(_claim_first_attempt, "gate-publication-invalid-plan", id="plan-hides-previous-reports"),
        pytest.param(_replace_archive, "gate-publication-archive-changed", id="archive-directory-replaced"),
        pytest.param(_claim_legacy_log, "gate-publication-archive-changed", id="archive-gains-legacy-log"),
        pytest.param(_change_archived_log, "gate-publication-archive-changed", id="archived-member-changed"),
        pytest.param(_lose_diagnostic_archive, "gate-publication-archive-changed", id="diagnostic-archive-lost"),
        pytest.param(_change_live_manifest, "gate-publication-current-changed:gates.json", id="live-manifest-changed"),
        pytest.param(
            _change_live_member, "gate-publication-current-changed:checks/lint.stdout.txt", id="live-member-changed"
        ),
        pytest.param(
            _change_member_during_copy,
            "gate-publication-current-changed:checks/lint.stdout.txt",
            id="live-member-changed-during-copy",
        ),
        pytest.param(
            _change_after_install,
            "gate-publication-installed-changed:checks/lint.stdout.txt",
            id="installed-member-changed",
        ),
        pytest.param(
            _redirect_live_checks,
            "gate-checks-directory-redirected",
            marks=NEEDS_DIRECTORY_LINKS,
            id="live-checks-redirected",
        ),
    ],
)
def test_resumed_publication_names_each_untrusted_journal_state(
    pending_lint_retry: _PendingPublication,
    outside_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Callable[[_PendingPublication, Path, pytest.MonkeyPatch], Callable[[], object]],
    code: str,
) -> None:
    """Recovery refuses with the guard's own code, keeps the journal and leaves the guarded state as it found it.

    A sealed journal binds its plan, stage, archived attempt and the live members it replaces. When any of them no
    longer matches, installing would publish a mixed or foreign report, so recovery must stop before the protected bytes
    change.
    """
    pending = pending_lint_retry
    observe = arrange(pending, outside_directory, monkeypatch)
    expected = observe()
    with pytest.raises(ValueError, match=f"^{re.escape(code)}$"):
        pending.gates._finish_publication(pending.output)
    assert observe() == expected
    assert (pending.output / JOURNAL).is_file()


def test_resumed_publication_refuses_a_manifest_rewritten_as_its_journal_closes(
    pending_lint_retry: _PendingPublication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid manifest that differs from the installed candidate after the journal closes is reported, never adopted.

    The rewrite keeps the manifest and its row summary coherent, so only the final comparison with the candidate can
    notice that the published receipt is no longer the one this writer verified.
    """
    pending = pending_lint_retry
    journal = pending.output / JOURNAL
    unlink = Path.unlink

    def rewrite_after_close(path: Path, *args: Any, **kwargs: Any) -> None:
        unlink(path, *args, **kwargs)
        if path == journal:
            manifest = pending.output / "gates.json"
            payload = json.loads(manifest.read_bytes())
            payload["checks"][0]["duration_seconds"] += 1.0
            manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            rows = "".join(json.dumps(check, sort_keys=True) + "\n" for check in payload["checks"])
            (pending.output / "gates.checks.jsonl").write_text(rows, encoding="utf-8")

    monkeypatch.setattr(Path, "unlink", rewrite_after_close)
    with pytest.raises(ValueError, match=r"^gate-publication-installed-changed:gates\.json$"):
        pending.gates._finish_publication(pending.output)


@pytest.fixture
def published_lint(tmp_path: Path) -> Path:
    """Publish one lint attempt and return its output directory."""
    output = tmp_path / "review"
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('first')")).returncode == 0
    return output


def _lint_argv(output: Path, command: str) -> list[str]:
    """Return the in-process runner argv for one lint command with the other gates out of scope."""
    return [str(RUN_GATES), "--out", str(output), "--lint", command, *_skip_gates("lint")]


def test_retry_refuses_a_live_member_changed_while_the_previous_attempt_is_archived(
    published_lint: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A member rewritten between archiving and the admission snapshot stops the retry before any command runs."""
    output = published_lint
    gates = _load_run_gates(monkeypatch)
    preserve = gates.preserve_previous_attempt
    summary = output / "gates.txt"

    def preserve_then_change(*args: Any) -> Any:
        archived = preserve(*args)
        summary.write_bytes(summary.read_bytes() + b"independent note\n")
        return archived

    marker = tmp_path / "lint-ran"
    monkeypatch.setattr(gates, "preserve_previous_attempt", preserve_then_change)
    monkeypatch.setattr(sys, "argv", _lint_argv(output, _marker_command(marker)))
    capsys.readouterr()
    assert gates.main() == 2
    assert "gate-recovery-blocked:gate-publication-current-changed:gates.txt\n" in capsys.readouterr().err
    assert not marker.exists()
    assert summary.read_bytes().endswith(b"independent note\n")


def test_retry_refuses_a_live_member_changed_by_its_own_command(published_lint: Path) -> None:
    """A command that rewrites the published report fails publication before the journal is sealed."""
    output = published_lint
    prior = (output / "gates.json").read_bytes()
    summary = output / "gates.txt"
    command = _python_command(
        f"from pathlib import Path; p = Path({str(summary)!r}); p.write_bytes(p.read_bytes() + b'command note\\n')"
    )
    result = _run_lint_gate(output.parent, output, None, command)
    assert result.returncode == 2, result.stderr
    assert "gate-recovery-blocked:gate-publication-current-changed:gates.txt\n" in result.stderr
    assert not (output / JOURNAL).exists()
    assert (output / "gates.json").read_bytes() == prior
    assert summary.read_bytes().endswith(b"command note\n")


def test_attempt_refuses_a_journal_name_occupied_during_execution(tmp_path: Path) -> None:
    """Another owner's file at the journal name is kept, and this attempt publishes nothing over it."""
    output = tmp_path / "review"
    journal = output / JOURNAL
    command = _python_command(f"from pathlib import Path; Path({str(journal)!r}).write_bytes(b'foreign owner')")
    result = _run_lint_gate(tmp_path, output, None, command)
    assert result.returncode == 2, result.stderr
    assert "gate-recovery-blocked:gate-publication-marker-occupied\n" in result.stderr
    assert journal.read_bytes() == b"foreign owner"
    assert not (output / "gates.json").exists()


def test_retry_refuses_an_unowned_skip_approval_destination(published_lint: Path, tmp_path: Path) -> None:
    """A skip-decision destination the previous receipt never owned is kept and blocks the retry before execution."""
    output = published_lint
    occupant = _foreign_file(output / "checks/tests-skip-approval.json")
    reason = "fixture keeps tests out of scope"
    decision = tmp_path / "decision.json"
    decision.write_text(
        json.dumps({"decision": "skip-tests", "reason": reason, "evidence": "fixture user decision"}), encoding="utf-8"
    )
    marker = tmp_path / "lint-ran"
    skipped = [part for gate in ("format", "types", "review") for part in (f"--skip-{gate}", "out of scope")]
    arguments = ["--lint", _marker_command(marker), "--skip-tests", reason, "--test-skip-approval", str(decision)]
    result = _run([sys.executable, str(RUN_GATES), "--out", str(output), *arguments, *skipped], cwd=tmp_path)
    assert result.returncode == 2, result.stderr
    assert "gate-recovery-blocked:gate-publication-unowned-approval\n" in result.stderr
    assert not marker.exists()
    assert occupant() == b"foreign owner"


def test_attempt_refuses_a_stage_replaced_during_execution(
    published_lint: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stage directory replaced while gates run is never sealed into a journal."""
    output = published_lint
    prior = (output / "gates.json").read_bytes()
    gates = _load_run_gates(monkeypatch)
    run_check = gates.run_check

    def run_then_replace_stage(gate_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = run_check(gate_id, *args, **kwargs)
        if gate_id == "review":
            _swap_directory(args[3])
        return result

    monkeypatch.setattr(gates, "run_check", run_then_replace_stage)
    monkeypatch.setattr(sys, "argv", _lint_argv(output, _python_command("print('second')")))
    capsys.readouterr()
    assert gates.main() == 2
    assert "gate-recovery-blocked:gate-publication-invalid-stage\n" in capsys.readouterr().err
    assert not (output / JOURNAL).exists()
    assert (output / "gates.json").read_bytes() == prior


@NEEDS_DIRECTORY_LINKS
def test_retry_refuses_redirected_checks_before_creating_a_stage(
    published_lint: Path, outside_directory: Path, tmp_path: Path
) -> None:
    """A linked checks directory stops the retry before any stage exists or any command runs."""
    output = published_lint
    checks = output / "checks"
    checks.rename(outside_directory / "checks")
    checks.symlink_to(outside_directory / "checks", target_is_directory=True)
    observe = _contents(outside_directory / "checks")
    expected = observe()
    stages = sorted(output.glob(".gate-stage-*"))
    marker = tmp_path / "lint-ran"
    result = _run_lint_gate(tmp_path, output, None, _marker_command(marker))
    assert result.returncode == 2, result.stderr
    assert "gate-recovery-blocked:gate-checks-directory-redirected\n" in result.stderr
    assert not marker.exists()
    assert sorted(output.glob(".gate-stage-*")) == stages
    assert observe() == expected


@NEEDS_DIRECTORY_LINKS
def test_retry_refuses_checks_redirected_before_the_previous_attempt_is_archived(
    published_lint: Path,
    outside_directory: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A checks directory linked away after the first check is still refused before archiving reads through it."""
    output = published_lint
    checks = output / "checks"
    observe = _contents(checks)
    expected = observe()
    gates = _load_run_gates(monkeypatch)
    token_hex = gates.secrets.token_hex

    def redirect_then_name(size: int) -> str:
        if not checks.is_symlink():
            checks.rename(outside_directory / "checks")
            checks.symlink_to(outside_directory / "checks", target_is_directory=True)
        return token_hex(size)

    marker = tmp_path / "lint-ran"
    monkeypatch.setattr(gates.secrets, "token_hex", redirect_then_name)
    monkeypatch.setattr(sys, "argv", _lint_argv(output, _marker_command(marker)))
    capsys.readouterr()
    assert gates.main() == 2
    assert "gate-recovery-blocked:gate-checks-directory-redirected\n" in capsys.readouterr().err
    assert not marker.exists()
    assert observe() == expected
    assert not any((stage / "previous").exists() for stage in output.glob(".gate-stage-*"))
    assert not (output / JOURNAL).exists()


def _lock_directory(lock: Path, outside: Path) -> Callable[[], object]:
    """Occupy the writer lock name with a directory."""
    lock.mkdir()
    return lambda: lock.is_dir()


def _lock_link(lock: Path, outside: Path) -> Callable[[], object]:
    """Occupy the writer lock name with a link to another owner's file."""
    lock.symlink_to(outside / "sentinel")
    return (outside / "sentinel").read_bytes


def _lock_alias(lock: Path, outside: Path) -> Callable[[], object]:
    """Give the writer lock a second name outside the run."""
    lock.write_bytes(b"")
    os.link(lock, outside / "lock-alias")
    return lambda: os.stat(lock).st_nlink


@pytest.mark.parametrize(
    "arrange",
    [
        pytest.param(_lock_directory, id="directory"),
        pytest.param(_lock_link, marks=NEEDS_FILE_LINKS, id="symbolic-link"),
        pytest.param(_lock_alias, marks=NEEDS_HARD_LINKS, id="hard-link"),
    ],
)
def test_runner_refuses_an_untrusted_writer_lock(
    tmp_path: Path, outside_directory: Path, arrange: Callable[[Path, Path], Callable[[], object]]
) -> None:
    """A writer lock that is not this run's own single regular file stops every gate before it starts."""
    output = tmp_path / "review"
    output.mkdir()
    observe = arrange(output / ".gate-writer.lock", outside_directory)
    expected = observe()
    marker = tmp_path / "lint-ran"
    result = _run_lint_gate(tmp_path, output, None, _marker_command(marker))
    assert result.returncode == 2, result.stderr
    assert "gate-recovery-blocked:gate-writer-lock-invalid\n" in result.stderr
    assert not marker.exists()
    assert observe() == expected


@pytest.mark.parametrize(
    ("prepared", "exit_code", "diagnostic", "reason"),
    [
        pytest.param(
            "confined",
            1,
            "worktree-profile-launch-failed:",
            "sandbox launcher refused or failed",
            id="sandbox-launcher",
        ),
        pytest.param(
            "declined",
            127,
            "missing-command:",
            "worktree sandbox declined: native sandbox unavailable; standard launcher refused or failed",
            id="standard-launcher-after-decline",
        ),
    ],
)
def test_unstarted_launch_keeps_its_route_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepared: str, exit_code: int, diagnostic: str, reason: str
) -> None:
    """A launcher the OS refuses records which route was chosen and why, beside its own launch diagnostic."""
    gates = _load_run_gates(monkeypatch)
    missing = tmp_path / "missing-launcher"
    declined = "worktree sandbox declined: native sandbox unavailable"
    monkeypatch.setattr(
        gates,
        "_prepare_gate_profile",
        lambda *args: ([str(missing)], {}) if prepared == "confined" else declined,
    )
    paths = {name: tmp_path / f"{name}.txt" for name in ("stdout", "stderr")}
    code, _, route = gates._launch_gate(
        "tests", [str(missing)], 5, paths, tmp_path, None, None, None, None, False, sandbox_policy="contributor"
    )
    assert route == {"mode": "not-started", "reason": reason}
    assert code == exit_code
    assert paths["stderr"].read_text(encoding="utf-8").startswith(diagnostic)


def _proof_for(repository: Path, change: str) -> bytes:
    """Return a passing child proof for ``repository``, altered so it describes a different invocation."""
    document = json.loads(_passing_proof(repository))
    if change == "worktree":
        document["worktree"] = repository.parent.as_posix()
    elif change == "interpreter":
        document["invoked_interpreter"] = (repository / "python").as_posix()
    elif change == "modules":
        document["modules"] = {"other": {"status": "pass"}}
    elif change == "errors":
        document["errors"] = ["collection error"]
    elif change == "tests":
        document["tests"] = {}
    return json.dumps(document).encode("utf-8")


@pytest.mark.parametrize(
    ("change", "status"),
    [
        pytest.param("matching", "pass", id="matching-invocation"),
        pytest.param("worktree", "fail", id="other-worktree"),
        pytest.param("interpreter", "fail", id="other-interpreter"),
        pytest.param("modules", "fail", id="other-declared-modules"),
        pytest.param("errors", "fail", id="passing-with-errors"),
        pytest.param("tests", "fail", id="passing-without-tests"),
    ],
)
def test_child_proof_must_describe_this_invocation(
    repository: Path, head: str, output: Path, monkeypatch: pytest.MonkeyPatch, change: str, status: str
) -> None:
    """Only a proof for this worktree, interpreter and module set, with collected tests and no errors, can pass."""
    gates = _load_run_gates(monkeypatch)
    (output / "checks").mkdir(parents=True)

    def submit(argv: list[str], timeout: int, stdout: Path, stderr: Path, *settings: Any) -> tuple[int, float]:
        stdout.write_bytes(b"")
        stderr.write_bytes(b"")
        Path(argv[5]).write_bytes(_proof_for(repository, change))
        return 0, 0.0

    monkeypatch.setattr(gates, "execute_command", submit)
    spec = gates.PythonTestSpec(Path(sys.executable), ("example",), ())
    result = gates.run_check("tests", "", "", 30, output, head, repository, spec)
    assert result["status"] == status
    rejected = "source-gate-proof-rejected:source-gate-proof-identity-mismatch\n" in _receipt_text(output, result)
    assert rejected is (status == "fail")
    if status == "fail":
        assert result["python_imports"]["reason"] == "proof-missing-or-invalid"
