"""Extraction parity: core modules + CLI entrypoint.

Proves the schema/index-paths/gate/runtime-log/telemetry move into
``src/codemap_py`` preserved behavior exactly:

- the legacy ``bin/_*.py`` (and ``scripts/codemap_py_cli.py``) shims are not
  copies — each aliases the real ``codemap_py`` submodule in ``sys.modules``,
  so old-name imports, direct attribute access, and monkeypatching of private
  internals all reach the one authoritative implementation;
- ``doctor --json`` is byte-identical for stdout, stderr, and exit code
  whether reached through the pre-existing ``scripts/codemap_py_entry.py``
  bootstrap or directly via ``python -m codemap_py`` (both now dispatch
  through :mod:`codemap_py.cli`), across a plain project root and one with
  spaces and non-ASCII characters;
- :func:`codemap_py.index_paths.resolve_index` resolves identically through
  the ``_index_identity`` shim and the package import for both path classes.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _parity_helpers import PATH_CLASSES

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SRC = _PLUGIN_ROOT / "src"
_BIN = _PLUGIN_ROOT / "bin"
_SCRIPTS = _PLUGIN_ROOT / "scripts"
_ENTRY = _SCRIPTS / "codemap_py_entry.py"

for _p in (_SRC, _BIN, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _index_identity  # noqa: E402  (bin/ shim — aliases codemap_py.index_paths)
import _runtime_log  # noqa: E402  (bin/ shim — aliases codemap_py.runtime_log)
import _rwgate  # noqa: E402  (bin/ shim — aliases codemap_py.rwgate)
import _schema  # noqa: E402  (bin/ shim — aliases codemap_py.schema)
import _telemetry  # noqa: E402  (bin/ shim — aliases codemap_py.telemetry)
import codemap_py.cli as cli_pkg  # noqa: E402  (needs the sys.path insert above)
import codemap_py.index_paths as index_paths_pkg  # noqa: E402  (needs the sys.path insert above)
import codemap_py.runtime_log as runtime_log_pkg  # noqa: E402
import codemap_py.rwgate as rwgate_pkg  # noqa: E402
import codemap_py.schema as schema_pkg  # noqa: E402
import codemap_py.telemetry as telemetry_pkg  # noqa: E402
import codemap_py_cli  # noqa: E402  (scripts/ shim — aliases codemap_py.cli)

_skip_entry_bootstrap_unsupported = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="entry bootstrap rejects <3.11 (exit 127) by design; -m has no interpreter guard, "
    "so entry-vs-module parity only holds on supported interpreters",
)


@pytest.fixture(name="project_root")
def _project_root(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """Create the project directory named by the indirect ``dirname`` parameter (a plain or non-ASCII name)."""
    root = tmp_path / request.param
    root.mkdir()
    return root


@pytest.fixture(name="child_env")
def _child_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Return the subprocess environment: logging off and no index-dir override inherited."""
    monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
    return {**os.environ, "CODEMAP_LOGGING": "false"}


# --- shim identity equivalence ----------------------------------------------


@pytest.mark.parametrize(
    ("shim", "pkg"),
    [
        pytest.param(_schema, schema_pkg, id="schema"),
        pytest.param(_index_identity, index_paths_pkg, id="index_paths"),
        pytest.param(_rwgate, rwgate_pkg, id="rwgate"),
        pytest.param(_runtime_log, runtime_log_pkg, id="runtime_log"),
        pytest.param(_telemetry, telemetry_pkg, id="telemetry"),
    ],
)
def test_bin_shim_aliases_the_package_module(shim: object, pkg: object) -> None:
    """Importing the legacy ``bin/_*.py`` shim yields the exact codemap_py module object.

    Object identity (not just equal behavior) proves that a test using the old bare-name import to monkeypatch a private
    attribute (e.g. ``monkeypatch.setattr(_rwgate, "_RELEASE_TIMEOUT", ...)``) mutates the same module the real
    dispatcher uses — never a separate re-exported copy.
    """
    assert shim is pkg


def test_scripts_shim_aliases_the_cli_module() -> None:
    """Expose the package command-line module through the compatibility alias.

    ``scripts/codemap_py_cli.py`` must be the very ``codemap_py.cli`` module object so old-name imports reach the one
    implementation.
    """
    assert codemap_py_cli is cli_pkg


# --- index-path resolution parity across path classes -----------------------


@pytest.mark.parametrize("project_root", PATH_CLASSES, indirect=True)
def test_resolve_index_matches_via_shim_and_package(project_root: Path) -> None:
    """Agree through the bin/ shim and the package import.

    The same root resolved through both names must yield identical paths and keys, for a plain directory name and one
    with spaces and non-ASCII characters.
    """
    via_shim = _index_identity.resolve_index(root=project_root, index_dir_override=None)
    via_pkg = index_paths_pkg.resolve_index(root=project_root, index_dir_override=None)

    assert via_shim.index_path == via_pkg.index_path
    assert via_shim.root_key == via_pkg.root_key
    assert via_shim.coordination_dir == via_pkg.coordination_dir


# --- CLI dispatch parity: entry script vs. `python -m codemap_py` -----------


def _run_entry(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke the pre-existing ``scripts/codemap_py_entry.py`` bootstrap."""
    return subprocess.run(
        [sys.executable, str(_ENTRY), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _run_module(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m codemap_py`` with ``src/`` supplied via ``PYTHONPATH``."""
    module_env = {**env, "PYTHONPATH": str(_SRC)}
    return subprocess.run(
        [sys.executable, "-m", "codemap_py", *args],
        cwd=str(cwd),
        env=module_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@_skip_entry_bootstrap_unsupported
@pytest.mark.parametrize("project_root", PATH_CLASSES, indirect=True)
def test_entry_and_module_doctor_output_are_byte_identical(project_root: Path, child_env: dict[str, str]) -> None:
    """Match stdout/stderr/exit code across both bootstrap paths.

    ``doctor --json`` runs through the ``scripts/`` entry bootstrap and through ``python -m codemap_py`` in a fresh
    project directory; every byte of output and the exit code must agree.
    """
    entry_result = _run_entry(["doctor", "--json"], cwd=project_root, env=child_env)
    module_result = _run_module(["doctor", "--json"], cwd=project_root, env=child_env)

    assert entry_result.returncode == 0, entry_result.stderr
    assert module_result.returncode == 0, module_result.stderr
    assert entry_result.returncode == module_result.returncode
    assert entry_result.stdout == module_result.stdout
    assert entry_result.stderr == module_result.stderr


@_skip_entry_bootstrap_unsupported
@pytest.mark.parametrize("project_root", PATH_CLASSES, indirect=True)
def test_entry_and_module_unknown_command_exit_parity(project_root: Path, child_env: dict[str, str]) -> None:
    """An invalid command produces the same usage error and exit 2 on both paths.

    The usage banner embeds argparse's program name; both bootstraps must still print the same text and exit code, with
    nothing on stdout.
    """
    entry_result = _run_entry(["bogus-command"], cwd=project_root, env=child_env)
    module_result = _run_module(["bogus-command"], cwd=project_root, env=child_env)

    assert entry_result.returncode == module_result.returncode == 2
    assert entry_result.stdout == module_result.stdout == ""
    assert entry_result.stderr == module_result.stderr


def test_runtime_log_import_no_longer_needs_bare_name_fallback() -> None:
    """codemap_py.runtime_log imports index_paths as a package module, not a bare name.

    Slice 1 replaced the transitional ``try: import _index_identity`` bootstrap (needed only while ``_runtime_log.py``
    lived in ``bin/`` with no package around it) with a normal package-internal import.
    """
    source = inspect.getsource(runtime_log_pkg)
    assert "from codemap_py.index_paths import" in source
    assert "import _index_identity" not in source
