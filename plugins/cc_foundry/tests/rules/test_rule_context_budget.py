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
# Total bytes of the eager rule files. See the module docstring before raising this.
# Raised from 58597: commit-message format bullets stay eager in claude-config.md because
# rule-inject.js context arrives beside the first git call's result, after that message
# was drafted; stub/detail pairs gained policy-sibling markers. Raised again from 59315:
# default-branch and history-safety bullets stay eager because `reset --hard` never
# triggers injection and `rebase` injects only after it ran.
EAGER_BUDGET_BYTES = 59455
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


def _eager_bytes() -> dict[str, int]:
    """Byte size of each always-loaded rule file, keyed by file name."""
    return {path.name: path.stat().st_size for path in _rule_files() if _is_eager(path)}


def test_rule_files_were_found() -> None:
    """The checks below are vacuous if the rules directory resolves to nothing."""
    assert len(_rule_files()) >= 10, f"only {len(_rule_files())} rule files under {RULES_DIR}"


def test_eager_rule_bytes_stay_within_budget() -> None:
    """Fail when always-loaded rule text grows past the pinned budget."""
    sizes = _eager_bytes()
    total = sum(sizes.values())
    largest = ", ".join(f"{name} {size}" for name, size in sorted(sizes.items(), key=lambda item: -item[1])[:5])
    assert total <= EAGER_BUDGET_BYTES, (
        f"always-loaded rules total {total} B, budget {EAGER_BUDGET_BYTES} B (largest: {largest}). "
        "Move the new text behind a narrower `paths` glob or into `_full/` with a trigger line; "
        "raise EAGER_BUDGET_BYTES only deliberately (see module docstring)."
    )


@pytest.mark.parametrize("name", HOOK_INJECTED_RULES)
def test_hook_injected_rules_stay_out_of_the_eager_set(name: str) -> None:
    """A hook-injected rule that also loaded at session start would reach the model twice.

    Args:
        name: Rule file the injection hook reads at run time.
    """
    rule = RULES_DIR / name
    assert rule.is_file(), f"rule-inject.js reads {rule}, which is missing"
    assert not _is_eager(rule), f"{name} is hook-injected but its `paths` glob loads it at session start"


@pytest.mark.parametrize("rule", _rule_files(), ids=lambda path: path.name)
def test_every_rule_declares_description_and_paths(rule: Path) -> None:
    """A rule without frontmatter ``paths`` is eager by accident rather than by decision.

    Args:
        rule: Top-level rule file.
    """
    front = _frontmatter(rule.read_text(encoding="utf-8"))
    assert front is not None, f"{rule.name} has no frontmatter"
    assert "description:" in front, f"{rule.name} frontmatter lacks `description`"
    assert _paths(front), f"{rule.name} frontmatter lacks a non-empty `paths` list"


@pytest.mark.parametrize("rule", _rule_files(), ids=lambda path: path.name)
def test_every_full_pointer_resolves(rule: Path) -> None:
    """A stub that points at ``_full/<name>.md`` must find that file, or its detail is unreachable.

    Args:
        rule: Top-level rule file.
    """
    missing = sorted(
        {name for name in _FULL_POINTER.findall(rule.read_text(encoding="utf-8")) if not (FULL_DIR / name).is_file()}
    )
    assert not missing, f"{rule.name} points at missing _full files: {missing}"
