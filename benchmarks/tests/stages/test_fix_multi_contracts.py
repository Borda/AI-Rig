"""Independent caller-completeness oracle tests for the multi-file executable stage."""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from _fix_multi_sources import (
    CHECKPOINT_PROVENANCE_LOG,
    DRY_RUN_LOG,
    FORWARDED_SETUP,
    STOP_DECISION,
    add_dry_run_parameter,
    complete_early_stopping_source,
    complete_model_checkpoint_source,
    complete_strategy_environment_source,
)
from _frozen_repo import BENCHMARKS, FROZEN_REPO, requires_frozen_repo

SUITE_PATH = Path(__file__).resolve().parents[2] / "suites" / "tasks-fix-multi.json"
sys.path.insert(0, str(BENCHMARKS))

from _bench_common.edit_patch_contracts import build_fix_multi_contract, run_fix_multi_oracle  # noqa: E402

_STRATEGIES = "src/lightning/pytorch/strategies"
_DDP = f"{_STRATEGIES}/ddp.py"
_EARLY_STOPPING_ELSE_STOP_DECISION = "            trainer.should_stop = trainer.should_stop or should_stop\n"
_EARLY_STOPPING_NORMAL_PATH = (
    "        trainer.should_stop = trainer.should_stop or should_stop\n"
    "        if should_stop:\n"
    "            self.stopped_epoch = trainer.current_epoch\n"
    "            self.stopping_reason_message = reason\n"
    "        if reason and self.verbose:\n"
    "            self._log_info(trainer, reason, self.log_rank_zero_only)\n"
)


def _tasks() -> dict[str, dict[str, object]]:
    """Load the canonical multi-file task records keyed by task id.

    Example:
        >>> tasks = _tasks()
        >>> all(key == task["id"] for key, task in tasks.items()) and bool(tasks)
        True
    """
    return {task["id"]: task for task in json.loads(SUITE_PATH.read_text(encoding="utf-8"))}


def _contract(task_id: str) -> object:
    """Return one immutable multi-file contract from the canonical task bytes.

    Example:
        >>> _contract("not-a-real-task")
        Traceback (most recent call last):
            ...
        KeyError: 'not-a-real-task'
    """
    return build_fix_multi_contract(_tasks()[task_id])


def _copy_contract_sources(repo_path: Path, destination: Path, contract: object) -> None:
    """Copy every source file declared by a contract into an isolated candidate tree.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> from types import SimpleNamespace
        >>> with TemporaryDirectory() as directory:
        ...     root = Path(directory)
        ...     _ = (root / "example.py").write_text("VALUE = 1", encoding="utf-8")
        ...     _copy_contract_sources(root, root / "candidate", SimpleNamespace(expected_paths=("example.py",)))
        ...     (root / "candidate/example.py").read_text(encoding="utf-8")
        'VALUE = 1'
    """
    for relative_path in contract.expected_paths:
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo_path / relative_path, target)


def _complete_early_stopping_else_source(source: str) -> str:
    """Return the FM-01 source with an equivalent observe-only if/else branch.

    Example:
        >>> _complete_early_stopping_else_source("self._run_early_stopping_check(trainer)")
        'self._run_early_stopping_check(trainer, dry_run=False)'
    """
    dry_run_branch = (
        "        if dry_run:\n"
        '            self._log_info(trainer, f"dry run: should_stop={should_stop}, reason={reason}", self.log_rank_zero_only)\n'
        "        else:\n"
        "            trainer.should_stop = trainer.should_stop or should_stop\n"
        "            if should_stop:\n"
        "                self.stopped_epoch = trainer.current_epoch\n"
        "                self.stopping_reason_message = reason\n"
        "            if reason and self.verbose:\n"
        "                self._log_info(trainer, reason, self.log_rank_zero_only)\n"
    )
    return add_dry_run_parameter(source).replace(_EARLY_STOPPING_NORMAL_PATH, dry_run_branch)


def _complete_early_stopping_negative_guard_source(source: str) -> str:
    """Return FM-01 source using an equivalent negative guard around mutations.

    Example:
        >>> _complete_early_stopping_negative_guard_source("self._run_early_stopping_check(trainer)")
        'self._run_early_stopping_check(trainer, dry_run=False)'
    """
    guarded_path = (
        "        if dry_run:\n"
        f"            {DRY_RUN_LOG}\n"
        "        if not dry_run:\n"
        "            trainer.should_stop = trainer.should_stop or should_stop\n"
        "            if should_stop:\n"
        "                self.stopped_epoch = trainer.current_epoch\n"
        "                self.stopping_reason_message = reason\n"
        "        if reason and self.verbose:\n"
        "            self._log_info(trainer, reason, self.log_rank_zero_only)\n"
    )
    return add_dry_run_parameter(source).replace(_EARLY_STOPPING_NORMAL_PATH, guarded_path)


@pytest.fixture
def stage_sources(tmp_path: Path) -> Callable[[str], object]:
    """Return a factory that copies one task's frozen sources into the candidate tree and returns its contract."""

    def stage(task_id: str) -> object:
        contract = _contract(task_id)
        _copy_contract_sources(FROZEN_REPO, tmp_path, contract)
        return contract

    return stage


@pytest.fixture
def strategy_contract(tmp_path: Path, stage_sources: Callable[[str], object]) -> object:
    """FM-03 contract whose staged candidate tree has the base and every override forwarding verbose."""
    contract = stage_sources("FM-03")
    for relative_path in contract.expected_paths:
        target = tmp_path / relative_path
        target.write_text(
            complete_strategy_environment_source(
                target.read_text(encoding="utf-8"), is_base=relative_path.endswith("strategies/strategy.py")
            ),
            encoding="utf-8",
        )
    return contract


class TestSuiteContract:
    """Static shape of the multi-file task suite and its contracts."""

    def test_strategy_paths_cover_base_and_every_override(self) -> None:
        """The independent oracle covers the base plus every production cooperative override.

        FM-03 must list six files ending with the XLA strategy and must not include the single-device XLA strategy,
        which has no cooperative override to check.
        """
        contract = _contract("FM-03")

        assert len(contract.expected_paths) == 6
        assert contract.expected_paths[-1] == f"{_STRATEGIES}/xla.py"
        assert f"{_STRATEGIES}/single_xla.py" not in contract.expected_paths

    @pytest.mark.parametrize(
        ("task_id", "identifier"),
        [
            pytest.param("FM-02", "on_exception", id="fm02-on-exception"),
            pytest.param("FM-02", "_save_last_checkpoint", id="fm02-save-last-checkpoint"),
            pytest.param("FM-02", "_save_none_monitor_checkpoint", id="fm02-save-none-monitor-checkpoint"),
            pytest.param("FM-02", "_update_best_and_save", id="fm02-update-best-and-save"),
            pytest.param("FM-03", "DDPStrategy", id="fm03-ddp"),
            pytest.param("FM-03", "FSDPStrategy", id="fm03-fsdp"),
            pytest.param("FM-03", "DeepSpeedStrategy", id="fm03-deepspeed"),
            pytest.param("FM-03", "ModelParallelStrategy", id="fm03-model-parallel"),
            pytest.param("FM-03", "SingleXLAStrategy", id="fm03-single-xla"),
            pytest.param("FM-03", "XLAStrategy", id="fm03-xla"),
        ],
    )
    def test_prompt_omits_answer_identifier(self, task_id: str, identifier: str) -> None:
        """Task text does not hand every arm the caller or override identifiers to be discovered.

        Discovery of the callers (FM-02) and strategy overrides (FM-03) is the measured work, so their names must not
        appear verbatim in the prompt.
        """
        assert identifier not in _tasks()[task_id]["prompt"]

    @pytest.mark.parametrize(
        ("task_id", "phrase"),
        [
            pytest.param("FM-02", "all four", id="fm02-all-four"),
            pytest.param("FM-03", "use codemap rdeps", id="fm03-use-codemap-rdeps"),
        ],
    )
    def test_prompt_omits_discovery_hint(self, task_id: str, phrase: str) -> None:
        """Task text does not state the answer count or tell the model which tool finds the answer.

        The phrase is matched case-insensitively because a hint is equally revealing in any casing.
        """
        assert phrase not in _tasks()[task_id]["prompt"].lower()


@requires_frozen_repo
class TestFrozenOracle:
    """Fix-multi oracles scored against the pinned frozen checkout and candidate copies of it."""

    @pytest.mark.parametrize("task_id", ["FM-01", "FM-02", "FM-03"])
    def test_baseline_fails_oracle(self, task_id: str) -> None:
        """Each task begins from a baseline that cannot accidentally satisfy its new contract.

        The unmodified frozen checkout is scored by the complete-caller oracle and must fail it.
        """
        assert run_fix_multi_oracle(FROZEN_REPO, _contract(task_id)) is False

    @pytest.mark.parametrize(
        ("task_id", "complete"),
        [
            pytest.param("FM-01", complete_early_stopping_source, id="early-stopping-early-return"),
            pytest.param("FM-01", _complete_early_stopping_else_source, id="early-stopping-else-branch"),
            pytest.param("FM-01", _complete_early_stopping_negative_guard_source, id="early-stopping-negative-guard"),
            pytest.param("FM-02", complete_model_checkpoint_source, id="model-checkpoint-exact-reasons"),
        ],
    )
    def test_accepts_complete_single_file_candidate(
        self, tmp_path: Path, stage_sources: Callable[[str], object], task_id: str, complete: Callable[[str], str]
    ) -> None:
        """A complete candidate passes whichever equivalent syntax implements the contract.

        FM-01 may use an early return, an if/else, or a negative guard around the persistent mutations; FM-02 must label
        every save with its exact reason. The oracle may not prefer one syntax over another.
        """
        contract = stage_sources(task_id)
        target = tmp_path / contract.expected_paths[0]
        target.write_text(complete(target.read_text(encoding="utf-8")), encoding="utf-8")

        assert run_fix_multi_oracle(tmp_path, contract) is True

    @pytest.mark.parametrize(
        ("task_id", "complete", "old", "new", "count", "expected"),
        [
            pytest.param(
                "FM-01",
                complete_early_stopping_source,
                DRY_RUN_LOG,
                'message = f"dry run: should_stop={should_stop}, reason={reason}"\n            log.info(message)',
                -1,
                True,
                id="early-stopping-log-through-variable",
            ),
            pytest.param(
                "FM-01",
                complete_early_stopping_source,
                DRY_RUN_LOG,
                'log_message = f"dry run: should_stop={should_stop}, reason={reason}"\n'
                "            self._log_info(trainer, log_message, self.log_rank_zero_only)",
                -1,
                True,
                id="early-stopping-log-through-log-info-helper",
            ),
            pytest.param(
                "FM-01",
                complete_early_stopping_source,
                "trainer, dry_run=False)",
                "trainer)",
                1,
                False,
                id="early-stopping-stale-internal-caller",
            ),
            pytest.param(
                "FM-01",
                complete_early_stopping_source,
                DRY_RUN_LOG,
                'log.info("dry run")',
                -1,
                False,
                id="early-stopping-generic-log-text",
            ),
            pytest.param(
                "FM-01",
                complete_early_stopping_source,
                DRY_RUN_LOG,
                f"if reason:\n                {DRY_RUN_LOG}",
                -1,
                False,
                id="early-stopping-log-only-with-reason",
            ),
            pytest.param(
                "FM-01",
                complete_early_stopping_source,
                DRY_RUN_LOG,
                f"if self.verbose:\n                {DRY_RUN_LOG}",
                -1,
                False,
                id="early-stopping-log-only-when-verbose",
            ),
            pytest.param(
                "FM-01",
                complete_early_stopping_source,
                DRY_RUN_LOG,
                'log.info("dry run: should_stop={should_stop}, reason={reason}")',
                -1,
                False,
                id="early-stopping-log-without-interpolation",
            ),
            pytest.param(
                "FM-01",
                complete_early_stopping_source,
                "        if dry_run:\n",
                f"{STOP_DECISION}        self.stopped_epoch = trainer.current_epoch\n        if dry_run:\n",
                1,
                False,
                id="early-stopping-mutates-before-dry-run-branch",
            ),
            pytest.param(
                "FM-01",
                _complete_early_stopping_else_source,
                _EARLY_STOPPING_ELSE_STOP_DECISION,
                "",
                1,
                False,
                id="early-stopping-else-branch-drops-normal-path",
            ),
            pytest.param(
                "FM-01",
                _complete_early_stopping_negative_guard_source,
                "        if not dry_run:\n            trainer.should_stop = trainer.should_stop or should_stop\n",
                "        trainer.should_stop = trainer.should_stop or should_stop\n        if not dry_run:\n",
                1,
                False,
                id="early-stopping-negative-guard-leaks-mutation",
            ),
            pytest.param(
                "FM-02",
                complete_model_checkpoint_source,
                "reason='top_k'",
                "reason='best'",
                -1,
                False,
                id="model-checkpoint-generic-reason-label",
            ),
            pytest.param(
                "FM-02",
                complete_model_checkpoint_source,
                CHECKPOINT_PROVENANCE_LOG,
                "        trainer.save_checkpoint(filepath, self.save_weights_only)\n"
                "        if reason:\n"
                '            rank_zero_info(f"{reason}: saving checkpoint to {filepath}")\n',
                -1,
                False,
                id="model-checkpoint-logs-after-save",
            ),
        ],
    )
    def test_single_file_candidate_verdict(
        self,
        tmp_path: Path,
        stage_sources: Callable[[str], object],
        task_id: str,
        complete: Callable[[str], str],
        old: str,
        new: str,
        count: int,
        expected: bool,
    ) -> None:
        """A single edit to a complete candidate flips the oracle only when it breaks the contract.

        Starting from a complete FM-01/FM-02 candidate, one edit either keeps the observable decision log and
        persistence-boundary provenance (accepted variants) or breaks a caller, the log content or its position
        (rejected variants).
        """
        contract = stage_sources(task_id)
        target = tmp_path / contract.expected_paths[0]
        candidate = complete(target.read_text(encoding="utf-8")).replace(old, new, count)
        target.write_text(candidate, encoding="utf-8")

        assert run_fix_multi_oracle(tmp_path, contract) is expected

    def test_accepts_strategy_overrides_forwarding_verbose(self, tmp_path: Path, strategy_contract: object) -> None:
        """FM-03 accepts cooperative propagation through the base and every production override.

        Every setup_environment in the six strategy files accepts and forwards verbose, which is the complete answer the
        oracle must pass.
        """
        assert run_fix_multi_oracle(tmp_path, strategy_contract) is True

    @pytest.mark.parametrize(
        ("path", "old", "new", "expected"),
        [
            pytest.param(
                _DDP, FORWARDED_SETUP, "super().setup_environment()", False, id="override-stops-forwarding-verbose"
            ),
            pytest.param(
                f"{_STRATEGIES}/strategy.py",
                "Setup any processes or distributed connections.",
                "Prepare the strategy environment.",
                True,
                id="base-docstring-edit-is-harmless",
            ),
            pytest.param(
                _DDP, "        self.setup_distributed()\n", "", False, id="override-deletes-existing-behavior"
            ),
            pytest.param(
                _DDP,
                f"        {FORWARDED_SETUP}\n",
                f"        {FORWARDED_SETUP}\n        super().setup(None, verbose=verbose)\n",
                False,
                id="override-calls-non-cooperative-full-setup",
            ),
        ],
    )
    def test_strategy_candidate_verdict(
        self, tmp_path: Path, strategy_contract: object, path: str, old: str, new: str, expected: bool
    ) -> None:
        """One edit to a complete FM-03 candidate flips the oracle only when it breaks cooperative forwarding.

        A missing forward, deleted existing behavior, or an extra non-cooperative setup call must be rejected, while a
        docstring-only change to the base method keeps the verdict unchanged.
        """
        target = tmp_path / path
        target.write_text(target.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")

        assert run_fix_multi_oracle(tmp_path, strategy_contract) is expected
