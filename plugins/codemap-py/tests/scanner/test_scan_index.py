"""Tests for the scan-index implementation: index creation, incremental scans and per-file parsing.

Verifies index creation through the real ``bin/scan-index`` CLI, incremental update behaviour, dynamic import
extraction, config-file reference scanning, exclusion loading and pruning, collision de-duplication, degraded-file
reporting and atomic index writes.

Fixture layout (shared ``conftest.py`` sources):     gamma.py          — leaf module, no imports; defines func_gamma
beta.py           — imports gamma; defines func_beta calling func_gamma     alpha.py          — imports beta, gamma;
defines func_alpha calling func_beta     pkg/__init__.py   — empty     pkg/delta.py      — imports alpha; defines
func_delta calling func_alpha
"""

from __future__ import annotations

import ast
import errno
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from _scan_helpers import index_path_for, write_tree

# The scan-index implementation now lives in the package:
# discovery/parsing -> codemap_py.scanner, graph/dedup/index-write -> codemap_py.graph.
# bin/scan-index is now a thin launcher, so unit-level access imports the package
# modules directly instead of loading the bin/ script via SourceFileLoader.
_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import codemap_py.graph as _graph_mod  # noqa: E402  (needs the sys.path insert above)
import codemap_py.rwgate as _rwgate_mod  # noqa: E402
import codemap_py.scanner as _scanner_mod  # noqa: E402

extract_dynamic_imports = _scanner_mod.extract_dynamic_imports
scan_config_refs = _scanner_mod.scan_config_refs
_classify_entity = _scanner_mod._classify_entity
EntityType = _scanner_mod.EntityType
_load_exclusions = _scanner_mod._load_exclusions
_iter_python_files = _scanner_mod._iter_python_files
_parse_file = _scanner_mod._parse_file
_dedup_modules = _graph_mod._dedup_modules
_dedup_key = _graph_mod._dedup_key
_replace_with_windows_retry = _rwgate_mod._replace_with_windows_retry


def _scan_command(scan_index: Path, root: Path, *extra: str) -> list[str]:
    """Command line that runs the scan-index CLI over *root*."""
    return [sys.executable, str(scan_index), "--root", str(root), *extra]


def _run_scan(scan_index: Path, root: Path, *extra: str, env: dict[str, str] | None = None) -> None:
    """Run the scan-index CLI over *root* with cwd at *root*, asserting a zero exit code."""
    result = subprocess.run(
        _scan_command(scan_index, root, *extra), capture_output=True, text=True, cwd=str(root), env=env
    )
    assert result.returncode == 0, result.stderr


def _scan_and_load(scan_index: Path, root: Path, *extra: str, env: dict[str, str] | None = None) -> dict:
    """Run the scan-index CLI over *root* and return the parsed index it wrote."""
    _run_scan(scan_index, root, *extra, env=env)
    assert index_path_for(root).exists(), "scan-index did not produce an index file"
    return json.loads(index_path_for(root).read_text())


def _relative_paths(files: list[Path], root: Path) -> set[str]:
    """POSIX paths of *files* relative to *root*."""
    return {path.relative_to(root).as_posix() for path in files}


def _pkg_mod_collision(scan_index: Path, root: Path, hash_seed: str) -> tuple[str, tuple[str, ...]]:
    """Scan *root* under *hash_seed* and return ``(kept, sorted dropped)`` of the ``pkg.mod`` collision record."""
    index = _scan_and_load(scan_index, root, env={**os.environ, "PYTHONHASHSEED": hash_seed})
    collision = next((c for c in index["collisions"] if c["name"] == "pkg.mod"), None)
    assert collision is not None, f"no pkg.mod collision recorded: {index['collisions']}"
    return collision["kept"], tuple(sorted(collision["dropped"]))


def _run_scans_concurrently(scan_index: Path, root: Path, count: int) -> list[tuple[int, str]]:
    """Start *count* scan-index processes together and return each ``(exit code, stderr)``."""
    procs = [
        subprocess.Popen(
            _scan_command(scan_index, root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(root),
        )
        for _ in range(count)
    ]
    results = []
    for proc in procs:
        _, stderr = proc.communicate(timeout=60)
        results.append((proc.returncode, stderr))
    return results


@mock.patch("codemap_py.graph.multiprocessing.get_context", autospec=True, return_value=object())
@mock.patch(
    "codemap_py.graph.ProcessPoolExecutor",
    autospec=True,
    side_effect=PermissionError(errno.EPERM, "Operation not permitted"),
)
def test_scan_falls_back_to_serial_when_process_pool_is_forbidden(
    mock_pool: mock.Mock,
    mock_context: mock.Mock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A sandbox-level pool EPERM falls back to serial parsing with the same result.

    The managed sandbox forbids creating a process pool. Scanning must still index every module (deterministically, in-
    process) and tell the user why parsing went serial instead of failing the whole scan.
    """
    (tmp_path / "module.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    monkeypatch.setattr(_graph_mod.sys, "platform", "darwin")

    result = _graph_mod.scan(tmp_path)

    assert mock_context.called
    assert mock_pool.called
    assert [module["name"] for module in result["modules"]] == ["module"]
    assert result["modules"][0]["status"] == "ok"
    assert "process pool unavailable (EPERM); parsing serially" in capsys.readouterr().err


def test_creates_index(tmp_path, gamma_src, beta_src, alpha_src, delta_src, scan_index):
    """Scan-index writes .cache/codemap/<name>.json containing all modules.

    A small five-module project (one leaf, two dependents, one package) is scanned from scratch; every module must
    appear in the written index under its dotted name.
    """
    write_tree(
        tmp_path,
        {
            "gamma.py": gamma_src,
            "beta.py": beta_src,
            "alpha.py": alpha_src,
            "pkg/__init__.py": "",
            "pkg/delta.py": delta_src,
        },
    )

    index = _scan_and_load(scan_index, tmp_path)

    assert {"alpha", "beta", "gamma", "pkg", "pkg.delta"}.issubset({m["name"] for m in index["modules"]})


def test_incremental_picks_up_new_file(tmp_path, scan_index):
    """Adding a file after the initial scan, ``--incremental`` indexes it.

    A full scan indexes ``base.py``; ``new_mod.py`` is then added and only ``--incremental`` is run, which must still
    discover the newly created file.
    """
    (tmp_path / "base.py").write_text("def f(): pass\n")
    _run_scan(scan_index, tmp_path)
    (tmp_path / "new_mod.py").write_text("import base\n")

    index = _scan_and_load(scan_index, tmp_path, "--incremental")

    assert "new_mod" in {m["name"] for m in index["modules"]}


class TestExtractDynamicImports:
    """Unit tests for extract_dynamic_imports — AST-based dynamic import detection."""

    @pytest.mark.parametrize(
        ("src", "expected"),
        [
            # importlib.import_module with a string constant is captured.
            pytest.param(
                'import importlib\nimportlib.import_module("my.pkg")',
                [{"literal": "my.pkg", "line": 2}],
                id="importlib-string-literal",
            ),
            # __import__ with a string constant is captured.
            pytest.param(
                "__import__('os.path')", [{"literal": "os.path", "line": 1}], id="dunder-import-string-literal"
            ),
            # Several literal calls in one file are all returned, in source order.
            pytest.param(
                'import importlib\nimportlib.import_module("pkg.a")\nimportlib.import_module("pkg.b")\n',
                [{"literal": "pkg.a", "line": 2}, {"literal": "pkg.b", "line": 3}],
                id="multiple-calls-in-source-order",
            ),
            # Ignore dynamic imports whose module name is not a literal.
            pytest.param("importlib.import_module(name)", [], id="dynamic-expression-skipped"),
            # Plain file with no dynamic imports returns empty list.
            pytest.param("import os\nimport sys\n", [], id="no-dynamic-imports-returns-empty"),
        ],
    )
    def test_literal_dynamic_imports_are_captured(self, src: str, expected: list[dict]):
        """Only string-literal ``importlib.import_module`` / ``__import__`` calls are captured, with their line.

        Static analysis can only record module names written as literals; calls with a computed name must be skipped
        instead of guessed at.
        """
        assert extract_dynamic_imports(ast.parse(src)) == expected


class TestClassifyEntity:
    """Unit tests for _classify_entity — entity_type + package derivation."""

    @pytest.mark.parametrize(
        ("path_str", "name", "expected_type", "expected_pkg"),
        [
            pytest.param("tests/test_foo.py", "tests.test_foo", EntityType.TEST, "tests", id="tests-dir"),
            pytest.param("test_bar.py", "test_bar", EntityType.PKG, "test_bar", id="root-test-file"),
            pytest.param("src/pkg/conftest.py", "pkg.conftest", EntityType.TEST, "pkg", id="conftest"),
            pytest.param("docs/conf.py", "docs.conf", EntityType.DOCS, "docs", id="docs-dir"),
            pytest.param("doc/api.py", "doc.api", EntityType.DOCS, "doc", id="doc-singular"),
            pytest.param("examples/demo.py", "examples.demo", EntityType.EXAMPLE, "examples", id="examples-dir"),
            pytest.param("example/usage.py", "example.usage", EntityType.EXAMPLE, "example", id="example-singular"),
            pytest.param("mypackage/core.py", "mypackage.core", EntityType.PKG, "mypackage", id="pkg-module"),
            pytest.param("mypackage/sub/mod.py", "mypackage.sub.mod", EntityType.PKG, "mypackage", id="pkg-submodule"),
            pytest.param("standalone.py", "standalone", EntityType.PKG, "standalone", id="root-script"),
            pytest.param(
                "src/mypackage/core.py", "src.mypackage.core", EntityType.PKG, "mypackage", id="src-layout-strip"
            ),
            pytest.param("src/pkg/conftest.py", "src.pkg.conftest", EntityType.TEST, "pkg", id="src-layout-test-strip"),
        ],
    )
    def test_entity_type_and_package_by_path(self, path_str, name, expected_type, expected_pkg):
        """_classify_entity returns the (entity_type, package) pair for each path pattern.

        Tests, docs and examples are told apart from production modules by directory or file name, with a leading
        ``src`` layout directory stripped from the package.
        """
        assert _classify_entity(Path(path_str), name) == (expected_type, expected_pkg)


class TestScanConfigRefs:
    """Unit tests for scan_config_refs — config file module-path reference scanner."""

    @pytest.mark.parametrize(
        ("filename", "content", "module"),
        [
            pytest.param(
                "pyproject.toml", '[tool.mypy]\nmodule = "mypackage.utils"\n', "mypackage.utils", id="pyproject"
            ),
            pytest.param("setup.cfg", "[options]\npackages = mypackage.core\n", "mypackage.core", id="setup-cfg"),
            pytest.param("conf.yaml", "defaults:\n  - module: mypackage.model\n", "mypackage.model", id="yaml-at-root"),
        ],
    )
    def test_known_module_in_config_file_detected(self, tmp_path: Path, filename: str, content: str, module: str):
        """A known module name in pyproject.toml, setup.cfg or a root-level YAML file is reported with its file.

        Config files name modules (mypy overrides, package lists, Hydra defaults); a reference there keeps the module
        alive for dead-code and impact queries.
        """
        (tmp_path / filename).write_text(content)

        refs = scan_config_refs(tmp_path, {module})

        assert refs[module][0]["file"] == filename

    @pytest.mark.parametrize(
        "files",
        [
            pytest.param({"pyproject.toml": 'name = "something.random"\n'}, id="unrelated-string"),
            pytest.param({}, id="no-config-files"),
        ],
    )
    def test_no_reference_returns_empty(self, tmp_path: Path, files: dict[str, str]):
        """Config text that names no known module, or no config files at all, yields an empty mapping.

        Only exact known module names count as references, so unrelated dotted strings must not be reported.
        """
        write_tree(tmp_path, files)

        assert scan_config_refs(tmp_path, {"mypackage.utils"}) == {}

    def test_context_truncated_to_120(self, tmp_path: Path):
        """Context field is at most 120 characters.

        A matching line carrying 200 trailing characters must still be reported, with its context cut to 120 so the
        index does not balloon on minified or generated config lines.
        """
        (tmp_path / "setup.cfg").write_text("x = mypackage.utils # " + "a" * 200 + "\n")

        refs = scan_config_refs(tmp_path, {"mypackage.utils"})

        assert refs["mypackage.utils"]
        assert all(len(r["context"]) <= 120 for r in refs["mypackage.utils"])


class TestLoadExclusions:
    """Loading extra dir-name/glob exclusions from pyproject.toml and .codemapignore."""

    def test_pyproject_exclude_dirs_and_globs(self, tmp_path: Path):
        """[tool.codemap] exclude splits bare names into dirs and glob entries into globs.

        Bare names prune whole directories during the walk; entries with glob characters match individual files, and
        each entry remembers which file it came from.
        """
        write_tree(tmp_path, {"pyproject.toml": '[tool.codemap]\nexclude = ["vendor", "gen/*.py"]\n'})

        ex = _load_exclusions(tmp_path)

        assert ex.dirs == frozenset({"vendor"})
        assert ex.globs == ("gen/*.py",)
        assert ex.sources == {"vendor": "pyproject.toml", "gen/*.py": "pyproject.toml"}

    def test_codemapignore_strips_comments_and_slashes(self, tmp_path: Path):
        """.codemapignore drops comments/blanks and trailing slashes on dir names.

        The file is hand-edited, so comments, blank lines, padding and ``dir/`` spellings must all normalize to the same
        exclusion entries.
        """
        write_tree(tmp_path, {".codemapignore": "# header\nvendored/\n\n  build_out/*.py  \n"})

        ex = _load_exclusions(tmp_path)

        assert ex.dirs == frozenset({"vendored"})
        assert ex.globs == ("build_out/*.py",)

    def test_both_sources_merge(self, tmp_path: Path):
        """Entries from both files merge; provenance records the first-seen origin.

        A project may configure exclusions in both places; the union applies and each entry reports where it was
        declared, which the scan summary surfaces.
        """
        write_tree(tmp_path, {"pyproject.toml": '[tool.codemap]\nexclude = ["a"]\n', ".codemapignore": "b\n"})

        ex = _load_exclusions(tmp_path)

        assert ex.dirs == frozenset({"a", "b"})
        assert ex.sources == {"a": "pyproject.toml", "b": ".codemapignore"}

    def test_absent_config_yields_empty(self, tmp_path: Path):
        """Absent config files yield empty exclusions.

        A project without any exclusion config must scan everything outside the built-in skip list.
        """
        ex = _load_exclusions(tmp_path)

        assert ex.dirs == frozenset()
        assert ex.globs == ()


class TestIterPythonFilesExclusions:
    """_iter_python_files honours SKIP_DIRS additions and configured exclusions."""

    def test_prunes_worktree_copy_and_vendored_entry(self, tmp_path: Path):
        """A .claude/worktrees copy and a .codemapignore-named dir are both pruned; meta counts them.

        Agent worktrees and vendored checkouts hold duplicate sources that would pollute the index with colliding module
        names, so only the real ``app.py`` may be walked and each pruned root is counted.
        """
        write_tree(
            tmp_path,
            {
                "app.py": "x = 1\n",
                ".claude/worktrees/agent-x/app.py": "x = 1\n",
                "pytorch-lightning-master/trainer.py": "y = 2\n",
                ".codemapignore": "pytorch-lightning-master\n",
            },
        )

        files, counts = _iter_python_files(tmp_path, _load_exclusions(tmp_path))

        assert _relative_paths(files, tmp_path) == {"app.py"}
        assert counts["pytorch-lightning-master"] == 1

    def test_glob_exclusion_skips_matching_files(self, tmp_path: Path):
        """A glob entry removes matching files while leaving siblings indexed.

        ``gen/*.py`` names generated sources; only those files are skipped and the skip is counted under the pattern.
        """
        write_tree(
            tmp_path,
            {
                "keep.py": "a = 1\n",
                "gen/auto.py": "b = 2\n",
                "pyproject.toml": '[tool.codemap]\nexclude = ["gen/*.py"]\n',
            },
        )

        files, counts = _iter_python_files(tmp_path, _load_exclusions(tmp_path))

        assert _relative_paths(files, tmp_path) == {"keep.py"}
        assert counts["gen/*.py"] == 1


class TestDedupKey:
    """_dedup_key ranks candidate paths: under-src > shortest > lexicographic."""

    @pytest.mark.parametrize(
        ("winner", "loser", "src_root"),
        [
            # A path under the source root outranks one outside it regardless of length.
            pytest.param("src/m.py", "a/b/c/m.py", "src", id="under-src-root-wins"),
            # With no src root, fewer path components wins.
            pytest.param("m.py", "pkg/m.py", "", id="shortest-path-wins-when-src-tied"),
            # Equal depth and src status falls back to lexicographic order.
            pytest.param("a/m.py", "b/m.py", "", id="lexicographic-tiebreak"),
        ],
    )
    def test_winner_sorts_before_loser(self, winner: str, loser: str, src_root: str):
        """Candidate paths rank under-src first, then shortest, then lexicographic.

        The key decides which copy of a duplicated module name survives, so each tie-break level must be pinned.
        """
        assert _dedup_key(winner, src_root) < _dedup_key(loser, src_root)


class TestDedupModules:
    """_dedup_modules produces a deterministic winner and records collisions."""

    @pytest.mark.parametrize("reverse", [False, True])
    def test_winner_independent_of_input_order(self, reverse: bool):
        """Same qualname at two paths yields the same winner regardless of input order.

        The scan order of files is filesystem-dependent, so the surviving copy and the collision record must come out
        identical for an input order and its reverse.
        """
        entries = [{"name": "pkg.mod", "path": "copy/pkg/mod.py"}, {"name": "pkg.mod", "path": "src/pkg/mod.py"}]
        order = list(reversed(entries)) if reverse else entries

        kept, collisions = _dedup_modules(list(order), "src")

        assert [m["path"] for m in kept] == ["src/pkg/mod.py"]
        assert collisions == [{"name": "pkg.mod", "kept": "src/pkg/mod.py", "dropped": ["copy/pkg/mod.py"]}]

    def test_no_collision_keeps_all(self):
        """Distinct names are all kept with an empty collision list.

        De-duplication must be a no-op when no two modules share a qualified name.
        """
        entries = [{"name": "a", "path": "a.py"}, {"name": "b", "path": "b.py"}]

        kept, collisions = _dedup_modules(entries, "")

        assert {m["name"] for m in kept} == {"a", "b"}
        assert collisions == []


class TestScanExclusionMeta:
    """End-to-end: a real scan writes excluded_roots and collisions into the index meta."""

    def test_excluded_roots_and_collisions_in_index(self, tmp_path: Path, scan_index):
        """Scanning a polluted tree prunes the worktree copy and records both meta keys.

        The tree holds the real ``app.py`` plus a worktree copy and a ``.codemapignore``-d vendored copy; the CLI must
        index only the real file, list the vendored pattern under ``excluded_roots`` and report no collisions.
        """
        write_tree(
            tmp_path,
            {
                "app.py": "VALUE = 1\n",
                ".claude/worktrees/agent-x/app.py": "VALUE = 99\n",
                "vendor_copy/app.py": "VALUE = 7\n",
                ".codemapignore": "vendor_copy\n",
            },
        )

        index = _scan_and_load(scan_index, tmp_path)

        assert {m["path"] for m in index["modules"]} == {"app.py"}
        assert "vendor_copy" in {r["pattern"] for r in index["excluded_roots"]}
        assert index["collisions"] == []

    def test_collision_recorded_and_winner_stable(self, tmp_path: Path, scan_index):
        """Two package trees sharing a top-level name collide; the winner is identical under every hash seed.

        The canonical ``pkg`` tree and a whole-tree copy under ``wt/`` both resolve to the dotted name ``pkg.mod``. Only
        one survives; the collision record names both, and the winner must not depend on set iteration order, which
        ``PYTHONHASHSEED`` randomises between runs (acceptance).
        """
        write_tree(
            tmp_path,
            {"pkg/__init__.py": "", "wt/pkg/__init__.py": "", "pkg/mod.py": "Z = 1\n", "wt/pkg/mod.py": "Z = 2\n"},
        )

        records = {_pkg_mod_collision(scan_index, tmp_path, seed) for seed in ("0", "1", "2", "3", "4")}

        assert len(records) == 1
        kept, dropped = records.pop()
        assert {kept, *dropped} == {"pkg/mod.py", "wt/pkg/mod.py"}


class TestEncodingDegradation:
    """A file whose bytes are not valid UTF-8 must be marked degraded, not silently indexed."""

    def test_parse_file_marks_invalid_utf8_degraded(self, tmp_path: Path):
        """_parse_file returns a degraded entry with an 'encoding' reason for non-UTF-8 bytes.

        Parsing replacement characters would index text the author never wrote, so such a file must carry no symbols.
        """
        bad = tmp_path / "bad.py"
        bad.write_bytes(b"x = '\xff\xfe not utf8'\n")

        entry = _parse_file(bad, tmp_path, tmp_path)

        assert entry["status"] == "degraded"
        assert entry["reason"].startswith("encoding:")
        assert "symbols" not in entry  # corrupted source never parsed into symbols

    def test_scan_index_marks_invalid_utf8_degraded(self, tmp_path: Path, scan_index):
        """End-to-end: an invalid-UTF-8 module lands as degraded in the index, not status ok.

        A healthy module sits beside a corrupt one; the CLI must succeed overall yet flag only the corrupt file.
        """
        (tmp_path / "good.py").write_text("y = 1\n")
        (tmp_path / "bad.py").write_bytes(b"x = '\xff\xfe'\n")

        index = _scan_and_load(scan_index, tmp_path)

        bad = next(m for m in index["modules"] if m["path"] == "bad.py")
        assert bad["status"] == "degraded"
        assert bad["reason"].startswith("encoding:")


class TestFileTooLargeDegradation:
    """An oversized file is marked degraded with a 'reason' key so the print loop never KeyErrors."""

    def test_too_large_uses_reason_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """_parse_file over the size cap returns a degraded entry keyed on 'reason', not 'error'.

        The size cap is lowered to 8 bytes so a tiny file trips it; the entry must use the unified ``reason`` key.
        """
        monkeypatch.setattr(_scanner_mod.discovery, "_MAX_FILE_SIZE_BYTES", 8)
        big = tmp_path / "big.py"
        big.write_text("x = 1234567890\n")  # >8 bytes on disk

        entry = _parse_file(big, tmp_path, tmp_path)

        assert entry["status"] == "degraded"
        assert "error" not in entry  # contract unified on 'reason'
        assert entry["reason"].startswith("file too large")

    def test_every_degraded_branch_exposes_reason(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Every degraded branch of _parse_file exposes 'reason' — the key the print loop reads.

        An oversized file, invalid UTF-8 bytes and a syntax error each take a different degraded branch; the scan
        summary reads ``m['reason']`` for all of them and must never raise KeyError.
        """
        monkeypatch.setattr(_scanner_mod.discovery, "_MAX_FILE_SIZE_BYTES", 8)
        too_large = tmp_path / "big.py"
        too_large.write_text("x = 1234567890\n")
        bad_utf8 = tmp_path / "bad.py"
        bad_utf8.write_bytes(b"x = '\xff\xfe'\n")
        bad_syntax = tmp_path / "broken.py"
        bad_syntax.write_text("def (:\n")

        entries = [_parse_file(p, tmp_path, tmp_path) for p in (too_large, bad_utf8, bad_syntax)]

        assert all(e["status"] == "degraded" for e in entries)
        assert all("reason" in e for e in entries)  # print loop does m['reason'] — must never KeyError


class TestAtomicIndexWrite:
    """Index write goes through a temp file so a kill mid-write leaves the prior index readable."""

    def test_no_stale_tmp_and_index_valid_after_write(self, tmp_path: Path, scan_index):
        """A completed run leaves a parseable index and no leftover .json.tmp sidecar.

        The index is written to a temp file and renamed into place; after a normal run the rename must have consumed the
        temp file.
        """
        (tmp_path / "gamma.py").write_text("g = 1\n")

        _scan_and_load(scan_index, tmp_path)  # parses: the final index is complete

        assert not (index_path_for(tmp_path).parent / f"{tmp_path.name}.json.tmp").exists()

    def test_rerun_preserves_readable_index(self, tmp_path: Path, scan_index):
        """Re-running over an existing index yields a still-parseable index (replace is atomic).

        A first scan creates the index and a second scan replaces it; the replacement must leave a complete index that
        still lists the module.
        """
        (tmp_path / "gamma.py").write_text("g = 1\n")
        _run_scan(scan_index, tmp_path)

        index = _scan_and_load(scan_index, tmp_path)

        assert any(m["name"] == "gamma" for m in index["modules"])

    def test_simulated_windows_replace_retries_transient_sharing_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """WinError 5 retries without replacing the destination non-atomically.

        On Windows an antivirus scanner or reader can briefly lock the index. The first two ``os.replace`` calls fail
        with a sharing error; the helper must retry (sleep stubbed) until the third, real replace lands the new file.
        """
        temp = tmp_path / "index.tmp"
        target = tmp_path / "index.json"
        temp.write_text('{"complete": true}')
        original_replace = _rwgate_mod.os.replace
        attempts = 0

        def _fail_twice(path, destination):
            """Raise transient Windows sharing errors for the first two replaces."""
            nonlocal attempts
            if Path(path) == temp and attempts < 2:
                attempts += 1
                error = PermissionError("destination temporarily locked")
                error.winerror = 5
                raise error
            return original_replace(path, destination)

        monkeypatch.setattr(_rwgate_mod.sys, "platform", "win32")
        monkeypatch.setattr(_rwgate_mod.time, "sleep", lambda _delay: None)
        monkeypatch.setattr(_rwgate_mod.os, "replace", _fail_twice)

        _replace_with_windows_retry(temp, target)

        assert attempts == 2
        assert json.loads(target.read_text()) == {"complete": True}

    def test_concurrent_writers_leave_valid_index(self, tmp_path: Path, scan_index):
        """Two writers racing on the same root must not corrupt the index (PID-qualified temp).

        The bg refresh, post-commit hook, and self-heal all invoke scan-index uncoordinated. A shared fixed ".json.tmp"
        would let two "w" opens interleave into corrupt bytes; the PID-qualified temp gives each writer its own file so
        every os.replace promotes a complete index. Whichever writer wins, the final file must parse and no PID-suffixed
        temp may leak.
        """
        (tmp_path / "gamma.py").write_text("g = 1\n")

        results = _run_scans_concurrently(scan_index, tmp_path, 2)

        assert [code for code, _ in results] == [0, 0], results
        index = json.loads(index_path_for(tmp_path).read_text())  # never corrupt/truncated
        assert any(m["name"] == "gamma" for m in index["modules"])
        assert not list(
            index_path_for(tmp_path).parent.glob(f"{tmp_path.name}.json.*.tmp")
        )  # no PID-suffixed temp leaked


def test_dot_directories_pruned_from_walk(tmp_path, gamma_src, scan_index):
    """Any dot-dir (vendored checkout, sandbox, agent scratch) is excluded from the index.

    Dot-directories hold vendored checkouts, sandboxes and agent scratch copies that are not part of the project's
    import space; only the real ``gamma`` module may be indexed.
    """
    write_tree(
        tmp_path,
        {
            "gamma.py": gamma_src,
            ".sandbox/vendored/src/vendored_mod.py": "def v():\n    return 1\n",
            ".agents/scratch.py": "def s():\n    return 2\n",
        },
    )

    index = _scan_and_load(scan_index, tmp_path)

    names = {m["name"] for m in index["modules"]}
    assert "gamma" in names
    assert not any("vendored_mod" in n or "scratch" in n for n in names)
