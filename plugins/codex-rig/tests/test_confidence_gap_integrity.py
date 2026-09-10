"""Regression checks for unambiguous confidence-gap closure provenance."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATHS = {
    "writer": PLUGIN_ROOT / "shared" / "write-result.py",
    "shared": PLUGIN_ROOT / "shared" / "validate-artifacts.py",
    "review": PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py",
}
VALIDATOR_CASES = [pytest.param(name, path, id=name) for name, path in VALIDATOR_PATHS.items()]


def _load_validator(name: str, path: Path) -> ModuleType:
    """Load one standalone validator by path without package installation."""
    specification = importlib.util.spec_from_file_location(f"codex_rig_{name}_gap_validator", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _validate(module: ModuleType, name: str, metadata: dict[str, object], gaps: list[str]) -> None:
    """Call one validator's intentionally equivalent confidence-closure contract."""
    if name == "shared":
        module._validate_confidence_gap_closures(metadata, gaps, "change-analysis")
    else:
        module.validate_confidence_gap_closures(
            metadata, gaps
        ) if name == "writer" else module._validate_confidence_gap_closures(metadata, gaps)


@pytest.mark.parametrize(
    ("gaps", "closures"),
    [
        pytest.param([" "], [], id="punctuation"),
        pytest.param(["Environment missing", "Environment missing"], [], id="environment-missing-environment-missing"),
        pytest.param(
            ["Environment missing"],
            [
                {"gap": "Environment missing", "status": "unresolved", "rationale": "First state."},
                {"gap": "Environment missing", "status": "unresolved", "rationale": "Second state."},
            ],
            id="environment-missing-gap-environment-missing-status-unresolved-rationale-first-state.-gap-env",
        ),
        pytest.param(
            ["Environment missing"],
            [{"gap": "Other", "status": "unresolved", "rationale": "Not declared."}],
            id="environment-missing-gap-other-status-unresolved-rationale-not-declared.",
        ),
        pytest.param(["Environment missing"], [], id="environment-missing-punctuation"),
    ],
)
@pytest.mark.parametrize(("name", "path"), VALIDATOR_CASES)
def test_every_validator_rejects_ambiguous_or_incomplete_confidence_closures(
    gaps: list[str], closures: list[dict[str, str]], name: str, path: Path
) -> None:
    """Prevent blank, duplicate, undeclared, or missing closure provenance in every writer path."""
    metadata: dict[str, object] = {"confidence_gap_closures": closures}
    with pytest.raises(SystemExit):
        _validate(_load_validator(name, path), name, metadata, gaps)


@pytest.mark.parametrize(
    "closure",
    [
        pytest.param({"gap": "Environment missing", "status": "closed", "evidence": "environment.log"}, id="closed"),
        pytest.param(
            {"gap": "Environment missing", "status": "unresolved", "rationale": "Host access is unavailable."},
            id="unresolved",
        ),
        pytest.param(
            {"gap": "Environment missing", "status": "deferred", "rationale": "The user deferred host access."},
            id="deferred",
        ),
    ],
)
@pytest.mark.parametrize(("name", "path"), VALIDATOR_CASES)
def test_every_validator_accepts_one_valid_closure_per_declared_gap(
    closure: dict[str, str], name: str, path: Path
) -> None:
    """Keep all three supported closure states usable when provenance is unambiguous."""
    metadata: dict[str, object] = {"confidence_gap_closures": [closure]}
    gaps = ["Environment missing"]
    _validate(_load_validator(name, path), name, metadata, gaps)
