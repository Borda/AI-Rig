"""Require gate receipts to retain their recorded bytes and executed test evidence."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _modules import load_module, load_module_scoped

VALIDATOR = load_module("gate_evidence_validator", Path(__file__).resolve().parents[3] / "shared/validate-artifacts.py")
GATE_IDS = ["lint", "format", "types", "tests", "review"]


def _write_json(path, value):
    """Write a deterministic JSON fixture and return its exact digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8", newline="\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def local_gates(tmp_path):
    """Retain local gate source bindings, the receipt, snapshot and three logs for each check."""
    mirror = tmp_path / "local-source-review-worktree"
    source = {
        "mode": "local-review-mirror",
        "worktree": mirror.as_posix(),
        "receipt_path": "local-source/review-worktree.json",
        "snapshot_path": "checks/local-source-snapshot.json",
    }
    source["receipt_sha256"] = _write_json(
        tmp_path / source["receipt_path"],
        {
            "schema_version": 2,
            "diff_format": "canonical-v1",
            "review_worktree": mirror.as_posix(),
            "revision": "a" * 40,
        },
    )
    source["snapshot_sha256"] = _write_json(
        tmp_path / source["snapshot_path"], {"repository": mirror.as_posix(), "revision": "a" * 40, "files": []}
    )
    checks = []
    for gate_id in GATE_IDS:
        logs = {
            key: f"checks/{gate_id}.{name}.txt"
            for key, name in (("command_path", "command"), ("stdout", "stdout"), ("stderr", "stderr"))
        }
        artifacts = {}
        for key, relative in logs.items():
            path = tmp_path / relative
            path.write_text("python -m pytest\n" if key == "command_path" else "", encoding="utf-8")
            artifacts[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        observation = {"snapshot_sha256": source["snapshot_sha256"]}
        checks.append(
            {
                "id": gate_id,
                "status": "pass",
                "exit_code": 0,
                "duration_seconds": 1,
                **logs,
                "source": {**source, "before": observation, "after": observation, "artifacts": artifacts},
            }
        )
    gates = {"status": "pass", "checks_failed": [], "checks": checks, "source": source}
    _write_json(tmp_path / "gates.json", gates)
    return tmp_path, gates


@pytest.fixture
def summarized_gates(local_gates):
    """Write the producer's current summary set, including a nonempty failure list."""
    output, gates = local_gates
    gates.update(status="fail", checks_failed=["lint"], checks_run=GATE_IDS, failed_count=1, checks_not_applicable=[])
    gates["checks"][0].update(status="fail", exit_code=1)
    _write_json(output / "gates.json", gates)
    (output / "gates.txt").write_bytes(
        "".join(f"{check['id']}:{check['status']}\r\n" for check in gates["checks"]).encode()
    )
    (output / "failed.txt").write_bytes(b"lint\r\n")
    (output / "gates.checks.jsonl").write_bytes(
        "".join(json.dumps(check, sort_keys=True) + "\r\n" for check in gates["checks"]).encode()
    )
    return output, gates


@pytest.mark.parametrize(
    ("name", "contradiction"),
    [
        pytest.param(
            "gates.txt", b"lint:pass\r\nformat:pass\r\ntypes:pass\r\ntests:pass\r\nreview:pass\r\n", id="statuses"
        ),
        pytest.param("failed.txt", b"tests\n", id="failure-list"),
        pytest.param("gates.checks.jsonl", b'{"id":"lint","status":"pass"}\n', id="different-records"),
        pytest.param("gates.checks.jsonl", b"{unreadable\n", id="invalid-jsonl"),
    ],
)
def test_rejects_gate_summary_that_disagrees_with_manifest(summarized_gates, name, contradiction):
    """A coherent receipt cannot validate alongside a summary with a contradictory status or row."""
    output, gates = summarized_gates
    assert VALIDATOR._validate_gates(output) == gates
    (output / name).write_bytes(contradiction)
    with pytest.raises(SystemExit, match=f"gate-summary-mismatch:{name}"):
        VALIDATOR._validate_gates(output)


@pytest.mark.parametrize("name", ["gates.txt", "failed.txt", "gates.checks.jsonl"])
def test_current_gate_summary_is_required(summarized_gates, name):
    """An otherwise coherent current runner receipt needs all three summary files."""
    output, _ = summarized_gates
    (output / name).unlink()
    with pytest.raises(SystemExit, match=f"gate-summary-missing:{name}"):
        VALIDATOR._validate_gates(output)


@pytest.mark.parametrize("aggregate", ["checks_run", "failed_count", "checks_not_applicable"])
def test_missing_aggregate_does_not_relax_summary_requirements(summarized_gates, aggregate):
    """Remaining producer evidence identifies the attempt when one aggregate and one summary are lost."""
    output, gates = summarized_gates
    gates.pop(aggregate)
    _write_json(output / "gates.json", gates)
    (output / "failed.txt").unlink()
    with pytest.raises(SystemExit, match=r"^gate-summary-missing:failed\.txt$"):
        VALIDATOR._validate_gates(output)


def test_execution_route_identifies_runner_without_aggregate_keys(local_gates):
    """An observed command route still requires the runner's evidence when the aggregate keys are absent."""
    output, gates = local_gates
    gates["checks"][0]["execution_route"] = {"mode": "standard", "reason": "fixture observed command"}
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match=r"^gate-summary-missing:gates\.txt$"):
        VALIDATOR._validate_gates(output)


def test_legacy_aggregate_counts_do_not_require_new_summary_files(local_gates):
    """A historical producer may record counts without ever writing the runner's summary set."""
    output, gates = local_gates
    gates.update(failed_count=0, checks_not_applicable=[])
    _write_json(output / "gates.json", gates)
    assert VALIDATOR._validate_gates(output) == gates


@pytest.fixture
def legacy_canonical_gates(local_gates):
    """Retain a historical unbound gate receipt using canonical check paths and no summaries."""
    output, gates = local_gates
    (output / "checks/local-source-snapshot.json").unlink()
    gates.pop("source")
    for check in gates["checks"]:
        check.pop("source")
    gates.update(failed_count=0, checks_not_applicable=[])
    _write_json(output / "gates.json", gates)
    return output, gates


def _lint_arguments(output, command):
    """Select one public lint command with the other four gates explicitly skipped."""
    skipped = [
        argument
        for gate in ("format", "types", "tests", "review")
        for argument in (f"--skip-{gate}", "fixture only runs lint")
    ]
    runner = Path(VALIDATOR.__file__).with_name("run_gates.py")
    return [str(runner), "--out", str(output), "--lint", command, *skipped]


def _run_lint(output, command):
    """Launch a public lint-only gate with its working directory inside the fixture."""
    return subprocess.run(
        [sys.executable, *_lint_arguments(output, command)],
        cwd=output,
        capture_output=True,
        text=True,
        check=False,
    )


def test_retry_archives_readable_legacy_receipt_with_canonical_log_names(legacy_canonical_gates):
    """The public runner rotates a historical receipt without inventing summaries it never produced."""
    output, gates = legacy_canonical_gates
    prior = (output / "gates.json").read_bytes()
    assert VALIDATOR._validate_gates(output) == gates

    retry = _run_lint(output, "exit 0")

    assert retry.returncode == 0, retry.stderr
    archive = output / "gate-attempts/001"
    assert (archive / "gates.json").read_bytes() == prior
    assert not (archive / "gates.txt").exists()
    assert not (archive / "failed.txt").exists()
    assert not (archive / "gates.checks.jsonl").exists()
    assert VALIDATOR._validate_gates(archive) == gates
    assert VALIDATOR._validate_gates(output)["checks_not_applicable"] == ["format", "types", "tests", "review"]


@pytest.fixture
def legacy_named_log(legacy_canonical_gates, request):
    """Move one recorded stream to its historical run-relative filename."""
    output, gates = legacy_canonical_gates
    check = gates["checks"][0]
    original = output / check["stdout"]
    check["stdout"] = getattr(request, "param", "prior-lint-output.txt")
    path = output / check["stdout"]
    # Windows may normalize the complete destination before creating any directory.
    for parent in path.parents:
        if parent.name == "..":
            parent.parent.mkdir(parents=True, exist_ok=True)
            assert parent.parent.is_dir()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A differently cased spelling can already refer to the original file on this filesystem.
    if not path.exists():
        original.rename(path)
    path.write_bytes(b"historical lint output\r\n")
    _write_json(output / "gates.json", gates)
    return output, gates, path


@pytest.mark.parametrize(
    "legacy_named_log",
    [
        "prior-lint-output.txt",
        "historical/logs/lint.out",
        "checks/Lint.stdout.txt",
        "checks/../checks/lint.stdout.txt",
        "old-layout/../historical/logs/lint.out",
    ],
    indirect=True,
)
def test_retry_archives_legacy_named_log_with_aggregate_counts(legacy_named_log):
    """A historical stream remains independently readable from the archive after rotating a counted receipt."""
    output, gates, path = legacy_named_log
    assert VALIDATOR._validate_gates(output) == gates
    previous = (output / "gates.json").read_bytes()
    content = path.read_bytes()
    retry = _run_lint(output, "exit 0")
    assert retry.returncode == 0, retry.stderr
    archive = output / "gate-attempts/001"
    assert (archive / "gates.json").read_bytes() == previous
    archived_log = archive / path.relative_to(output)
    assert archived_log.read_bytes() == content
    assert VALIDATOR._validate_gates(archive) == gates
    assert VALIDATOR._validate_gates(output)["checks_run"] == GATE_IDS
    archived_log.rename(archive / "retained-historical-log")
    with pytest.raises(SystemExit, match="gate-check-missing-log:0:stdout"):
        VALIDATOR._validate_gates(archive)


def test_historical_log_from_another_run_remains_outside_the_archive(legacy_canonical_gates, tmp_path_factory):
    """A parent-relative reference to a different run blocks rotation and leaves the referenced bytes intact."""
    output, gates = legacy_canonical_gates
    other = tmp_path_factory.mktemp("other-gate-run") / "lint.stdout.txt"
    other.write_bytes(b"other run's log")
    gates["checks"][0]["stdout"] = Path(os.path.relpath(other, output)).as_posix()
    _write_json(output / "gates.json", gates)
    prior = (output / "gates.json").read_bytes()
    with pytest.raises(SystemExit, match="gate-check-log-outside-output"):
        VALIDATOR._validate_gates(output)
    rejected = _run_lint(output, "echo started > lint-started.txt")
    assert rejected.returncode == 2, rejected.stderr
    assert not (output / "lint-started.txt").exists()
    assert not (output / "gate-attempts/001").exists()
    assert (output / "gates.json").read_bytes() == prior
    assert other.read_bytes() == b"other run's log"


def test_legacy_retry_preserves_unreferenced_canonical_log_before_execution(legacy_named_log):
    """An occupied canonical output that the prior receipt never named blocks execution and rotation."""
    output, gates, _ = legacy_named_log
    gates.pop("checks_not_applicable")
    _write_json(output / "gates.json", gates)
    prior = (output / "gates.json").read_bytes()
    canonical = output / "checks/lint.stdout.txt"
    canonical.write_bytes(b"independent notes\r\n")
    identity = canonical.stat()
    assert VALIDATOR._validate_gates(output) == gates
    retry = _run_lint(output, "echo started > lint-started.txt")
    assert retry.returncode == 2, retry.stderr
    assert "gate-recovery-blocked:gate-publication-unowned-log:checks/lint.stdout.txt\n" in retry.stderr
    assert not (output / "lint-started.txt").exists()
    assert os.path.samestat(identity, canonical.stat())
    assert canonical.read_bytes() == b"independent notes\r\n"
    assert (output / "gates.json").read_bytes() == prior
    assert not (output / "gate-attempts/001").exists()
    assert VALIDATOR._validate_gates(output) == gates


def test_existing_public_damage_marker_blocks_first_gate(tmp_path):
    """Keep an existing public damage report and refuse to execute gates into the unreadable destination."""
    marker = tmp_path / "gate-log-errors.json"
    marker.write_bytes(b'{"note":"retained by another process"}\r\n')
    identity = marker.stat()
    result = _run_lint(tmp_path, "echo started > lint-started.txt")
    assert result.returncode == 2, result.stderr
    assert not (tmp_path / "lint-started.txt").exists()
    assert not (tmp_path / "gates.json").exists()
    assert not (tmp_path / ".gate-publication.json").exists()
    assert os.path.samestat(identity, marker.stat())
    assert marker.read_bytes() == b'{"note":"retained by another process"}\r\n'


def _interrupt_legacy_rotation(output, monkeypatch):
    """Seal a lint retry over the current receipt, stop it as the prior archive enters history, return its stage."""
    arguments = _lint_arguments(output, "exit 0")
    script = Path(arguments[0])
    monkeypatch.syspath_prepend(str(script.parent))
    monkeypatch.setattr(sys, "argv", arguments)
    runner = load_module_scoped("legacy_gate_runner", script, monkeypatch)
    rename = os.rename
    numbered = output / "gate-attempts/001"

    def interrupt_rotation(source, destination):
        """Leave the prior archive staged and the journal sealed when rotation starts."""
        if Path(destination) == numbered:
            raise OSError("interrupted archive rotation")
        rename(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", interrupt_rotation)
        assert runner.main() == 2
    return output / json.loads((output / ".gate-publication.json").read_bytes())["stage"]


def test_resumed_legacy_journal_refuses_changed_archived_log(legacy_named_log, monkeypatch):
    """A historical log changed in the staged archive after sealing stops recovery before the archive rotates."""
    output, _, path = legacy_named_log
    previous = (output / "gates.json").read_bytes()
    archived = _interrupt_legacy_rotation(output, monkeypatch) / "previous" / path.relative_to(output)
    archived.write_bytes(b"changed after sealing\r\n")
    result = _run_lint(output, "echo started > lint-started.txt")
    assert result.returncode == 2, result.stderr
    assert "gate-recovery-blocked:gate-publication-archive-changed\n" in result.stderr
    assert archived.read_bytes() == b"changed after sealing\r\n"
    assert (output / "gates.json").read_bytes() == previous
    assert not (output / "gate-attempts/001").exists()
    assert not (output / "lint-started.txt").exists()


def test_legacy_journal_cannot_replace_unreferenced_canonical_log(legacy_named_log, monkeypatch):
    """An interrupted older journal claiming an independent canonical log cannot replace it during recovery."""
    output, gates, _ = legacy_named_log
    gates.pop("checks_not_applicable")
    _write_json(output / "gates.json", gates)
    previous = (output / "gates.json").read_bytes()
    assert VALIDATOR._validate_gates(output) == gates
    stage = _interrupt_legacy_rotation(output, monkeypatch)
    numbered = output / "gate-attempts/001"
    marker = output / ".gate-publication.json"
    owner = json.loads(marker.read_bytes())
    plan_path = stage / "publication.json"
    plan = json.loads(plan_path.read_bytes())
    relative = "checks/lint.stdout.txt"
    public = output / relative
    public.write_bytes(b"independent lint output")
    identity = public.stat()
    (stage / "previous" / relative).write_bytes(public.read_bytes())
    # Older receipts admitted the colliding public file into their replay plan and staged archive.
    plan["before"][relative] = [
        identity.st_dev,
        identity.st_ino,
        identity.st_ctime_ns,
        identity.st_mtime_ns,
        hashlib.sha256(public.read_bytes()).hexdigest(),
    ]
    owner["sha256"] = _write_json(plan_path, plan)
    _write_json(marker, owner)

    result = _run_lint(output, "echo started > lint-started.txt")
    assert result.returncode == 2, result.stderr
    assert "gate-recovery-blocked:gate-publication-unowned-log:checks/lint.stdout.txt\n" in result.stderr
    assert os.path.samestat(identity, public.stat())
    assert public.read_bytes() == b"independent lint output"
    assert (output / "gates.json").read_bytes() == previous
    assert not (output / "lint-started.txt").exists()
    assert not numbered.exists()
    assert marker.exists()


def test_checks_present_historical_summary_without_requiring_all_summaries(local_gates):
    """Legacy runs remain readable unless a summary they actually retain contradicts the manifest."""
    output, gates = local_gates
    marker = output / "failed.txt"
    marker.write_bytes(b"")
    assert VALIDATOR._validate_gates(output) == gates
    marker.write_bytes(b"lint\n")
    with pytest.raises(SystemExit, match=r"^gate-summary-mismatch:failed\.txt$"):
        VALIDATOR._validate_gates(output)


@pytest.mark.parametrize(
    "relative", ["checks/tests.stdout.txt", "local-source/review-worktree.json", "checks/local-source-snapshot.json"]
)
def test_validator_rejects_changed_gate_artifacts(local_gates, relative):
    """Fail validation when a log, receipt or frozen snapshot changes after the gate records its digest."""
    output, _ = local_gates
    assert VALIDATOR._validate_gates(output)["status"] == "pass"
    (output / relative).write_text("tampered\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="gate-evidence"):
        VALIDATOR._validate_gates(output)


def test_validator_rejects_removed_artifact_hashes(local_gates):
    """Do not allow a local gate to bypass hash validation by deleting its artifact map."""
    output, gates = local_gates
    gates["checks"][3]["source"].pop("artifacts")
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence"):
        VALIDATOR._validate_gates(output)


def test_runtime_snapshot_requires_the_top_level_local_source(local_gates):
    """A runner-owned snapshot prevents removing the source binding from an executed local gate run."""
    output, gates = local_gates
    gates.pop("source")
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence-local-source-missing"):
        VALIDATOR._validate_gates(output)


def test_local_receipt_accepts_the_top_level_project_environment(local_gates):
    """The runner adds the project environment only at top level after constructing per-check source receipts."""
    output, gates = local_gates
    gates["source"]["project_env"] = (output / "environment").as_posix()
    _write_json(output / "gates.json", gates)
    assert VALIDATOR._validate_gates(output)["status"] == "pass"


def test_validator_rejects_a_removed_local_source_mode(local_gates):
    """Keep each executed gate bound to the declared local mirror even if its mode field is removed."""
    output, gates = local_gates
    gates["checks"][3]["source"].pop("mode")
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence-local-source-mismatch"):
        VALIDATOR._validate_gates(output)


def test_validator_rejects_a_changed_snapshot_observation(local_gates):
    """A passing gate must observe the admitted snapshot both before and after execution."""
    output, gates = local_gates
    gates["checks"][3]["source"]["after"] = {"snapshot_sha256": "b" * 64}
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence-local-observation-mismatch"):
        VALIDATOR._validate_gates(output)


def test_validator_binds_python_proof_to_the_hashed_sidecar(local_gates):
    """Do not accept an inline passing proof that disagrees with the unchanged retained sidecar."""
    output, gates = local_gates
    proof = {"status": "fail"}
    relative = "checks/tests.python-imports.json"
    digest = _write_json(output / relative, proof)
    check = gates["checks"][3]
    check["python_imports"] = {"status": "pass"}
    check["source"]["artifacts"][relative] = digest
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence-python-proof-mismatch"):
        VALIDATOR._validate_gates(output)


def test_retained_python_proof_cannot_be_removed_from_the_gate(local_gates):
    """A retained import-origin proof cannot be ignored by stripping both inline proof and its hash entry."""
    output, gates = local_gates
    _write_json(output / "checks/tests.python-imports.json", {"status": "fail"})
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence-python-proof-mismatch"):
        VALIDATOR._validate_gates(output)


def test_reader_waits_for_current_gate_publication(local_gates):
    """A current receipt cannot be accepted while the writer installs its evidence."""
    output, _ = local_gates
    marker = output / ".gate-publication.json"
    _write_json(marker, {})
    with pytest.raises(SystemExit, match="gate-publication-in-progress"):
        VALIDATOR._validate_gates(output)
    marker.unlink()
    assert VALIDATOR._validate_gates(output)["status"] == "pass"


@pytest.mark.parametrize(
    ("change", "error"),
    [
        pytest.param("marker", "gate-publication-in-progress", id="publication-in-progress"),
        pytest.param("manifest", "gate-publication-manifest-changed", id="publication-completed"),
    ],
)
def test_reader_rechecks_publication_after_validating_gate_files(local_gates, monkeypatch, change, error):
    """A reader spanning the start or completion of publication cannot return its earlier receipt."""
    output, gates = local_gates
    validate = VALIDATOR._validate_gate_evidence

    def publication_between_reads(*args, **kwargs):
        """Make a publisher transition after the reader has checked the retained evidence."""
        validate(*args, **kwargs)
        if change == "marker":
            _write_json(output / ".gate-publication.json", {})
        else:
            replacement = output / "new-gates.json"
            _write_json(replacement, gates)
            replacement.replace(output / "gates.json")

    monkeypatch.setattr(VALIDATOR, "_validate_gate_evidence", publication_between_reads)
    with pytest.raises(SystemExit, match=error):
        VALIDATOR._validate_gates(output)


def test_private_mirror_gate_stage_keeps_original_worktree_coordinates(local_gates):
    """Check the private bytes against the original mirror coordinates only for the internal producer."""
    output, _ = local_gates
    stage = output / "private-stage"
    stage.mkdir()
    (output / "gates.json").rename(stage / "gates.json")
    (output / "checks").rename(stage / "checks")
    (output / "local-source").rename(stage / "local-source")
    with pytest.raises(SystemExit, match="gate-evidence-local-source-mismatch"):
        VALIDATOR._validate_gates(stage)
    assert VALIDATOR._validate_gates(stage, mirror_root=output)["status"] == "pass"


@pytest.fixture
def tracked_membership_failure(local_gates):
    """Retain a clean child pass with an independent failure to reobserve the admitted inventory."""
    output, gates = local_gates
    (output / "checks/local-source-snapshot.json").unlink()
    for item in gates["checks"]:
        item.pop("source")
    worktree = (output / "frozen-source").as_posix()
    head = "a" * 40
    source = {"worktree": worktree, "expected_head": head}
    gates.update(status="fail", checks_failed=["tests"], source=source)
    check = gates["checks"][3]
    diagnostic = "tracked-source-inventory-changed"
    proof = {"status": "pass", "mode": "in-process-pytest", "worktree": worktree}
    check.update(status="fail", python_imports=proof)
    observed = {"head": head, "status": "", "ignored": "[]"}
    artifacts = {}
    check["source"] = {
        **source,
        "before": observed,
        "after": observed,
        "guard_error": diagnostic,
        "artifacts": artifacts,
    }
    inventory = worktree.encode() + b"\0" + head.encode() + b"\0pkg.py"
    digest = hashlib.sha256(inventory).hexdigest()
    relative = f"checks/tracked-source-{digest}.bin"
    (output / relative).write_bytes(inventory)
    artifacts[relative] = digest
    proof_path = "checks/tests.python-imports.json"
    artifacts[proof_path] = _write_json(output / proof_path, proof)
    argv = [
        "python",
        "-B",
        "run_gates.py",
        "--_python-tests-child",
        worktree,
        str(output / "checks" / ("pytest-scratch-" + "a" * 32) / "tests.python-imports.json"),
        '["pkg"]',
        '["-q"]',
        "python",
        json.dumps({"tracked_inventory": {"path": str(output / relative), "sha256": digest, "head": head}}),
    ]
    (output / check["command_path"]).write_text("argv-json:" + json.dumps(argv) + "\n", encoding="utf-8")
    (output / check["stderr"]).write_text(diagnostic + "\n", encoding="utf-8")
    for key in ("command_path", "stdout", "stderr"):
        artifacts[check[key]] = hashlib.sha256((output / check[key]).read_bytes()).hexdigest()
    _write_json(output / "gates.json", gates)
    return output, gates, check, relative


def test_clean_test_receipt_distinguishes_parent_failure_from_child_exit(tracked_membership_failure):
    """Accept a zero child exit only when the recorded parent inventory guard explains the failure."""
    output, _, check, _ = tracked_membership_failure
    assert VALIDATOR._validate_gates(output)["checks"][3] == check


def test_parent_membership_guard_needs_original_inventory(tracked_membership_failure):
    """A failed membership recheck still requires the artifact the child originally consumed."""
    output, _, _, relative = tracked_membership_failure
    (output / relative).unlink()
    with pytest.raises(SystemExit, match="gate-evidence-missing-file"):
        VALIDATOR._validate_gates(output)


def test_parent_membership_guard_needs_its_own_diagnostic_line(tracked_membership_failure):
    """A different stderr line cannot authorize a zero-exit failure even with an updated digest."""
    output, gates, check, _ = tracked_membership_failure
    relative = check["stderr"]
    (output / relative).write_text("unrelated-tracked-source-inventory-changed\n", encoding="utf-8")
    check["source"]["artifacts"][relative] = hashlib.sha256((output / relative).read_bytes()).hexdigest()
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-check-invalid-fail-exit-code"):
        VALIDATOR._validate_gates(output)


def test_historical_inventory_exception_validates_all_other_gate_evidence(tracked_membership_failure):
    """Prove one retained historical discrepancy without accepting the damaged receipt as current evidence."""
    output, gates, check, relative = tracked_membership_failure
    check["source"].pop("guard_error")
    check["status"] = "pass"
    gates.update(status="pass", checks_failed=[])
    _write_json(output / "gates.json", gates)
    assert VALIDATOR._validate_gates(output)["status"] == "pass"
    damaged = b"retained damaged inventory\n"
    (output / relative).write_bytes(damaged)
    report = output / "tracked-source-errors.json"
    recorded = {relative: hashlib.sha256(damaged).hexdigest()}
    _write_json(report, recorded)

    with pytest.raises(SystemExit, match="gate-evidence-digest-mismatch"):
        VALIDATOR._validate_gates(output)
    assert VALIDATOR._validate_gates(output, damaged_inventories=recorded) == gates

    _write_json(report, {relative: None})
    with pytest.raises(SystemExit, match="gate-evidence-inventory-damage-report-mismatch"):
        VALIDATOR._validate_gates(output, damaged_inventories=recorded)
    _write_json(report, recorded)

    (output / check["stdout"]).write_text("unrelated artifact changed\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="gate-evidence-digest-mismatch"):
        VALIDATOR._validate_gates(output, damaged_inventories=recorded)


def test_historical_inventory_exception_requires_the_reported_absence(tracked_membership_failure):
    """Treat a missing inventory as historical damage only while that exact retained file is absent."""
    output, _, _, relative = tracked_membership_failure
    missing = output / relative
    missing.unlink()
    recorded = {relative: None}
    _write_json(output / "tracked-source-errors.json", recorded)

    with pytest.raises(SystemExit, match="gate-evidence-missing-file"):
        VALIDATOR._validate_gates(output)
    assert VALIDATOR._validate_gates(output, damaged_inventories=recorded)["status"] == "fail"

    missing.write_bytes(b"present inventory\n")
    with pytest.raises(SystemExit, match="gate-evidence-inventory-damage-report-mismatch"):
        VALIDATOR._validate_gates(output, damaged_inventories=recorded)


def test_pass_requires_an_executed_gate():
    """Five skipped gates cannot justify a passing result."""
    gates = {
        "status": "pass",
        "checks_failed": [],
        "checks": [{"id": gate_id, "status": "not-applicable"} for gate_id in GATE_IDS],
    }
    result = {"status": "pass", "checks_run": GATE_IDS, "checks_failed": []}
    with pytest.raises(SystemExit, match="result-pass-without-executed-gates"):
        VALIDATOR._reconcile_result_with_gates(result, gates)


def test_terminal_review_can_retain_inapplicable_code_tests():
    """A completed terminal review still passes when code tests were intentionally inapplicable."""
    gates = {
        "status": "pass",
        "checks_failed": [],
        "checks": [
            {"id": gate_id, "status": "pass" if gate_id == "review" else "not-applicable"} for gate_id in GATE_IDS
        ],
    }
    VALIDATOR._reconcile_result_with_gates(
        {"status": "pass", "checks_run": GATE_IDS, "checks_failed": [], "metadata": {"review_status": "closed"}},
        gates,
        "code-review",
    )


def test_incomplete_result_can_retain_skipped_test_gate():
    """An honest failed result may keep tests unexecuted while preserving the verification gap."""
    gates = {
        "status": "pass",
        "checks_failed": [],
        "checks": [{"id": gate_id, "status": "not-applicable"} for gate_id in GATE_IDS],
    }
    VALIDATOR._reconcile_result_with_gates({"status": "fail", "checks_run": GATE_IDS, "checks_failed": []}, gates)


@pytest.mark.parametrize(
    "invalid_scratch",
    [
        pytest.param("other-scratch", id="foreign-name"),
        pytest.param("pytest-scratch-123", id="short-token"),
        pytest.param("pytest-scratch-" + "z" * 32, id="invalid-token"),
        pytest.param("nested/pytest-scratch-" + "a" * 32, id="extra-directory"),
    ],
)
def test_passing_clean_test_gate_requires_the_inventory_named_by_its_child_command(local_gates, invalid_scratch):
    """Validate archived inventories beside their receipts even after the original run directory moves."""
    output, gates = local_gates
    gates.pop("source")
    (output / "checks/local-source-snapshot.json").unlink()
    for check in gates["checks"]:
        check.pop("source")
    check = gates["checks"][3]
    head = "a" * 40
    worktree = (output / "frozen-source").as_posix()
    check["source"] = {"expected_head": head, "worktree": worktree}
    artifacts = {
        check[key]: hashlib.sha256((output / check[key]).read_bytes()).hexdigest()
        for key in ("command_path", "stdout", "stderr")
    }
    check["source"]["artifacts"] = artifacts
    proof = {"status": "pass"}
    check["python_imports"] = proof
    artifacts["checks/tests.python-imports.json"] = _write_json(output / "checks/tests.python-imports.json", proof)
    original = output.parent / "original"
    argv = [
        "python",
        "-B",
        "run_gates.py",
        "--_python-tests-child",
        worktree,
        str(original / "checks/tests.python-imports.json"),
        '["pkg"]',
        '["-q"]',
        "python",
    ]
    command = output / check["command_path"]

    def save():
        command.write_text("argv-json:" + json.dumps(argv) + "\n", encoding="utf-8")
        artifacts[check["command_path"]] = hashlib.sha256(command.read_bytes()).hexdigest()
        _write_json(output / "gates.json", gates)

    save()
    assert VALIDATOR._validate_gates(output)["status"] == "pass"
    raw = worktree.encode() + b"\0" + head.encode() + b"\0pkg.py"
    digest = hashlib.sha256(raw).hexdigest()
    relative = f"checks/tracked-source-{digest}.bin"
    (output / relative).write_bytes(raw)
    argv.append(json.dumps({"tracked_inventory": {"path": str(original / relative), "sha256": digest, "head": head}}))
    artifacts[relative] = digest
    save()
    assert VALIDATOR._validate_gates(output)["status"] == "pass"
    argv[5] = str(original / "checks/pytest-scratch/tests.python-imports.json")
    save()
    assert VALIDATOR._validate_gates(output)["status"] == "pass"
    argv[5] = str(original / "checks" / ("pytest-scratch-" + "a" * 32) / "tests.python-imports.json")
    save()
    assert VALIDATOR._validate_gates(output)["status"] == "pass"
    argv[5] = str(original / "checks" / invalid_scratch / "tests.python-imports.json")
    save()
    with pytest.raises(SystemExit, match="gate-evidence-tracked-inventory"):
        VALIDATOR._validate_gates(output)
    argv[5] = str(original / "checks/pytest-scratch/tests.python-imports.json")
    del artifacts[relative]
    (output / relative).unlink()
    save()
    with pytest.raises(SystemExit, match="gate-evidence-tracked-inventory"):
        VALIDATOR._validate_gates(output)
