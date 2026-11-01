"""Hold the always-loaded foundry rule set to a byte budget and keep every moved obligation reachable.

Claude Code loads a rule file at session start when its ``paths`` frontmatter is absent or matches everything (``**`` or
``**/*``); any narrower glob loads on the first Read, Edit or Write of a matching file. Every always-loaded byte is re-
read on every turn, so the eager set is the cost line, and a section added to it silently taxes every session. The
budget below pins that set. A rule that only matters while touching one kind of file belongs behind a narrower ``paths``
glob (``rules/markdown.md``, ``rules/foundry-config.md``) or in ``rules/_full/`` behind a trigger line. A rule that only
matters during one activity is injected by ``hooks/rule-inject.js`` after the first call of that activity runs
(``rules/agent-spawn.md`` on ``Agent()``, ``rules/git-commit.md`` on a commit-creating git call) and must stay out of
the eager set, or it would load twice.

Raise ``EAGER_BUDGET_BYTES`` only deliberately: the new always-on text must justify per-turn cost across every session,
and the same change should say why a lazy home does not fit. Lower it when a section moves out, so the pin keeps
tracking reality and the next addition is judged against the real total rather than slack.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RULES_DIR = Path(__file__).resolve().parents[2] / "rules"
FULL_DIR = RULES_DIR / "_full"
EAGER_GLOBS = frozenset({"**", "**/*"})
#: Rules ``hooks/rule-inject.js`` injects on their activity; eager loading would duplicate them.
HOOK_INJECTED_RULES = ("agent-spawn.md", "git-commit.md")

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_FULL_POINTER = re.compile(r"_full/([A-Za-z0-9_.-]+\.md)")


def _rule_files() -> list[Path]:
    """Top-level rule files; ``_full/`` holds on-demand detail that is never linked into the rules directory."""
    return sorted(RULES_DIR.glob("*.md"))


def _frontmatter(text: str) -> str | None:
    """Return the frontmatter body of ``text``, or ``None`` when the file has none."""
    match = _FRONTMATTER.match(text)
    return match.group(1) if match else None


def _paths(front: str) -> list[str] | None:
    """Parse the ``paths`` key of a rule frontmatter block.

    Handles the two shapes the rule files use: a block list of ``- 'glob'`` items and an inline ``['glob', ...]`` list.

    Returns:
        The globs, or ``None`` when the key is absent.

    Examples:
        >>> _paths("description: x\\npaths:\\n  - '**'\\n  - docs/**")
        ['**', 'docs/**']
        >>> _paths("paths: ['**/*.py']")
        ['**/*.py']
        >>> _paths("description: x") is None
        True
    """
    lines = front.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("paths:"):
            continue
        inline = line.split(":", 1)[1].strip()
        if inline.startswith("["):
            items = inline.strip("[]").split(",")
        else:
            items = []
            for follow in lines[index + 1 :]:
                stripped = follow.strip()
                if not stripped.startswith("- "):
                    break
                items.append(stripped[2:])
        return [item.strip().strip("'\"") for item in items if item.strip()]
    return None


def _is_eager(path: Path) -> bool:
    """Report whether a rule loads at session start: no ``paths``, or any glob that matches every file."""
    front = _frontmatter(path.read_text(encoding="utf-8"))
    globs = _paths(front) if front is not None else None
    return globs is None or any(glob in EAGER_GLOBS for glob in globs)


_RULE_CASES = [pytest.param(path, id=path.name) for path in _rule_files()]


@pytest.mark.parametrize("name", HOOK_INJECTED_RULES)
def test_hook_injected_rules_stay_out_of_the_eager_set(name: str) -> None:
    """A hook-injected rule that also loaded at session start would reach the model twice.

    ``rule-inject.js`` reads the named rule when its activity starts, so the rule must exist and carry a ``paths``
    glob narrow enough to keep it out of the session-start set.

    Args:
        name: Rule file the injection hook reads at run time.
    """
    rule = RULES_DIR / name
    assert rule.is_file(), f"rule-inject.js reads {rule}, which is missing"
    assert not _is_eager(rule), f"{name} is hook-injected but its `paths` glob loads it at session start"


@pytest.mark.parametrize("rule", _RULE_CASES)
def test_every_rule_declares_description_and_paths(rule: Path) -> None:
    """A rule without frontmatter ``paths`` is eager by accident rather than by decision.

    Each top-level rule must state its load scope, plus a description, so no rule joins the eager set by omission.

    Args:
        rule: Top-level rule file.
    """
    front = _frontmatter(rule.read_text(encoding="utf-8"))
    assert front is not None, f"{rule.name} has no frontmatter"
    assert "description:" in front, f"{rule.name} frontmatter lacks `description`"
    assert _paths(front), f"{rule.name} frontmatter lacks a non-empty `paths` list"


@pytest.mark.parametrize("rule", _RULE_CASES)
def test_every_full_pointer_resolves(rule: Path) -> None:
    """A stub that points at ``_full/<name>.md`` must find that file, or its detail is unreachable.

    Every ``_full/`` file name mentioned in a top-level rule has to exist in the ``_full`` directory.

    Args:
        rule: Top-level rule file.
    """
    missing = sorted(
        {name for name in _FULL_POINTER.findall(rule.read_text(encoding="utf-8")) if not (FULL_DIR / name).is_file()}
    )
    assert not missing, f"{rule.name} points at missing _full files: {missing}"
