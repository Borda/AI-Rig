"""Acceptance contract for the agentic answer oracle.

Each test pins one failure mode the audit found in the oracle itself: a scored answer being marked wrong because the
oracle, not the model, resolved imports differently; free evidence credit from substring containment; and the emitted
prompt withholding the convention it scores against.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchmarks._bench_common import agentic_contracts


def _task(**overrides: Any) -> dict[str, Any]:
    """Build a production-importer task, replacing defaults with explicit overrides.

    >>> task = _task(primary_module="example.core")
    >>> task["primary_module"], task["answer_contract"]
    ('example.core', {'fields': ['production_importers']})
    """
    task = {
        "id": "BA-TEST",
        "primary_module": "pkg.trainer.trainer",
        "answer_contract": {"fields": ["production_importers"]},
    }
    task.update(overrides)
    return task


def _write(root: Path, relative: str, source: str) -> None:
    """Create missing parents and replace a synthetic module's UTF-8 source.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     root = Path(directory)
    ...     _write(root, "pkg/example.py", "VALUE = 3")
    ...     (root / "pkg/example.py").read_text(encoding="utf-8")
    'VALUE = 3'
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


@pytest.fixture(name="mixed_import_tree")
def _mixed_import_tree(tmp_path: Path) -> Path:
    """Source tree whose only importer uses one mixed ``from`` statement.

    ``utilities`` is a package holding a real ``parsing`` submodule, so the importer's statement resolves one alias
    concretely and one not at all. The module under test (``pkg.trainer.trainer``) is reached only through the package
    import that statement also carries.

    Example:
        >>> root = getfixture("mixed_import_tree")
        >>> (root / "pkg/utilities/parsing.py").is_file(), (root / "pkg/trainer/trainer.py").is_file()
        (True, True)
    """
    root = tmp_path / "src"
    for package in ("pkg", "pkg.trainer", "pkg.utilities"):
        _write(root, f"{package.replace('.', '/')}/__init__.py", "")
    _write(root, "pkg/trainer/trainer.py", "class Trainer:\n    pass\n")
    _write(root, "pkg/utilities/parsing.py", "def parse():\n    return None\n")
    _write(
        root,
        "pkg/consumer.py",
        "from pkg.utilities import GradClipAlgorithmType, parsing\nfrom pkg.trainer import trainer\n",
    )
    return root


def test_mixed_from_import_credits_package_and_submodule(mixed_import_tree: Path) -> None:
    """A partially resolving ``from`` import still credits its package.

    Before the fix a single concretely resolving alias suppressed package credit entirely, so the importer the task
    exists to find never appeared in the expected set and a correct answer was scored as a false positive.
    """
    oracle = agentic_contracts.build_oracle(_task(), mixed_import_tree)
    assert oracle.expected["production_importers"] == ("pkg.consumer",)


def test_package_import_credit_is_not_all_or_nothing(mixed_import_tree: Path) -> None:
    """The concrete submodule keeps its credit alongside the package."""
    oracle = agentic_contracts.build_oracle(
        _task(primary_module="pkg.utilities.parsing"),
        mixed_import_tree,
    )
    assert oracle.expected["production_importers"] == ("pkg.consumer",)


def test_correct_answer_scores_one_on_mixed_import_tree(mixed_import_tree: Path) -> None:
    """Naming the true importer is scored correct, not penalized."""
    oracle = agentic_contracts.build_oracle(_task(), mixed_import_tree)
    score = agentic_contracts.score_answer(oracle, {"production_importers": ["pkg.consumer"]})
    assert score.correct is True
    assert score.quality_score == pytest.approx(1.0)


def test_import_convention_is_disclosed_in_the_scored_prompt() -> None:
    """The oracle's import convention reaches the model it scores."""
    instruction = agentic_contracts.answer_format_instruction(_task())
    assert agentic_contracts.IMPORT_CONVENTION_INSTRUCTION in instruction
    assert "from a.b import c" in instruction
    assert "a.b.c" in instruction


def test_oracle_uses_the_shared_import_convention(mixed_import_tree: Path) -> None:
    """Agentic and MB/GR oracles resolve imports through one helper."""
    import ast

    from benchmarks._bench_common import python_source

    tree = ast.parse((mixed_import_tree / "pkg" / "consumer.py").read_text(encoding="utf-8"))
    names = {"pkg.utilities", "pkg.utilities.parsing", "pkg.trainer", "pkg.trainer.trainer"}
    shared = python_source.extract_import_targets(tree, package="pkg", keep=names, credit_submodules=True)
    assert agentic_contracts._import_targets(tree, "pkg.consumer", False, names) == shared


def _evidence_oracle() -> agentic_contracts.AgenticOracle:
    """Build an oracle expecting exactly one importer for raw-text evidence scoring.

    >>> oracle = _evidence_oracle()
    >>> oracle.fields, oracle.expected
    (('production_importers',), {'production_importers': ('pkg.core',)})
    """
    return agentic_contracts.AgenticOracle(
        task_id="BA-TEST",
        fields=("production_importers",),
        expected={"production_importers": ("pkg.core",)},
    )


@pytest.mark.parametrize(
    "text",
    ["inspected pkg.core_utils only", "inspected other.pkg.core.helpers only", "inspected mypkg.core only"],
)
def test_evidence_recall_rejects_substring_containment(text: str) -> None:
    """A longer dotted name no longer donates recall to a shorter one."""
    metrics = agentic_contracts.score_evidence_metrics(_evidence_oracle(), exposure_text=text, tool_calls=1)
    assert metrics.erec == pytest.approx(0.0)
    assert metrics.deff == pytest.approx(0.0)


@pytest.mark.parametrize(
    "text",
    [
        "pkg.core",
        "importers: pkg.core, pkg.other",
        "found 'pkg.core' in the graph",
        "src/pkg/core.py imports pkg.core",
        "the only importer is pkg.core.",
        "(pkg.core)",
    ],
)
def test_evidence_recall_credits_a_whole_name_mention(text: str) -> None:
    """Genuine whole-name mentions keep their credit."""
    metrics = agentic_contracts.score_evidence_metrics(_evidence_oracle(), exposure_text=text, tool_calls=1)
    assert metrics.erec == pytest.approx(1.0)


def test_report_recall_uses_the_same_whole_name_rule() -> None:
    """Exposure and report recall share one matching rule."""
    oracle = _evidence_oracle()
    metrics = agentic_contracts.score_evidence_metrics(
        oracle,
        exposure_text="pkg.core",
        report_text="pkg.core_utils",
        tool_calls=1,
    )
    assert metrics.erec == pytest.approx(1.0)
    assert metrics.rrec == pytest.approx(0.0)
