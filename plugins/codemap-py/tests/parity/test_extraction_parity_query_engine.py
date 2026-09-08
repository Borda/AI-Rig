"""Query engine extraction parity.

Proves the query-engine move into :mod:`codemap_py.query` preserved behavior
exactly, by comparing the pre-extraction monolithic ``bin/scan-query``
(checked out from ``HEAD`` — an uncommitted working-tree move, so ``HEAD``
still holds the true pre-extraction script, same convention as
``test_extraction_parity_scanner_discovery.py``/
``test_extraction_parity_graph_coverage_testimpact.py``) against two current surfaces:

- the now-thin ``bin/scan-query`` launcher (delegates to
  :func:`codemap_py.query.main`) — byte-identical stdout/stderr/exit code is
  the primary regression gate, since both the golden script and the launcher
  share the same ``scan-query`` argv[0] basename, so argparse's ``prog``
  (derived from ``os.path.basename(sys.argv[0])``, never pinned by
  ``_build_parser()``) is identical on both sides even for usage/error
  banners;
- ``python -m codemap_py query ...`` (:mod:`codemap_py.cli`'s in-process
  dispatch under the shared-index read lease) — cross-path equivalence for the
  extracted package path. This surface's argv[0] basename is ``__main__.py``,
  not ``scan-query``, so argparse's own usage/error banners legitimately
  differ in that one token (pre-existing argparse behavior, not a regression
  introduced by the extraction — see :func:`_error_suffix` below). Every
  other byte, including every legacy JSON field/value (and the entire payload
  for commands other than ``undocumented``/``uncovered``) on every success path and
  the ``_die_json``-style missing-index error, is asserted identical.

Coverage: all 28 non-composite subcommands (:data:`_QUERY_CASES`, enumerated
from ``codemap_py.query``'s ``_add_*_subparsers`` helpers) plus the two
composite commands (``batch``, ``diff-impact``) exercised separately since
each needs its own input shape (stdin JSON / a git working-tree diff), plus
three error paths (missing index, unknown subcommand, missing required
positional). Every case runs across both path classes (plain, and one with
spaces + non-ASCII characters — repo convention, see
``test_extraction_parity_core_modules_and_cli_entrypoint.py``).

Query output is JSON-only — grep across ``codemap_py/query.py`` found no
``--json``/``--text`` toggle on any subcommand (every ``cmd_*`` handler
calls ``_print(json.dumps(...))`` unconditionally) — so there is no separate
text-output mode to exercise here.

FILE OWNERSHIP NOTE: duplicates the old-bin checkout / path-classes helpers
from ``test_extraction_parity_scanner_discovery.py``/``test_extraction_parity_graph_coverage_testimpact.py`` rather than
factoring them into ``conftest.py`` — this task's boundary permits editing
only this new file plus the report, not the shared ``conftest.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
_PLUGIN_ROOT = _TESTS_DIR.parent
_SRC = _PLUGIN_ROOT / "src"
_NEW_SCAN_QUERY = _PLUGIN_ROOT / "bin" / "scan-query"
_PARITY_GOLDEN_DIR = _TESTS_DIR / "data" / "parity_golden"

# The pre-extraction monolith and the three sibling shims it bare-imports
# (all self-contained — stdlib only, confirmed by inspection at HEAD). Frozen
# as static fixtures under tests/data/parity_golden/ rather than read via
# `git show HEAD:...` — a HEAD-pinned checkout silently degrades to the
# post-extraction thin launcher once the extraction commit lands (HEAD then
# advances past the monolith), and is simply absent in a shallow CI clone that
# never fetched the old SHA. See _assert_is_monolith for the loud-failure guard
# against a golden fixture being accidentally overwritten in kind.
_OLD_BIN_FILES = ("scan-query", "_exclusions.py", "_schema.py", "_telemetry.py")

# The monolith is roughly 10x-100x larger than its post-extraction thin
# launcher/shim counterpart; thresholds sit with headroom below the monolith
# line count and well above the current thin-file line count.
_MONOLITH_MIN_LINES = {
    "scan-query": 500,
    "_exclusions.py": 100,
    "_schema.py": 50,
    "_telemetry.py": 50,
}

# Substrings present in every current (post-extraction) bin/ shim or launcher
# docstring, absent from every pre-extraction monolith source — their presence
# in a golden fixture means it was accidentally overwritten with
# post-extraction content instead of holding the frozen monolith.
_POST_EXTRACTION_MARKERS = ("codemap_py", "thin launcher", "compatibility shim")


def _assert_is_monolith(name: str, content: str) -> None:
    """Fail loudly if *content* looks like the post-extraction shim, not the golden monolith."""
    lines = content.splitlines()
    assert len(lines) > _MONOLITH_MIN_LINES[name], (
        f"parity_golden/{name} has only {len(lines)} lines (expected > {_MONOLITH_MIN_LINES[name]}) "
        "— looks like the post-extraction thin bin/ file was committed in place of the "
        "pre-extraction monolith golden"
    )
    for marker in _POST_EXTRACTION_MARKERS:
        assert marker not in content, (
            f"parity_golden/{name} contains {marker!r}, which only appears in post-extraction "
            "bin/ sources — the golden fixture was accidentally overwritten with the thin "
            "launcher/shim instead of the monolith"
        )


_PATH_CLASSES = ["proj", "proj café ünïcode dir"]

# Every non-composite scan-query subcommand (batch/diff-impact are covered by
# dedicated tests below since each needs its own input shape), with args that
# resolve against the fixture project built by _write_fixture_project.
_QUERY_CASES = [
    pytest.param(["deps", "pkg.alpha"], id="deps"),
    pytest.param(["rdeps", "pkg.gamma"], id="rdeps"),
    pytest.param(["central"], id="central"),
    pytest.param(["coupled"], id="coupled"),
    pytest.param(["path", "pkg.alpha", "pkg.gamma"], id="path"),
    pytest.param(["list"], id="list"),
    pytest.param(["packages"], id="packages"),
    pytest.param(["symbol", "func_gamma"], id="symbol"),
    pytest.param(["symbols", "pkg.gamma"], id="symbols"),
    pytest.param(["find-symbol", "func_.*"], id="find-symbol"),
    pytest.param(["fn-deps", "pkg.alpha::func_alpha"], id="fn-deps"),
    pytest.param(["fn-rdeps", "pkg.gamma::func_gamma"], id="fn-rdeps"),
    pytest.param(["fn-central"], id="fn-central"),
    pytest.param(["fn-blast", "pkg.gamma::func_gamma"], id="fn-blast"),
    pytest.param(["test-impact", "pkg.alpha::func_alpha"], id="test-impact"),
    pytest.param(["mock-rdeps", "pkg.beta::func_beta"], id="mock-rdeps"),
    pytest.param(["subprocess-deps", "pkg.subproc"], id="subprocess-deps"),
    pytest.param(["subprocess-rdeps", "pkg.gamma"], id="subprocess-rdeps"),
    pytest.param(["fixture-rdeps", "sample"], id="fixture-rdeps"),
    pytest.param(["fixture-graph", "tests.test_alpha"], id="fixture-graph"),
    pytest.param(["import-types", "pkg.alpha"], id="import-types"),
    pytest.param(["undocumented", "pkg.undoc"], id="undocumented"),
    pytest.param(["uncovered", "pkg.undoc"], id="uncovered"),
    # No --with-coverage rebuild in this fixture, so this is a deterministic
    # error path (exit 1, "no coverage data") rather than a success path —
    # parity across all three surfaces holds regardless of exit code.
    pytest.param(["coverage", "pkg.alpha::func_alpha"], id="coverage"),
    pytest.param(["coverage-gap", "pkg.alpha"], id="coverage-gap"),
    pytest.param(["xrefs", "pkg.gamma::func_gamma"], id="xrefs"),
    pytest.param(["dead-symbols"], id="dead-symbols"),
    pytest.param(["dead-modules"], id="dead-modules"),
]


def _materialize_old_bin(dest: Path) -> Path:
    """Copy the frozen pre-extraction monolithic ``scan-query`` + siblings into *dest*.

    Returns the path to the copied ``scan-query`` script, written under the literal basename ``scan-query`` instead of a
    random temporary name — argparse derives ``prog`` from ``os.path.basename(sys.argv[0])`` with no explicit override
    in ``_build_parser()``, so keeping the golden script's basename identical to the current ``bin/scan-query``
    launcher's is what makes even the argparse usage/error banners byte-identical between old and new (see module
    docstring).
    """
    for name in _OLD_BIN_FILES:
        content = (_PARITY_GOLDEN_DIR / name).read_text()
        _assert_is_monolith(name, content)
        (dest / name).write_text(content)
    return dest / "scan-query"


@pytest.fixture(name="old_scan_query", scope="module")
def _old_scan_query(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-stable path to the golden pre-extraction ``scan-query``."""
    return _materialize_old_bin(tmp_path_factory.mktemp("old_bin_query_engine"))


def _write_fixture_project(root: Path) -> None:
    """Write a small project exercising every query-command family.

    Layout:
        pkg/gamma.py    — leaf module, no imports; func_gamma
        pkg/beta.py     — imports gamma; func_beta calls func_gamma; docstring
                          xref to gamma (sphinx_xrefs)
        pkg/alpha.py    — imports beta, gamma, and stdlib os; func_alpha calls
                          func_beta; docstring xrefs to both beta and gamma
        pkg/subproc.py  — subprocess.run(["python", ".../gamma.py"]) — one
                          resolved subprocess edge (subprocess-deps/-rdeps)
        pkg/undoc.py    — one undocumented, uncovered, dead-adjacent function
        tests/test_alpha.py — a pytest fixture ("sample"), a real call into
                          func_alpha (test-impact/coverage), and a
                          mock.patch("pkg.beta.func_beta") call (mock-rdeps)
    """
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "gamma.py").write_text(
        '"""Leaf module -- no imports."""\n'
        "\n"
        "\n"
        "def func_gamma(x):\n"
        '    """Return x incremented by one."""\n'
        "    return x + 1\n"
    )
    (pkg / "beta.py").write_text(
        '"""Imports gamma."""\n'
        "\n"
        "import pkg.gamma as gamma\n"
        "\n"
        "\n"
        "def func_beta(x):\n"
        '    """Double the result of :func:`pkg.gamma.func_gamma`."""\n'
        "    return gamma.func_gamma(x) * 2\n"
    )
    (pkg / "alpha.py").write_text(
        '"""Imports beta and gamma, plus stdlib os."""\n'
        "\n"
        "import os\n"
        "\n"
        "import pkg.beta as beta\n"
        "import pkg.gamma as gamma\n"
        "\n"
        "\n"
        "def func_alpha(x):\n"
        '    """Calls :func:`pkg.beta.func_beta` and :func:`pkg.gamma.func_gamma`."""\n'
        "    _ = os.getcwd()\n"
        "    return beta.func_beta(x) + gamma.func_gamma(x)\n"
    )
    (pkg / "subproc.py").write_text(
        '"""Spawns pkg/gamma.py as a subprocess."""\n'
        "\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def run_gamma_script():\n"
        '    """Run gamma.py as a child process."""\n'
        '    subprocess.run(["python", str(Path(__file__).parent / "gamma.py")])\n'
    )
    (pkg / "undoc.py").write_text("def undoc_fn(x):\n    return x + 1\n")
    tests = root / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_alpha.py").write_text(
        '"""Tests exercising pkg.alpha / pkg.beta."""\n'
        "\n"
        "from unittest import mock\n"
        "\n"
        "import pytest\n"
        "\n"
        "import pkg.alpha\n"
        "\n"
        "\n"
        "@pytest.fixture\n"
        "def sample():\n"
        '    """Sample input value."""\n'
        "    return 3\n"
        "\n"
        "\n"
        "def test_alpha_uses_beta(sample):\n"
        '    """func_alpha combines beta and gamma results."""\n'
        "    assert pkg.alpha.func_alpha(sample) == 9\n"
        "\n"
        "\n"
        "def test_alpha_with_mock():\n"
        '    """Mocks pkg.beta.func_beta to isolate func_alpha\'s own logic."""\n'
        '    with mock.patch("pkg.beta.func_beta") as m:\n'
        "        m.return_value = 10\n"
        "        pkg.alpha.func_alpha(5)\n"
    )


def _scan(root: Path) -> None:
    """Run the current ``bin/scan-index`` once against *root* (asserts success)."""
    result = subprocess.run(
        [sys.executable, str(_PLUGIN_ROOT / "bin" / "scan-index"), "--root", str(root)],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(name="built_project", scope="module", params=_PATH_CLASSES)
def _built_project(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Path:
    """Build and scan the fixture project once per path class; return its root."""
    base = tmp_path_factory.mktemp("query_parity")
    root = base / request.param
    _write_fixture_project(root)
    _scan(root)
    return root


def _env() -> dict[str, str]:
    """Build the controlled subprocess environment for parity checks.

    ``PYTHONUTF8=1`` pins stdio/argv decoding to UTF-8 regardless of the host's console codepage (relevant on Windows,
    where a non-ASCII path would otherwise decode via cp1252 and diverge between the two child processes under
    comparison).
    """
    env = {**os.environ, "CODEMAP_LOGGING": "false", "PYTHONUTF8": "1"}
    env.pop("CODEMAP_INDEX_DIR", None)
    return env


def _run_old(
    old_scan_query: Path, args: list[str], cwd: Path, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke the checked-out pre-extraction ``scan-query``."""
    return subprocess.run(
        [sys.executable, str(old_scan_query), *args],
        cwd=str(cwd),
        env=_env(),
        capture_output=True,
        encoding="utf-8",
        input=stdin,
        timeout=30,
        check=False,
    )


def _run_new_bin(args: list[str], cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the current thin ``bin/scan-query`` launcher."""
    return subprocess.run(
        [sys.executable, str(_NEW_SCAN_QUERY), *args],
        cwd=str(cwd),
        env=_env(),
        capture_output=True,
        encoding="utf-8",
        input=stdin,
        timeout=30,
        check=False,
    )


def _run_cli_module(args: list[str], cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m codemap_py query ...`` (:mod:`codemap_py.cli` dispatch)."""
    env = {**_env(), "PYTHONPATH": str(_SRC)}
    return subprocess.run(
        [sys.executable, "-m", "codemap_py", "query", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        encoding="utf-8",
        input=stdin,
        timeout=30,
        check=False,
    )


@pytest.fixture(name="coupled_ranking_project")
def _coupled_ranking_project(tmp_path: Path) -> Path:
    """Create a frozen index whose total and internal dependency rankings differ.

    ``dep_count`` is deliberately inverse to ``internal_dep_count``.  This
    isolates the public ``coupled --top`` ordering contract from scanner
    details: a total-dependency sort would put ``pkg.echo`` first, while the
    intended internal-import sort starts with ``pkg.alpha``.
    """
    root = tmp_path / "coupled_ranking"
    root.mkdir()
    cache = root / ".cache" / "codemap"
    cache.mkdir(parents=True)
    modules = [
        {
            "name": "pkg.alpha",
            "path": "pkg/alpha.py",
            "status": "ok",
            "dep_count": 1,
            "direct_imports": ["pkg.bravo", "pkg.charlie", "pkg.delta", "pkg.echo", "pkg.foxtrot"],
        },
        {
            "name": "pkg.bravo",
            "path": "pkg/bravo.py",
            "status": "ok",
            "dep_count": 2,
            "direct_imports": ["pkg.charlie", "pkg.delta", "pkg.echo", "pkg.foxtrot"],
        },
        {
            "name": "pkg.charlie",
            "path": "pkg/charlie.py",
            "status": "ok",
            "dep_count": 3,
            "direct_imports": ["pkg.delta", "pkg.echo", "pkg.foxtrot"],
        },
        {
            "name": "pkg.delta",
            "path": "pkg/delta.py",
            "status": "ok",
            "dep_count": 4,
            "direct_imports": ["pkg.echo", "pkg.foxtrot"],
        },
        {
            "name": "pkg.echo",
            "path": "pkg/echo.py",
            "status": "ok",
            "dep_count": 5,
            "direct_imports": ["pkg.foxtrot"],
        },
        {
            "name": "pkg.foxtrot",
            "path": "pkg/foxtrot.py",
            "status": "ok",
            "dep_count": 6,
            "direct_imports": [],
        },
    ]
    index = {"scan_version": 11, "modules": modules}
    (cache / f"{root.name}.json").write_text(json.dumps(index), encoding="utf-8")
    return root


def _error_suffix(stderr: str) -> str:
    """Return the ``: error: ...`` tail of an argparse stderr message, prog-name-free.

    argparse's own usage/error banners embed ``prog`` (``os.path.basename(sys.argv[0])``, never pinned by
    ``_build_parser()``), which legitimately differs between the ``scan-query`` script and the ``codemap_py`` module.
    The substantive error text always follows the first
    ``": error: "`` marker and never itself contains a prog-derived token, so comparing that suffix (rather than the
    whole banner) verifies the actual error content is unchanged while not asserting on the known-divergent prog token.

    Example:
        >>> _error_suffix("usage: old: error: bad argument\\n")
        'bad argument\\n'
    """
    marker = ": error: "
    idx = stderr.find(marker)
    return stderr if idx == -1 else stderr[idx + len(marker) :]


_ADDITIVE_QUERY_KEYS = frozenset({"unique_total", "unique_qualified_names", "count_semantics"})
_V12_QUERY_ADDITIONS = {"fn-rdeps": frozenset({"resolved_qname"})}
_V13_REMOVED_NOT_COVERED = frozenset({"relative-import", "from-import-submodule"})
_COUNT_SEMANTIC_KEYS = {
    "undocumented": frozenset({"total", "unique_total"}),
    "uncovered": frozenset({"definition", "total", "showing", "unique_total"}),
}


def _restore_v13_not_covered(legacy: object, current: object) -> None:
    """Restore only v13 import-gap labels removed from current query payloads.

    Scan v13 resolves relative and ``from package import submodule`` edges that the frozen v11 query oracle reported as
    uncovered. The parity allowance is deliberately limited to those two labels and preserves the legacy order.
    """
    if isinstance(legacy, dict) and isinstance(current, dict):
        for key, legacy_value in legacy.items():
            current_value = current.get(key)
            if key == "not_covered" and isinstance(legacy_value, list) and isinstance(current_value, list):
                present = set(current_value)
                current_value.extend(
                    item for item in legacy_value if item in _V13_REMOVED_NOT_COVERED and item not in present
                )
                continue
            _restore_v13_not_covered(legacy_value, current_value)
    elif isinstance(legacy, list) and isinstance(current, list):
        for legacy_item, current_item in zip(legacy, current, strict=False):
            _restore_v13_not_covered(legacy_item, current_item)


def _drop_loaded_index_path(node: object) -> object:
    """Strip ``index.index_path`` from *node* in place and return it.

    The current engine reports the index file it actually loaded; the frozen v11 oracle predates the field, and the
    value is an absolute path under pytest's tmp dir, so it could never be byte-equal to a golden anyway. Parity is
    asserted on everything else. The field's own contract — that it comes from the load rather than from re-running the
    resolver — is pinned in ``tests/gate/test_cli_gate.py``, not here.
    """
    if isinstance(node, dict):
        block = node.get("index")
        if isinstance(block, dict):
            block.pop("index_path", None)
        for value in node.values():
            _drop_loaded_index_path(value)
    elif isinstance(node, list):
        for item in node:
            _drop_loaded_index_path(item)
    return node


def _assert_golden_query_parity(
    old: subprocess.CompletedProcess[str], new: subprocess.CompletedProcess[str], case: list[str]
) -> None:
    """Assert byte parity except the fixed additive metadata on two quality commands."""
    assert old.returncode == new.returncode
    assert old.stderr == new.stderr
    command = case[0]
    if command in _V12_QUERY_ADDITIONS:
        legacy = json.loads(old.stdout)
        current = _drop_loaded_index_path(json.loads(new.stdout))
        assert isinstance(legacy, dict)
        assert isinstance(current, dict)
        _restore_v13_not_covered(legacy, current)
        assert set(current) == set(legacy) | _V12_QUERY_ADDITIONS[command]
        assert {key: current[key] for key in legacy} == legacy
        assert current["resolved_qname"] == legacy["qname"]
        return
    if command not in _COUNT_SEMANTIC_KEYS:
        legacy = json.loads(old.stdout)
        current = _drop_loaded_index_path(json.loads(new.stdout))
        _restore_v13_not_covered(legacy, current)
        assert current == legacy
        return

    legacy = json.loads(old.stdout)
    current = _drop_loaded_index_path(json.loads(new.stdout))
    assert isinstance(legacy, dict)
    assert isinstance(current, dict)
    _restore_v13_not_covered(legacy, current)
    assert set(current) == set(legacy) | _ADDITIVE_QUERY_KEYS
    assert {key: current[key] for key in legacy} == legacy

    unique_names = current["unique_qualified_names"]
    assert isinstance(current["unique_total"], int)
    assert isinstance(unique_names, list)
    assert all(isinstance(name, str) and name for name in unique_names)
    assert unique_names == sorted(set(unique_names))
    assert current["unique_total"] == len(unique_names)
    legacy_names = {finding["qualified_name"] for finding in legacy[command]}
    assert unique_names == sorted(legacy_names)

    semantics = current["count_semantics"]
    assert isinstance(semantics, dict)
    assert set(semantics) == _COUNT_SEMANTIC_KEYS[command]
    assert all(isinstance(value, str) and value for value in semantics.values())


class TestOldVsNewBinParity:
    """Compare the golden pre-extraction ``scan-query`` with the current thin launcher.

    Both share the ``scan-query`` argv[0] basename, so stable error semantics are asserted identical. Every success
    payload is byte-identical except for ``undocumented`` and ``uncovered``: their legacy fields must be identical, and
    only their fixed unique-count metadata may be additive.
    """

    @pytest.mark.parametrize("case", _QUERY_CASES)
    def test_query_kind_matches_golden(self, old_scan_query: Path, built_project: Path, case: list[str]) -> None:
        """Every command preserves golden output, with a narrow quality-metadata exception."""
        old = _run_old(old_scan_query, case, built_project)
        new = _run_new_bin(case, built_project)
        _assert_golden_query_parity(old, new, case)

    def test_batch_matches_golden(self, old_scan_query: Path, built_project: Path) -> None:
        """Preserve golden graph results while validating the new per-item coverage contract."""
        items = [{"cmd": "deps", "args": ["pkg.alpha"]}, {"cmd": "symbol", "args": ["func_gamma"]}]
        stdin = json.dumps(items)
        old = _run_old(old_scan_query, ["batch"], built_project, stdin=stdin)
        new = _run_new_bin(["batch"], built_project, stdin=stdin)
        assert old.returncode == new.returncode
        legacy = json.loads(old.stdout)
        current = _drop_loaded_index_path(json.loads(new.stdout))
        _restore_v13_not_covered(legacy, current)
        for entry, item in zip(current["batch"], items, strict=True):
            standalone = _drop_loaded_index_path(
                json.loads(_run_new_bin([item["cmd"], *item["args"]], built_project).stdout)
            )
            assert entry["result"].pop("index") == standalone["index"]
        # The frozen oracle hoists the first query's coverage. That placement is
        # intentionally replaced; the dedicated mixed-result tests pin its summary.
        aggregate = current.pop("index")
        legacy.pop("index")
        assert aggregate["query_complete"] is True
        assert aggregate["truncated"] is False
        assert current == legacy
        assert old.stderr == new.stderr

    def test_missing_index_matches_golden(self, old_scan_query: Path, tmp_path: Path) -> None:
        """A project with no built index produces the same ``_die_json`` error old-vs-new."""
        old = _run_old(old_scan_query, ["list"], tmp_path)
        new = _run_new_bin(["list"], tmp_path)
        assert old.returncode == new.returncode == 1
        assert old.stdout == new.stdout
        assert old.stderr == new.stderr

    def test_unknown_subcommand_matches_golden(self, old_scan_query: Path, built_project: Path) -> None:
        """An invalid subcommand preserves its error despite additive flags."""
        old = _run_old(old_scan_query, ["totally-bogus-cmd"], built_project)
        new = _run_new_bin(["totally-bogus-cmd"], built_project)
        assert old.returncode == new.returncode == 2
        assert old.stdout == new.stdout == ""
        assert _error_suffix(old.stderr) == _error_suffix(new.stderr)
        assert "[--compact]" in new.stderr

    def test_missing_required_arg_matches_golden(self, old_scan_query: Path, built_project: Path) -> None:
        """A subcommand missing its required positional errors identically old-vs-new."""
        old = _run_old(old_scan_query, ["symbol"], built_project)
        new = _run_new_bin(["symbol"], built_project)
        assert old.returncode == new.returncode == 2
        assert old.stdout == new.stdout == ""
        assert old.stderr == new.stderr


class TestCrossPathParity:
    """Compare ``bin/scan-query`` with ``python -m codemap_py query ...``.

    Cross-path equivalence for the extracted package path (:mod:`codemap_py.cli` calling :func:`codemap_py.query.main`
    in-process under the shared-index read lease). Success-path JSON output is asserted byte-identical; the two
    argparse-native usage-error cases compare via :func:`_error_suffix` since their ``prog`` token legitimately differs
    (see that helper's docstring).
    """

    @pytest.mark.parametrize("case", _QUERY_CASES)
    def test_query_kind_matches_module_dispatch(self, built_project: Path, case: list[str]) -> None:
        """Every non-composite subcommand is byte-identical bin-vs-module-dispatch."""
        new_bin = _run_new_bin(case, built_project)
        cli = _run_cli_module(case, built_project)
        assert new_bin.returncode == cli.returncode
        assert new_bin.stdout == cli.stdout
        assert new_bin.stderr == cli.stderr

    def test_batch_matches_module_dispatch(self, built_project: Path) -> None:
        """The ``batch`` composite command matches bin-vs-module-dispatch (stdin JSON array)."""
        stdin = json.dumps([{"cmd": "deps", "args": ["pkg.alpha"]}, {"cmd": "symbol", "args": ["func_gamma"]}])
        new_bin = _run_new_bin(["batch"], built_project, stdin=stdin)
        cli = _run_cli_module(["batch"], built_project, stdin=stdin)
        assert new_bin.returncode == cli.returncode
        assert new_bin.stdout == cli.stdout
        assert new_bin.stderr == cli.stderr

    def test_missing_index_matches_module_dispatch(self, tmp_path: Path) -> None:
        """A missing index produces the same ``_die_json`` error bin-vs-module-dispatch.

        No argparse usage banner is involved (this is a plain JSON error object), so full byte identity holds including
        stderr.
        """
        new_bin = _run_new_bin(["list"], tmp_path)
        cli = _run_cli_module(["list"], tmp_path)
        assert new_bin.returncode == cli.returncode == 1
        assert new_bin.stdout == cli.stdout
        assert new_bin.stderr == cli.stderr

    def test_unknown_subcommand_error_content_matches_module_dispatch(self, built_project: Path) -> None:
        """An invalid subcommand's error content (prog token aside) matches bin-vs-module."""
        new_bin = _run_new_bin(["totally-bogus-cmd"], built_project)
        cli = _run_cli_module(["totally-bogus-cmd"], built_project)
        assert new_bin.returncode == cli.returncode == 2
        assert new_bin.stdout == cli.stdout == ""
        assert _error_suffix(new_bin.stderr) == _error_suffix(cli.stderr)


@pytest.mark.parametrize("runner", [pytest.param(_run_new_bin, id="bin"), pytest.param(_run_cli_module, id="module")])
def test_coupled_top_five_is_ordered_by_internal_dependency_count(
    coupled_ranking_project: Path,
    runner: object,
) -> None:
    """Sort on internal imports, not ``dep_count``.

    The deliberate inverse ``dep_count`` order means a total-dependency sort would look plausible but violate the
    command's documented coupling metric.
    """
    result = runner(["coupled", "--top", "5"], coupled_ranking_project)  # type: ignore[operator]

    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)["coupled"]
    assert [row["name"] for row in rows] == ["pkg.alpha", "pkg.bravo", "pkg.charlie", "pkg.delta", "pkg.echo"]
    assert [row["internal_dep_count"] for row in rows] == [5, 4, 3, 2, 1]
    assert [row["dep_count"] for row in rows] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("runner", [pytest.param(_run_new_bin, id="bin"), pytest.param(_run_cli_module, id="module")])
def test_coupled_help_explains_internal_import_ranking(coupled_ranking_project: Path, runner: object) -> None:
    """Public help must state the ordering metric behind ``coupled --top``.

    Prevents a user from interpreting a rank as highest total ``dep_count`` and carrying that mismatch into benchmark
    ground truth or an agent prompt.
    """
    result = runner(["coupled", "--help"], coupled_ranking_project)  # type: ignore[operator]

    assert result.returncode == 0
    assert "internal import count" in result.stdout.lower()

    def test_missing_required_arg_error_content_matches_module_dispatch(self, built_project: Path) -> None:
        """A missing required positional's error content matches bin-vs-module (prog aside)."""
        new_bin = _run_new_bin(["symbol"], built_project)
        cli = _run_cli_module(["symbol"], built_project)
        assert new_bin.returncode == cli.returncode == 2
        assert new_bin.stdout == cli.stdout == ""
        assert _error_suffix(new_bin.stderr) == _error_suffix(cli.stderr)


@pytest.fixture(name="diff_impact_project", scope="module", params=_PATH_CLASSES)
def _diff_impact_project(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Path:
    """Git repo with one committed baseline, then one uncommitted tracked edit.

    Isolated from ``built_project`` — module-scoped, not class-scoped-as-instance-method (pytest deprecates the latter),
    and its own fixture rather than sharing ``built_project`` so the working-tree mutation this test needs never leaks
    into the other parametrized cases that assume a pristine, unmutated tree.
    """
    base = tmp_path_factory.mktemp("query_parity_git")
    root = base / request.param
    _write_fixture_project(root)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=str(root), check=True, timeout=10)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(root), check=True, timeout=10)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, timeout=10)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(root), check=True, timeout=10)
    _scan(root)
    gamma = root / "pkg" / "gamma.py"
    gamma.write_text(gamma.read_text().replace("return x + 1", "return x + 1  # trivial working-tree change"))
    return root


class TestDiffImpactParity:
    """Provide an isolated Git working tree for diff-impact parity checks.

    Uses the module-level :func:`diff_impact_project` fixture (committed baseline, then one working-tree edit to a
    tracked, imported-and-called leaf module).
    """

    def test_diff_impact_matches_golden(self, old_scan_query: Path, diff_impact_project: Path) -> None:
        """Match both query engines over a real working-tree edit."""
        old = _run_old(old_scan_query, ["diff-impact", "--base", "HEAD"], diff_impact_project)
        new = _run_new_bin(["diff-impact", "--base", "HEAD"], diff_impact_project)
        assert old.returncode == new.returncode == 0
        # Compared as parsed JSON rather than as bytes, so the one deliberately additive
        # field (the loaded index path, absent from the frozen oracle) can be dropped.
        assert _drop_loaded_index_path(json.loads(new.stdout)) == json.loads(old.stdout)
        assert old.stderr == new.stderr

    def test_diff_impact_matches_module_dispatch(self, diff_impact_project: Path) -> None:
        """Match bin-vs-module-dispatch."""
        new_bin = _run_new_bin(["diff-impact", "--base", "HEAD"], diff_impact_project)
        cli = _run_cli_module(["diff-impact", "--base", "HEAD"], diff_impact_project)
        assert new_bin.returncode == cli.returncode == 0
        assert new_bin.stdout == cli.stdout
        assert new_bin.stderr == cli.stderr
