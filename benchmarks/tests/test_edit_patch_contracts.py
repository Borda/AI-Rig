"""Regression tests for the provider-neutral edit and patch benchmark contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from _bench_common.edit_patch_contracts import (
    EditExecution,
    StageIdentity,
    assess_patch_answer,
    build_edit_task_contract,
    build_fix_single_contract,
    compare_stage_identities,
    run_fix_single_oracle,
    score_edit_execution,
    semantic_index_sha256,
    validate_patch_index_bundle,
    validate_provider_binding,
)
from _bench_common.subprocess_env import minimal_child_env


def _patch_task() -> dict[str, object]:
    """Build a scoreable patch task with separate primary and regression commands.

    >>> task = _patch_task()
    >>> task["scoreable"], task["gt_files_changed"]
    (True, ['src/fixture.py'])
    >>> task["test_command"] not in task["regression_test_commands"]
    True
    """
    return {
        "id": "PT-fixture",
        "type": "patch_task",
        "prompt": "Fix the fixture and return one unified diff.",
        "pre_fix_commit": "a" * 40,
        "test_fixture_patch": (
            "diff --git a/tests/test_fixture.py b/tests/test_fixture.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/tests/test_fixture.py\n"
            "@@ -0,0 +1 @@\n"
            "+def test_fixture() -> None: pass\n"
        ),
        "test_command": "pytest tests/test_fixture.py::test_fix -x",
        "gt_files_changed": ["src/fixture.py"],
        "regression_test_commands": ["pytest tests/test_fixture.py -x"],
        "scoreable": True,
    }


def test_provider_neutral_patch_contract_requires_executable_oracle() -> None:
    """A patch task without a target test cannot enter the shared edit stage."""
    task = _patch_task()
    task.pop("test_command")

    with pytest.raises(ValueError, match="test_command"):
        build_edit_task_contract(task)


def test_provider_neutral_patch_contract_requires_regression_and_staged_fixture() -> None:
    """A target-only patch task cannot enter the shared executable contract."""
    task = _patch_task()
    task.pop("regression_test_commands")

    with pytest.raises(ValueError, match="regression_test_commands"):
        build_edit_task_contract(task)

    task = _patch_task()
    task.pop("test_fixture_patch")

    with pytest.raises(ValueError, match="test_fixture_patch"):
        build_edit_task_contract(task)

    task = _patch_task()
    task["gt_files_changed"] = ["tests/test_fixture.py"]

    with pytest.raises(ValueError, match="must not overlap"):
        build_edit_task_contract(task)


def test_patch_score_requires_application_targeted_and_regression_evidence() -> None:
    """Keyword/file diagnostics cannot replace patch and test evidence."""
    contract = build_edit_task_contract(_patch_task())
    answer = assess_patch_answer("```diff\ndiff --git a/src/fixture.py b/src/fixture.py\n```")
    execution = EditExecution(
        patch_applied=True,
        targeted_test_passed=True,
        regression_test_passed=False,
        changed_paths=("src/fixture.py",),
    )

    score = score_edit_execution(contract, answer, execution)

    assert score.primary_correct is True
    assert score.safety_passed is False
    assert score.pooling_eligible is False


def test_edit_execution_serializes_provider_neutral_patch_evidence() -> None:
    """Patch adapters need the same JSON-safe execution boundary as Fix stages.

    Regression: Claude Patch completed its independent execution, then failed
    before persisting cell 1 because ``EditExecution`` lacked ``as_dict()``.
    """
    execution = EditExecution(
        patch_applied=True,
        targeted_test_passed=True,
        regression_test_passed=True,
        changed_paths=("src/fixture.py",),
        command_evidence={"target": {"returncode": 0}},
    )

    assert execution.as_dict() == {
        "patch_applied": True,
        "targeted_test_passed": True,
        "regression_test_passed": True,
        "changed_paths": ("src/fixture.py",),
        "baseline_target_failed": True,
        "baseline_regressions_passed": True,
        "fixture_intact": True,
        "source_integrity": True,
        "index_integrity": None,
        "cleanup_verified": True,
        "command_evidence": {"target": {"returncode": 0}},
        "error": None,
    }


def test_patch_score_rejects_lifecycle_failure_and_unexpected_paths() -> None:
    """A passing target cannot compensate for a leaked fixture or extra source path."""
    contract = build_edit_task_contract(_patch_task())
    answer = assess_patch_answer("```diff\ndiff --git a/src/fixture.py b/src/fixture.py\n```")
    execution = EditExecution(
        patch_applied=True,
        targeted_test_passed=True,
        regression_test_passed=True,
        changed_paths=("src/fixture.py", "src/unexpected.py"),
        baseline_target_failed=True,
        baseline_regressions_passed=True,
        fixture_intact=False,
    )

    score = score_edit_execution(contract, answer, execution)

    assert score.primary_correct is False
    assert score.safety_passed is False
    assert score.changed_path_boundary_passed is False
    assert score.pooling_eligible is False


def test_pt05_contract_excludes_reference_only_documentation() -> None:
    """PT-05 accepts its behaviorally complete source-only patch boundary."""
    suite_path = Path(__file__).parents[1] / "suites" / "tasks-patch.json"
    task = next(item for item in json.loads(suite_path.read_text(encoding="utf-8"))["tasks"] if item["id"] == "PT-05")
    contract = build_edit_task_contract(task)
    answer = assess_patch_answer(
        "```diff\n"
        "diff --git a/src/lightning/pytorch/loops/training_epoch_loop.py "
        "b/src/lightning/pytorch/loops/training_epoch_loop.py\n"
        "```"
    )
    execution = EditExecution(
        patch_applied=True,
        targeted_test_passed=True,
        regression_test_passed=True,
        changed_paths=("src/lightning/pytorch/loops/training_epoch_loop.py",),
    )

    score = score_edit_execution(contract, answer, execution)

    assert contract.expected_paths == ("src/lightning/pytorch/loops/training_epoch_loop.py",)
    assert score.changed_path_boundary_passed is True
    assert score.pooling_eligible is True


def test_malformed_or_multiple_patch_envelopes_fail_closed() -> None:
    """Only one fenced diff may become a mutable-cell candidate."""
    with pytest.raises(ValueError, match="exactly one fenced diff"):
        assess_patch_answer("```diff\ndiff --git a/a b/a\n```\n```diff\ndiff --git a/b b/b\n```")


def test_patch_answer_preserves_a_trailing_blank_context_marker() -> None:
    """Fence framing may be removed, but a diff's meaningful leading space must survive."""
    answer = assess_patch_answer("```diff\ndiff --git a/a.py b/a.py\n@@ -1 +1 @@\n old\n \n```")

    assert answer.diff.endswith("\n ")


def test_stage_identity_detects_prior_stage_drift() -> None:
    """A new stage cannot silently replace an already admitted stage identity."""
    prior = StageIdentity(stage="agentic-v1", revision="rev-1", task_suite_sha256="a" * 64, contract_sha256="b" * 64)
    observed = StageIdentity(stage="agentic-v1", revision="rev-1", task_suite_sha256="c" * 64, contract_sha256="b" * 64)

    with pytest.raises(ValueError, match="prior-stage identity changed"):
        compare_stage_identities({prior.stage: prior}, {observed.stage: observed})


def test_provider_binding_cannot_change_scientific_fields() -> None:
    """Provider metadata may vary, but the shared contract hashes may not."""
    contract = build_edit_task_contract(_patch_task())
    stage = StageIdentity(
        stage="patch-v1",
        revision="rev-1",
        task_suite_sha256="a" * 64,
        contract_sha256="b" * 64,
    )
    binding = dict(contract.scientific_field_hashes(stage))

    validate_provider_binding(contract, stage, binding)
    binding["oracle_sha256"] = "c" * 64

    with pytest.raises(ValueError, match="scientific fields"):
        validate_provider_binding(contract, stage, binding)


def test_patch_index_validation_rejects_post_build_byte_drift(tmp_path: Path) -> None:
    """Provider preflight must recheck installed graph bytes, not trust preparation history."""
    contract = build_edit_task_contract(_patch_task())
    index_path = tmp_path / ".cache/codemap/patch/PT-fixture.json"
    index_path.parent.mkdir(parents=True)
    payload = {
        "scan_version": 13,
        "scanned_at": "locked",
        "project": "provider-parity-PT-fixture",
        "scan_root": str(tmp_path),
        "modules": [{"name": "fixture", "path": "src/fixture.py"}],
    }
    index_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    index_path.write_bytes(index_bytes)
    locks_path = tmp_path / "locks.json"
    locks_path.write_text(
        json.dumps(
            {
                "schema_version": "provider-parity-patch-index-locks-v1",
                "canonical_scan_root": str(tmp_path),
                "tasks": {
                    "PT-fixture": {
                        "baseline_commit": contract.baseline_commit,
                        "module_count": 1,
                        "raw_sha256_at_canonical_root": hashlib.sha256(index_bytes).hexdigest(),
                        "scan_version": 13,
                        "scanned_at": "locked",
                        "semantic_sha256": semantic_index_sha256(payload, tmp_path),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    coordinates = validate_patch_index_bundle(tmp_path, locks_path, [contract])
    assert coordinates[contract.task_id]["baseline_commit"] == contract.baseline_commit
    assert coordinates[contract.task_id]["raw_index_sha256"] == hashlib.sha256(index_bytes).hexdigest()
    assert coordinates[contract.task_id]["scan_version"] == "13"

    payload["modules"][0]["name"] = "tampered"
    index_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="semantic SHA-256 drifted"):
        validate_patch_index_bundle(tmp_path, locks_path, [contract])


def _patch_index_locks(tmp_path: Path, contract: object, *, canonical_root: str) -> Path:
    """Write a scan and matching byte/semantic locks, retaining the caller's canonical-root spelling.

    >>> from tempfile import TemporaryDirectory
    >>> from types import SimpleNamespace
    >>> with TemporaryDirectory() as directory:
    ...     contract = SimpleNamespace(baseline_commit="abc")
    ...     path = _patch_index_locks(Path(directory), contract, canonical_root="example")
    ...     locks = json.loads(path.read_text())
    ...     task, = locks["tasks"].values()
    ...     locks["canonical_scan_root"], task["baseline_commit"], task["module_count"]
    ('example', 'abc', 1)
    """
    index_path = tmp_path / ".cache/codemap/patch/PT-fixture.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scan_version": 13,
        "scanned_at": "locked",
        "project": "provider-parity-PT-fixture",
        "scan_root": str(tmp_path),
        "modules": [{"name": "fixture", "path": "src/fixture.py"}],
    }
    index_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    index_path.write_bytes(index_bytes)
    locks_path = tmp_path / "locks.json"
    locks_path.write_text(
        json.dumps(
            {
                "schema_version": "provider-parity-patch-index-locks-v1",
                "canonical_scan_root": canonical_root,
                "tasks": {
                    "PT-fixture": {
                        "baseline_commit": contract.baseline_commit,
                        "module_count": 1,
                        "raw_sha256_at_canonical_root": hashlib.sha256(index_bytes).hexdigest(),
                        "scan_version": 13,
                        "scanned_at": "locked",
                        "semantic_sha256": semantic_index_sha256(payload, tmp_path),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return locks_path


@pytest.mark.parametrize(
    "canonical_root",
    ["/canonical/checkout", r"C:\canonical\checkout", r"\\share\canonical\checkout"],
)
def test_patch_index_locks_accept_a_canonical_root_recorded_on_either_platform(
    tmp_path: Path, canonical_root: str
) -> None:
    """Lock documents are portable data, so the reading host does not decide absoluteness.

    Regression: the gate used host-flavoured ``Path.is_absolute()``. A POSIX-recorded
    ``/canonical/checkout`` reads as relative on Windows and fail-closed a valid lock; a
    Windows-recorded ``C:\\…`` is rejected on POSIX for the mirror reason.
    """
    contract = build_edit_task_contract(_patch_task())
    locks_path = _patch_index_locks(tmp_path, contract, canonical_root=canonical_root)

    coordinates = validate_patch_index_bundle(tmp_path, locks_path, [contract])

    assert coordinates[contract.task_id]["baseline_commit"] == contract.baseline_commit


def test_patch_index_locks_still_reject_a_relative_canonical_root(tmp_path: Path) -> None:
    """Widening to both flavours must not accept a root that is absolute under neither."""
    contract = build_edit_task_contract(_patch_task())
    locks_path = _patch_index_locks(tmp_path, contract, canonical_root="canonical/checkout")

    with pytest.raises(ValueError, match="absolute canonical root"):
        validate_patch_index_bundle(tmp_path, locks_path, [contract])


def test_semantic_index_identity_ignores_the_recording_host_separator() -> None:
    """One graph keeps one identity whether it was scanned on Windows or on POSIX.

    Regression: only the root was stripped, so the digest still covered
    ``<runtime-root>\\src\\x.py`` on Windows and ``<runtime-root>/src/x.py`` on POSIX.
    Every recorded lock was captured on POSIX, so a Windows run could never verify one —
    and no test caught it, because tests derive both sides through this same function.
    """
    posix_payload = {
        "scan_version": 13,
        "scan_root": "/canonical/checkout",
        "modules": [{"file": "/canonical/checkout/src/app.py", "imports": ["/canonical/checkout/src/helper.py"]}],
    }
    windows_payload = {
        "scan_version": 13,
        "scan_root": r"D:\canonical\checkout",
        "modules": [{"file": r"D:\canonical\checkout\src\app.py", "imports": [r"D:\canonical\checkout\src\helper.py"]}],
    }

    posix_digest = semantic_index_sha256(posix_payload, PurePosixPath("/canonical/checkout"))
    windows_digest = semantic_index_sha256(windows_payload, PureWindowsPath(r"D:\canonical\checkout"))

    assert posix_digest == windows_digest


def test_semantic_index_identity_leaves_non_path_backslashes_alone() -> None:
    """Only a tail that followed the runtime root is renormalized; content is not touched."""
    root = PurePosixPath("/canonical/checkout")
    payload = {"modules": [{"file": "/canonical/checkout/src/app.py", "doc": "escape \\n stays literal"}]}
    unrooted = {"modules": [{"file": "/elsewhere/src/app.py", "doc": "escape \\n stays literal"}]}

    assert semantic_index_sha256(payload, root) != semantic_index_sha256(unrooted, root)
    assert semantic_index_sha256(unrooted, root) == semantic_index_sha256(unrooted, PurePosixPath("/other/root"))


SUITE_PATH = Path(__file__).resolve().parents[1] / "suites" / "tasks-fix-single.json"
_FIXED_EARLY_STOPPING = """
class EarlyStopping(Callback):
{body}
    def __init__(self, monitor, patience=3):
        if patience < 1:
            raise MisconfigurationException(f"patience must be >= 1, got {{patience}}")
        self.patience = patience
"""


def _fix_single_candidate(root: Path, body: str) -> object:
    """Write the canonical single-file candidate with the supplied class body and return its contract.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     root = Path(directory)
    ...     contract = _fix_single_candidate(root, "pass")
    ...     contract.task_id, (root / contract.expected_paths[0]).is_file()
    ('FS-01', True)
    """
    task = next(item for item in json.loads(SUITE_PATH.read_text(encoding="utf-8")) if item["id"] == "FS-01")
    contract = build_fix_single_contract(task)
    candidate = root / contract.expected_paths[0]
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(_FIXED_EARLY_STOPPING.format(body=body), encoding="utf-8")
    return contract


def test_fix_single_oracle_does_not_execute_candidate_code_in_the_scoring_process(tmp_path: Path) -> None:
    """A candidate that kills its interpreter must fail the oracle, not the scorer."""
    contract = _fix_single_candidate(tmp_path, "    import os\n\n    os._exit(0)\n")

    with pytest.raises(ValueError, match="returned no verdict"):
        run_fix_single_oracle(tmp_path, contract, timeout_s=30.0)


def test_fix_single_oracle_bounds_a_candidate_that_never_returns(tmp_path: Path) -> None:
    """A non-terminating candidate is a failed candidate rather than an unbounded stall."""
    contract = _fix_single_candidate(tmp_path, "    while True:\n        pass\n")

    assert run_fix_single_oracle(tmp_path, contract, timeout_s=5.0) is False


def test_fix_single_oracle_keeps_candidate_writes_out_of_the_scorer_working_directory(tmp_path: Path) -> None:
    """Candidate file writes land in the disposable sandbox, never beside the scorer."""
    contract = _fix_single_candidate(
        tmp_path, '    with open("candidate-escape.txt", "w") as handle:\n        handle.write("x")\n'
    )

    assert run_fix_single_oracle(tmp_path, contract, timeout_s=30.0) is True
    assert not (Path.cwd() / "candidate-escape.txt").exists()
    assert not (tmp_path / "candidate-escape.txt").exists()


def test_fix_single_oracle_verdict_survives_candidate_stdout_noise(tmp_path: Path) -> None:
    """Candidate printing must not be mistaken for the worker's verdict line."""
    contract = _fix_single_candidate(tmp_path, "    print('{\"passed\": false}')\n")

    assert run_fix_single_oracle(tmp_path, contract, timeout_s=30.0) is True


def test_oracle_child_environment_keeps_simulated_windows_interpreter_startup_variables() -> None:
    """The oracle's pruned environment still lets a Windows child interpreter start.

    Regression: the worker was spawned with ``PATH`` alone. A Windows CPython reads
    ``SystemRoot`` while seeding hash randomization, so every candidate died with
    ``_Py_HashRandomization_Init: failed to get random numbers`` before running a line.
    """
    source = {
        "PATH": r"C:\Windows\System32",
        "SystemRoot": r"C:\Windows",
        "COMSPEC": r"C:\Windows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT",
        "TEMP": r"C:\Temp",
        "ANTHROPIC_API_KEY": "secret",
    }

    env = minimal_child_env(source, platform="win32")

    assert env["PATH"] == r"C:\Windows\System32"
    assert env["SystemRoot"] == r"C:\Windows"
    assert {"COMSPEC", "PATHEXT", "TEMP"} <= set(env)
    assert "ANTHROPIC_API_KEY" not in env


def test_oracle_child_environment_stays_pruned_to_path_off_simulated_windows() -> None:
    """No POSIX host inherits more than the search path from the scoring process."""
    env = minimal_child_env({"PATH": "/usr/bin", "SystemRoot": "/ignored", "HOME": "/home/bench"}, platform="linux")

    assert env == {"PATH": "/usr/bin"}
