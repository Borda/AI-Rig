"""Independent caller-completeness oracle tests for the multi-file executable stage."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from _launcher_capability import _pinned_frozen_checkout_is_available

SUITE_PATH = Path(__file__).resolve().parents[1] / "suites" / "tasks-fix-multi.json"
BENCHMARKS = Path(__file__).resolve().parents[1]
FROZEN_REPO = Path(os.environ.get("PL_REPO_PATH", str(BENCHMARKS.parent / ".sandbox" / "pytorch-lightning")))
FROZEN_REPO_COMMIT = "be98784a1a03581b7051a355ae1084fd352d7cea"
sys.path.insert(0, str(BENCHMARKS))

from _bench_common.edit_patch_contracts import build_fix_multi_contract, run_fix_multi_oracle  # noqa: E402


_requires_frozen_repo = pytest.mark.skipif(
    not _pinned_frozen_checkout_is_available(FROZEN_REPO, FROZEN_REPO_COMMIT),
    reason=f"pinned frozen benchmark checkout is unavailable at {FROZEN_REPO}",
)


def _contract(task_id: str) -> object:
    """Return one immutable multi-file contract from the canonical task bytes.

    Example:
        >>> _contract("not-a-real-task")
        Traceback (most recent call last):
            ...
        StopIteration
    """
    tasks = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    return build_fix_multi_contract(next(task for task in tasks if task["id"] == task_id))


def test_pinned_frozen_checkout_rejects_git_directory_without_head(tmp_path: Path) -> None:
    """A partial Git directory cannot admit source-dependent contract coverage."""
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)

    assert source.is_dir()
    assert _pinned_frozen_checkout_is_available(source, FROZEN_REPO_COMMIT) is False


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


def _complete_early_stopping_source(source: str) -> str:
    """Return the FM-01 source with explicit callers and an observe-only dry run branch.

    Example:
        >>> _complete_early_stopping_source("self._run_early_stopping_check(trainer)")
        'self._run_early_stopping_check(trainer, dry_run=False)'
    """
    return (
        source.replace(
            'def _run_early_stopping_check(self, trainer: "pl.Trainer") -> None:',
            'def _run_early_stopping_check(self, trainer: "pl.Trainer", dry_run: bool = False) -> None:',
        )
        .replace("self._run_early_stopping_check(trainer)", "self._run_early_stopping_check(trainer, dry_run=False)")
        .replace(
            "        trainer.should_stop = trainer.should_stop or should_stop\n",
            "        if dry_run:\n"
            '            log.info(f"dry run: should_stop={should_stop}, reason={reason}")\n'
            "            return\n"
            "        trainer.should_stop = trainer.should_stop or should_stop\n",
        )
    )


def _complete_early_stopping_else_source(source: str) -> str:
    """Return the FM-01 source with an equivalent observe-only if/else branch.

    Example:
        >>> _complete_early_stopping_else_source("self._run_early_stopping_check(trainer)")
        'self._run_early_stopping_check(trainer, dry_run=False)'
    """
    source = source.replace(
        'def _run_early_stopping_check(self, trainer: "pl.Trainer") -> None:',
        'def _run_early_stopping_check(self, trainer: "pl.Trainer", dry_run: bool = False) -> None:',
    ).replace("self._run_early_stopping_check(trainer)", "self._run_early_stopping_check(trainer, dry_run=False)")
    normal_path = (
        "        trainer.should_stop = trainer.should_stop or should_stop\n"
        "        if should_stop:\n"
        "            self.stopped_epoch = trainer.current_epoch\n"
        "            self.stopping_reason_message = reason\n"
        "        if reason and self.verbose:\n"
        "            self._log_info(trainer, reason, self.log_rank_zero_only)\n"
    )
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
    return source.replace(normal_path, dry_run_branch)


def _complete_early_stopping_negative_guard_source(source: str) -> str:
    """Return FM-01 source using an equivalent negative guard around mutations.

    Example:
        >>> _complete_early_stopping_negative_guard_source("self._run_early_stopping_check(trainer)")
        'self._run_early_stopping_check(trainer, dry_run=False)'
    """
    source = source.replace(
        'def _run_early_stopping_check(self, trainer: "pl.Trainer") -> None:',
        'def _run_early_stopping_check(self, trainer: "pl.Trainer", dry_run: bool = False) -> None:',
    ).replace("self._run_early_stopping_check(trainer)", "self._run_early_stopping_check(trainer, dry_run=False)")
    normal_path = (
        "        trainer.should_stop = trainer.should_stop or should_stop\n"
        "        if should_stop:\n"
        "            self.stopped_epoch = trainer.current_epoch\n"
        "            self.stopping_reason_message = reason\n"
        "        if reason and self.verbose:\n"
        "            self._log_info(trainer, reason, self.log_rank_zero_only)\n"
    )
    guarded_path = (
        "        if dry_run:\n"
        '            log.info(f"dry run: should_stop={should_stop}, reason={reason}")\n'
        "        if not dry_run:\n"
        "            trainer.should_stop = trainer.should_stop or should_stop\n"
        "            if should_stop:\n"
        "                self.stopped_epoch = trainer.current_epoch\n"
        "                self.stopping_reason_message = reason\n"
        "        if reason and self.verbose:\n"
        "            self._log_info(trainer, reason, self.log_rank_zero_only)\n"
    )
    return source.replace(normal_path, guarded_path)


def _complete_strategy_environment_source(source: str, *, is_base: bool) -> str:
    """Return one FM-03 source with cooperative verbose environment propagation.

    Example:
        >>> _complete_strategy_environment_source("super().setup_environment()", is_base=False)
        'super().setup_environment(verbose=verbose)'
        >>> _complete_strategy_environment_source("super().setup_environment()", is_base=True)
        'super().setup_environment()'
    """
    source = source.replace(
        "def setup_environment(self) -> None:",
        "def setup_environment(self, verbose: bool = False) -> None:",
        1,
    )
    if is_base:
        return source.replace(
            "        assert self.accelerator is not None\n",
            "        if verbose:\n"
            '            log.debug("setting up strategy environment")\n'
            "        assert self.accelerator is not None\n",
            1,
        )
    return source.replace(
        "super().setup_environment()",
        "super().setup_environment(verbose=verbose)",
        1,
    )


def _complete_model_checkpoint_source(source: str) -> str:
    """Return the FM-02 source with exact persistence-boundary provenance labels.

    Example:
        >>> source = "; ".join(["self._save_checkpoint(trainer, filepath)"] * 2)
        >>> first, second = _complete_model_checkpoint_source(source).split("; ")
        >>> first
        "self._save_checkpoint(trainer, filepath, reason='exception')"
        >>> second
        "self._save_checkpoint(trainer, filepath, reason='last')"
    """
    source = source.replace(
        'def _save_checkpoint(self, trainer: "pl.Trainer", filepath: str) -> None:',
        'def _save_checkpoint(self, trainer: "pl.Trainer", filepath: str, reason: str = "") -> None:',
    ).replace(
        "        trainer.save_checkpoint(filepath, self.save_weights_only)\n",
        "        if reason:\n"
        '            rank_zero_info(f"{reason}: saving checkpoint to {filepath}")\n'
        "        trainer.save_checkpoint(filepath, self.save_weights_only)\n",
        1,
    )
    for reason in ("exception", "last", "none", "top_k"):
        source = source.replace(
            "self._save_checkpoint(trainer, filepath)",
            f"self._save_checkpoint(trainer, filepath, reason={reason!r})",
            1,
        )
    return source


@_requires_frozen_repo
@pytest.mark.parametrize("task_id", ("FM-01", "FM-02", "FM-03"))
def test_frozen_baseline_fails_every_complete_caller_oracle(task_id: str) -> None:
    """Each task begins from a baseline that cannot accidentally satisfy its new contract."""
    assert run_fix_multi_oracle(FROZEN_REPO, _contract(task_id)) is False


def test_strategy_contract_covers_every_cooperative_environment_override() -> None:
    """The independent oracle covers the base plus every production cooperative override."""
    contract = _contract("FM-03")

    assert len(contract.expected_paths) == 6
    assert contract.expected_paths[-1] == "src/lightning/pytorch/strategies/xla.py"
    assert "src/lightning/pytorch/strategies/single_xla.py" not in contract.expected_paths


def test_fix_multi_prompts_preserve_discovery_as_the_measured_work() -> None:
    """Prevent task text from giving every arm the caller and override answer surface."""
    tasks = {task["id"]: task for task in json.loads(SUITE_PATH.read_text(encoding="utf-8"))}

    for leaked_caller in (
        "on_exception",
        "_save_last_checkpoint",
        "_save_none_monitor_checkpoint",
        "_update_best_and_save",
    ):
        assert leaked_caller not in tasks["FM-02"]["prompt"]
    assert "all four" not in tasks["FM-02"]["prompt"].lower()

    for leaked_class in (
        "DDPStrategy",
        "FSDPStrategy",
        "DeepSpeedStrategy",
        "ModelParallelStrategy",
        "SingleXLAStrategy",
        "XLAStrategy",
    ):
        assert leaked_class not in tasks["FM-03"]["prompt"]
    assert "use codemap rdeps" not in tasks["FM-03"]["prompt"].lower()


@_requires_frozen_repo
def test_early_stopping_contract_requires_explicit_callers_and_observable_decision_log(tmp_path: Path) -> None:
    """FM-01 rejects omitted callers and dry-run logs that do not describe the computed decision."""
    contract = _contract("FM-01")
    _copy_contract_sources(FROZEN_REPO, tmp_path, contract)
    target = tmp_path / contract.expected_paths[0]
    complete = _complete_early_stopping_source(target.read_text(encoding="utf-8"))
    target.write_text(complete, encoding="utf-8")

    assert run_fix_multi_oracle(tmp_path, contract) is True

    target.write_text(
        complete.replace(
            'log.info(f"dry run: should_stop={should_stop}, reason={reason}")',
            'message = f"dry run: should_stop={should_stop}, reason={reason}"\n            log.info(message)',
        ),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is True

    target.write_text(
        complete.replace(
            'log.info(f"dry run: should_stop={should_stop}, reason={reason}")',
            'log_message = f"dry run: should_stop={should_stop}, reason={reason}"\n'
            "            self._log_info(trainer, log_message, self.log_rank_zero_only)",
        ),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is True

    target.write_text(complete.replace("trainer, dry_run=False)", "trainer)", 1), encoding="utf-8")
    assert run_fix_multi_oracle(tmp_path, contract) is False

    target.write_text(
        complete.replace('log.info(f"dry run: should_stop={should_stop}, reason={reason}")', 'log.info("dry run")'),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is False

    target.write_text(
        complete.replace(
            'log.info(f"dry run: should_stop={should_stop}, reason={reason}")',
            'if reason:\n                log.info(f"dry run: should_stop={should_stop}, reason={reason}")',
        ),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is False

    target.write_text(
        complete.replace(
            'log.info(f"dry run: should_stop={should_stop}, reason={reason}")',
            'if self.verbose:\n                log.info(f"dry run: should_stop={should_stop}, reason={reason}")',
        ),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is False

    target.write_text(
        complete.replace(
            'log.info(f"dry run: should_stop={should_stop}, reason={reason}")',
            'log.info("dry run: should_stop={should_stop}, reason={reason}")',
        ),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is False

    target.write_text(
        complete.replace(
            "        if dry_run:\n",
            "        trainer.should_stop = trainer.should_stop or should_stop\n"
            "        self.stopped_epoch = trainer.current_epoch\n"
            "        if dry_run:\n",
            1,
        ),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is False


@_requires_frozen_repo
def test_early_stopping_contract_accepts_semantically_equivalent_else_branch(tmp_path: Path) -> None:
    """FM-01 must not prefer early-return syntax over an observe-only if/else implementation."""
    contract = _contract("FM-01")
    _copy_contract_sources(FROZEN_REPO, tmp_path, contract)
    target = tmp_path / contract.expected_paths[0]
    complete = _complete_early_stopping_else_source(target.read_text(encoding="utf-8"))
    target.write_text(complete, encoding="utf-8")

    assert run_fix_multi_oracle(tmp_path, contract) is True

    target.write_text(
        complete.replace("            trainer.should_stop = trainer.should_stop or should_stop\n", "", 1),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is False


@_requires_frozen_repo
def test_early_stopping_contract_accepts_semantically_equivalent_negative_guard(tmp_path: Path) -> None:
    """FM-01 accepts a negative guard when all persistent mutations stay inside it."""
    contract = _contract("FM-01")
    _copy_contract_sources(FROZEN_REPO, tmp_path, contract)
    target = tmp_path / contract.expected_paths[0]
    complete = _complete_early_stopping_negative_guard_source(target.read_text(encoding="utf-8"))
    target.write_text(complete, encoding="utf-8")

    assert run_fix_multi_oracle(tmp_path, contract) is True

    target.write_text(
        complete.replace(
            "        if not dry_run:\n            trainer.should_stop = trainer.should_stop or should_stop\n",
            "        trainer.should_stop = trainer.should_stop or should_stop\n        if not dry_run:\n",
            1,
        ),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is False


@_requires_frozen_repo
def test_strategy_contract_requires_every_environment_override_to_forward_verbose(tmp_path: Path) -> None:
    """FM-03 accepts cooperative propagation and rejects a missing forward."""
    contract = _contract("FM-03")
    _copy_contract_sources(FROZEN_REPO, tmp_path, contract)
    for relative_path in contract.expected_paths:
        target = tmp_path / relative_path
        target.write_text(
            _complete_strategy_environment_source(
                target.read_text(encoding="utf-8"),
                is_base=relative_path.endswith("strategies/strategy.py"),
            ),
            encoding="utf-8",
        )

    assert run_fix_multi_oracle(tmp_path, contract) is True

    incomplete = tmp_path / contract.expected_paths[1]
    incomplete.write_text(
        incomplete.read_text(encoding="utf-8").replace(
            "super().setup_environment(verbose=verbose)", "super().setup_environment()", 1
        ),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is False


@_requires_frozen_repo
def test_strategy_contract_ignores_harmless_method_docstring_changes(tmp_path: Path) -> None:
    """FM-03 preserves behavior when only the base environment-method docstring changes."""
    contract = _contract("FM-03")
    _copy_contract_sources(FROZEN_REPO, tmp_path, contract)
    for relative_path in contract.expected_paths:
        target = tmp_path / relative_path
        target.write_text(
            _complete_strategy_environment_source(
                target.read_text(encoding="utf-8"),
                is_base=relative_path.endswith("strategies/strategy.py"),
            ),
            encoding="utf-8",
        )

    base = tmp_path / "src/lightning/pytorch/strategies/strategy.py"
    base.write_text(
        base.read_text(encoding="utf-8").replace(
            "Setup any processes or distributed connections.", "Prepare the strategy environment.", 1
        ),
        encoding="utf-8",
    )

    assert run_fix_multi_oracle(tmp_path, contract) is True


@_requires_frozen_repo
def test_strategy_contract_rejects_deleted_behavior_and_full_setup_calls(tmp_path: Path) -> None:
    """FM-03 cannot pass by deleting existing behavior or invoking the non-cooperative full setup."""
    contract = _contract("FM-03")
    _copy_contract_sources(FROZEN_REPO, tmp_path, contract)
    for relative_path in contract.expected_paths:
        target = tmp_path / relative_path
        target.write_text(
            _complete_strategy_environment_source(
                target.read_text(encoding="utf-8"),
                is_base=relative_path.endswith("strategies/strategy.py"),
            ),
            encoding="utf-8",
        )

    ddp = tmp_path / "src/lightning/pytorch/strategies/ddp.py"
    complete = ddp.read_text(encoding="utf-8")
    ddp.write_text(complete.replace("        self.setup_distributed()\n", "", 1), encoding="utf-8")
    assert run_fix_multi_oracle(tmp_path, contract) is False

    ddp.write_text(
        complete.replace(
            "        super().setup_environment(verbose=verbose)\n",
            "        super().setup_environment(verbose=verbose)\n        super().setup(None, verbose=verbose)\n",
            1,
        ),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is False


@_requires_frozen_repo
def test_model_checkpoint_contract_requires_exact_reasons_and_a_pre_save_provenance_log(tmp_path: Path) -> None:
    """FM-02 rejects generic labels and logging after the persistence boundary."""
    contract = _contract("FM-02")
    _copy_contract_sources(FROZEN_REPO, tmp_path, contract)
    target = tmp_path / contract.expected_paths[0]
    complete = _complete_model_checkpoint_source(target.read_text(encoding="utf-8"))
    target.write_text(complete, encoding="utf-8")

    assert run_fix_multi_oracle(tmp_path, contract) is True

    target.write_text(complete.replace("reason='top_k'", "reason='best'"), encoding="utf-8")
    assert run_fix_multi_oracle(tmp_path, contract) is False

    target.write_text(
        complete.replace(
            "        if reason:\n"
            '            rank_zero_info(f"{reason}: saving checkpoint to {filepath}")\n'
            "        trainer.save_checkpoint(filepath, self.save_weights_only)\n",
            "        trainer.save_checkpoint(filepath, self.save_weights_only)\n"
            "        if reason:\n"
            '            rank_zero_info(f"{reason}: saving checkpoint to {filepath}")\n',
        ),
        encoding="utf-8",
    )
    assert run_fix_multi_oracle(tmp_path, contract) is False
