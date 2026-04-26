"""Index data contract shared between scan-index (writer) and scan-query (reader).

Both scripts import via sys.path.insert on __file__'s directory — this file must
live alongside them in bin/.
"""

from __future__ import annotations

from enum import Enum
from typing import TypedDict

# Increment when the index JSON structure changes incompatibly.
SCAN_VERSION: int = 3


class Resolution(str, Enum):
    """Resolution kind for a call edge. Inherits str so json.dump serialises values as plain strings."""

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
