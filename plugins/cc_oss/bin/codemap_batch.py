#!/usr/bin/env python
"""codemap_batch.py — run oss:review's per-module codemap supplement queries in one ``codemap-py`` process.

consumers: review/modes/codemap-context.md

The review pre-flight appends two query families per changed module to the staged structural context:
``qa`` (``mock-rdeps`` + ``uncovered``) and ``docs`` (``xrefs --broken`` + ``undocumented``). Spawning
``codemap-py`` once per query paid process start-up and index load ``2 x modules`` times; this helper
builds one ``codemap-py query batch`` request and renders the batch result back into the exact text the
per-module loops printed, so every consumer of the staged context reads the same lines.

Usage::

    python codemap_batch.py build qa MODULES_FILE \\
        | codemap-py query --timeout 20 batch - 2>/dev/null \\
        | python codemap_batch.py render --fallback qa MODULES_FILE >> "$CODEMAP_CONTEXT_STAGE"

``MODULES_FILE`` holds one dotted module name per line. Both subcommands filter it through the same
:func:`load_modules`, so the batch item at position ``family_width * j + k`` always belongs to module ``j``.

Each result line is ``json.dumps`` of the batch item's ``result``, which is the same payload a single call prints.
Checked against the real tool: ``rdeps``/``mock-rdeps``/``xrefs`` lines and error payloads are byte-identical, while
``uncovered`` and ``undocumented`` carry the same object with ``module`` placed after ``count_semantics`` instead of
earlier (equal after parsing; key order inside one JSON object is not meaningful to the agents reading it).

Fail-open, like the loops it replaces: a missing modules file yields no modules, and unparsable or short
batch output renders the module headers with the missing query lines left out (what the old loop printed
when every ``codemap-py`` call produced nothing).

``render --fallback`` first reruns every query the batch left unanswered as its own
``codemap-py query --timeout 5 ...`` call, the exact argv of the old loops, so one slow query costs only its own line
instead of blanking every section. A missing result is the failure signal: ``codemap-py query batch`` prints once, after
the last query, so its whole-process ``--timeout`` alarm (SIGALRM, exit 2), a failed start or an index error leaves
stdout empty, and a pipe hides the exit status anyway. Answered positions are never rerun, and an absent ``codemap-py``
leaves the headers-only text.

Exit codes:
    0 — success (including empty module list or unusable batch output)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

#: Query families: ``(subcommand, *fixed flags)`` per query, in the order the old per-module loop issued them.
#: Flags precede the module in each item, exactly as the loop's argv did.
QUERY_FAMILIES: dict[str, tuple[tuple[str, ...], ...]] = {
    "qa": (("mock-rdeps",), ("uncovered", "--top", "20")),
    "docs": (("xrefs", "--broken"), ("undocumented",)),
}

#: Heading prefix printed before each module's query lines.
HEADINGS: dict[str, str] = {"qa": "QA queries", "docs": "Docs queries"}

#: Per-query alarm (seconds) of the old per-module loops; the fallback runs each unanswered query alone under it.
QUERY_TIMEOUT = 5

#: Seconds past the alarm before the fallback kills a call itself; the alarm is Unix-only, so this bounds any hang.
_KILL_GRACE = 5


class QueryItem(TypedDict):
    """One ``codemap-py query batch`` request item: the subcommand and its argv tail."""

    cmd: str
    args: list[str]


#: Mirrors ``codemap_cache._MODULE_RE``: real index names may start with ``.`` or contain ``-``.
_MODULE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _valid_module(name: str) -> bool:
    """Check a module name is a plain dotted token that cannot act as a flag or escape a path.

    Args:
        name: Candidate module name from the modules file.

    Returns:
        True when the name is non-empty, matches ``_MODULE_RE``, has no ``..`` and does not start with ``-``.

    Examples:
        >>> _valid_module("pkg.mod"), _valid_module(".github.scripts.x"), _valid_module("plugins.codemap-py.bin.foo")
        (True, True, True)
        >>> _valid_module("--flag"), _valid_module("a..b"), _valid_module("a b")
        (False, False, False)
    """
    return bool(_MODULE_RE.match(name)) and ".." not in name and not name.startswith("-")


def load_modules(path: Path) -> list[str]:
    """Read the modules file, keeping valid names in file order.

    Args:
        path: File with one dotted module name per line.

    Returns:
        Valid module names; empty when the file is missing or unreadable. Blank and invalid lines are skipped, and
        duplicates are kept so batch positions stay aligned with the file.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     source = Path(tmp) / "mods"
        ...     _ = source.write_text("a.b\\n\\n--bad\\nc.d\\n", encoding="utf-8")
        ...     load_modules(source)
        ['a.b', 'c.d']
        >>> load_modules(Path("does-not-exist"))
        []
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    names = (line.strip() for line in text.splitlines())
    return [name for name in names if name and _valid_module(name)]


def build_request(family: str, modules: list[str]) -> list[QueryItem]:
    """Assemble the ``codemap-py query batch`` request: every query of the family for every module.

    Args:
        family: Key of :data:`QUERY_FAMILIES`.
        modules: Valid module names, in output order.

    Returns:
        ``{"cmd": ..., "args": [...]}`` items, module-major then query order.

    Examples:
        >>> build_request("qa", ["pkg.mod"])
        [{'cmd': 'mock-rdeps', 'args': ['pkg.mod']}, {'cmd': 'uncovered', 'args': ['--top', '20', 'pkg.mod']}]
        >>> build_request("docs", ["a"])[0]
        {'cmd': 'xrefs', 'args': ['--broken', 'a']}
        >>> build_request("qa", [])
        []
    """
    return [{"cmd": cmd, "args": [*flags, module]} for module in modules for cmd, *flags in QUERY_FAMILIES[family]]


def _batch_items(batch_text: str) -> list[object]:
    """Extract the ``batch`` item list from ``codemap-py query batch`` output.

    Args:
        batch_text: Raw stdout of the batch call.

    Returns:
        The decoded ``batch`` list; empty when the text is not a JSON object carrying one. Trailing text after the
        first JSON value is ignored.

    Examples:
        >>> _batch_items('{"batch": [{"ok": true}], "count": 1} trailing')
        [{'ok': True}]
        >>> _batch_items("not json")
        []
        >>> _batch_items('{"error": "x"}')
        []
    """
    try:
        decoded, _ = json.JSONDecoder().raw_decode(batch_text.lstrip())
    except ValueError:
        return []
    items = decoded.get("batch") if isinstance(decoded, dict) else None
    return items if isinstance(items, list) else []


def _batch_texts(items: list[object], count: int) -> list[str | None]:
    """Turn batch items into the staged line of each request position.

    Args:
        items: Decoded ``batch`` list, possibly shorter than the request or holding malformed entries.
        count: Number of items the request carried.

    Returns:
        One entry per request position: the ``result`` as a JSON line, or None where the batch has no result.

    Examples:
        >>> _batch_texts([{"result": {"a": 1}}, {"ok": False}], 3)
        ['{"a": 1}\\n', None, None]
    """
    texts: list[str | None] = []
    for position in range(count):
        item = items[position] if position < len(items) else None
        result = item.get("result") if isinstance(item, dict) else None
        texts.append(None if result is None else f"{json.dumps(result)}\n")
    return texts


def _single_query(executable: str, item: QueryItem) -> str | None:
    """Run one request item as its own ``codemap-py query`` call, with the argv of the old per-module loops.

    Args:
        executable: Resolved path of ``codemap-py``.
        item: Request item from :func:`build_request`.

    Returns:
        Raw stdout of the call, or None when it printed nothing, timed out or could not start. The exit status is
        not read: a failing query prints its error payload on stdout and the old loops staged that payload. Stderr is
        discarded, like the loops' ``2>/dev/null``.

    No doctest — spawns a process; covered by pytest.
    """
    argv = [executable, "query", "--timeout", str(QUERY_TIMEOUT), item["cmd"], *item["args"]]
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell; module names pass _valid_module
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=QUERY_TIMEOUT + _KILL_GRACE,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.decode("utf-8", errors="replace") or None


def render(family: str, modules: list[str], batch_text: str, *, fallback: bool = False) -> str:
    """Rebuild the text the per-module loops printed from one batch result.

    Each module gets a ``### <heading>: <module>`` line, one JSON line per query that produced a result, then a blank
    line. Items are matched by position (module-major, query order), never by payload, and failed items are rendered
    too: a failing single query printed its error payload on stdout, and the batch ``result`` carries that same payload.

    Args:
        family: Key of :data:`QUERY_FAMILIES`.
        modules: The module list the request was built from.
        batch_text: Raw stdout of ``codemap-py query batch``; unusable text renders headers only.
        fallback: Rerun each query the batch left unanswered as a single ``codemap-py`` call and stage its raw stdout
            at that position. Does nothing when the batch answered everything or ``codemap-py`` is not on ``PATH``.

    Returns:
        The staged text, ending in a newline, or an empty string when there are no modules.

    Examples:
        >>> out = '{"batch": [{"result": {"a": 1}}, {"result": {"b": 2}}], "count": 2}'
        >>> print(render("qa", ["m"], out), end="")
        ### QA queries: m
        {"a": 1}
        {"b": 2}
        <BLANKLINE>
        >>> print(render("qa", ["m"], out, fallback=True), end="")  # complete batch: nothing to rerun
        ### QA queries: m
        {"a": 1}
        {"b": 2}
        <BLANKLINE>
        >>> print(render("docs", ["m"], ""), end="")
        ### Docs queries: m
        <BLANKLINE>
        >>> render("qa", [], out)
        ''
    """
    request = build_request(family, modules)
    texts = _batch_texts(_batch_items(batch_text), len(request))
    executable = shutil.which("codemap-py") if fallback and None in texts else None
    if executable is not None:
        texts = [text if text is not None else _single_query(executable, item) for text, item in zip(texts, request)]
    width = len(QUERY_FAMILIES[family])
    parts: list[str] = []
    for index, module in enumerate(modules):
        parts.append(f"### {HEADINGS[family]}: {module}\n")
        parts.extend(text for text in texts[index * width : (index + 1) * width] if text)
        parts.append("\n")
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    """Run the ``build`` or ``render`` subcommand.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` always once arguments parse; argparse exits ``2`` on a usage error.

    No doctest — reads stdin and writes raw bytes; covered by pytest.
    """
    parser = argparse.ArgumentParser(
        prog="codemap_batch.py",
        description="Build or render the batched per-module codemap-py supplement queries of oss:review.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("build", "Print the codemap-py query batch request JSON for the family."),
        ("render", "Read the batch result from stdin and print the staged per-module text."),
    ):
        command = sub.add_parser(name, help=text, description=text)
        if name == "render":
            command.add_argument(
                "--fallback",
                action="store_true",
                help="Rerun each query the batch left unanswered as a single 'codemap-py query --timeout 5' call.",
            )
        command.add_argument("family", choices=sorted(QUERY_FAMILIES), help="Query family.")
        command.add_argument("modules_file", type=Path, help="File with one dotted module name per line.")
    args = parser.parse_args(argv)
    modules = load_modules(args.modules_file)
    if args.command == "build":
        output = json.dumps(build_request(args.family, modules))
    else:
        batch_text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        output = render(args.family, modules, batch_text, fallback=args.fallback)
    # Bytes, not text mode: Windows text mode would turn the staged file's newlines into CRLF.
    sys.stdout.buffer.write(output.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
