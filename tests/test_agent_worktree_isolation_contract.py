"""Keep worktree isolation a per-spawn decision, never an agent-wide default.

``foundry:sw-engineer`` once declared ``isolation: worktree`` in its frontmatter. Claude Code then gave *every* spawn of
that agent its own temporary git worktree, read-only reviewers included. An isolated agent cannot write into the
session worktree or the main checkout, so a ``/oss:review --worktree`` reviewer wrote its findings under its own
worktree's gitignored ``.temp/`` instead. Gitignored files are not tracked changes, so the harness auto-removed that
worktree when the agent finished and five of eight findings were lost. Callers that expected the agent's edits in their
own tree (merge-conflict resolution, agent scaffolding, TDD implementation loops) were broken the same way, silently.

Two rules keep that from coming back:

1. No Claude agent definition under ``plugins/*/agents/`` declares an ``isolation`` frontmatter key, so isolation is
   always chosen by the caller of one specific spawn.
2. Every documented ``Agent(...)`` call that passes worktree isolation is on the reviewed allow list below. Adding an
   isolated spawn site is then a deliberate decision: its caller must transplant the agent's changes back and must
   never hand the agent a deliverable path inside a checkout.

Removing agent-wide isolation put the parallel hypothesis investigators of ``/develop:debug`` and ``/develop:fix`` into
the caller's tree together. A third rule keeps them from corrupting it: every non-isolated parallel ``sw-engineer``
investigator spawn site carries the read-only clause (no tracked-file edits, no ``git stash``/``checkout``/``reset``/
``restore``, probes in a ``mktemp -d`` copy or the run directory).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

#: Reviewed spawn sites allowed to pass worktree isolation, as (repo-relative file, ``subagent_type`` value) pairs.
#: Each one is a parallel or speculative writer whose orchestrator transplants the result back into its own tree.
ALLOWED_ISOLATED_SPAWNS: frozenset[tuple[str, str]] = frozenset(
    {
        # Phase 2 specialists commit in their own worktrees; Phase 3 cherry-picks the commits onto the PR branch.
        ("plugins/cc_oss/skills/resolve/modes/action-item-dispatch.md", "<specialist>"),
        # Parallel bin/ extraction clusters may share a source file and revert with `git checkout`; the orchestrator
        # transplants each worktree diff with `git apply`.
        ("plugins/cc_foundry/skills/distill/modes/executables.md", "foundry:sw-engineer"),
    }
)

#: The agent whose frontmatter isolation caused the incident; the frontmatter scan must always include it.
SW_ENGINEER_AGENT = PLUGINS_DIR / "cc_foundry" / "agents" / "sw-engineer.md"

_AGENT_CALL = re.compile(r"Agent\((?P<args>.*)")
_ISOLATION_WORKTREE = re.compile(r"""\bisolation\s*[=:]\s*["']?worktree\b""")
_SUBAGENT_TYPE = re.compile(r"""\bsubagent_type\s*[=:]\s*["']?(?P<value>[^"',)\s]+)""")
_FRONTMATTER_ISOLATION = re.compile(r"^isolation\s*:", re.MULTILINE)


def _isolated_spawns(text: str) -> set[str]:
    """Return the ``subagent_type`` of every ``Agent(...)`` line in ``text`` that passes worktree isolation.

    Only the remainder of a line after ``Agent(`` is inspected, so prose that merely mentions the isolation argument
    is not a spawn site.

    Args:
        text: Markdown file contents.

    Returns:
        One ``subagent_type`` value per isolated spawn line; ``"<unknown>"`` when the line names no subagent type.

    Examples:
        >>> sorted(_isolated_spawns('Agent(subagent_type="foundry:sw-engineer", isolation="worktree", prompt="x")'))
        ['foundry:sw-engineer']
        >>> sorted(_isolated_spawns('Agent(subagent_type="<specialist>", isolation: "worktree")'))
        ['<specialist>']
        >>> _isolated_spawns('Agent(subagent_type="foundry:curator", prompt="review")')
        set()
        >>> _isolated_spawns('spawn with `isolation="worktree"` only for parallel writers')
        set()
    """
    found: set[str] = set()
    for line in text.splitlines():
        call = _AGENT_CALL.search(line)
        if not call or not _ISOLATION_WORKTREE.search(call.group("args")):
            continue
        subagent = _SUBAGENT_TYPE.search(call.group("args"))
        found.add(subagent.group("value") if subagent else "<unknown>")
    return found


def _frontmatter(text: str) -> str:
    """Return the YAML frontmatter block of a Markdown file, or an empty string when it has none.

    Args:
        text: Markdown file contents.

    Returns:
        Text between the opening ``---`` line and the next ``---`` line, exclusive.

    Examples:
        >>> _frontmatter("---\\nname: x\\nisolation: worktree\\n---\\nbody isolation: none\\n")
        'name: x\\nisolation: worktree'
        >>> _frontmatter("# no frontmatter\\n")
        ''
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[1:index])
    return ""


def _documented_isolated_spawns() -> set[tuple[str, str]]:
    """Return every isolated spawn site documented in shipped plugin Markdown, test trees excluded.

    Returns:
        ``(repo-relative POSIX path, subagent_type)`` pairs, one per isolated ``Agent(...)`` line.
    """
    shipped = (path for path in PLUGINS_DIR.rglob("*.md") if "tests" not in path.relative_to(PLUGINS_DIR).parts)
    return {
        (path.relative_to(REPO_ROOT).as_posix(), subagent)
        for path in shipped
        for subagent in _isolated_spawns(path.read_text(encoding="utf-8"))
    }


_AGENT_FILES = sorted(PLUGINS_DIR.glob("*/agents/*.md"))
_AGENT_CASES = [pytest.param(path, id=path.relative_to(PLUGINS_DIR).as_posix()) for path in _AGENT_FILES]


class TestAgentWorktreeIsolation:
    """Worktree isolation stays a reviewed per-spawn choice across every Claude plugin."""

    @pytest.mark.parametrize("path", _AGENT_CASES)
    def test_frontmatter_never_declares_isolation(self, path: Path) -> None:
        """No agent definition declares an ``isolation`` frontmatter key.

        A frontmatter key isolates every spawn of that agent, so read-only reviewers and callers expecting in-place
        edits lose their output to an auto-removed worktree. Isolation belongs on the specific ``Agent()`` call.
        """
        frontmatter = _frontmatter(path.read_text(encoding="utf-8"))

        assert not _FRONTMATTER_ISOLATION.search(frontmatter), (
            f"{path.relative_to(REPO_ROOT).as_posix()} declares `isolation` in frontmatter; pass "
            '`isolation="worktree"` on the Agent() calls that transplant results back instead'
        )

    def test_frontmatter_scan_covers_sw_engineer(self) -> None:
        """The frontmatter scan includes ``foundry:sw-engineer``, the agent whose default isolation lost findings.

        A moved or renamed agent file would otherwise make the frontmatter guard pass vacuously for the one agent it was
        written for.
        """
        assert SW_ENGINEER_AGENT in _AGENT_FILES

    def test_isolated_spawn_sites_match_reviewed_allow_list(self) -> None:
        """Documented isolated spawn sites equal the reviewed allow list exactly.

        A new isolated site fails until reviewed for result transplant and deliverable paths; an allowed site that
        stopped passing isolation fails too, so a parallel writer cannot silently start sharing the caller's tree.
        """
        found = _documented_isolated_spawns()

        assert found == ALLOWED_ISOLATED_SPAWNS, (
            f"unreviewed isolated spawns: {sorted(found - ALLOWED_ISOLATED_SPAWNS)}; "
            f"allowed sites no longer isolated: {sorted(ALLOWED_ISOLATED_SPAWNS - found)}"
        )


#: Opening of the read-only clause every parallel hypothesis investigator prompt carries; the rest is checked below.
READ_ONLY_CLAUSE = "Read-only investigation: never edit, create or delete tracked files"

#: Commands an investigator sharing the caller's tree must never run, and where its probes run instead.
READ_ONLY_TERMS = ("`git stash`", "`git checkout`", "`git reset`", "`git restore`", "`mktemp -d`", "run directory")

#: Non-isolated spawn sites of parallel ``foundry:sw-engineer`` hypothesis investigators, with the minimum number of
#: read-only clauses each must carry (one per spawn prompt it spells out; the shared template counts once).
READ_ONLY_INVESTIGATOR_SITES = (
    pytest.param("plugins/cc_develop/skills/_shared/preflight-helpers.md", 1, id="team-spawn-template"),
    pytest.param("plugins/cc_develop/skills/fix/modes/team-mode.md", 2, id="fix-team-hypotheses-a-b"),
    pytest.param("plugins/cc_develop/skills/debug/SKILL.md", 1, id="debug-team-hypotheses"),
)

_INVESTIGATOR_LINE = re.compile(r"sw-engineer.*hypothes|hypothes.*sw-engineer", re.IGNORECASE)


def _clause_count(text: str) -> int:
    """Count read-only clauses in ``text``: each opening must be followed, within its sentence group, by every term.

    Examples:
        >>> _clause_count("x")
        0
    """
    count = 0
    for chunk in text.split(READ_ONLY_CLAUSE)[1:]:
        window = chunk[:400]
        count += all(term in window for term in READ_ONLY_TERMS)
    return count


class TestReadOnlyInvestigators:
    """Parallel hypothesis investigators sharing the caller's tree never write to it."""

    @pytest.mark.parametrize(("relative", "minimum"), READ_ONLY_INVESTIGATOR_SITES)
    def test_spawn_site_carries_the_read_only_clause(self, relative: str, minimum: int) -> None:
        """Each investigator spawn site states the full read-only clause, once per spelled-out prompt.

        Two investigators in one tree can stash each other's probes and the user's uncommitted work, or interleave edits
        to the same file and report false evidence; worktree isolation would lose their reports instead.
        """
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")

        assert _clause_count(text) >= minimum, f"{relative} lacks the read-only investigator clause"

    def test_every_investigator_spawn_file_is_covered(self) -> None:
        """Every skill file that spawns ``sw-engineer`` hypothesis investigators is a reviewed read-only site.

        A new investigator site that is neither isolated nor read-only would reopen the shared-tree hazard silently.
        """
        reviewed = {param.values[0] for param in READ_ONLY_INVESTIGATOR_SITES}
        skill_files = (path for path in PLUGINS_DIR.glob("*/skills/**/*.md") if "tests" not in path.parts)
        found = {
            path.relative_to(REPO_ROOT).as_posix()
            for path in skill_files
            if _INVESTIGATOR_LINE.search(path.read_text(encoding="utf-8"))
        }

        assert found <= reviewed, f"unreviewed investigator spawn sites: {sorted(found - reviewed)}"
