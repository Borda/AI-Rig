"""Parse the ``integrate`` command line and dispatch one mode."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from .apply_sync import cmd_apply, cmd_sync
from .audit import cmd_audit
from .demo import cmd_demo
from .plan import cmd_plan
from .types import _EXIT_USAGE, IntegrationError, Runtime, Source


def _add_runtime_flag(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--runtime", choices=[r.value for r in Runtime], default=Runtime.BOTH.value)


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated option while preserving order and dropping empty items.

    Examples:
        >>> _split_csv('claude,codex')
        ['claude', 'codex']
        >>> _split_csv('claude,,codex,')
        ['claude', 'codex']
        >>> _split_csv('')
        []
    """
    return [item for item in value.split(",") if item]


def _parse_since(value: str) -> date:
    """Parse an ISO calendar date for the inclusive telemetry evidence window."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--since must use YYYY-MM-DD") from exc


def _add_audit_parser(subparsers: argparse._SubParsersAction) -> None:
    sub = subparsers.add_parser("audit")
    _add_runtime_flag(sub)
    sub.add_argument("--json", action="store_true")
    sub.add_argument("--since", type=_parse_since, default=None)


def _add_plan_parser(subparsers: argparse._SubParsersAction) -> None:
    sub = subparsers.add_parser("plan")
    _add_runtime_flag(sub)
    sub.add_argument("--consumers", type=_split_csv, default=None)
    sub.add_argument("--source", choices=[x.value for x in Source], default=None)
    sub.add_argument("--out", default=None)


def _add_apply_parser(subparsers: argparse._SubParsersAction) -> None:
    sub = subparsers.add_parser("apply")
    sub.add_argument("--plan", required=True)
    sub.add_argument("--approve", required=True)


def _add_sync_parser(subparsers: argparse._SubParsersAction) -> None:
    sub = subparsers.add_parser("sync")
    sub.add_argument("--source", choices=[x.value for x in Source], required=True)
    sub.add_argument("--plan", required=True)
    sub.add_argument("--approve", required=True)
    _add_runtime_flag(sub)


def _add_demo_parser(subparsers: argparse._SubParsersAction) -> None:
    sub = subparsers.add_parser("demo")
    _add_runtime_flag(sub)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codemap-py integrate", description="Manage codemap-py's cross-runtime integration state."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    _add_audit_parser(subparsers)
    _add_plan_parser(subparsers)
    _add_apply_parser(subparsers)
    _add_sync_parser(subparsers)
    _add_demo_parser(subparsers)
    return parser


#: Dispatch table from integrate subcommand name to its handler, which returns an exit code.
_COMMANDS: dict[str, Callable[[argparse.Namespace, Path], int]] = {
    "audit": cmd_audit,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "sync": cmd_sync,
    "demo": cmd_demo,
}


def _emit_bounded_error(exc: IntegrationError) -> int:
    payload: dict[str, object] = {"error": exc.code, "detail": str(exc)}
    if exc.detail:
        payload["context"] = exc.detail
    sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")
    return exc.exit_code


def run(argv: Sequence[str], plugin_root: Path) -> int:
    """Dispatch ``codemap-py integrate <mode>``.

    Sole CLI boundary for the integration engine: argparse usage errors already exit ``2``
    with their own bounded stderr message; every other failure is caught here and turned
    into one bounded JSON stderr line — never a bare traceback.

    Args:
        argv: Arguments after ``integrate`` (e.g. ``["audit", "--json"]``).
        plugin_root: codemap-py's own resolved plugin root, passed through unchanged from
            :func:`codemap_py.cli.main`.

    Returns:
        Process exit code: ``0`` success, ``1`` runtime/domain/refusal failure, ``2`` bad
        syntax or a failed/invalid approval.
    """
    try:
        namespace = _build_parser().parse_args(list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else _EXIT_USAGE
    try:
        return _COMMANDS[namespace.mode](namespace, plugin_root)
    except IntegrationError as exc:
        return _emit_bounded_error(exc)
    except Exception as exc:
        return _emit_bounded_error(IntegrationError("internal_error", f"{type(exc).__name__}: {exc}"))
