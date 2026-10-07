"""Subprocess environment, contamination detection, and scan-query call parsing."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from _bench_common.codemap_discovery import codemap_bin_on_path


def _subprocess_env(index_path: Path) -> dict[str, str]:
    """Build subprocess environment with codemap bin dir and CODEMAP_INDEX set.

    Args:
        index_path: Path to the pre-built codemap index.

    Returns:
        Environment dict for subprocess.Popen.
    """
    plugin_root = Path(__file__).resolve().parents[3] / "plugins" / "codemap-py"
    env = codemap_bin_on_path(os.environ.copy(), plugin_root)
    env["CODEMAP_INDEX"] = str(index_path)
    env["CODEMAP_ENABLED"] = "true"
    env["CODEMAP_LOGGING"] = "false"
    return env


#: Markers that betray plain-arm access to the codemap index or binary. Matched against
#: the FULL untruncated tool input (Bash command / Read path) in _handle, not the truncated tool_log:
#: the prebuilt index at .cache/{codemap,scan}/*.json holds every structural answer, so a raw Read/cat
#: of it lets the control arm self-serve answers without ever calling scan-query.
_CONTAMINATION_MARKERS: tuple[str, ...] = ("scan-query", "codemap-py/bin", ".cache/codemap", ".cache/scan")


def _is_contaminating_access(text: str) -> bool:
    """Return True when *text* touches the codemap index or binary.

    Backslash path separators are normalised to forward slashes before matching, so a
    Windows-style ``.cache\\codemap\\proj.json`` path — how a real Read ``file_path``
    or Bash argument reports it on Windows — still matches the forward-slash
    :data:`_CONTAMINATION_MARKERS`.

    Args:
        text: A full Bash command string or a Read ``file_path`` (untruncated).

    Returns:
        True when any :data:`_CONTAMINATION_MARKERS` substring is present.

    Examples:
        >>> _is_contaminating_access("cat /repo/.cache/codemap/proj.json")
        True
        >>> _is_contaminating_access("grep -rn Trainer src/")
        False
    """
    return any(marker in text.replace("\\", "/") for marker in _CONTAMINATION_MARKERS)


#: Subcommands recognised by scan-query (mirrors the _CODEMAP_TOOLS help block).
#: ``central``, ``path``, and ``fn-blast`` back the graph series; ``diff-impact`` backs the
#: diff-impact series; ``batch`` is the JSON-array multi-query form (measured, not forced).
_SCAN_QUERY_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "symbol",
        "find-symbol",
        "symbols",
        "fn-rdeps",
        "rdeps",
        "undocumented",
        "uncovered",
        "coupled",
        "xrefs",
        "central",
        "path",
        "fn-blast",
        "diff-impact",
        "batch",
    }
)

# Batch mode reads a JSON array of ``{"cmd": ..., "args": [...]}`` items and runs each in one process.
# When the codemap arm uses batch, each inner item's ``cmd`` must still be attributed to its own
# subcommand counter (so batched `fn-rdeps` counts as an `fn-rdeps` use, not vanishing into `batch`).
# The array may be passed as an inline heredoc/echo pipe or a file argument.
#: Name of the ``scan-query`` subcommand that runs several queries from one JSON array.
_BATCH_SUBCOMMAND = "batch"


def _parse_scan_query_subcommand(command: str) -> str | None:
    """Extract the scan-query subcommand from a Bash command line.

    The first non-flag token following ``scan-query`` (after skipping the
    ``--index <path>`` option and any other leading ``--flag``/``--flag value``
    pairs) is the subcommand. Returns None when the command is not a scan-query
    invocation or no recognised subcommand is present.

    Args:
        command: Raw Bash command string (as recorded in tool_log / tool input).

    Returns:
        The subcommand name (e.g. ``"fn-rdeps"``), or None.

    Examples:
        >>> _parse_scan_query_subcommand("scan-query --index /x.json fn-rdeps a.b --exclude-tests")
        'fn-rdeps'
        >>> _parse_scan_query_subcommand("scan-query symbol Trainer")
        'symbol'
        >>> _parse_scan_query_subcommand("grep -r foo .") is None
        True
    """
    if "scan-query" not in command:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    # Locate the scan-query executable token (may be a path like .../bin/scan-query).
    start = None
    allowed_prefix = {"env", "command", "time"}
    for i, tok in enumerate(tokens):
        if tok == "scan-query" or tok.endswith("/scan-query"):
            if any(prev not in allowed_prefix and "=" not in prev for prev in tokens[:i]):
                continue
            start = i + 1
            break
    if start is None:
        return None
    i = start
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            if tok == "--index" and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 2
            elif tok.startswith("--index="):
                i += 1
            else:
                return None
            continue
        return tok if tok in _SCAN_QUERY_SUBCOMMANDS else None
    return None


#: Match a JSON array embedded anywhere in a Bash command line (heredoc body, echo/printf pipe, or an
#: inline single-quoted argument). Non-greedy across the whole command; the outermost `[ ... ]` pair is
#: taken and re-validated as JSON before any item is trusted, so a stray bracket in prose is rejected.
_BATCH_ARRAY_RE = re.compile(r"\[\s*\{.*\}\s*\]", re.DOTALL)


def _parse_batch_subcommands(command: str) -> list[str]:
    """Return the inner subcommand names of a ``scan-query batch`` invocation.

    Batch mode reads a JSON array of ``{"cmd": <name>, "args": [...]}`` items. Each inner ``cmd`` is a
    real subcommand use that must be attributed to its own counter — a batched ``fn-rdeps`` counts as an
    ``fn-rdeps`` use, not as an opaque ``batch``. Only the ``cmd`` value of each object is extracted;
    unknown ``cmd`` values (not in :data:`_SCAN_QUERY_SUBCOMMANDS`) are dropped. Returns an empty list
    when the command is not a batch invocation or carries no decodable JSON array.

    Args:
        command: Raw Bash command string (as recorded in tool input).

    Returns:
        List of recognised inner subcommand names, in array order (duplicates preserved).

    Examples:
        >>> _parse_batch_subcommands(
        ...     'scan-query batch <<< \\'[{"cmd": "fn-rdeps", "args": ["m::f"]}, {"cmd": "rdeps", "args": ["m"]}]\\''
        ... )
        ['fn-rdeps', 'rdeps']
        >>> _parse_batch_subcommands("scan-query symbol Trainer")
        []
        >>> _parse_batch_subcommands('scan-query batch <<< \\'[{"cmd": "bogus"}]\\'')
        []
    """
    if _parse_scan_query_subcommand(command) != _BATCH_SUBCOMMAND:
        return []
    match = _BATCH_ARRAY_RE.search(command)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    subs: list[str] = []
    for item in items:
        if isinstance(item, dict):
            cmd = item.get("cmd")
            if isinstance(cmd, str) and cmd in _SCAN_QUERY_SUBCOMMANDS:
                subs.append(cmd)
    return subs


def _embedded_json_objects(raw: str) -> list[dict]:
    """Return every JSON object embedded in *raw*, tolerating surrounding prose or trailing text.

    A scan-query ``tool_result`` is sometimes wrapped in a prose preamble, truncated, or
    concatenated with other output, so ``json.loads`` over the whole string raises and its index
    metadata is silently lost (empirically ~16/17 codemap runs recorded no ``index.method`` despite
    running ``rdeps``, producing a false "index-lookup only" signal). This scans for each ``{`` and
    uses :meth:`json.JSONDecoder.raw_decode` — which decodes one value and ignores whatever follows —
    to recover each object regardless of what surrounds it.

    Args:
        raw: A ``tool_result`` text payload (pure JSON, prose+JSON, or concatenated objects).

    Returns:
        Each successfully decoded top-level JSON object, in order of appearance (non-object
        JSON values such as bare arrays or numbers are skipped).

    Examples:
        >>> _embedded_json_objects('prefix {"index": {"method": "rdeps"}} tail')
        [{'index': {'method': 'rdeps'}}]
        >>> _embedded_json_objects('{"a": 1}{"b": 2}')
        [{'a': 1}, {'b': 2}]
        >>> _embedded_json_objects('no json here')
        []
    """
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    i, n = 0, len(raw)
    while i < n:
        if raw[i] != "{":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(raw, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        i = max(end, i + 1)  # skip past the decoded span so inner braces are not re-scanned
    return objects


# Legacy per-task turn cap. Canonical provider-parity arms rely exclusively on the shared
# wall-clock budget because Codex has no equivalent public turn-cap control. The old structural
# experiment keeps its existing task-sensitive cap unchanged for historical comparability.
#: Legacy ``--max-turns`` cap for every task type that does not enumerate callers.
_TURN_FLOOR_DEFAULT = 40
#: Minimum legacy ``--max-turns`` cap for caller-enumeration tasks.
_TURN_FLOOR_CALLER = 80
#: Turns granted per ground-truth unique caller when that exceeds the caller-task floor.
_TURN_PER_CALLER = 4
#: Task types whose turn cap scales with the number of callers to enumerate.
_CALLER_TASK_TYPES: frozenset[str] = frozenset({"develop_blast_radius", "fn_call_graph"})


def _max_turns_for_task(task: dict) -> int:
    """Return the legacy per-task ``--max-turns`` cap.

    Caller-enumeration tasks (``develop_blast_radius``, ``fn_call_graph``) scale the cap with the
    ground-truth unique-caller count so a task with many callers gets more head-room; every other
    task type uses a flat floor. The value does not depend on the arm — the plain and the codemap arm
    receive the same cap for a given task. Canonical provider-parity runs omit this CLI flag and
    instead use their shared wall-clock budget.

    Args:
        task: Task dict from tasks-bench.json; reads ``type`` and ``ground_truth.unique_caller_count``.

    Returns:
        The max-turns cap for the task.

    Examples:
        >>> _max_turns_for_task({"type": "symbol_extraction"})
        40
        >>> _max_turns_for_task({"type": "develop_blast_radius", "ground_truth": {"unique_caller_count": 30}})
        120
        >>> _max_turns_for_task({"type": "fn_call_graph", "ground_truth": {"unique_caller_count": 5}})
        80
    """
    if task.get("type") in _CALLER_TASK_TYPES:
        caller_count = task.get("ground_truth", {}).get("unique_caller_count", 0)
        return max(_TURN_FLOOR_CALLER, caller_count * _TURN_PER_CALLER)
    return _TURN_FLOOR_DEFAULT
