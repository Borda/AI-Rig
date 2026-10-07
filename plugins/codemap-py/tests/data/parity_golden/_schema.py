"""Index data contract shared between scan-index (writer) and scan-query (reader).

Both scripts import via sys.path.insert on __file__'s directory — this file must live alongside them in bin/.

consumers: bin/scan-index, bin/scan-query — imported as Python module; not a standalone executable
"""

from __future__ import annotations

from enum import Enum
from typing import TypedDict

# Increment when the index JSON structure changes incompatibly.
SCAN_VERSION: int = 11

# Per-feature minimum index versions.
# v4 and v5 were design epochs shipped together in one release (SCAN_VERSION 4–10).
# Each feature checks its own constant via _require_feature() in scan-query.
CALL_GRAPH_MIN_VER: int = 3  # v3 — call edges (`calls` per symbol); powers fn-deps/fn-rdeps/fn-central/fn-blast
MOCK_PATCHES_MIN_VER: int = 4  # v4.1 — mock_patches, mock_rdep_count, fn_rdep_test_count
UNCOVERED_MIN_VER: int = 4  # v4.2 — fn_rdep_test_count per symbol
IMPORT_GROUPS_MIN_VER: int = 4  # v4.3 — import_groups
DOCSTRING_MIN_VER: int = 4  # v4.4 — has_docstring, docstring_first_line
SPHINX_XREFS_MIN_VER: int = 5  # v4.5 — sphinx_xrefs, sphinx_xref_count
DEAD_SYMBOL_MIN_VER: int = 6  # v4.6 — dead-symbol query; requires sphinx_xref_count (v4.5)
MODULE_ALIASES_MIN_VER: int = 7  # v5.1 — module_aliases at index root
SUBPROCESS_CALLS_MIN_VER: int = 8  # v5.2 — subprocess_calls per module, subprocess_rdep_count at root
FIXTURE_GRAPH_MIN_VER: int = 9  # v5.3 — fixture_uses per test module, fixture_exports per conftest
COVERAGE_MIN_VER: int = 10  # v5.4 — coverage_pct, covered_by per symbol (requires --with-coverage build)
ENTITY_TYPE_MIN_VER: int = 11  # v5.5 — entity_type ("pkg"|"test"|"docs"|"example"), package (top-level name)

# Root-level keys every index file must carry to be loadable. A file missing any of
# these is structurally broken (a truncated write, a hand-edited file, or the output
# of an incompatible tool) and must be rejected before any command reads it — a
# partial-serve on such a file produces silently wrong answers. ``scan_root`` and the
# many optional feature keys are intentionally NOT required: older indexes omit them
# and per-feature version gates handle their absence.
REQUIRED_INDEX_KEYS: frozenset[str] = frozenset({"scan_version", "modules"})

# Oldest index structure scan-query can read. Indexes below this predate the loadable
# contract entirely; the reader refuses them and asks for a rebuild rather than
# guessing at a shape it no longer understands.
MIN_LOADABLE_VERSION: int = 3


def validate_index(index: object) -> str | None:  # noqa: PLR0911
    """Return an error slug when *index* is not a loadable codemap index, else None.

    Pure structural gate run by the reader immediately after ``json.load`` — before
    any command touches the data — so a truncated, hand-edited, or incompatible index
    is rejected with a clear rebuild instruction instead of being partly served. The
    checks, in order: the top-level value is an object; every key in
    :data:`REQUIRED_INDEX_KEYS` is present; ``scan_version`` is an int at or above
    :data:`MIN_LOADABLE_VERSION`; ``modules`` is a list; and ``collisions`` (when
    present) is a list of objects — a corrupt collision record poisons
    ``query_complete`` for every command, so its shape is sanity-checked here.

    Args:
        index: the value decoded from the index JSON (any type — callers pass
            straight from ``json.load`` without pre-checking).

    Returns:
        A short slug naming the first failed check (``"not_object"``,
        ``"missing_keys"``, ``"bad_version"``, ``"version_too_old"``,
        ``"modules_not_list"``, ``"collisions_not_list"``), or None when the index
        satisfies every structural invariant.

    Examples:
        >>> validate_index({"scan_version": 11, "modules": []}) is None
        True
        >>> validate_index({"scan_version": 11, "modules": [], "collisions": []}) is None
        True
        >>> validate_index([])
        'not_object'
        >>> validate_index({"modules": []})
        'missing_keys'
        >>> validate_index({"scan_version": "x", "modules": []})
        'bad_version'
        >>> validate_index({"scan_version": 1, "modules": []})
        'version_too_old'
        >>> validate_index({"scan_version": 11, "modules": {}})
        'modules_not_list'
        >>> validate_index({"scan_version": 11, "modules": [], "collisions": 3})
        'collisions_not_list'
        >>> validate_index({"scan_version": 11, "modules": [], "collisions": [1]})
        'collisions_not_list'
    """
    if not isinstance(index, dict):
        return "not_object"
    if not REQUIRED_INDEX_KEYS.issubset(index):
        return "missing_keys"
    version = index.get("scan_version")
    # bool is an int subclass — reject it explicitly so ``True`` never reads as v1.
    if not isinstance(version, int) or isinstance(version, bool):
        return "bad_version"
    if version < MIN_LOADABLE_VERSION:
        return "version_too_old"
    if not isinstance(index.get("modules"), list):
        return "modules_not_list"
    collisions = index.get("collisions")
    if collisions is not None:
        if not isinstance(collisions, list):
            return "collisions_not_list"
        if not all(isinstance(c, dict) for c in collisions):
            return "collisions_not_list"
    return None


class Resolution(str, Enum):
    """Resolution kind for a call edge.

    Inherits str so json.dump serialises values as plain strings.
    """

    IMPORT = "import"
    LOCAL = "local"
    SELF = "self"
    BUILTIN = "builtin"
    STAR = "star"
    UNRESOLVED = "unresolved"


# Resolutions that represent calls within the project (exclude builtins, star, unresolved).
VALID_CALL_RESOLUTIONS: frozenset[str] = frozenset({Resolution.IMPORT, Resolution.LOCAL, Resolution.SELF})


class Symbol(TypedDict, total=False):
    name: str
    qualified_name: str
    type: str  # "class" | "function" | "method"
    start_line: int
    end_line: int
    calls: list[dict]  # v3 call edges — absent in v2 indexes
    fn_rdep_test_count: int  # v4.1 — count of callers whose source module has is_test=True
    mock_rdep_count: int  # v4.1 — count of test files mocking this symbol via patch()
    has_docstring: bool  # v4.4 — True when ast.get_docstring(node) is not None
    docstring_first_line: str | None  # v4.4 — first non-empty line, stripped, ≤80 chars; None when absent
    coverage_pct: (
        float  # v5.4 — fraction of symbol's lines measured (0.0–1.0); absent when index built without --with-coverage
    )
    covered_by: list[str] | None  # v5.4 — test node IDs that executed this symbol; null when --cov-context not used
