"""Readers for skill text and shipped scripts shared by the develop plugin tests.

The contract tests read skill Markdown and compare marker phrases, cut sections between two markers, or extract the
fenced ``bash`` blocks a skill ships. The bin tests load a script by path under a unique module name. One definition of
each lives here so a fix to the fence grammar or the whitespace rule lands everywhere at once.

The module name is plugin-specific because ``--import-mode=importlib`` tests share one ``sys.modules``: a bare
``_skill_text`` would collide with the Codex Rig helper of that name in a full-repository run.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

_BASH_BLOCK_RE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
_BASH_FENCE_RE = re.compile(r"```bash\n(.*?)\n```", re.DOTALL)
_NESTED_BASH_FENCE_RE = re.compile(r"^([ \t]*)```bash\n(.*?)\n\1```", re.MULTILINE | re.DOTALL)


def flat(text: str) -> str:
    """Collapse every whitespace run to one space so a prose marker matches wherever the source wraps."""
    return " ".join(text.split())


def between(text: str, start_marker: str, end_marker: str) -> str:
    """Return the text from the first ``start_marker`` up to the first ``end_marker`` after it.

    Raises:
        ValueError: A marker is absent, so a rewritten section fails loudly instead of matching nothing.
    """
    start = text.index(start_marker)
    return text[start : text.index(end_marker, start)]


def bash_blocks(text: str) -> list[str]:
    """Return the body of every fenced ``bash`` block, keeping the newline that precedes the closing fence."""
    return _BASH_BLOCK_RE.findall(text)


def bash_fences(text: str) -> list[str]:
    """Return the body of every fenced ``bash`` block, without the newline that precedes the closing fence."""
    return _BASH_FENCE_RE.findall(text)


def nested_bash_fences(text: str) -> list[str]:
    """Return every ``bash`` fence body, matching the closing fence at the SAME indent as the opening one.

    A skill nests ``bash`` fences inside numbered-list items, indented three spaces. Without the indent anchor an
    indented open marker would skip past its own indented close marker to the next unindented fence anywhere below and
    silently merge two unrelated fences into one.
    """
    return [match.group(2) for match in _NESTED_BASH_FENCE_RE.finditer(text)]


def load_module(path: Path, name: str) -> ModuleType:
    """Load the script at ``path`` as a module called ``name``, so sibling plugins cannot shadow each other.

    Several plugins ship scripts with the same file name (``codemap_resolve``, ``make_run_dir``); loading by path under
    a unique name keeps each test bound to its own plugin's copy.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
