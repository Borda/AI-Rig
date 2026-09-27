"""Parse the query command line and dispatch one command to its handler."""

from __future__ import annotations
import argparse
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from codemap_py import index_paths, query_state as state

# Transitional seam: exclusion rules live in codemap_py.scanner, but this
# module still reaches them through the old bare-name ``_exclusions`` import
# (bin/_exclusions.py, itself a shim onto codemap_py.scanner) via a
# bin/-relative sys.path insert, the same route bin/scan-index used to take.
# Every other import below is a direct package-internal import.
# parents[3] not [2]: this file sits one level deeper than the pre-split query.py
_BIN = Path(__file__).resolve().parents[3] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))
from codemap_py.schema import (  # noqa: E402
    EntityType,
)
from codemap_py.telemetry import CliInvocation  # noqa: E402
from .callgraph import cmd_fn_blast, cmd_fn_central, cmd_fn_deps, cmd_fn_rdeps, cmd_mock_rdeps, cmd_test_impact  # noqa: E402
from .diff_batch import cmd_batch, cmd_diff_impact  # noqa: E402
from .docs_coverage import UncoveredSort, cmd_coverage, cmd_coverage_gap, cmd_uncovered, cmd_undocumented  # noqa: E402
from .errors import _EXIT_BAD_INPUT, _die_json  # noqa: E402
from .index_io import (  # noqa: E402
    _GIT_TIMEOUT_S,
    _autobuild_disabled,
    _detect_root_mismatch,
    _load_index_leased,
    _resolve_project_root,
    find_index,
    maybe_self_heal,
    warn_if_stale,
)
from .modules import (  # noqa: E402
    _as_entity,
    _as_module_list,
    cmd_central,
    cmd_coupled,
    cmd_deps,
    cmd_import_types,
    cmd_list,
    cmd_packages,
    cmd_path,
    cmd_rdeps,
)
from .output import _print  # noqa: E402
from .subprocess_fixtures import cmd_fixture_graph, cmd_fixture_rdeps, cmd_subprocess_deps, cmd_subprocess_rdeps  # noqa: E402
from .symbols import _reject_multiline_args, cmd_find_symbol, cmd_symbol, cmd_symbols  # noqa: E402
from .xrefs_dead import cmd_dead_modules, cmd_dead_symbols, cmd_xrefs  # noqa: E402


def _add_module_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register module-level query subcommands: deps, rdeps, central, coupled, path, list, packages."""
    p_deps = sub.add_parser("deps", help="What does a module import?")
    p_deps.add_argument("module")
    p_deps.add_argument(
        "--stdlib",
        action="store_true",
        default=False,
        help="Restrict to stdlib imports only (requires v4.3+ index).",
    )
    p_deps.add_argument(
        "--third-party",
        action="store_true",
        default=False,
        help="Restrict to third-party imports only (requires v4.3+ index).",
    )
    p_deps.add_argument(
        "--internal",
        action="store_true",
        default=False,
        help="Restrict to internal (project-owned) imports only (requires v4.3+ index).",
    )

    p_rdeps = sub.add_parser("rdeps", help="What imports a module?")
    p_rdeps.add_argument("module")
    p_rdeps.add_argument("--exclude-tests", action="store_true", default=False, help="Exclude test files from results")
    p_rdeps.add_argument(
        "--entity",
        default=None,
        choices=[e.value for e in EntityType],
        help="Restrict importers to this entity type (requires v5.5+ index for docs/example).",
    )
    p_rdeps.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Preview at most N static importers; 0 returns every importer (default).",
    )

    p_central = sub.add_parser("central", help="Most-imported modules (highest blast radius).")
    p_central.add_argument("--top", type=int, default=None, metavar="N", help="Default: 10, or every --among module.")
    p_central.add_argument(
        "--exclude-tests", action="store_true", default=False, help="Exclude test files from results"
    )
    p_central.add_argument(
        "--entity",
        default=None,
        choices=[e.value for e in EntityType],
        help="Restrict to this entity type (requires v5.5+ index for docs/example).",
    )
    p_central.add_argument(
        "--among",
        default=None,
        metavar="MODULES",
        help="Comma-separated dotted modules; rank only these. Names matching no candidate return as 'unmatched'.",
    )

    coupled_help = "Modules ranked by internal import count (highest coupling)."
    p_coupled = sub.add_parser("coupled", help=coupled_help, description=coupled_help)
    p_coupled.add_argument("--top", type=int, default=10, metavar="N")
    p_coupled.add_argument(
        "--exclude-tests", action="store_true", default=False, help="Exclude test files from results"
    )
    p_coupled.add_argument(
        "--entity",
        default=None,
        choices=[e.value for e in EntityType],
        help="Restrict to this entity type (requires v5.5+ index for docs/example).",
    )

    p_path = sub.add_parser("path", help="Shortest import path between two modules.")
    p_path.add_argument("frm", metavar="from")
    p_path.add_argument("to")

    p_list = sub.add_parser("list", help="List all indexed modules.")
    p_list.add_argument(
        "--limit", type=int, default=100, metavar="N", help="Max modules to return (default 100). Use 0 for all."
    )

    sub.add_parser(
        "packages",
        help="Top-level packages with module/test/docs/example counts (requires v5.5+ index for docs/example).",
    )


def _add_symbol_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register symbol-level query subcommands: symbol, symbols, find-symbol."""
    p_symbol = sub.add_parser("symbol", help="Get source of a symbol by name (function/class/method).")
    p_symbol.add_argument("name", help="Symbol name, e.g. 'authenticate' or 'MyClass.method'")
    p_symbol.add_argument(
        "--limit", type=int, default=20, metavar="N", help="Max results (default 20). Use 0 for unlimited."
    )
    p_symbol.add_argument("--exclude-tests", action="store_true", default=False, help="Exclude test files from results")
    p_symbol.add_argument(
        "--with-imports",
        action="store_true",
        default=False,
        help="Include module-level import block alongside each symbol's source.",
    )

    p_symbols = sub.add_parser("symbols", help="List all symbols in a module.")
    p_symbols.add_argument("module", help="Dotted module name, e.g. 'mypackage.auth'")

    p_find = sub.add_parser("find-symbol", help="Regex search across all symbol names.")
    p_find.add_argument("pattern", help="Python regex pattern, e.g. 'auth' or '^My.*Handler$'")
    p_find.add_argument(
        "--limit", type=int, default=20, metavar="N", help="Max results (default 20). Use 0 for unlimited."
    )
    p_find.add_argument("--exclude-tests", action="store_true", default=False, help="Exclude test files from results")


def _add_callgraph_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register call-graph subcommands that require a version 3 or newer index.

    Registers ``fn-deps``, ``fn-rdeps``, ``fn-central``, ``fn-blast``, ``test-impact``, and ``mock-rdeps``.
    """
    p_fn_deps = sub.add_parser("fn-deps", help="What does a function call? (requires v3 index)")
    p_fn_deps.add_argument("qname", help="Full qname: module::symbol, e.g. 'mypackage.auth::validate_token'")

    p_fn_rdeps = sub.add_parser("fn-rdeps", help="What calls a function? (requires v3 index)")
    p_fn_rdeps.add_argument("qname", help="Full qname: module::symbol")
    p_fn_rdeps.add_argument(
        "--exclude-tests", action="store_true", default=False, help="Exclude test files from results"
    )

    p_fn_central = sub.add_parser("fn-central", help="Most-called functions globally (requires v3 index)")
    p_fn_central.add_argument("--top", type=int, default=10, metavar="N")
    p_fn_central.add_argument(
        "--exclude-tests", action="store_true", default=False, help="Exclude test files from results"
    )

    p_fn_blast = sub.add_parser("fn-blast", help="Transitive reverse-call blast radius (requires v3 index)")
    p_fn_blast.add_argument("qname", help="Full qname: module::symbol")

    p_test_impact = sub.add_parser(
        "test-impact",
        help="Which tests are affected by changing a function or module? (requires v3+ index)",
    )
    p_test_impact.add_argument(
        "qname",
        help="module::symbol for function-level impact, or bare module name for module-level impact.",
    )
    p_test_impact.add_argument(
        "--no-mocks",
        dest="include_mocks",
        action="store_false",
        default=True,
        help="Exclude tests that only mock qname (no call/import path) from results.",
    )

    p_mock_rdeps = sub.add_parser(
        "mock-rdeps",
        help="Test files that mock a symbol via patch() (requires v4.1+ index)",
    )
    p_mock_rdeps.add_argument(
        "query",
        help="Full qname (module::symbol) for one symbol, or bare module for all mocked symbols in that module",
    )


def _add_subprocess_fixture_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register subprocess/fixture/import-group subcommands (require v4.3+/v5.2+/v5.3+ index)."""
    p_sub_deps = sub.add_parser(
        "subprocess-deps",
        help="What does this module spawn as a subprocess? (requires v5.2+ index)",
    )
    p_sub_deps.add_argument("module", help="Dotted module name whose subprocess calls are listed.")

    p_sub_rdeps = sub.add_parser(
        "subprocess-rdeps",
        help="What modules spawn this module as a subprocess? (requires v5.2+ index)",
    )
    p_sub_rdeps.add_argument("module", help="Dotted module name whose subprocess callers are listed.")

    p_fix_rdeps = sub.add_parser(
        "fixture-rdeps",
        help="Test files that use a pytest fixture (requires v5.3+ index).",
    )
    p_fix_rdeps.add_argument("fixture_name", help="Fixture name whose reverse-dependencies are queried.")

    p_fix_graph = sub.add_parser(
        "fixture-graph",
        help="Full pytest fixture dependency tree for a test file (requires v5.3+ index).",
    )
    p_fix_graph.add_argument(
        "test_file",
        help="Dotted test-module name (tests.foo) or path (tests/foo.py).",
    )

    p_import_types = sub.add_parser(
        "import-types",
        help="Return stdlib/third_party/internal import groups for a module (requires v4.3+ index).",
    )
    p_import_types.add_argument("module", help="Dotted module name, e.g. 'mypackage.auth'")


def _add_docs_coverage_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register docstring/test/line-coverage subcommands: undocumented, uncovered, coverage, coverage-gap."""
    p_undoc = sub.add_parser(
        "undocumented",
        help="List public symbols missing a docstring, sorted by LOC desc (requires v4.4+ index).",
    )
    p_undoc.add_argument(
        "module",
        nargs="?",
        default=None,
        help="Dotted module name to scan (omit and pass --all to scan every non-test module).",
    )
    p_undoc.add_argument(
        "--all",
        dest="all_modules",
        action="store_true",
        default=False,
        help="Scan all non-test modules in the index.",
    )

    p_uncov = sub.add_parser(
        "uncovered",
        help="Public symbols with no test callers and no mocks (requires v4.2+ index).",
    )
    p_uncov.add_argument(
        "module",
        nargs="?",
        default=None,
        help="Dotted module name to scan (omit and pass --all to scan every non-test module).",
    )
    p_uncov.add_argument(
        "--all",
        dest="all_modules",
        action="store_true",
        default=False,
        help="Scan all non-test modules in the index.",
    )
    p_uncov.add_argument(
        "--sort",
        choices=[k.value for k in UncoveredSort],
        default=UncoveredSort.LOC.value,
        help="Sort order: loc (default — biggest first), name (alphabetical), module (group by module).",
    )
    p_uncov.add_argument(
        "--top",
        type=int,
        default=20,
        metavar="N",
        help="Cap output to top N results (default 20).",
    )

    p_coverage = sub.add_parser(
        "coverage",
        help="Show coverage_pct and covered_by for a symbol or whole module (requires v5.4+ index).",
    )
    p_coverage.add_argument(
        "qname",
        help="Full qname (module::symbol) for one symbol, or bare module for every symbol in the module.",
    )

    p_cov_gap = sub.add_parser(
        "coverage-gap",
        help="Public symbols with coverage_pct below --threshold, sorted by gap desc (requires v5.4+ index).",
    )
    p_cov_gap.add_argument(
        "module",
        nargs="?",
        default=None,
        help="Dotted module name to scan (omit and pass --all to scan every non-test module).",
    )
    p_cov_gap.add_argument(
        "--all",
        dest="all_modules",
        action="store_true",
        default=False,
        help="Scan all non-test modules in the index.",
    )
    p_cov_gap.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        metavar="P",
        help="Coverage fraction (0.0–1.0) below which a symbol is reported (default 0.8).",
    )


def _add_xref_dead_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register doc-xref/dead-code subcommands: xrefs, dead-symbols, dead-modules."""
    p_xrefs = sub.add_parser(
        "xrefs",
        help="List doc cross-references for a symbol, or find broken refs (requires v4.5+ index).",
    )
    p_xrefs.add_argument(
        "query",
        help="Symbol qname (default mode) or module name (with --broken).",
    )
    p_xrefs.add_argument(
        "--broken",
        action="store_true",
        default=False,
        help="Find xrefs whose resolved target is not a known symbol in the index.",
    )

    p_dead_syms = sub.add_parser(
        "dead-symbols",
        help="Public symbols with zero callers anywhere (requires v4.6+ index).",
    )
    p_dead_syms.add_argument(
        "--min-loc",
        type=int,
        default=5,
        metavar="N",
        help="Skip symbols spanning fewer than N lines (default 5 — drops trivial properties).",
    )

    sub.add_parser(
        "dead-modules",
        help="Modules with zero external importers (requires v4.6+ index).",
    )


def _add_composite_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register composite subcommands that run their own in-process sub-queries: diff-impact, batch."""
    p_diff_impact = sub.add_parser(
        "diff-impact",
        help="Structural blast radius of the git change set: per-module rdeps/coupled, "
        "per-symbol fn-rdeps, unioned test-impact, risk tiers (requires v3+ index).",
    )
    p_diff_impact.add_argument(
        "--base",
        default="HEAD",
        metavar="REF",
        help="Git ref or range to diff against (default HEAD — staged + unstaged working-tree changes).",
    )
    p_diff_impact.add_argument(
        "--diff-file",
        default=None,
        metavar="PATH",
        help="Read the change set from a unified-diff file (e.g. `gh pr diff` output) instead of "
        "local git — for PR review where the change is not in the local object store. '-' reads stdin.",
    )

    p_batch = sub.add_parser(
        "batch",
        help="Run many queries in one process, sharing one coverage block (reads a JSON array).",
    )
    p_batch.add_argument(
        "input",
        nargs="?",
        default="-",
        help="A JSON array of [{cmd, args}] objects, a path to a file holding one, or '-' for stdin (default).",
    )


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    """Register flags shared by every query subcommand."""
    parser.add_argument("--index", metavar="PATH", help="Explicit path to the index JSON (auto-discovered if omitted).")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override project root for FILE-PATH RESOLUTION ONLY (does not re-scan or re-target the index; "
        "highest priority, supersedes scan_root and git root). Disagreeing with the index's scan_root flags "
        "root_mismatch and forces query_complete=false.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        metavar="N",
        help="Hard timeout in seconds; 0 = no limit (default). Uses SIGALRM — Unix only.",
    )
    parser.add_argument(
        "--no-heal",
        action="store_true",
        default=False,
        help="Disable the bounded incremental self-heal on a stale index (answer from the stale index as-is).",
    )
    parser.add_argument(
        "--verbose-coverage",
        action="store_true",
        default=False,
        help="Always emit the full coverage block, even after the first query of a session (disables the diet).",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="Emit compact coverage metadata and bounded alias-limitation evidence.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "tsv"),
        default="json",
        dest="output_format",
        help=(
            "Result encoding. json (default) is lossless. tsv writes table rows to stdout and the "
            "metadata envelope to stderr, and is refused for results that are not a single flat table."
        ),
    )


class _ScanQueryArgumentParser(argparse.ArgumentParser):
    """Preserve argparse failures while guiding common invalid scan-query commands."""

    def error(self, message: str) -> None:
        """Append one explicit migration hint to a known invalid subcommand error."""
        match = re.match(r"argument command: invalid choice: '([^']+)'", message)
        suggestion = ""
        if match:
            command = match.group(1)
            if command == "search":
                suggestion = "use 'find-symbol' to search symbols."
            elif command in {"callers", "find-references"}:
                suggestion = "use 'fn-rdeps' for function callers."
            elif command == "imports":
                suggestion = "use 'rdeps' for importers or 'deps' for imports."
            elif command == "help":
                suggestion = "use '--help' to list commands."
        if suggestion:
            message = f"{message}\n\nHint: {suggestion}"
        if state._invocation is not None and state._capture is None:
            state._invocation.result = {"error": "invalid_arguments", "detail": message}
        super().error(message)


def _build_parser() -> argparse.ArgumentParser:
    """Build the scan-query argument parser: every subcommand plus the shared global flags."""
    parser = _ScanQueryArgumentParser(
        description="Query the codemap structural index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_module_subparsers(sub)
    _add_symbol_subparsers(sub)
    _add_callgraph_subparsers(sub)
    _add_subprocess_fixture_subparsers(sub)
    _add_docs_coverage_subparsers(sub)
    _add_xref_dead_subparsers(sub)
    _add_composite_subparsers(sub)
    _add_global_flags(parser)
    return parser


def _resolve_index_path(args: argparse.Namespace) -> Path:
    """Resolve the index JSON path from ``--index``, else auto-discover it.

    An explicit ``--index`` is guarded against path traversal — it must resolve
    inside the CWD, git root, or an exact resolver-selected target for an
    explicit ``--root`` before being trusted.

    Args:
        args: parsed top-level namespace (``args.index`` may be ``None``).
    """
    if not args.index:
        return find_index()
    resolved = Path(args.index).resolve()
    cwd = Path.cwd().resolve()
    try:
        git_root = Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_S,
            ).strip()
        ).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        git_root = cwd
    configured_index = (
        index_paths.resolve_index(root=args.root).index_path if os.environ.get("CODEMAP_INDEX_DIR") else None
    )
    default_root_index = (
        index_paths.resolve_index(root=args.root, index_dir_override=None).index_path if args.root is not None else None
    )
    if not (
        resolved.is_relative_to(cwd)
        or resolved.is_relative_to(git_root)
        or resolved == configured_index
        or resolved == default_root_index
    ):
        # emit a parseable JSON error (not just a bare stderr + exit) so a
        # caller sees the guard rejection in the same channel as every other failure.
        _print(f"scan-query: --index path outside project root: {resolved}", file=sys.stderr)
        _die_json({"error": "index path outside project root", "path": str(resolved)}, _EXIT_BAD_INPUT)
    return resolved


def main(argv: Sequence[str] | None = None) -> None:
    """Run one query attempt with terminal telemetry and restore invocation-local state."""
    previous, previous_command = state._invocation, state._CMD
    arguments = list(sys.argv[1:] if argv is None else argv)
    previous_alarm = signal.getsignal(signal.SIGALRM) if hasattr(signal, "SIGALRM") else None
    previous_timer = signal.getitimer(signal.ITIMER_REAL) if hasattr(signal, "getitimer") else (0.0, 0.0)
    timer_started = time.monotonic()
    with CliInvocation("query", arguments) as invocation:
        # Preserve the last complete root even if a later root option is malformed.
        root_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
        root_parser.add_argument("--root", type=Path)
        root_args = argparse.Namespace(root=None)
        try:
            root_parser.parse_known_args(arguments, namespace=root_args)
        except argparse.ArgumentError:
            pass  # The full parser owns malformed-root diagnostics and exit status.
        invocation.root = root_args.root
        state._invocation, state._CMD = invocation, ""
        try:
            _run_query(arguments)
        finally:
            if hasattr(signal, "SIGALRM") and signal.getsignal(signal.SIGALRM) != previous_alarm:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous_alarm)
                if previous_timer[0] > 0:
                    # Restore the caller's deadline, not a fresh full-duration timer.
                    remaining = max(1e-6, previous_timer[0] - (time.monotonic() - timer_started))
                    signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])
            state._invocation, state._CMD = previous, previous_command


def _run_query(argv: Sequence[str]) -> None:
    """Parse CLI arguments, load the index, and dispatch to the appropriate command.

    Args:
        argv: Explicit argument vector excluding the program name, from the script
            or in-process dispatcher. The engine owns its read lease.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "rdeps" and args.limit < 0:
        parser.error("rdeps --limit must be 0 or a positive integer")
    state._CMD = args.command
    if state._invocation is not None:
        state._invocation.command = args.command
        state._invocation.root = args.root
    state._verbose_coverage = args.verbose_coverage
    state._force_compact_coverage = args.compact
    state._FORMAT = args.output_format
    _reject_multiline_args(args)

    if args.timeout > 0 and hasattr(signal, "SIGALRM"):

        def _timeout_handler(signum: int, frame: object) -> None:  # noqa: ARG001
            """Retain timeout evidence while preserving the CLI's existing exit contract."""
            if state._invocation is not None:
                state._invocation.result = {"error": "timeout", "timeout_seconds": args.timeout}
            _print(f"scan-query: timed out after {args.timeout}s", file=sys.stderr)
            sys.exit(2)

        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(args.timeout)

    index_path = _resolve_index_path(args)
    index = _load_index_leased(index_path)
    if state._invocation is not None:
        state._invocation.root = _resolve_project_root(args.root, index)
    if not _autobuild_disabled() and not args.no_heal:
        # refresh a stale index inline (bounded) so the answer reflects the
        # current tree — e.g. an edge added by a just-committed change is visible.
        index = maybe_self_heal(index, index_path, _resolve_project_root(args.root, index))
    warn_if_stale(index)
    project_root = _resolve_project_root(args.root, index)

    # flag a query resolved against a different tree than the index was built for.
    # Set before any command runs so _coverage picks it up; also warn on stderr so a
    # human sees it even if they ignore the coverage block. query_complete is forced
    # false downstream in _query_complete.
    state._root_mismatch = _detect_root_mismatch(args.root, index)
    if state._root_mismatch:
        _print(
            f"⚠ codemap: index scan_root ({index.get('scan_root')}) differs from queried root "
            f"({project_root}) — result describes a different project; re-scan or pass a matching --root.",
            file=sys.stderr,
        )

    # batch and diff-impact both need the top-level parser to run their own in-process
    # sub-queries, so they route here rather than through _dispatch_command (which the
    # sub-queries themselves use). Neither may nest inside batch — see _batch_item_argv.
    if args.command == "batch":
        cmd_batch(index, args, parser, project_root)
    elif args.command == "diff-impact":
        cmd_diff_impact(index, args, parser, project_root)
    else:
        _dispatch_command(index, args, parser, project_root)


# Command → handler lookup for _dispatch_command. Each value takes the same
# (index, args, project_root) triple and extracts whatever it needs from
# args — a dict dispatch keeps _dispatch_command itself to one lookup + one
# call regardless of how many subcommands exist, instead of an ever-growing
# if/elif chain. project_root is unused by most handlers; args is passed
# through whole to the two (uncovered, dead-symbols/-modules) that take the
# full namespace rather than individual fields.
_COMMAND_HANDLERS: dict[str, Callable[[dict, argparse.Namespace, Path], None]] = {
    "deps": lambda i, a, r: cmd_deps(  # noqa: ARG005 (r unused — shared handler signature)
        i, a.module, stdlib_only=a.stdlib, third_party_only=a.third_party, internal_only=a.internal
    ),
    "rdeps": lambda i, a, r: cmd_rdeps(  # noqa: ARG005
        i, a.module, exclude_tests=a.exclude_tests, entity=_as_entity(a.entity), limit=a.limit
    ),
    "central": lambda i, a, r: cmd_central(  # noqa: ARG005
        i, a.top, exclude_tests=a.exclude_tests, entity=_as_entity(a.entity), among=_as_module_list(a.among)
    ),
    "coupled": lambda i, a, r: cmd_coupled(i, a.top, exclude_tests=a.exclude_tests, entity=_as_entity(a.entity)),  # noqa: ARG005
    "path": lambda i, a, r: cmd_path(i, a.frm, a.to),  # noqa: ARG005
    "list": lambda i, a, r: cmd_list(i, limit=a.limit),  # noqa: ARG005
    "packages": lambda i, a, r: cmd_packages(i),  # noqa: ARG005
    "symbol": lambda i, a, r: cmd_symbol(
        i, a.name, a.limit, exclude_tests=a.exclude_tests, with_imports=a.with_imports, project_root=r
    ),
    "symbols": lambda i, a, r: cmd_symbols(i, a.module),  # noqa: ARG005
    "find-symbol": lambda i, a, r: cmd_find_symbol(i, a.pattern, a.limit, exclude_tests=a.exclude_tests),  # noqa: ARG005
    "fn-deps": lambda i, a, r: cmd_fn_deps(i, a.qname),  # noqa: ARG005
    "fn-rdeps": lambda i, a, r: cmd_fn_rdeps(i, a.qname, exclude_tests=a.exclude_tests),  # noqa: ARG005
    "fn-central": lambda i, a, r: cmd_fn_central(i, a.top, exclude_tests=a.exclude_tests),  # noqa: ARG005
    "fn-blast": lambda i, a, r: cmd_fn_blast(i, a.qname),  # noqa: ARG005
    "test-impact": lambda i, a, r: cmd_test_impact(i, a.qname, include_mocks=a.include_mocks),  # noqa: ARG005
    "mock-rdeps": lambda i, a, r: cmd_mock_rdeps(i, a.query),  # noqa: ARG005
    "subprocess-deps": lambda i, a, r: cmd_subprocess_deps(i, a.module),  # noqa: ARG005
    "subprocess-rdeps": lambda i, a, r: cmd_subprocess_rdeps(i, a.module),  # noqa: ARG005
    "fixture-rdeps": lambda i, a, r: cmd_fixture_rdeps(i, a.fixture_name),  # noqa: ARG005
    "fixture-graph": lambda i, a, r: cmd_fixture_graph(i, a.test_file),  # noqa: ARG005
    "import-types": lambda i, a, r: cmd_import_types(i, a.module),  # noqa: ARG005
    "undocumented": lambda i, a, r: cmd_undocumented(i, a.module, all_modules=a.all_modules),  # noqa: ARG005
    "uncovered": lambda i, a, r: cmd_uncovered(i, a),  # noqa: ARG005
    "coverage": lambda i, a, r: cmd_coverage(i, a.qname),  # noqa: ARG005
    "coverage-gap": lambda i, a, r: cmd_coverage_gap(i, a.module, all_modules=a.all_modules, threshold=a.threshold),  # noqa: ARG005
    "xrefs": lambda i, a, r: cmd_xrefs(i, a.query, broken=a.broken),  # noqa: ARG005
    "dead-symbols": lambda i, a, r: cmd_dead_symbols(i, a),  # noqa: ARG005
    "dead-modules": lambda i, a, r: cmd_dead_modules(i, a),  # noqa: ARG005
}


def _dispatch_command(
    index: dict, args: argparse.Namespace, parser: argparse.ArgumentParser, project_root: Path
) -> None:
    """Route a parsed ``args`` namespace to its command handler.

    Shared by :func:`main` and :func:`cmd_batch` so a batched item runs through the
    exact same code path as a standalone invocation. ``batch`` is intentionally not
    routed here — nesting a batch inside a batch is rejected by :func:`cmd_batch`.
    Dispatch is a plain dict lookup (:data:`_COMMAND_HANDLERS`) rather than an
    if/elif chain, so adding a subcommand never grows this function's complexity.

    Args:
        index: parsed codemap index dict.
        args: the argparse namespace for a single command (``args.command`` set).
        parser: the top-level parser (unused here; kept for signature symmetry with
            :func:`cmd_batch`, which needs it to re-parse item argv).
        project_root: resolved project root for file-path lookups.
    """
    del parser  # symmetry only; _dispatch_command re-parses nothing
    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is not None:
        handler(index, args, project_root)
