"""Pin how the phase-boundary blocks of the develop skills reach their two shared scripts.

Every phased skill refreshes its compaction contract through ``write_skill_contract.py``; the user's ``--keep`` text
reaches it through ``--keep-sentinel`` (the script reads the session sentinel itself), and the fix and review boundary
blocks print the compaction Boundary step through ``compaction_boundary.py``. These tests keep the retired inline forms
from coming back: a hand-assembled ``user-keep`` line, and the ``sed`` range plus resolver plus gate lines.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_CONTRACT_CALL = re.compile(r"bin/write_skill_contract\.py\"(?P<args>[^\n]*)")


def _skill_text(skill: str) -> str:
    return (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")


class TestKeepSentinel:
    """The user's ``--keep`` text is read by the script from ``dev-<skill>-keep-items``, never assembled in bash."""

    @pytest.mark.parametrize(
        ("skill", "keep_calls"),
        [
            pytest.param("debug", 1, id="debug"),
            pytest.param("feature", 2, id="feature"),
            pytest.param("fix", 1, id="fix"),
            pytest.param("refactor", 1, id="refactor"),
            pytest.param("review", 1, id="review"),
        ],
    )
    def test_boundary_calls_name_the_skill_sentinel(self, skill: str, keep_calls: int) -> None:
        """Each keep-carrying contract call leads with ``--keep-sentinel dev-<skill>-keep-items``.

        The flag must be the first argument: the script only takes it as a leading option pair, so a value that
        merely looks like a flag is never mistaken for one.
        """
        text = _skill_text(skill)
        leading = f'--keep-sentinel dev-{skill}-keep-items "develop:{skill}"'
        assert sum(call["args"].lstrip().startswith(leading) for call in _CONTRACT_CALL.finditer(text)) == keep_calls

    @pytest.mark.parametrize(
        "skill",
        [
            pytest.param("debug", id="debug"),
            pytest.param("feature", id="feature"),
            pytest.param("fix", id="fix"),
            pytest.param("refactor", id="refactor"),
            pytest.param("review", id="review"),
        ],
    )
    def test_no_inline_keep_assembly_remains(self, skill: str) -> None:
        """No block reads the keep sentinel or appends ``user-keep:`` itself any more."""
        text = _skill_text(skill)
        assert "_KEEP" not in text
        assert "user-keep" not in text


class TestCompactionBoundaryCall:
    """The fix and review boundary blocks call ``compaction_boundary.py`` instead of inlining its steps."""

    @pytest.mark.parametrize(
        ("skill", "calls"),
        [
            pytest.param("fix", 2, id="fix-two-boundaries"),
            pytest.param("review", 1, id="review-one-boundary"),
        ],
    )
    def test_boundary_blocks_call_the_script_with_the_shared_dir(self, skill: str, calls: int) -> None:
        """Each boundary block passes its resolved ``_DEV_SHARED`` directory to the propagated script."""
        call = 'bin/compaction_boundary.py" "$_DEV_SHARED"'
        assert _skill_text(skill).count(call) == calls

    @pytest.mark.parametrize(
        "skill",
        [
            pytest.param("debug", id="debug"),
            pytest.param("feature", id="feature"),
            pytest.param("fix", id="fix"),
            pytest.param("plan", id="plan"),
            pytest.param("refactor", id="refactor"),
            pytest.param("review", id="review"),
        ],
    )
    def test_no_inline_boundary_print_or_gate_remains(self, skill: str) -> None:
        """The ``sed`` Boundary-step range, the resolver lookup and the gate line are gone from every skill."""
        text = _skill_text(skill)
        assert "## Boundary step" not in text
        assert "compact_gate.py" not in text
        assert "_RS=" not in text
