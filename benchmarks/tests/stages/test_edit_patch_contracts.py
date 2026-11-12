"""Regression tests for the provider-neutral edit and patch benchmark contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

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

SUITES_DIR = Path(__file__).resolve().parents[2] / "suites"
PATCH_SUITE_PATH = SUITES_DIR / "tasks-patch.json"
FIX_SINGLE_SUITE_PATH = SUITES_DIR / "tasks-fix-single.json"
_FIXTURE_DIFF_ANSWER = "```diff\ndiff --git a/src/fixture.py b/src/fixture.py\n```"
_INDEX_RELATIVE_PATH = Path(".cache/codemap/patch/PT-fixture.json")
_FIXED_EARLY_STOPPING = """
class EarlyStopping(Callback):
{body}
    def __init__(self, monitor, patience=3):
        if patience < 1:
            raise MisconfigurationException(f"patience must be >= 1, got {{patience}}")
        self.patience = patience
"""


def _patch_task(*, drop: tuple[str, ...] = (), **overrides: object) -> dict[str, object]:
    """Build a scoreable patch task with separate primary and regression commands.

    Keys named in ``drop`` are removed and ``overrides`` replace the defaults, so a test states only what it breaks.

    >>> task = _patch_task()
    >>> task["scoreable"], task["gt_files_changed"]
    (True, ['src/fixture.py'])
    >>> task["test_command"] not in task["regression_test_commands"]
    True
    >>> "test_command" in _patch_task(drop=("test_command",))
    False
    """
    task: dict[str, object] = {
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
    task.update(overrides)
    for key in drop:
        task.pop(key)
    return task


def _patch_index_locks(tmp_path: Path, contract: Any, *, canonical_root: str) -> Path:
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
    index_path = tmp_path / _INDEX_RELATIVE_PATH
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


def _fix_single_candidate(root: Path, body: str) -> Any:
    """Write the canonical single-file candidate with the supplied class body and return its contract.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     root = Path(directory)
    ...     contract = _fix_single_candidate(root, "pass")
    ...     contract.task_id, (root / contract.expected_paths[0]).is_file()
    ('FS-01', True)
    """
    task = next(item for item in json.loads(FIX_SINGLE_SUITE_PATH.read_text(encoding="utf-8")) if item["id"] == "FS-01")
    contract = build_fix_single_contract(task)
    candidate = root / contract.expected_paths[0]
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(_FIXED_EARLY_STOPPING.format(body=body), encoding="utf-8")
    return contract


@pytest.fixture(name="contract")
def _contract() -> Any:
    """Return the shared edit contract built from the default scoreable patch task."""
    return build_edit_task_contract(_patch_task())


@pytest.fixture(name="stage")
def _stage() -> StageIdentity:
    """Return the stage identity the provider-binding checks are anchored to."""
    return StageIdentity(stage="patch-v1", revision="rev-1", task_suite_sha256="a" * 64, contract_sha256="b" * 64)


class TestBuildEditTaskContract:
    """A patch task enters the shared edit stage only with an executable oracle and a disjoint fixture."""

    @pytest.mark.parametrize(
        ("task", "match"),
        [
            pytest.param(_patch_task(drop=("test_command",)), "test_command", id="no-target-test"),
            pytest.param(
                _patch_task(drop=("regression_test_commands",)), "regression_test_commands", id="no-regression"
            ),
            pytest.param(_patch_task(drop=("test_fixture_patch",)), "test_fixture_patch", id="no-staged-fixture"),
            pytest.param(
                _patch_task(gt_files_changed=["tests/test_fixture.py"]), "must not overlap", id="fixture-overlap"
            ),
        ],
    )
    def test_rejects_task_without_complete_oracle(self, task: dict[str, object], match: str) -> None:
        """A task missing part of its executable oracle, or editing its own fixture, cannot be scored.

        A target-only patch task, one without the staged test fixture, or one whose expected changes include the fixture
        itself would let the stage score keyword and file diagnostics instead of executed patch evidence.
        """
        with pytest.raises(ValueError, match=match):
            build_edit_task_contract(task)


class TestScoreEditExecution:
    """Patch scores need application, targeted-test and regression evidence inside the expected path boundary."""

    def test_requires_application_targeted_and_regression_evidence(self, contract: Any) -> None:
        """Keyword/file diagnostics cannot replace patch and test evidence.

        The patch applied and the target test passed, but the regression suite failed: the answer is primary-correct yet
        unsafe, and must not be pooled with trustworthy results.
        """
        execution = EditExecution(
            patch_applied=True,
            targeted_test_passed=True,
            regression_test_passed=False,
            changed_paths=("src/fixture.py",),
        )

        score = score_edit_execution(contract, assess_patch_answer(_FIXTURE_DIFF_ANSWER), execution)

        assert score.primary_correct is True
        assert score.safety_passed is False
        assert score.pooling_eligible is False

    def test_rejects_lifecycle_failure_and_unexpected_paths(self, contract: Any) -> None:
        """A passing target cannot compensate for a leaked fixture or extra source path.

        Every test passes here, but the staged fixture was not left intact and a source file outside the expected set
        changed, so neither the primary result nor the path boundary may count.
        """
        execution = EditExecution(
            patch_applied=True,
            targeted_test_passed=True,
            regression_test_passed=True,
            changed_paths=("src/fixture.py", "src/unexpected.py"),
            baseline_target_failed=True,
            baseline_regressions_passed=True,
            fixture_intact=False,
        )

        score = score_edit_execution(contract, assess_patch_answer(_FIXTURE_DIFF_ANSWER), execution)

        assert score.primary_correct is False
        assert score.safety_passed is False
        assert score.changed_path_boundary_passed is False
        assert score.pooling_eligible is False

    def test_pt05_contract_excludes_reference_only_documentation(self) -> None:
        """PT-05 accepts its behaviorally complete source-only patch boundary.

        The shipped PT-05 task lists documentation as reference only; a patch touching just the source file must pass
        the boundary and stay eligible for pooling.
        """
        task = next(
            item for item in json.loads(PATCH_SUITE_PATH.read_text(encoding="utf-8"))["tasks"] if item["id"] == "PT-05"
        )
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


class TestEditExecution:
    """Execution evidence is serialized the same way for every provider adapter."""

    def test_serializes_provider_neutral_patch_evidence(self) -> None:
        """Patch adapters need the same JSON-safe execution boundary as Fix stages.

        Regression: Claude Patch completed its independent execution, then failed before persisting cell 1 because
        ``EditExecution`` lacked ``as_dict()``.
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


class TestAssessPatchAnswer:
    """A model answer becomes a mutable-cell candidate only through one well-formed fenced diff."""

    def test_rejects_multiple_fenced_diffs(self) -> None:
        """Only one fenced diff may become a mutable-cell candidate.

        Two fences leave the intended patch ambiguous, so the answer fails closed instead of picking one.
        """
        with pytest.raises(ValueError, match="exactly one fenced diff"):
            assess_patch_answer("```diff\ndiff --git a/a b/a\n```\n```diff\ndiff --git a/b b/b\n```")

    def test_preserves_trailing_blank_context_marker(self) -> None:
        """Fence framing may be removed, but a diff's meaningful leading space must survive.

        A blank context line in a unified diff is a single space; stripping it would corrupt the hunk the patch applier
        later reads.
        """
        answer = assess_patch_answer("```diff\ndiff --git a/a.py b/a.py\n@@ -1 +1 @@\n old\n \n```")

        assert answer.diff.endswith("\n ")


class TestStageIdentity:
    """Stage identities pin the scientific fields that make results from different providers comparable."""

    def test_comparison_detects_prior_stage_drift(self) -> None:
        """A new stage cannot silently replace an already admitted stage identity.

        The prior and observed identities differ only in the task-suite hash, which is exactly the silent swap this
        comparison exists to catch.
        """
        prior = StageIdentity(
            stage="agentic-v1", revision="rev-1", task_suite_sha256="a" * 64, contract_sha256="b" * 64
        )
        observed = StageIdentity(
            stage="agentic-v1", revision="rev-1", task_suite_sha256="c" * 64, contract_sha256="b" * 64
        )

        with pytest.raises(ValueError, match="prior-stage identity changed"):
            compare_stage_identities({prior.stage: prior}, {observed.stage: observed})

    def test_provider_binding_accepts_unchanged_scientific_fields(self, contract: Any, stage: StageIdentity) -> None:
        """A provider binding that repeats the contract's own hashes is admitted.

        Provider metadata may vary around the binding, but its scientific fields must equal the shared contract's.
        """
        binding = dict(contract.scientific_field_hashes(stage))

        assert validate_provider_binding(contract, stage, binding) is None

    def test_provider_binding_rejects_changed_scientific_field(self, contract: Any, stage: StageIdentity) -> None:
        """Provider metadata may vary, but the shared contract hashes may not.

        Changing the oracle hash in an otherwise valid binding would let one provider be scored against a different
        test, so the binding is refused.
        """
        binding = dict(contract.scientific_field_hashes(stage))
        binding["oracle_sha256"] = "c" * 64

        with pytest.raises(ValueError, match="scientific fields"):
            validate_provider_binding(contract, stage, binding)


class TestValidatePatchIndexBundle:
    """Installed graph bytes are rechecked against locks recorded when the patch indexes were prepared."""

    def test_returns_locked_coordinates(self, tmp_path: Path, contract: Any) -> None:
        """Untouched index bytes validate and report their baseline commit, byte hash and scan version."""
        locks_path = _patch_index_locks(tmp_path, contract, canonical_root=str(tmp_path))
        index_bytes = (tmp_path / _INDEX_RELATIVE_PATH).read_bytes()

        coordinates = validate_patch_index_bundle(tmp_path, locks_path, [contract])

        assert coordinates[contract.task_id]["baseline_commit"] == contract.baseline_commit
        assert coordinates[contract.task_id]["raw_index_sha256"] == hashlib.sha256(index_bytes).hexdigest()
        assert coordinates[contract.task_id]["scan_version"] == "13"

    def test_rejects_post_build_byte_drift(self, tmp_path: Path, contract: Any) -> None:
        """Provider preflight must recheck installed graph bytes, not trust preparation history.

        The index is edited after the locks were written; the semantic digest no longer matches, so validation fails
        even though the file is still well-formed JSON.
        """
        locks_path = _patch_index_locks(tmp_path, contract, canonical_root=str(tmp_path))
        index_path = tmp_path / _INDEX_RELATIVE_PATH
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        payload["modules"][0]["name"] = "tampered"
        index_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        with pytest.raises(ValueError, match="semantic SHA-256 drifted"):
            validate_patch_index_bundle(tmp_path, locks_path, [contract])

    @pytest.mark.parametrize(
        "canonical_root", ["/canonical/checkout", r"C:\canonical\checkout", r"\\share\canonical\checkout"]
    )
    def test_accepts_canonical_root_recorded_on_either_platform(
        self, tmp_path: Path, contract: Any, canonical_root: str
    ) -> None:
        """Lock documents are portable data, so the reading host does not decide absoluteness.

        Regression: the gate used host-flavoured ``Path.is_absolute()``. A POSIX-recorded ``/canonical/checkout`` reads
        as relative on Windows and fail-closed a valid lock; a Windows-recorded ``C:\\…`` is rejected on POSIX for the
        mirror reason.
        """
        locks_path = _patch_index_locks(tmp_path, contract, canonical_root=canonical_root)

        coordinates = validate_patch_index_bundle(tmp_path, locks_path, [contract])

        assert coordinates[contract.task_id]["baseline_commit"] == contract.baseline_commit

    def test_rejects_relative_canonical_root(self, tmp_path: Path, contract: Any) -> None:
        """Widening to both flavours must not accept a root that is absolute under neither."""
        locks_path = _patch_index_locks(tmp_path, contract, canonical_root="canonical/checkout")

        with pytest.raises(ValueError, match="absolute canonical root"):
            validate_patch_index_bundle(tmp_path, locks_path, [contract])


class TestSemanticIndexSha256:
    """The semantic digest identifies one graph regardless of the host that scanned it."""

    def test_ignores_recording_host_separator(self) -> None:
        """One graph keeps one identity whether it was scanned on Windows or on POSIX.

        Regression: only the root was stripped, so the digest still covered ``<runtime-root>\\src\\x.py`` on Windows and
        ``<runtime-root>/src/x.py`` on POSIX. Every recorded lock was captured on POSIX, so a Windows run could never
        verify one — and no test caught it, because tests derive both sides through this same function.
        """
        posix_payload = {
            "scan_version": 13,
            "scan_root": "/canonical/checkout",
            "modules": [{"file": "/canonical/checkout/src/app.py", "imports": ["/canonical/checkout/src/helper.py"]}],
        }
        windows_payload = {
            "scan_version": 13,
            "scan_root": r"D:\canonical\checkout",
            "modules": [
                {"file": r"D:\canonical\checkout\src\app.py", "imports": [r"D:\canonical\checkout\src\helper.py"]}
            ],
        }

        posix_digest = semantic_index_sha256(posix_payload, PurePosixPath("/canonical/checkout"))
        windows_digest = semantic_index_sha256(windows_payload, PureWindowsPath(r"D:\canonical\checkout"))

        assert posix_digest == windows_digest

    def test_ignores_the_scan_root_key_that_hashes_the_runtime_root(self) -> None:
        """The scan-root key identifies where a scan ran, so two scans of one graph in different roots hash the same.

        A fixture indexed in two temporary directories records different root keys; the graph identity must not depend
        on which directory held the scan.
        """
        first = {"scan_version": 14, "scan_root_key": "a" * 64, "modules": [{"file": "src/app.py"}]}
        second = {"scan_version": 14, "scan_root_key": "b" * 64, "modules": [{"file": "src/app.py"}]}
        root = PurePosixPath("/canonical/checkout")

        assert semantic_index_sha256(first, root) == semantic_index_sha256(second, root)

    def test_leaves_non_path_backslashes_alone(self) -> None:
        """Only a tail that followed the runtime root is renormalized; content is not touched.

        A literal backslash sequence in documentation text must still influence the digest, and a payload with no path
        under the root must not depend on which root was supplied.
        """
        root = PurePosixPath("/canonical/checkout")
        payload = {"modules": [{"file": "/canonical/checkout/src/app.py", "doc": "escape \\n stays literal"}]}
        unrooted = {"modules": [{"file": "/elsewhere/src/app.py", "doc": "escape \\n stays literal"}]}

        assert semantic_index_sha256(payload, root) != semantic_index_sha256(unrooted, root)
        assert semantic_index_sha256(unrooted, root) == semantic_index_sha256(unrooted, PurePosixPath("/other/root"))


class TestRunFixSingleOracle:
    """The Fix-Single oracle runs candidate code in a contained child, never in the scoring process."""

    def test_candidate_that_kills_its_interpreter_fails_the_oracle_not_the_scorer(self, tmp_path: Path) -> None:
        """A candidate that kills its interpreter must fail the oracle, not the scorer.

        ``os._exit`` in the candidate would end an in-process scorer; in the child it only leaves the worker without a
        verdict, which is reported as an oracle error.
        """
        contract = _fix_single_candidate(tmp_path, "    import os\n\n    os._exit(0)\n")

        with pytest.raises(ValueError, match="returned no verdict"):
            run_fix_single_oracle(tmp_path, contract, timeout_s=30.0)

    @pytest.mark.parametrize(
        ("body", "timeout_s", "expected_verdict"),
        [
            pytest.param("    while True:\n        pass\n", 1.0, False, id="non-terminating-candidate-is-bounded"),
            pytest.param(
                "    print('{\"passed\": false}')\n", 30.0, True, id="candidate-stdout-noise-is-not-the-verdict"
            ),
        ],
    )
    def test_verdict_is_independent_of_candidate_behavior(
        self, tmp_path: Path, body: str, timeout_s: float, expected_verdict: bool
    ) -> None:
        """A non-terminating candidate is a failed candidate, and candidate printing is not the worker's verdict line.

        A candidate that never returns fails rather than causing an unbounded stall; a candidate that prints a verdict-
        shaped line must not be mistaken for the worker's own verdict line. The deadline only bounds the child, so the
        short value for the looping candidate changes how long the test waits, not what it proves.
        """
        contract = _fix_single_candidate(tmp_path, body)

        assert run_fix_single_oracle(tmp_path, contract, timeout_s=timeout_s) is expected_verdict

    def test_keeps_candidate_writes_out_of_the_scorer_working_directory(self, tmp_path: Path) -> None:
        """Candidate file writes land in the disposable sandbox, never beside the scorer.

        A relative write from the candidate must not appear in the scorer's own working directory or in the repository
        checkout the oracle was pointed at.
        """
        contract = _fix_single_candidate(
            tmp_path, '    with open("candidate-escape.txt", "w") as handle:\n        handle.write("x")\n'
        )

        assert run_fix_single_oracle(tmp_path, contract, timeout_s=30.0) is True
        assert not (Path.cwd() / "candidate-escape.txt").exists()
        assert not (tmp_path / "candidate-escape.txt").exists()


class TestMinimalChildEnv:
    """The oracle's pruned child environment carries only what the target platform needs to start Python."""

    def test_keeps_simulated_windows_interpreter_startup_variables(self) -> None:
        """The oracle's pruned environment still lets a Windows child interpreter start.

        Regression: the worker was spawned with ``PATH`` alone. A Windows CPython reads ``SystemRoot`` while seeding
        hash randomization, so every candidate died with ``_Py_HashRandomization_Init: failed to get random numbers``
        before running a line.
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

    def test_stays_pruned_to_path_off_simulated_windows(self) -> None:
        """No POSIX host inherits more than the search path from the scoring process.

        Home directories and Windows-only variables in the parent environment must not leak into a candidate's child.
        """
        env = minimal_child_env({"PATH": "/usr/bin", "SystemRoot": "/ignored", "HOME": "/home/bench"}, platform="linux")

        assert env == {"PATH": "/usr/bin"}
