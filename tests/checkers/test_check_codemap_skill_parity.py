"""Contract: ``check-codemap-skill-parity`` flags drift between Codemap's Claude and Codex skill trees.

The two runtimes ship one skill roster each. The hook fails when a roster misses or adds a skill, when the pinned
integration mode table differs between the runtimes or from the shared contract, or when a skill's NOT-for clause defers
to different (or no, or unknown) skills in the two runtimes. Each case builds a consistent synthetic tree under
``tmp_path`` and breaks exactly one fact, so the real plugin files are never mutated.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from _repo_checker_run import run_checker

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "check_codemap_skill_parity.py"

_SKILLS = ("scan-codebase", "query-code", "test-impact", "rename-refs", "integration", "debrief-coding")
_MODE_TABLE = "| Mode | Args | Mutation | Exit |\n| --- | --- | --- | --- |\n| `plan` | none | no | 0 |\n"
_CLAUDE = "plugins/codemap-py/claude-skills"
_CODEX = "plugins/codemap-py/codex-skills"
_CONTRACT = "plugins/codemap-py/shared/integration-contract.md"


def _skill_text(skill: str, sigil: str) -> str:
    """Return a minimal ``SKILL.md`` whose NOT-for clause defers to one other skill using ``sigil``."""
    target = "query-code" if skill == "scan-codebase" else "scan-codebase"
    table = _MODE_TABLE if skill == "integration" else ""
    return f"---\nname: {skill}\n---\n{table}\nNOT for: another job (use `{sigil}codemap-py:{target}`).\n"


@pytest.fixture(name="tree")
def _tree(tmp_path: Path) -> Path:
    """Build matching Claude and Codex rosters plus the shared contract holding exactly the canonical skills."""
    for runtime_dir, sigil in ((_CLAUDE, "/"), (_CODEX, "$")):
        for skill in _SKILLS:
            skill_file = tmp_path / runtime_dir / skill / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text(_skill_text(skill, sigil), encoding="utf-8", newline="\n")
    contract = tmp_path / _CONTRACT
    contract.parent.mkdir(parents=True)
    contract.write_text(f"# Integration contract\n\n{_MODE_TABLE}", encoding="utf-8", newline="\n")
    return tmp_path


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    """Replace the first ``old`` with ``new`` in ``root/rel``, failing loudly when ``old`` is absent."""
    path = root / rel
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{rel} does not contain {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the checker against ``root`` as the hook does."""
    return run_checker(_SCRIPT, "--root", str(root))


class TestMatchingTrees:
    """Trees whose facts agree pass, however the prose is worded."""

    @pytest.mark.parametrize(
        ("rel", "old", "new"),
        [
            pytest.param(
                _CLAUDE + "/query-code/SKILL.md",
                "NOT for: another job",
                "NOT for: some other job",
                id="reworded-clause-same-skill-claude",
            ),
            pytest.param(
                _CODEX + "/rename-refs/SKILL.md",
                "NOT for: another job (use `$codemap-py:scan-codebase`)",
                "NOT for: a wholly different task, see `$codemap-py:scan-codebase` first",
                id="reworded-clause-same-skill",
            ),
        ],
    )
    def test_consistent_trees_pass(self, tree: Path, rel: str, old: str, new: str) -> None:
        """Rewording a NOT-for clause that still defers to the same skill is not drift.

        Only which skill is named matters, so prose latitude between the runtimes stays allowed.
        """
        _edit(tree, rel, old, new)

        result = _run(tree)

        assert (result.returncode, result.stdout) == (0, "")


class TestRosterDrift:
    """Each runtime exposes exactly the six canonical skills."""

    @pytest.mark.parametrize("runtime_dir", [pytest.param(_CLAUDE, id="claude"), pytest.param(_CODEX, id="codex")])
    def test_missing_skill_is_reported(self, tree: Path, runtime_dir: str) -> None:
        """A runtime that lacks a canonical skill is named in the finding.

        Removing ``query-code`` from one runtime breaks the roster for that runtime and the cross-runtime match.
        """
        (tree / runtime_dir / "query-code" / "SKILL.md").unlink()

        result = _run(tree)

        assert result.returncode == 1
        assert "roster missing skill(s) ['query-code']" in result.stdout

    @pytest.mark.parametrize("runtime_dir", [pytest.param(_CLAUDE, id="claude"), pytest.param(_CODEX, id="codex")])
    def test_extra_skill_is_reported(self, tree: Path, runtime_dir: str) -> None:
        """An undocumented extra skill in one runtime is named in the finding.

        A copy of ``query-code`` under a new name keeps its NOT-for clause valid, so only the roster check can fail.
        """
        shutil.copytree(tree / runtime_dir / "query-code", tree / runtime_dir / "bogus-extra-skill")

        result = _run(tree)

        assert result.returncode == 1
        assert "undocumented extra skill(s) ['bogus-extra-skill']" in result.stdout


class TestModeTableDrift:
    """The pinned integration mode table is identical in both runtimes and in the shared contract."""

    @pytest.mark.parametrize(
        ("rel", "expected"),
        [
            pytest.param(
                _CLAUDE + "/integration/SKILL.md", "claude-skills/integration/SKILL.md:4", id="claude-stale-mode"
            ),
            pytest.param(
                _CODEX + "/integration/SKILL.md", "codex-skills/integration/SKILL.md:4", id="codex-stale-mode"
            ),
            pytest.param(_CONTRACT, "claude-skills/integration/SKILL.md:4", id="contract-stale-mode"),
        ],
    )
    def test_stale_mode_name_is_reported(self, tree: Path, rel: str, expected: str) -> None:
        """A retired mode name in a skill, or in the contract the skills must match, is flagged at the skill table.

        The table is compared verbatim, so a stale ``init`` row where ``plan`` belongs is exactly the drift guarded.
        """
        _edit(tree, rel, "`plan`", "`init`")

        result = _run(tree)

        assert result.returncode == 1
        assert expected in result.stdout

    def test_missing_table_is_reported(self, tree: Path) -> None:
        """A skill that lost its mode table entirely is reported rather than crashing the checker."""
        _edit(tree, _CODEX + "/integration/SKILL.md", "| Mode | Args | Mutation | Exit |", "| Name | Args |")

        result = _run(tree)

        assert result.returncode == 1
        assert "has no `| Mode ... | Exit |` table" in result.stdout


class TestNotForDrift:
    """NOT-for clauses defer to the same, known skills in both runtimes."""

    @pytest.mark.parametrize(
        ("rel", "old", "new", "expected"),
        [
            pytest.param(
                _CLAUDE + "/rename-refs/SKILL.md",
                "/codemap-py:scan-codebase",
                "/codemap-py:query-code",
                "NOT-for references differ between runtimes",
                id="contradictory-skill-reference",
            ),
            pytest.param(
                _CODEX + "/rename-refs/SKILL.md",
                "(use `$codemap-py:scan-codebase`)",
                "(no equivalent skill; edit `~/.claude/plugins/cache/foo` directly)",
                "no NOT-for/Skip-for clause names a skill",
                id="cache-path-masquerading-as-skill-ref",
            ),
            pytest.param(
                _CLAUDE + "/rename-refs/SKILL.md",
                "/codemap-py:scan-codebase",
                "/codemap-py:bogus-skill",
                "references unknown skill(s) ['bogus-skill']",
                id="unknown-skill",
            ),
        ],
    )
    def test_divergent_reference_is_reported(self, tree: Path, rel: str, old: str, new: str, expected: str) -> None:
        """A runtime that defers to another skill, to no skill, or to an unknown skill fails the check.

        Each case breaks one runtime's clause for ``rename-refs`` and the finding names the broken fact.
        """
        _edit(tree, rel, old, new)

        result = _run(tree)

        assert result.returncode == 1
        assert expected in result.stdout
