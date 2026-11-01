"""Regression checks for unambiguous confidence-gap closure provenance in every result-writing validator."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
#: Validator path, entry-point name and extra positional arguments for the three equivalent closure contracts.
VALIDATORS = {
    "writer": (PLUGIN_ROOT / "shared" / "write-result.py", "validate_confidence_gap_closures", ()),
    "shared": (PLUGIN_ROOT / "shared" / "validate-artifacts.py", "_validate_confidence_gap_closures", ("assess",)),
    "review": (
        PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py",
        "_validate_confidence_gap_closures",
        (),
    ),
}
VALIDATOR_CASES = [pytest.param(path, name, extra, id=key) for key, (path, name, extra) in VALIDATORS.items()]
GAP = "Environment missing"


def _validate(
    path: Path, entry_point: str, extra: tuple[str, ...], metadata: dict[str, object], gaps: list[str]
) -> None:
    """Load one standalone validator by path and call its confidence-closure entry point."""
    specification = importlib.util.spec_from_file_location(f"codex_rig_gap_validator_{path.stem}", path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    getattr(module, entry_point)(metadata, gaps, *extra)


@pytest.mark.parametrize(
    ("gaps", "closures"),
    [
        pytest.param([" "], [], id="blank-gap"),
        pytest.param([GAP, GAP], [], id="duplicate-gap"),
        pytest.param(
            [GAP],
            [
                {"gap": GAP, "status": "unresolved", "rationale": "First state."},
                {"gap": GAP, "status": "unresolved", "rationale": "Second state."},
            ],
            id="duplicate-closure",
        ),
        pytest.param(
            [GAP], [{"gap": "Other", "status": "unresolved", "rationale": "Not declared."}], id="undeclared-closure"
        ),
        pytest.param([GAP], [], id="missing-closure"),
    ],
)
@pytest.mark.parametrize(("path", "entry_point", "extra"), VALIDATOR_CASES)
def test_rejects_ambiguous_or_incomplete_closures(
    gaps: list[str], closures: list[dict[str, str]], path: Path, entry_point: str, extra: tuple[str, ...]
) -> None:
    """Blank, duplicate, undeclared, or missing closure provenance is rejected by every writer path.

    Each of the three validators ships as a standalone script, so the same five malformed gap/closure pairings run
    against all of them; one validator drifting from the shared contract would let ambiguous provenance through.
    """
    metadata: dict[str, object] = {"confidence_gap_closures": closures}

    with pytest.raises(SystemExit):
        _validate(path, entry_point, extra, metadata, gaps)


@pytest.mark.parametrize(
    "closure",
    [
        pytest.param({"gap": GAP, "status": "closed", "evidence": "environment.log"}, id="closed"),
        pytest.param({"gap": GAP, "status": "unresolved", "rationale": "Host access is unavailable."}, id="unresolved"),
        pytest.param({"gap": GAP, "status": "deferred", "rationale": "The user deferred host access."}, id="deferred"),
    ],
)
@pytest.mark.parametrize(("path", "entry_point", "extra"), VALIDATOR_CASES)
def test_accepts_one_valid_closure_per_declared_gap(
    closure: dict[str, str], path: Path, entry_point: str, extra: tuple[str, ...]
) -> None:
    """All three supported closure states stay usable when provenance is unambiguous.

    One declared gap with exactly one closure that is closed, unresolved, or deferred must pass every validator; a
    stricter validator would block legitimate results while a looser one would hide the ambiguity cases above.
    """
    metadata: dict[str, object] = {"confidence_gap_closures": [closure]}

    _validate(path, entry_point, extra, metadata, [GAP])
