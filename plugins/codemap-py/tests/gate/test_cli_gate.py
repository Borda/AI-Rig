"""Codemap-py CLI gate wiring and doctor resolver.

Proves the shipped ``codemap-py index/query`` enters the RW gate (a live writer lease forces ``query`` to return a
bounded ``index_busy``) and that ``doctor`` reports the exact path from the single index resolver, including the flat
``CODEMAP_INDEX_DIR`` layout.

Also covers the exit-1 surfaces the capability contract requires to be bounded and structured rather than a traceback: a
corrupt index reached through the dispatcher and a writer refusing to downgrade a newer-schema index.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_BIN = _PLUGIN_ROOT / "bin"
_LAUNCHER = _BIN / ("codemap-py.cmd" if sys.platform == "win32" else "codemap-py")
_SCRIPTS = _PLUGIN_ROOT / "scripts"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _index_identity  # noqa: E402  (needs the bin/ path insert above)
import _rwgate  # noqa: E402  (needs the bin/ path insert above)
import _schema  # noqa: E402  (needs the bin/ path insert above)
import codemap_py_cli as _cli  # noqa: E402  (needs the scripts/ path insert above)

# These tests exercise resolver equality and gate wiring THROUGH the launcher, so
# they need an eligible interpreter on the cell. The unsupported-interpreter
# rejection contract itself (exit 127, empty stdout) is asserted — never skipped —
# by test_interpreter.py on the same cell.
_RUNNING_SUPPORTED = _cli.is_supported(sys.implementation.name, sys.version_info.major, sys.version_info.minor)
pytestmark = pytest.mark.skipif(
    not _RUNNING_SUPPORTED,
    reason="CLI behavior needs a supported CPython; the 127 rejection contract is covered by test_interpreter.py",
)


def _run_cli(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the launcher with pytest's supported interpreter and inherited test environment."""
    env = {**os.environ, "CODEMAP_PYTHON": sys.executable}
    return subprocess.run(
        [str(_LAUNCHER), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
        cwd=None if cwd is None else str(cwd),
    )


@pytest.mark.parametrize("flag", [pytest.param("--help", id="long"), pytest.param("-h", id="short")])
def test_index_help_succeeds_without_the_scan_index_launcher(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    """`index --help` prints usage and exits 0 even when bin/scan-index is absent.

    The dispatcher used to check for the launcher before looking at the arguments, so asking for help returned a
    missing_executable error and exit 1. That also broke `index --help && query --help`: the chain aborted on the first
    command and the caller never received the query help it was after.
    """
    (tmp_path / "bin").mkdir()

    exit_code = _cli.main(["index", flag], plugin_root=tmp_path)

    assert exit_code == 0
    assert "usage: codemap-py index" in capsys.readouterr().out


def test_index_without_the_launcher_still_reports_the_missing_executable(tmp_path: Path) -> None:
    """A real index request with no launcher keeps its structured missing_executable error.

    The help short-circuit must not swallow the genuine failure it sits in front of, which is the only signal a caller
    gets that the plugin's own executables did not install.
    """
    (tmp_path / "bin").mkdir()

    assert _cli.main(["index"], plugin_root=tmp_path) == 1


@pytest.mark.parametrize("with_override", [pytest.param(False, id="default"), pytest.param(True, id="override")])
def test_doctor_index_path_matches_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_override: bool
) -> None:
    """Doctor's index_path equals _index_identity.resolve_index(), override or not."""
    if with_override:
        monkeypatch.setenv("CODEMAP_INDEX_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
    expected = str(_index_identity.resolve_index().index_path)

    result = _run_cli(["doctor", "--json"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["index_path"] == expected


def test_doctor_override_reports_the_flat_resolver_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under CODEMAP_INDEX_DIR doctor reports the flat ``<override>/<project>.json``.

    Formerly doctor printed a root-keyed ``<override>/<root-key>/<project>.json`` that no writer ever produced, so the
    path it reported did not exist.
    """
    monkeypatch.setenv("CODEMAP_INDEX_DIR", str(tmp_path))
    identity = _index_identity.resolve_index()

    result = _run_cli(["doctor", "--json"])
    reported = Path(json.loads(result.stdout)["index_path"])
    assert reported == identity.index_path
    assert reported.parent == tmp_path.resolve()


def test_override_lease_write_and_report_paths_are_one_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove end to end that leased, written, loaded, and doctor-reported paths agree.

    The finding was a four-way split under ``CODEMAP_INDEX_DIR`` — the resolver root-keyed the path, the writer wrote
    flat, doctor printed the root-keyed one, and the reader loaded the flat one. Asserting them equal one at a time
    cannot catch that; only building an index and comparing every reported path against the file that actually appeared
    does.
    """
    project = tmp_path / "proj"
    project.mkdir()
    (project / "mod.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    override = tmp_path / "shared"
    monkeypatch.setenv("CODEMAP_INDEX_DIR", str(override))

    build = _run_cli(["index", "--root", str(project)])
    assert build.returncode == 0, build.stderr

    written = sorted(override.rglob("*.json"))
    assert written == [override.resolve() / "proj.json"], f"writer published elsewhere: {written}"

    identity = _index_identity.resolve_index(root=project)
    assert identity.index_path == written[0]

    reported = Path(json.loads(_run_cli(["doctor", "--json"], cwd=project).stdout)["index_path"])
    assert reported == written[0]


def test_override_query_leases_and_loads_the_path_doctor_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a real query leases and loads exactly the file doctor names.

    The sibling test above stops at the written and doctor-reported paths — it never
    loads an index, so a reader resolving a different path would still pass it. That is
    the half of the split this one closes: the query runs for real, and the gate's
    coordination root is located afterwards (the writer's own is cleared first, so the
    one found can only be this query's), making the leased path and the loaded path
    observed rather than inferred.

    Both halves of the original split fail loudly and separately here: a writer that
    publishes elsewhere trips the ``is_file`` arrange check, a reader that leases
    elsewhere trips the ``leased`` assertion with the path it actually used.
    """
    project = tmp_path / "proj"
    project.mkdir()
    (project / "mod.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    override = tmp_path / "shared"
    monkeypatch.setenv("CODEMAP_INDEX_DIR", str(override))
    assert _run_cli(["index", "--root", str(project)]).returncode == 0
    index_file = override.resolve() / "proj.json"
    assert index_file.is_file(), "writer did not publish at the flat override path"
    shutil.rmtree(index_file.parent / _index_identity.COORDINATION_DIRNAME, ignore_errors=True)

    result = _run_cli(["query", "central", "--top", "3"], cwd=project)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["central"], "query returned no data — it did not load the published index"
    leased = sorted(p.parent for p in tmp_path.rglob(_index_identity.COORDINATION_DIRNAME))
    assert leased == [index_file.parent], f"query leased somewhere other than the published index: {leased}"
    reported = Path(json.loads(_run_cli(["doctor", "--json"], cwd=project).stdout)["index_path"])
    assert reported == index_file


def test_query_reports_the_index_path_it_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A query's own output names the index file it read, under a flat override."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "mod.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    override = tmp_path / "shared"
    monkeypatch.setenv("CODEMAP_INDEX_DIR", str(override))
    assert _run_cli(["index", "--root", str(project)]).returncode == 0
    index_file = override.resolve() / "proj.json"

    result = _run_cli(["query", "central", "--top", "3"], cwd=project)

    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)["index"]["index_path"]) == index_file


def test_reported_index_path_comes_from_the_load_not_the_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emitted path survives a resolver whose answer changed after the load.

    This is the whole point of the field: a consumer comparing its own probe against a
    path the provider recomputes on demand compares two runs of one function and learns
    nothing. Re-pointing ``CODEMAP_INDEX_DIR`` between the load and the emit makes the
    two answers differ, so an implementation that resolved at emit time fails here.
    """
    from codemap_py import query

    project = tmp_path / "proj"
    project.mkdir()
    (project / "mod.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    override = tmp_path / "shared"
    monkeypatch.setenv("CODEMAP_INDEX_DIR", str(override))
    assert _run_cli(["index", "--root", str(project)]).returncode == 0
    index_file = override.resolve() / "proj.json"

    index = query._load_index_leased(index_file)
    monkeypatch.setenv("CODEMAP_INDEX_DIR", str(tmp_path / "elsewhere"))

    assert Path(query._cmd_coverage(index, command="central")["index_path"]) == index_file


def _hold_writer_intent(index_path: str, ready: Path, hold: float) -> None:
    """Acquire an exclusive writer lease and hold it past the reader's timeout."""

    def _build(_target: Path) -> None:
        """Hold the writer lease until the reader timeout is exercised."""
        ready.write_text("live")
        time.sleep(hold)

    _rwgate.write_index(index_path, _build, timeout=30.0)


def test_query_under_live_writer_returns_index_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live writer lease forces the shipped query into a bounded index_busy."""
    monkeypatch.setenv("CODEMAP_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("CODEMAP_GATE_TIMEOUT", "1")
    index_path = str(_index_identity.resolve_index().index_path)
    ready = tmp_path / "writer.ready"

    writer = threading.Thread(target=_hold_writer_intent, args=(index_path, ready, 3.0))
    writer.start()
    deadline = time.time() + 5
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert ready.exists(), "writer never acquired intent"

    result = _run_cli(["query", "central"])
    writer.join(10)

    assert result.returncode == 1, f"expected index_busy exit 1, got {result.returncode}: {result.stderr}"
    assert json.loads(result.stderr.strip().splitlines()[-1])["error"] == "index_busy"


def test_corrupt_index_via_dispatcher_is_a_structured_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt index through ``codemap-py query`` exits 1 with a structured error.

    The standalone ``scan-query`` path was already graceful; the dispatcher was not. ``rwgate._load_index`` guarded only
    FileNotFoundError, so JSONDecodeError escaped ahead of the query engine's own handling and reached the user as a raw
    traceback — which capability-contract.md forbids for every exit-1 surface.
    """
    monkeypatch.setenv("CODEMAP_INDEX_DIR", str(tmp_path))
    index_path = _index_identity.resolve_index().index_path
    index_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = "{ this is not json"
    index_path.write_text(corrupt, encoding="utf-8")

    dispatched = _run_cli(["query", "central"])

    assert "Traceback" not in dispatched.stderr
    assert "is not valid JSON" in dispatched.stderr
    assert str(index_path) in dispatched.stderr  # names the offending file

    # The defect was dispatcher-only: standalone scan-query already degraded gracefully.
    # Parity with it is the fix, so assert against it rather than a hard-coded code —
    # the gate leases the read but leaves parsing to the query engine, which owns the
    # diagnosable error path for a corrupt index.
    index_path.write_text(corrupt, encoding="utf-8")
    standalone = subprocess.run(
        [sys.executable, str(_BIN / "scan-query"), "central"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "CODEMAP_INDEX_DIR": str(tmp_path)},
    )
    assert "Traceback" not in standalone.stderr
    assert dispatched.returncode == standalone.returncode


def test_writer_refuses_to_downgrade_a_newer_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An older writer refuses to overwrite a newer-schema index.

    The refusal was advertised in the gate's docstring but implemented as ``return None``, so an older tool silently
    downgraded a shared index and forced the newer reader to rebuild on every query.
    """
    monkeypatch.setenv("CODEMAP_INDEX_DIR", str(tmp_path))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "mod.py").write_text("x = 1\n", encoding="utf-8")

    index_path = _index_identity.resolve_index(root=project).index_path
    index_path.parent.mkdir(parents=True, exist_ok=True)
    newer = {"scan_version": _schema.SCAN_VERSION + 1, "modules": [], "sentinel": "keep-me"}
    index_path.write_text(json.dumps(newer), encoding="utf-8")

    result = _run_cli(["index", "--root", str(project)])

    assert result.returncode == 1, result.stdout
    assert "Traceback" not in result.stderr
    assert json.loads(result.stderr.strip().splitlines()[-1])["error"] == "index_version_skew"
    # The newer index must survive the refusal untouched.
    assert json.loads(index_path.read_text())["sentinel"] == "keep-me"
