"""Contract: the ``check-policy-parity`` hook flags hand-synced policy text that drifted between its copies.

The hook holds restated paragraphs and rule sets identical across the files that carry them: reviewer attribution, the
rating legend, the four quality-gates rule sets, the local Git grant paragraph, the overlay routing scan and the
vitality scoring rules. It runs on the repository and, here, on a synthetic tree where each group is broken in turn.
"""

from __future__ import annotations

import runpy
import subprocess
from pathlib import Path

import pytest
from _repo_checker_run import run_checker

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "check_policy_parity.py"
_NS = runpy.run_path(str(_SCRIPT))

_PLUGINS = _NS["QUALITY_PLUGINS"]
_SATELLITES = _NS["QUALITY_SATELLITES"]
_SECTIONS = ("Alpha", "Beta", "Gamma", "Delta", "Epsilon")
_PARAGRAPH = f"{_NS['GRANT_START']}: inspect and edit locally,\n{_NS['GRANT_END']}"
_SCAN = "`git grep -n -I -i -E -e 'index[-_]?url' -e 'find[-_]links' -- .`"
_VITALITY_RULES = (
    "**Confidence formula**: conf = 1.0 minus degraders",
    "**Band rule**: worst band wins",
    "**Copy rule**: copy bands",
)
_RULE_PATH = "plugins/{}/rules/quality-gates.md"
_DELTA_PATH = "plugins/{}/rules/quality-gates-delta.md"


def _rule_text(plugin: str, sections: tuple[str, ...], routing: str) -> str:
    """Return a minimal ``quality-gates.md`` stating two named rules in every section plus the routing section."""
    parts = [f"## {name}\n\n- **{name} one**: text\n- **{name} two**: text\n" for name in sections]
    parts.append(f"## {routing}\n\n- **Routing note**: text\n")
    if plugin == "cc_foundry":
        parts.append(f"## Proportional Execution\n\n{_NS['PROPORTIONAL_TRIGGER']}\n")
    else:
        parts.append("## Proportional Execution\n\n- **Plan first**: text\n")
    return "\n".join(parts)


def _files(sections: tuple[str, ...], routing: str) -> dict[str, str]:
    """Return every file the checker reads, as a repository-relative path mapped to clean synthetic content."""
    files = {
        ".codex/config.toml": f'extra_policy = """\nintro\n{_PARAGRAPH}\n"""\n',
        ".codex/README.md": f"readme\n{_PARAGRAPH}\n",
        "plugins/codex-rig/README.md": f"readme\n{_PARAGRAPH}\n",
        "plugins/codex-rig/shared/native-skill-contract.md": f"contract\n{_PARAGRAPH}\n- {_SCAN}\n",
        "plugins/cc_oss/skills/review/templates/agent-prompts.md": f"1. {_SCAN}\n",
        "plugins/cc_oss/skills/review/templates/review-report.md": f"{_NS['HISTORICAL_LEGEND']}\n",
        f"plugins/cc_foundry/rules/_full/{_NS['QUALITY_RULE']}": "## Proportional Execution\n\n- **Plan first**: text\n",
    }
    for plugin in _NS["REVIEW_PLUGINS"]:
        files[f"plugins/{plugin}/skills/review/SKILL.md"] = (
            f"{_NS['SKILL_MARKER']}\n> - role names only\n> - item two\n\n{_NS['CURRENT_LEGEND']}\n"
        )
        files[f"plugins/{plugin}/skills/review/templates/consolidator-prompt.md"] = (
            f"{_NS['CONSOLIDATOR_MARKER']}\n- role names only\n- item two\n"
        )
    for relative in _NS["VITALITY_FILES"]:
        files[relative] = "intro\n- " + "\n".join(_VITALITY_RULES) + "\n"
    for plugin in _PLUGINS:
        files[_RULE_PATH.format(plugin)] = _rule_text(plugin, sections, routing)
    for plugin in _SATELLITES:
        files[_DELTA_PATH.format(plugin)] = f"## {routing}\n\n- **Routing note**: text\n"
    return files


def _write_tree(root: Path, sections: tuple[str, ...] = _SECTIONS, routing: str = "Output Routing") -> None:
    """Write the synthetic repository under ``root`` with the given rule sections and routing section heading."""
    for relative, text in _files(sections, routing).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def _run(root: Path | None) -> subprocess.CompletedProcess[str]:
    """Run the checker command line as the hook does, against ``root`` or the repository when ``None``."""
    arguments = [] if root is None else ["--root", str(root)]
    return run_checker(_SCRIPT, *arguments, timeout=60)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Return a synthetic repository in which every parity group is clean."""
    _write_tree(tmp_path)
    return tmp_path


def _edit(root: Path, relative: str, old: str, new: str) -> None:
    """Replace ``old`` with ``new`` in ``relative`` under ``root``, requiring that ``old`` is there."""
    path = root / relative
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{relative} lacks {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


class TestCleanTrees:
    """The hook starts clean on the repository and on a consistent synthetic tree."""

    def test_repository_passes(self) -> None:
        """Every restated policy paragraph in the repository already reads the same, so the hook starts clean.

        The commit gate would otherwise reject the first commit that touches any file for drift nobody introduced.
        """
        result = _run(None)

        assert (result.returncode, result.stdout) == (0, "")

    def test_consistent_synthetic_tree_passes(self, tree: Path) -> None:
        """A tree with all groups consistent exits zero, so the violation cases below fail for their edit alone.

        Without this baseline a violation test could pass because the synthetic tree itself was malformed.
        """
        result = _run(tree)

        assert (result.returncode, result.stdout) == (0, "")


class TestFlagsDrift:
    """Each group reports a violation naming the drifted file and the group when one copy departs from the others."""

    @pytest.mark.parametrize(
        ("relative", "old", "new", "expected"),
        [
            pytest.param(
                "plugins/cc_develop/skills/review/SKILL.md",
                "item two",
                "item changed",
                "[review attribution (skill)]",
                id="attribution-skill-bullet",
            ),
            pytest.param(
                "plugins/cc_oss/skills/review/templates/consolidator-prompt.md",
                "item two",
                "item changed",
                "[review attribution (consolidator)]",
                id="attribution-consolidator-bullet",
            ),
            pytest.param(
                "plugins/cc_oss/skills/review/SKILL.md",
                _NS["SKILL_MARKER"],
                "> Something else:",
                "expected exactly one line starting",
                id="attribution-marker-missing",
            ),
            pytest.param(
                "plugins/cc_develop/skills/review/SKILL.md",
                _NS["CURRENT_LEGEND"],
                "Legend: 1 = Approve · 5 = Block.",
                "[rating legend] unknown wording",
                id="legend-unknown-wording",
            ),
            pytest.param(
                "plugins/cc_oss/skills/review/templates/review-report.md",
                _NS["HISTORICAL_LEGEND"],
                "no legend",
                "the historical wording is stated nowhere",
                id="legend-historical-gone",
            ),
            pytest.param(
                ".codex/README.md",
                "inspect and edit locally",
                "inspect only",
                "[local Git grant-scope paragraph]",
                id="grant-readme-reworded",
            ),
            pytest.param(
                "plugins/codex-rig/shared/native-skill-contract.md",
                "inspect and edit locally",
                "inspect and push",
                "[local Git grant-scope paragraph]",
                id="grant-contract-reworded",
            ),
            pytest.param(
                "plugins/codex-rig/shared/native-skill-contract.md",
                "-e 'find[-_]links'",
                "-e 'extra[-_]index'",
                "[overlay routing scan]",
                id="routing-pattern-differs",
            ),
            pytest.param(
                "plugins/codex-rig/shared/native-skill-contract.md",
                _SCAN,
                "no scan here",
                "no routing content-scan",
                id="routing-command-missing",
            ),
            pytest.param(
                "plugins/cc_oss/agents/repo-warden.md",
                "conf = 1.0 minus degraders",
                "conf = 1.0 plus degraders",
                "[vitality rule: Confidence formula]",
                id="vitality-confidence-formula",
            ),
            pytest.param(
                "plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md",
                "**Band rule**: worst band wins",
                "**Band rule**: worst band wins\n**Band rule**: restated",
                "stated on exactly one line, found 2",
                id="vitality-rule-stated-twice",
            ),
            pytest.param(
                _RULE_PATH.format("cc_oss"),
                "- **Alpha two**: text",
                "- **Alpha two**: text\n- **Alpha extra**: text",
                "states 'alpha extra' only in ['cc_oss']",
                id="quality-rule-added-to-one-copy",
            ),
            pytest.param(
                _RULE_PATH.format("cc_foundry"),
                "- **Beta one**: text\n",
                "",
                "states 'beta one' only in ['cc_develop', 'cc_oss', 'cc_research']",
                id="quality-rule-dropped-from-one-copy",
            ),
            pytest.param(
                f"plugins/cc_foundry/rules/_full/{_NS['QUALITY_RULE']}",
                "- **Plan first**: text",
                "- **Plan first**: text\n- **Plan extra**: text",
                "states 'plan extra' only in ['cc_foundry']",
                id="quality-bound-proportional-detail-adds-rule",
            ),
            pytest.param(
                _DELTA_PATH.format("cc_research"),
                "- **Routing note**: text",
                "- **Routing note**: text\n- **Delta only**: text",
                "states 'delta only' absent from quality-gates.md",
                id="quality-delta-adds-rule",
            ),
        ],
    )
    def test_flags_drifted_copy(self, tree: Path, relative: str, old: str, new: str, expected: str) -> None:
        """One edited copy makes the hook fail with a line naming the group, so the drift is fixed where it began.

        Each case edits a single file of a clean synthetic tree. The report must carry the group tag or the missing text
        so a reader knows which restated policy to resync.
        """
        _edit(tree, relative, old, new)

        result = _run(tree)

        assert (result.returncode, expected in result.stdout) == (1, True), result.stdout

    def test_names_the_drifted_file_and_line(self, tree: Path) -> None:
        """A drifted attribution block is reported as ``path:line`` of the copy that differs from the first file.

        The output contract is one ``path[:line]: message`` line per violation, which editors and the commit log turn
        into a jump target.
        """
        _edit(tree, "plugins/cc_develop/skills/review/SKILL.md", "item two", "item changed")

        result = _run(tree)

        assert result.stdout.startswith("plugins/cc_develop/skills/review/SKILL.md:1: ")


class TestFlagsMissingInput:
    """A group that cannot read one of its files reports the file instead of passing vacuously."""

    @pytest.mark.parametrize(
        "relative",
        [
            pytest.param(".codex/README.md", id="grant-copy"),
            pytest.param(_RULE_PATH.format("cc_develop"), id="quality-rule"),
            pytest.param(_DELTA_PATH.format("cc_oss"), id="quality-delta"),
            pytest.param("plugins/cc_oss/agents/repo-warden.md", id="vitality-file"),
        ],
    )
    def test_flags_missing_file(self, tree: Path, relative: str) -> None:
        """Deleting a compared file fails the hook with ``file is missing`` for that path.

        A copy that silently disappears would otherwise shrink the comparison to the files left and stay green.
        """
        (tree / relative).unlink()

        result = _run(tree)

        assert (result.returncode, f"{relative}: " in result.stdout, "file is missing" in result.stdout) == (
            1,
            True,
            True,
        )

    def test_flags_comparison_gone_vacuous(self, tmp_path: Path) -> None:
        """Fewer than five shared quality-gates sections means heading detection broke, which must not pass quietly.

        With every shared section dropped the per-section comparison has nothing to compare and would otherwise exit
        zero.
        """
        _write_tree(tmp_path, sections=("Alpha", "Beta"))

        result = _run(tmp_path)

        assert (result.returncode, "heading detection regressed" in result.stdout) == (1, True)

    def test_flags_stale_output_routing_exemption(self, tmp_path: Path) -> None:
        """An exempt section that no copy shares any more is reported so the exemption is dropped, not forgotten.

        A stale exemption would keep silencing a section that was renamed, disabling the guard for it.
        """
        _write_tree(tmp_path, routing="Routing Renamed")

        result = _run(tmp_path)

        assert (result.returncode, "exempt section 'output routing'" in result.stdout) == (1, True)
