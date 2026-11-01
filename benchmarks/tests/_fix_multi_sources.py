"""Source rewrites that turn the frozen multi-file scaffold sources into complete candidates."""

from __future__ import annotations

FORWARDED_SETUP = "super().setup_environment(verbose=verbose)"
DRY_RUN_LOG = 'log.info(f"dry run: should_stop={should_stop}, reason={reason}")'
STOP_DECISION = "        trainer.should_stop = trainer.should_stop or should_stop\n"
CHECKPOINT_PROVENANCE_LOG = (
    "        if reason:\n"
    '            rank_zero_info(f"{reason}: saving checkpoint to {filepath}")\n'
    "        trainer.save_checkpoint(filepath, self.save_weights_only)\n"
)


def add_dry_run_parameter(source: str) -> str:
    """Return the FM-01 source with the dry-run parameter declared and passed by every internal caller.

    Example:
        >>> add_dry_run_parameter("self._run_early_stopping_check(trainer)")
        'self._run_early_stopping_check(trainer, dry_run=False)'
    """
    return source.replace(
        'def _run_early_stopping_check(self, trainer: "pl.Trainer") -> None:',
        'def _run_early_stopping_check(self, trainer: "pl.Trainer", dry_run: bool = False) -> None:',
    ).replace("self._run_early_stopping_check(trainer)", "self._run_early_stopping_check(trainer, dry_run=False)")


def complete_early_stopping_source(source: str) -> str:
    """Return the FM-01 source with explicit callers and an observe-only dry run branch.

    Example:
        >>> complete_early_stopping_source("self._run_early_stopping_check(trainer)")
        'self._run_early_stopping_check(trainer, dry_run=False)'
    """
    return add_dry_run_parameter(source).replace(
        STOP_DECISION,
        f"        if dry_run:\n            {DRY_RUN_LOG}\n            return\n{STOP_DECISION}",
    )


def complete_strategy_environment_source(source: str, *, is_base: bool) -> str:
    """Return one FM-03 source with cooperative verbose environment propagation.

    Example:
        >>> complete_strategy_environment_source("super().setup_environment()", is_base=False)
        'super().setup_environment(verbose=verbose)'
        >>> complete_strategy_environment_source("super().setup_environment()", is_base=True)
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
    return source.replace("super().setup_environment()", FORWARDED_SETUP, 1)


def complete_model_checkpoint_source(source: str) -> str:
    """Return the FM-02 source with exact persistence-boundary provenance labels.

    Example:
        >>> source = "; ".join(["self._save_checkpoint(trainer, filepath)"] * 2)
        >>> first, second = complete_model_checkpoint_source(source).split("; ")
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
        CHECKPOINT_PROVENANCE_LOG,
        1,
    )
    for reason in ("exception", "last", "none", "top_k"):
        source = source.replace(
            "self._save_checkpoint(trainer, filepath)",
            f"self._save_checkpoint(trainer, filepath, reason={reason!r})",
            1,
        )
    return source
