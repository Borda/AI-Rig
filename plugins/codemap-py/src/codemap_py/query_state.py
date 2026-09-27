"""Per-process query state shared across the query command modules.

These seven names are the only query globals whose writer and reader are different concerns: ``main``/``_run_query`` set
them once per invocation and command, coverage and error reporting read them much later. They live on this module rather
than on any one command module so that every reader resolves the value through a single object.

**Always access them as attributes of this module** — ``state._CMD``, never ``from .query_state import _CMD``. A ``from
... import`` binding is resolved once at import time and never sees a later rebind, which is exactly what these names
do.
"""

from __future__ import annotations

from codemap_py.telemetry import CliInvocation

#: Name of the command currently executing, set in ``main`` and cleared between batch items.
_CMD: str = ""

#: Telemetry record for the active invocation; ``None`` when telemetry is off.
_invocation: CliInvocation | None = None

# batch mode: when a list is installed here, _print captures each command's stdout
# JSON into it instead of writing to the real stdout, so cmd_* functions (which all
# emit via _print) can be reused unchanged and their results collected per item.
# stderr writes (file=... kwarg) always pass through untouched.
_capture: list[str] | None = None

#: Path of the index this process actually opened, captured at load time and emitted
#: as ``index.index_path``. Deliberately NOT recomputed from the resolver when emitted:
#: a consumer comparing its own probe path against a resolver-derived answer compares
#: two runs of the same function and learns nothing. This value is the only one that
#: CAN disagree with the resolver — a stale ``CODEMAP_INDEX_DIR`` in the querying
#: process, a different git root, a self-heal that rewrote elsewhere — which is exactly
#: what makes it worth reporting. Empty until a load succeeds; the key is then omitted.
_LOADED_INDEX_PATH: str = ""

# set once in main() before any command runs. True when the index's stored
# scan_root does not match where this query is being resolved against (a mismatched
# ``--root``, or a CWD outside the scanned tree). A root-mismatched index answers about a
# DIFFERENT project, so its graph can never be a complete answer here — this flag both
# surfaces in every coverage block and forces query_complete=false in _query_complete.
_root_mismatch: bool = False

#: Set from ``--verbose-coverage`` in main(); forces the full coverage block.
_verbose_coverage: bool = False

#: Set from ``--compact`` in main(); opt-in coverage diet.
_force_compact_coverage: bool = False

#: Output encoding for result payloads; set from ``--format`` in main().
_FORMAT: str = "json"
