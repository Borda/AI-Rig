"""Record types for one parsed symbol and one call edge."""

from __future__ import annotations
import ast
from dataclasses import dataclass
from codemap_py.schema import Resolution, SymbolType


@dataclass(frozen=True)
class CallEdge:
    """Single outgoing call edge from a function or method.

    Dataclass (not TypedDict): named construction and attribute access; converted to dict by as_dict() at the
    _parse_file boundary before JSON serialisation.
    """

    target: str  # "pkg.db::fetch_user" (module::symbol) or raw chain if unresolved
    resolution: Resolution

    def as_dict(self) -> dict:
        """Serialise to a plain dict for JSON output."""
        return {"target": self.target, "resolution": self.resolution}


@dataclass(frozen=True)
class Symbol:
    """Extracted symbol (class, function, or method) with call edges.

    Dataclass (not TypedDict): see CallEdge note above.
    """

    name: str
    qualified_name: str
    type: SymbolType
    start_line: int
    end_line: int
    calls: list[CallEdge]
    has_docstring: bool = False  # v4.4 — True when ast.get_docstring returned non-None
    docstring_first_line: str | None = None  # v4.4 — first non-empty line, stripped, ≤80 chars

    def as_dict(self) -> dict:
        """Serialise to a plain dict (including nested CallEdge list) for JSON output."""
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "type": self.type,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "calls": [c.as_dict() for c in self.calls],
            "has_docstring": self.has_docstring,
            "docstring_first_line": self.docstring_first_line,
        }


_DOCSTRING_FIRST_LINE_MAX = 80


def _docstring_fields(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[bool, str | None]:
    """Return ``(has_docstring, docstring_first_line)`` for a class/function/async-function node.

    The first line is the first non-empty stripped line of the docstring, truncated
    to ``_DOCSTRING_FIRST_LINE_MAX`` characters. ``docstring_first_line`` is ``None``
    when the symbol has no docstring, or when every line of the docstring is blank.

    Args:
        node: AST node whose docstring to extract (must be a definition node accepted by ``ast.get_docstring``).

    Examples:
        >>> import ast
        >>> tree = ast.parse('def f():\\n    \"\"\"Hi there.\"\"\"')
        >>> _docstring_fields(tree.body[0])
        (True, 'Hi there.')
        >>> tree = ast.parse("def g():\\n    pass")
        >>> _docstring_fields(tree.body[0])
        (False, None)
    """
    raw = ast.get_docstring(node, clean=False)
    if raw is None:
        return False, None
    first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    if not first:
        return True, None
    return True, first[:_DOCSTRING_FIRST_LINE_MAX]
