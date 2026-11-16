"""Engine ``--timeout`` deadlines give the same outcome on the SIGALRM path and on the portable path.

Every case runs a real engine (``query`` or ``graph``) in a fresh interpreter through a small harness. The portable path
is forced on any host by deleting ``signal.SIGALRM`` before the engine is imported, because the deadline selector reads
the attribute at call time; on hosts without SIGALRM (native Windows) the portable cases are the real thing. Portable
cases therefore carry no platform skip. The slow workload is a real competing writer lease: the engine waits in the
gate's poll loop, and the gate's own wait (5s) outlasts the deadline (1s), so only a working deadline produces exit 2
before the gate would give up with ``index_busy`` (exit 1).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from codemap_py import rwgate

_GATE_WAIT_SECONDS = 5.0
_skip_no_sigalrm = pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="The alarm path needs SIGALRM.")
_MECHANISMS = [
    pytest.param(False, id="sigalrm", marks=_skip_no_sigalrm),
    pytest.param(True, id="portable"),
]
_ENGINES = [
    pytest.param("query", "scan-query", ["--no-heal", "central"], id="query"),
    pytest.param("graph", "scan-index", [], id="index"),
]

_HARNESS = """\
import signal, sys
sys.path.insert(0, {src!r})
if {portable!r} and hasattr(signal, "SIGALRM"):
    del signal.SIGALRM
from codemap_py import {module}
{patch}
sys.argv = [{prog!r}, *{args!r}]
try:
    {module}.main({main_kwargs})
except KeyboardInterrupt:
    sys.exit(130)
"""


def _engine_env(tmp_path: Path) -> dict[str, str]:
    """Return an environment with telemetry routed to ``tmp_path/logs`` and a gate wait longer than the deadline."""
    inherited = {k: v for k, v in os.environ.items() if k not in {"CODEMAP_LOG_DIR", "CODEMAP_INDEX_DIR"}}
    return {
        **inherited,
        "CODEMAP_LOGGING": "true",
        "CODEMAP_LOG_DIR": str(tmp_path / "logs"),
        "CODEMAP_GATE_TIMEOUT": str(_GATE_WAIT_SECONDS),
        "TMPDIR": str(tmp_path),
    }


def _run_engine(
    src: Path,
    module: str,
    prog: str,
    args: list[str],
    *,
    portable: bool,
    cwd: Path,
    env: dict[str, str],
    patch: str = "",
    main_kwargs: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run one engine attempt through the harness and capture its text output."""
    code = _HARNESS.format(
        src=str(src), portable=portable, module=module, patch=patch, prog=prog, args=args, main_kwargs=main_kwargs
    )
    return subprocess.run(
        [sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, timeout=30, check=False
    )


def _records(tmp_path: Path) -> list[dict]:
    """Return every CLI telemetry record written beneath ``tmp_path/logs``."""
    return [
        json.loads(line)
        for path in (tmp_path / "logs").rglob("cli*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _run_while_contended(
    index: Path, run: Callable[[], subprocess.CompletedProcess[str]]
) -> tuple[subprocess.CompletedProcess[str], float]:
    """Hold the real writer lease on ``index`` while ``run`` executes; return its result and wall time."""
    outcome: list[tuple[subprocess.CompletedProcess[str], float]] = []
    original = json.loads(index.read_text(encoding="utf-8"))

    def build_while_contended(_path: Path) -> dict:
        """Keep the writer lease held for the whole competing engine run."""
        started = time.monotonic()
        result = run()
        outcome.append((result, time.monotonic() - started))
        return original

    rwgate.write_index(index, build_while_contended)
    return outcome[0]


@pytest.mark.integration
@pytest.mark.parametrize("portable", _MECHANISMS)
@pytest.mark.parametrize(("module", "prog", "engine_args"), _ENGINES)
class TestSlowEngineTimesOut:
    """A forced slow engine stops at its deadline with the existing exit contract on both mechanisms."""

    def test_exits_two_with_one_timeout_record(
        self, module, prog, engine_args, portable, project, scan_query, tmp_path
    ):
        """The run exits 2, prints the timeout line and logs exactly one timeout record before the gate gives up.

        The writer lease is held by this test process; with a 5s gate wait and ``--timeout 1`` the engine can only
        finish early if the deadline fires. The record must match the SIGALRM path field for field.
        """
        root, index = project
        env = _engine_env(tmp_path)
        args = ["--timeout", "1", *engine_args]

        result, _ = _run_while_contended(
            index,
            lambda: _run_engine(
                scan_query.parent.parent / "src", module, prog, args, portable=portable, cwd=root, env=env
            ),
        )

        assert result.returncode == 2, result.stderr
        assert f"{prog}: timed out after 1s" in result.stderr
        records = _records(tmp_path)
        assert [(r["exit_code"], r["result"]) for r in records] == [(2, {"error": "timeout", "timeout_seconds": 1})]


@pytest.mark.integration
@pytest.mark.parametrize("portable", _MECHANISMS)
@pytest.mark.parametrize(("module", "prog", "engine_args"), _ENGINES)
def test_engine_finishing_early_keeps_its_own_outcome(module, prog, engine_args, portable, scan_query, tmp_path):
    """An engine that finishes before its deadline exits 0 with exactly one success record and no timeout line.

    On the portable path the index runs in a supervised worker, so this also proves the parent stays silent and the
    worker's own record is the only one; the query proves the watchdog is cancelled and no interrupt leaks out.
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "module.py").write_text("def function():\n    return 1\n", encoding="utf-8")
    env = _engine_env(tmp_path)
    src = scan_query.parent.parent / "src"
    built = _run_engine(
        src, "graph", "scan-index", [], portable=False, cwd=root, env={**env, "CODEMAP_LOGGING": "false"}
    )
    assert built.returncode == 0, built.stderr

    result = _run_engine(src, module, prog, ["--timeout", "30", *engine_args], portable=portable, cwd=root, env=env)

    assert result.returncode == 0, result.stderr
    assert "timed out" not in result.stderr
    assert [r["exit_code"] for r in _records(tmp_path)] == [0]


@pytest.mark.integration
@pytest.mark.parametrize("portable", _MECHANISMS)
def test_ctrl_c_during_query_stays_an_interrupt(portable, project, scan_query, tmp_path):
    """A real Ctrl-C while a deadline is armed is logged as exit 130, never as a timeout.

    The harness raises ``KeyboardInterrupt`` from inside the query (as a console Ctrl-C would) with a 30s deadline that
    has not fired, so the watchdog's timed-out flag is unset and the interrupt must propagate unchanged.
    """
    root, _ = project
    patch = (
        "def _interrupted(args):\n    raise KeyboardInterrupt\n"
        "import codemap_py.query.cli as _cli\n_cli._resolve_index_path = _interrupted"
    )

    result = _run_engine(
        scan_query.parent.parent / "src",
        "query",
        "scan-query",
        ["--timeout", "30", "--no-heal", "central"],
        portable=portable,
        cwd=root,
        env=_engine_env(tmp_path),
        patch=patch,
    )

    assert result.returncode == 130, result.stderr
    assert "timed out" not in result.stderr
    assert [(r["exit_code"], r["result"]["error"]) for r in _records(tmp_path)] == [(130, "KeyboardInterrupt")]


@pytest.mark.integration
def test_index_rebuilds_after_portable_deadline_stopped_its_worker(project, scan_query, tmp_path):
    """A worker stopped at the deadline leaves the shared index directory usable for an immediate rebuild.

    The worker is stopped while it waits for the writer lease, so it never holds a writer intent; this covers the stop
    itself (no stray lock or registry state blocks the next build), not reclaim of an intent the worker held.
    """
    root, index = project
    env = _engine_env(tmp_path)
    src = scan_query.parent.parent / "src"
    stopped, _ = _run_while_contended(
        index,
        lambda: _run_engine(src, "graph", "scan-index", ["--timeout", "1"], portable=True, cwd=root, env=env),
    )
    assert stopped.returncode == 2, stopped.stderr

    rebuilt = _run_engine(src, "graph", "scan-index", ["--timeout", "30"], portable=True, cwd=root, env=env)

    assert rebuilt.returncode == 0, rebuilt.stderr


@pytest.mark.integration
def test_supervised_worker_leaves_deadline_to_its_parent(project, scan_index, tmp_path):
    """A worker launched with the supervisor's marker arms no deadline of its own, on any host.

    Run directly with the marker and ``--timeout 1`` under a held writer lease and a 2s gate wait, the worker must
    outlast its nominal deadline and end with the gate's ``index_busy`` (exit 1). This is what makes the portable index
    cases above prove that the parent, not the worker, stopped the run.
    """
    root, index = project
    env = {**_engine_env(tmp_path), "CODEMAP_GATE_TIMEOUT": "2", "_CODEMAP_DEADLINE_WORKER": "1"}

    result, _ = _run_while_contended(
        index,
        lambda: subprocess.run(
            [sys.executable, str(scan_index), "--timeout", "1"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ),
    )

    assert result.returncode == 1, result.stderr
    assert json.loads(result.stderr.strip().splitlines()[-1])["error"] == "index_busy"


@pytest.mark.integration
def test_owned_query_hard_exits_with_one_timeout_record(project, scan_query, tmp_path):
    """A process-owning query whose main thread ignores the interrupt is ended by the backstop with exit 2.

    The patched work ignores SIGINT after the watchdog was armed and then spins, so only the owner's hard-exit backstop
    can end the run; it must still print the timeout line and write exactly one timeout record before exiting.
    """
    root, _ = project
    patch = (
        "def _stuck(args):\n"
        "    import time as _time\n"
        "    signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "    end = _time.monotonic() + 20\n"
        "    while _time.monotonic() < end:\n"
        "        pass\n"
        "import codemap_py.query.cli as _cli\n_cli._resolve_index_path = _stuck"
    )

    result = _run_engine(
        scan_query.parent.parent / "src",
        "query",
        "scan-query",
        ["--timeout", "1", "--no-heal", "central"],
        portable=True,
        cwd=root,
        env=_engine_env(tmp_path),
        patch=patch,
        main_kwargs="owns_process=True",
    )

    assert result.returncode == 2, result.stderr
    assert "scan-query: timed out after 1s" in result.stderr
    records = _records(tmp_path)
    assert [(r["exit_code"], r["result"]) for r in records] == [(2, {"error": "timeout", "timeout_seconds": 1})]


_STUCK_QUERY_PATCH = """\
import signal, sys, time
sys.path.insert(0, {src!r})
if hasattr(signal, "SIGALRM"):
    del signal.SIGALRM
import codemap_py.query.cli as _cli

def _stuck(args):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    end = time.monotonic() + {spin}
    while time.monotonic() < end:
        pass
    raise SystemExit(7)

_cli._resolve_index_path = _stuck
"""


def _run_code(code: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a harness program in a fresh interpreter and capture its text output."""
    return subprocess.run(
        [sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False
    )


@pytest.mark.integration
def test_scan_query_launcher_owns_its_process(project, scan_query, tmp_path):
    """The real ``scan-query`` launcher enables the backstop, so a stuck portable query still exits 2 on time.

    The launcher file runs as ``__main__`` with the portable path forced and work that ignores SIGINT and spins for 20s;
    only the launcher's ``owns_process=True`` wiring can end it at the deadline with one timeout record.
    """
    root, _ = project
    code = _STUCK_QUERY_PATCH.format(src=str(scan_query.parent.parent / "src"), spin=20) + (
        f"sys.argv = [{str(scan_query)!r}, '--timeout', '1', '--no-heal', 'central']\n"
        f"import runpy\nrunpy.run_path({str(scan_query)!r}, run_name='__main__')\n"
    )

    result = _run_code(code, cwd=root, env=_engine_env(tmp_path))

    assert result.returncode == 2, result.stderr
    assert "scan-query: timed out after 1s" in result.stderr
    records = _records(tmp_path)
    assert [(r["exit_code"], r["result"]) for r in records] == [(2, {"error": "timeout", "timeout_seconds": 1})]


@pytest.mark.integration
def test_embedded_query_never_hard_exits(project, scan_query, tmp_path):
    """An embedded ``query.main()`` call keeps the backstop off, so its host process is never ended from inside.

    The stuck work outlives the deadline plus the backstop grace (4s against 1s + 2s) and then exits with its own status
    7; any ``os._exit`` from the watchdog would surface as status 2 instead.
    """
    root, _ = project
    code = _STUCK_QUERY_PATCH.format(src=str(scan_query.parent.parent / "src"), spin=4) + (
        "sys.argv = ['scan-query', '--timeout', '1', '--no-heal', 'central']\n_cli.main()\n"
    )

    result = _run_code(code, cwd=root, env=_engine_env(tmp_path))

    assert result.returncode == 7, result.stderr
