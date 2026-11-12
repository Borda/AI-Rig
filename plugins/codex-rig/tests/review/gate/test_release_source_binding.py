"""Bind release receipt Git access to the repository recorded by executed gates."""

from pathlib import Path

import pytest
from _modules import load_module

VALIDATOR = load_module("release_source_binding", Path(__file__).resolve().parents[3] / "shared/validate-artifacts.py")


def test_release_gate_source_requires_repository():
    """An object ID alone cannot authorize reads from an artifact-selected repository."""
    with pytest.raises(SystemExit, match="release-gate-repository-missing"):
        VALIDATOR._validate_release_gate_source({"release_head": "a" * 40}, {"checks": []})


def test_release_gate_source_returns_recorded_repository(tmp_path):
    """Pass the executed gate repository to downstream release receipt validation."""
    assert (
        VALIDATOR._validate_release_gate_source(
            {"release_head": "a" * 40}, {"source": {"worktree": tmp_path.as_posix()}, "checks": []}
        )
        == tmp_path
    )
