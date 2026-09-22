"""Keep Markdown prose out of the shell fences that plugin skills ship as runnable blueprints.

Skill files interleave instruction prose with ``bash`` fences that the agent executes verbatim, and
``build_blueprint_manifest.py`` hashes each block so ``blueprint-allow.js`` can pre-authorize it. A paragraph inserted a
few lines too early therefore does three things at once: the shell runs it, the manifest blesses it, and the reader
never sees it as instruction because it sits inside a code fence.

That is not hypothetical. A reviewer-attribution paragraph landed inside the ``Agent Resolution`` fence of
``cc_oss/skills/review/SKILL.md``. Its leading ``>`` parsed as an output redirect, so every ``/oss:review`` run created
a stray file named ``Review`` in the working directory, its backtick spans ran as command substitutions, and the
manifest gained a ``kind: "command"`` entry for the paragraph.

The patterns below are prose shapes that no shell block legitimately starts a line with. They are deliberately narrow:
bare ``>`` redirects, ``|`` pipelines and ``#`` comments all occur in real blocks, so only blockquotes with prose-like
bodies, bold-lead lines, whole table rows and bold bullet labels are rejected.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

_FENCE = re.compile(r"^\s*(`{3,})(.*)$")
_SHELL_INFO = re.compile(r"^(bash|sh|shell|zsh|console)\b")

# A shell line may legitimately begin with ``>`` (``> out.txt`` truncates a file), so a blockquote
# counts as prose only when it carries a Markdown code span or reads like a sentence. Every other
# pattern below is invalid shell on its own.
_PROSE_PATTERNS = (
    ("blockquote", re.compile(r"^>\s+(?=.*(?:`|\S+(?:\s+\S+){7,}))")),
    ("bold-lead", re.compile(r"^\s*\*\*\S")),
    ("table-row", re.compile(r"^\s*\|.*\|\s*$")),
    ("bullet-bold", re.compile(r"^\s*[-*]\s+\*\*")),
)


def _shell_fence_lines(text: str) -> list[tuple[int, str]]:
    """Return ``(line_number, line)`` for every line inside a shell-tagged fence.

    Args:
        text: Full Markdown file contents.

    Returns:
        Body lines of shell fences only; fence delimiters themselves are excluded.

    Examples:
        >>> _shell_fence_lines("```bash\\necho hi\\n```\\n")
        [(2, 'echo hi')]
        >>> _shell_fence_lines("```python\\nprint()\\n```\\n")
        []
    """
    inside: list[tuple[int, str]] = []
    depth = 0
    info = ""
    for number, line in enumerate(text.splitlines(), 1):
        fence = _FENCE.match(line)
        if fence:
            if depth and not fence.group(2).strip():
                depth, info = 0, ""
            elif not depth:
                depth, info = len(fence.group(1)), fence.group(2).strip()
            continue
        if depth and _SHELL_INFO.match(info):
            inside.append((number, line))
    return inside


def _prose_violations(text: str, label: str) -> list[str]:
    """Return one message per Markdown-prose line found inside a shell fence of ``text``.

    Args:
        text: Full Markdown file contents.
        label: Human-readable file name used in the message.

    Returns:
        One ``label:line: kind — excerpt`` string per violation, in file order.

    Examples:
        >>> _prose_violations("```bash\\n**Note:** run it\\n```\\n", "SKILL.md")
        ['SKILL.md:2: bold-lead inside shell fence — **Note:** run it']
    """
    violations = []
    for number, line in _shell_fence_lines(text):
        for kind, pattern in _PROSE_PATTERNS:
            if pattern.match(line):
                violations.append(f"{label}:{number}: {kind} inside shell fence — {line.strip()[:120]}")
                break
    return violations


@pytest.mark.parametrize("path", sorted(PLUGINS_DIR.rglob("*.md")), ids=lambda path: str(path.relative_to(PLUGINS_DIR)))
def test_shell_fences_contain_no_markdown_prose(path: Path) -> None:
    """Fail the exact file whose shell fence swallowed instruction prose."""
    label = str(path.relative_to(REPO_ROOT))
    assert _prose_violations(path.read_text(encoding="utf-8"), label) == []


def test_guard_fires_on_the_shape_that_shipped_and_spares_a_real_redirect() -> None:
    """Separate an embedded instruction paragraph from a legitimate truncating redirect."""
    shipped = (
        "```bash\n"
        "export CSID=1\n"
        "> Review presentation is additive: keep the aggregate summary as prose "
        "and add `Reviewers:` after `Agents:`.\n"
        "```\n"
    )
    assert _prose_violations(shipped, "SKILL.md") == [
        "SKILL.md:3: blockquote inside shell fence — > Review presentation is additive: keep the aggregate "
        "summary as prose and add `Reviewers:` after `Agents:`."
    ]
    assert _prose_violations("```bash\n> out.txt\necho done\n```\n", "SKILL.md") == []
    assert _prose_violations("```python\n**not shell**\n```\n", "SKILL.md") == []
