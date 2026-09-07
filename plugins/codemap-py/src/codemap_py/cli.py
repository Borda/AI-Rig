#!/usr/bin/env python3
"""codemap_py.cli — codemap-py CLI dispatcher.

Dispatches ``codemap-py {index,query,doctor}`` to the current ``bin/`` executables using argument arrays with
``shell=False``, resolving the index identity through the single index resolver
(:func:`codemap_py.index_paths.resolve_index`). It also owns the interpreter probe. No general shell-command mode
exists.

Every public read/update enters the shared-index RW gate, but this dispatcher is not where that happens: the engines
lease themselves — :func:`codemap_py.query.main` takes a shared read lease around each index load,
:func:`codemap_py.graph.main` an exclusive writer lease around build and publish. Gating there rather than here covers
every entry point (this dispatcher, the ``bin/scan-query`` and ``bin/scan-index`` launchers, the query engine's
automatic repair spawn, and a hook's background refresh) instead of only this one. This placement also prevents a lock
order inversion between a parent and a child process that needs its own lease.

``scripts/codemap_py_cli.py`` is a compatibility shim that aliases this module in ``sys.modules``, replacing the
transitional ``bin/`` path-insertion helper with direct imports of :mod:`codemap_py.index_paths` and
:mod:`codemap_py.rwgate`. ``query`` calls :func:`codemap_py.query.main` directly in-process under the shared read lease.
``index`` still shells out to ``bin/scan-index`` as a subprocess — a thin launcher over :func:`codemap_py.graph.main` —
rather than calling it in-process like ``query`` does.

Exit codes: ``0`` success, ``1`` runtime/index failure (bounded structured stderr — ``index_busy`` /
``index_coordination_unavailable``), ``2`` invalid syntax, ``127`` no eligible CPython interpreter (including an invalid
authoritative ``CODEMAP_PYTHON``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from codemap_py import index_paths, integration, query

_MIN = (3, 11)
_MAX_EXCLUSIVE = (3, 15)
_PROBE_FIELDS = 3
_USAGE = "usage: codemap-py {index,query,doctor,integrate} [args...]"
_HELP = """usage: codemap-py {index,query,doctor,integrate} [args...]

Commands:
  index [args...]       Build or update the structural index.
  query [args...]       Query the structural index as JSON.
  doctor [--json]       Report the resolved interpreter and index path.
  integrate [args...]   Run a supported integration command.

Direct compact query examples:
  codemap-py query --compact rdeps mypackage.auth
  codemap-py query --compact fn-rdeps 'mypackage.auth::validate_token'
  codemap-py query --compact coupled --top 5
  codemap-py query --compact undocumented mypackage.auth
  codemap-py query --compact uncovered mypackage.auth

Run `codemap-py query --help` for every query and its arguments.
"""
_HELP_FLAGS = frozenset({"--help", "-h"})
_INDEX_HELP = """usage: codemap-py index [--root PATH] [args...]

Build or update the structural index for a project root.

  codemap-py index
  codemap-py index --root /path/to/project

Full options come from the `scan-index` launcher, which is not installed here.
"""
_PROBE_SNIPPET = "import sys;v=sys.version_info;print(sys.implementation.name,v.major,v.minor)"
_NO_INTERPRETER_EXIT = 127
_USAGE_EXIT = 2
_RUNTIME_ERROR_EXIT = 1

ProbeResult = tuple[str, int, int]
Probe = Callable[[Sequence[str]], "ProbeResult | None"]


def _default_plugin_root() -> Path:
    """Return the plugin root when the CLI is imported outside the entry.

    ``cli.py`` lives at ``<plugin-root>/src/codemap_py/cli.py`` — one level deeper than the pre-slice-1
    ``scripts/codemap_py_cli.py`` — so this walks three parents, not two, to still land on the plugin root.
    """
    return Path(__file__).resolve().parents[2]


def is_supported(impl: str, major: int, minor: int) -> bool:
    """Return whether an interpreter identity satisfies the CPython bound.

    Examples:
        >>> is_supported("cpython", 3, 12)
        True
        >>> is_supported("cpython", 3, 10)
        False
        >>> is_supported("pypy", 3, 12)
        False
    """
    return impl == "cpython" and (major, minor) >= _MIN and (major, minor) < _MAX_EXCLUSIVE


def candidate_interpreters(env: Mapping[str, str], platform: str) -> list[list[str]]:
    """Return ordered interpreter candidates for a platform.

    An authoritative ``CODEMAP_PYTHON`` override, when set, is the sole
    candidate — a present-but-invalid override must fail hard, never fall
    through to the defaults.

    Examples:
        >>> candidate_interpreters({}, "linux")
        [['python3'], ['python']]
        >>> candidate_interpreters({}, "win32")
        [['py', '-3'], ['python.exe'], ['python3.exe']]
        >>> candidate_interpreters({"CODEMAP_PYTHON": "/x/py"}, "linux")
        [['/x/py']]
    """
    override = env.get("CODEMAP_PYTHON", "").strip()
    if override:
        return [[override]]
    if platform == "win32":
        return [["py", "-3"], ["python.exe"], ["python3.exe"]]
    return [["python3"], ["python"]]


def _probe_version(executable: Sequence[str]) -> ProbeResult | None:
    """Run the version probe for a candidate; return identity or ``None``."""
    try:
        completed = subprocess.run(
            [*executable, "-c", _PROBE_SNIPPET],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    parts = completed.stdout.split()
    if len(parts) != _PROBE_FIELDS:
        return None
    impl, major, minor = parts
    if not (major.isdigit() and minor.isdigit()):
        return None
    return impl, int(major), int(minor)


def resolve_interpreter(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    probe: Probe = _probe_version,
) -> tuple[list[str] | None, str | None]:
    """Resolve the first eligible interpreter, or a hard-fail diagnostic.

    Returns:
        ``(argv, None)`` for the selected interpreter, or ``(None, diagnostic)``
        when no candidate satisfies CPython ``>=3.11,<3.15`` (including an
        invalid authoritative ``CODEMAP_PYTHON``).
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    override_present = bool(env.get("CODEMAP_PYTHON", "").strip())
    for candidate in candidate_interpreters(env, platform):
        identity = probe(candidate)
        if identity is not None and is_supported(*identity):
            return candidate, None
    scope = "authoritative CODEMAP_PYTHON" if override_present else "PATH"
    return None, f"codemap-py: no eligible CPython >=3.11,<3.15 interpreter found via {scope}"


def _emit_error(code: str, detail: str) -> int:
    """Write one bounded structured stderr line and return runtime-failure exit code ``1``."""
    sys.stderr.write(json.dumps({"error": code, "detail": detail}) + "\n")
    return _RUNTIME_ERROR_EXIT


def _child_argv(script: str, rest: Sequence[str], plugin_root: Path, root: Path) -> list[str]:
    """Build the child argv, pinning ``--root`` to the resolver's canonical root.

    Pinning ``--root`` keeps scan-index/scan-query resolution aligned with the shared-index resolver root, so both agree
    on the DEFAULT-layout index path; a user-supplied ``--root`` is honoured untouched.

    Under ``CODEMAP_INDEX_DIR`` the child no longer derives its own path either: the resolver's flat
    ``<override>/<project>.json`` is what it writes and reads. The override case used to resolve here to a subdirectory
    named for ``<root-key>`` that the child never touched, so the gate coordinated one file while the child wrote
    another.
    """
    pin = [] if "--root" in rest else ["--root", str(root)]
    return [sys.executable, str(plugin_root / "bin" / script), *pin, *rest]


def _doctor(rest: Sequence[str], plugin_root: Path) -> int:
    """Report the resolved interpreter, version, plugin root, and index path."""
    info = sys.version_info
    resolved = index_paths.resolve_index()
    report = {
        "python": sys.executable,
        "version": f"{info.major}.{info.minor}.{info.micro}",
        "implementation": sys.implementation.name,
        "supported": is_supported(sys.implementation.name, info.major, info.minor),
        "plugin_root": str(plugin_root),
        "index_path": str(resolved.index_path),
    }
    if "--json" in rest:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("\n".join(f"{key}: {report[key]}" for key in sorted(report)))
    return 0


def _query_argv(rest: Sequence[str], root: Path) -> list[str]:
    """Build the in-process argv for :func:`codemap_py.query.main`, pinning ``--root``.

    Mirrors :func:`_child_argv`'s pinning behavior (a user-supplied ``--root`` wins) without the
    ``sys.executable``/script-path prefix a subprocess argv needs.
    """
    pin = [] if "--root" in rest else ["--root", str(root)]
    return [*pin, *rest]


def _run_query(rest: Sequence[str], plugin_root: Path) -> int:
    """Run the query engine in-process; the engine owns its own read lease.

    Runs the query engine as a direct, in-process call to
    :func:`codemap_py.query.main` — same argv contract and exit-code shape a
    subprocess would give, minus the process-spawn cost per query. ``main``
    raises ``SystemExit`` on every error path (see ``codemap_py.query``'s
    ``_die_json``/``_exit_error``/``_emit_gate_error``); that is caught here and
    turned into a plain return code so this function's contract matches
    ``_run_index``'s.

    This function must NOT wrap the call in a read lease of its own. The engine
    leases each index load itself and releases it before spawning its self-heal
    writer; a lease held out here would still be held across that spawn, and the
    child writer — which cannot distinguish its parent's reader token from any
    other live reader — would wait out its full deadline and fail on every stale
    query. Leasing in the engine also means the gate covers ``bin/scan-query``
    and every other entry point, not just this dispatcher.
    """
    del plugin_root  # kept for call-site symmetry with _run_index; unused now query runs in-process
    resolved = index_paths.resolve_index()
    argv = _query_argv(rest, resolved.root)
    try:
        query.main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else _RUNTIME_ERROR_EXIT
    return 0


def _run_index(rest: Sequence[str], plugin_root: Path) -> int:
    """Run scan-index as a subprocess; the engine owns its own writer lease.

    As with :func:`_run_query`, no lease is taken here. ``scan-index`` runs :func:`codemap_py.graph.main`, which builds
    and publishes under an exclusive lease; wrapping the child in a second one from this parent process would make the
    child block on its own parent's writer intent until its deadline expired, turning every ``codemap-py index``
    invocation into ``index_busy``.
    """
    if not (plugin_root / "bin" / "scan-index").is_file():
        # A help request must never depend on the executable it documents. Forwarding is preferred when the
        # launcher exists (its help is authoritative), but an absent launcher used to make `index --help`
        # exit 1 — which also killed `index --help && query --help` chains before the second command ran.
        if any(argument in _HELP_FLAGS for argument in rest):
            sys.stdout.write(_INDEX_HELP)
            return 0
        return _emit_error("missing_executable", "scan-index")
    resolved = index_paths.resolve_index()
    argv = _child_argv("scan-index", rest, plugin_root, resolved.root)
    return subprocess.run(argv, check=False).returncode


def main(argv: Sequence[str] | None = None, plugin_root: Path | None = None) -> int:
    """Dispatch ``index``/``query``/``doctor``/``integrate``."""
    argv = sys.argv[1:] if argv is None else list(argv)
    root = _default_plugin_root() if plugin_root is None else plugin_root
    if argv in (["--help"], ["-h"]):
        sys.stdout.write(_HELP)
        return 0
    if not argv:
        sys.stderr.write(_USAGE + "\n")
        return _USAGE_EXIT
    command, rest = argv[0], argv[1:]
    if command == "doctor":
        return _doctor(rest, root)
    if command == "index":
        return _run_index(rest, root)
    if command == "query":
        return _run_query(rest, root)
    if command == "integrate":
        return integration.run(rest, root)
    sys.stderr.write(f"codemap-py: unknown command {command!r}\n{_USAGE}\n")
    return _USAGE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
