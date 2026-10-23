"""Real CLI regressions for automatic working-tree freshness and checkout ownership."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _git(root: Path, *args: str) -> str:
    """Run Git against a disposable repository and return stdout."""
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture(name="working_tree")
def _working_tree(tmp_path: Path, scan_index: Path) -> tuple[Path, Path]:
    """Create an indexed committed graph with no importers of the leaf."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".cache/\n.worktrees/\n", encoding="utf-8")
    (root / "leaf.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    (root / "caller.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    result = subprocess.run([sys.executable, str(scan_index), "--root", str(root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return root, root / ".cache" / "codemap" / f"{root.name}.json"


def _query(scan_query: Path, root: Path, *args: str, frozen: bool = False) -> dict:
    """Run a real query from the selected checkout without inheriting frozen mode accidentally."""
    env = {**os.environ}
    env.pop("SCAN_NO_AUTOBUILD", None)
    if frozen:
        env["SCAN_NO_AUTOBUILD"] = "1"
    result = subprocess.run([sys.executable, str(scan_query), *args], cwd=root, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


@pytest.mark.integration
def test_query_refreshes_unstaged_and_new_importers(working_tree, scan_query: Path) -> None:
    """Both working-tree edges appear automatically without staging or committing source."""
    root, _ = working_tree
    (root / "caller.py").write_text("import leaf\n", encoding="utf-8")
    (root / "new.py").write_text("import leaf\n", encoding="utf-8")
    before = _git(root, "status", "--porcelain")
    result = _query(scan_query, root, "rdeps", "leaf")
    assert set(result["imported_by"]) == {"caller", "new"}
    assert result["index"]["stale"] is False
    assert result["index"]["query_complete"] is True
    assert _git(root, "status", "--porcelain") == before


@pytest.mark.parametrize("delete_final", [False, True])
@pytest.mark.integration
def test_query_removes_deleted_modules(working_tree, scan_query: Path, delete_final: bool) -> None:
    """Deleting eligible files, including the final file, updates the stored graph."""
    root, index_path = working_tree
    (root / "caller.py").unlink()
    if delete_final:
        (root / "leaf.py").unlink()
    _query(scan_query, root, "list")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert {module["name"] for module in index["modules"]} == (set() if delete_final else {"leaf"})


@pytest.mark.parametrize("mode", ["environment", "flag"])
@pytest.mark.integration
def test_frozen_working_tree_query_never_mutates_index(working_tree, scan_query: Path, mode: str) -> None:
    """Both freeze mechanisms retain byte-identical stored data and report unstaged staleness."""
    root, index_path = working_tree
    (root / "caller.py").write_text("import leaf\n", encoding="utf-8")
    before = index_path.read_bytes()
    flags = ["--no-heal"] if mode == "flag" else []
    result = _query(scan_query, root, *flags, "rdeps", "leaf", frozen=mode == "environment")
    assert result["imported_by"] == []
    assert result["index"]["stale"] is True
    assert "the index is stale" in result["index"]["note"]
    assert "self-heal was attempted" not in result["index"]["note"]
    assert index_path.read_bytes() == before


@pytest.mark.integration
def test_legacy_git_blob_hashes_do_not_exhaust_heal_cap(working_tree, scan_index: Path, scan_query: Path) -> None:
    """Sixty unchanged legacy blobs leave room to heal the one genuinely edited source."""
    root, index_path = working_tree
    for number in range(60):
        (root / f"other_{number}.py").write_bytes(b"VALUE = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "wide graph")
    result = subprocess.run([sys.executable, str(scan_index), "--root", str(root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    index = json.loads(index_path.read_text(encoding="utf-8"))
    legacy = {}
    for record in _git(root, "ls-files", "-s").splitlines():
        meta, path = record.split("\t")
        if path.endswith(".py"):
            legacy[path] = meta.split()[1]
    index["file_shas"] = legacy
    index["file_shas"]["__coverage_mtime__"] = "123"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    (root / "caller.py").write_bytes(b"import leaf\n")
    result = _query(scan_query, root, "rdeps", "leaf")
    assert result["imported_by"] == ["caller"]
    assert result["index"]["stale"] is False


@pytest.mark.integration
def test_nested_worktree_bootstraps_its_own_index(working_tree, scan_query: Path) -> None:
    """An unindexed nested worktree never reads or overwrites its parent's cached graph."""
    root, parent_index = working_tree
    nested = root / ".worktrees" / "review"
    _git(root, "worktree", "add", "--detach", str(nested), "HEAD")
    (nested / "caller.py").write_text("import leaf\n", encoding="utf-8")
    before = parent_index.read_bytes()
    result = _query(scan_query, nested, "rdeps", "leaf")
    own_index = nested / ".cache" / "codemap" / "review.json"
    assert result["imported_by"] == ["caller"]
    assert result["index"]["root_mismatch"] is False
    assert json.loads(own_index.read_text(encoding="utf-8"))["scan_root"] == str(nested.resolve())
    assert parent_index.read_bytes() == before


@pytest.mark.integration
def test_additions_count_once_toward_the_heal_cap(working_tree, scan_query: Path) -> None:
    """Thirty newly discovered files fit the fifty-path cap and all contribute their import edges."""
    root, _ = working_tree
    expected = {f"added_{number}" for number in range(30)}
    for module in expected:
        (root / f"{module}.py").write_bytes(b"import leaf\n")
    result = _query(scan_query, root, "rdeps", "leaf")
    assert set(result["imported_by"]) == expected
    assert result["index"]["stale"] is False


@pytest.mark.integration
def test_explicit_custom_index_is_not_rewritten(working_tree, scan_query: Path) -> None:
    """A custom snapshot is answered honestly without rewriting it or the canonical cache."""
    root, index_path = working_tree
    custom = root / "snapshot.json"
    before = index_path.read_bytes()
    custom.write_bytes(before)
    (root / "caller.py").write_bytes(b"import leaf\n")
    result = _query(scan_query, root, "--index", str(custom), "rdeps", "leaf")
    assert result["imported_by"] == []
    assert result["index"]["stale"] is True
    assert custom.read_bytes() == index_path.read_bytes() == before


@pytest.mark.parametrize("mode", ["environment", "flag"])
@pytest.mark.integration
def test_missing_nested_index_respects_frozen_mode(working_tree, scan_query: Path, mode: str) -> None:
    """A frozen nested worktree fails at its own missing index instead of borrowing its parent's graph."""
    root, index_path = working_tree
    nested = root / ".worktrees" / "frozen"
    _git(root, "worktree", "add", "--detach", str(nested), "HEAD")
    before = index_path.read_bytes()
    tree_before = {
        path.relative_to(nested): path.read_bytes() if path.is_file() else None for path in nested.rglob("*")
    }
    env = {**os.environ, "SCAN_NO_AUTOBUILD": "1" if mode == "environment" else "0"}
    flags = ["--no-heal"] if mode == "flag" else []
    result = subprocess.run(
        [sys.executable, str(scan_query), *flags, "list"], cwd=nested, env=env, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "Index not found" in result.stdout + result.stderr
    assert not (nested / ".cache" / "codemap" / "frozen.json").exists()
    assert index_path.read_bytes() == before
    assert {
        path.relative_to(nested): path.read_bytes() if path.is_file() else None for path in nested.rglob("*")
    } == tree_before


@pytest.mark.integration
def test_frozen_missing_index_waits_for_active_writer(working_tree, scan_query: Path) -> None:
    """A frozen query waits at the shared gate when a writer is publishing its first index."""
    from codemap_py import rwgate

    root, index_path = working_tree
    payload = index_path.read_bytes()
    index_path.unlink()
    entered = threading.Event()
    release = threading.Event()

    def publish(target: Path) -> None:
        entered.set()
        assert release.wait(10), "test did not release the writer"
        rwgate.atomic_publish(target, payload)

    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(rwgate.write_index, index_path, publish)
        try:
            assert entered.wait(10), "writer never acquired its lease"
            result = subprocess.run(
                [sys.executable, str(scan_query), "--no-heal", "list"],
                cwd=root,
                env={**os.environ, "SCAN_NO_AUTOBUILD": "1", "CODEMAP_GATE_TIMEOUT": "0.1"},
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1
            assert json.loads(result.stderr)["error"] == "index_busy"
            assert not index_path.exists()
        finally:
            release.set()
        writer.result(timeout=10)
    assert _query(scan_query, root, "--no-heal", "rdeps", "leaf", frozen=True)["index"]["stale"] is False


@pytest.mark.integration
def test_noop_incremental_upgrades_legacy_fingerprint_header(working_tree, scan_index: Path) -> None:
    """A metadata-only legacy upgrade publishes its source fingerprint in the bounded header."""
    from codemap_py.scanner.discovery import fingerprint_file_hashes

    root, index_path = working_tree
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index.pop("source_fingerprint")
    index["large_extra"] = "padding" * 1000
    index_path.write_text(json.dumps(index), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root), "--incremental"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    published = index_path.read_text(encoding="utf-8")
    expected = fingerprint_file_hashes(index["file_shas"])
    assert f'"source_fingerprint":"{expected}"' in published[:512]
    assert json.loads(published)["modules"] == index["modules"]


@pytest.mark.integration
def test_deleting_implementation_restores_unchanged_stub(working_tree, scan_index: Path, scan_query: Path) -> None:
    """Automatic deletion refresh restores a suppressed stub and matches a full scan of the same bytes."""
    root, index_path = working_tree
    (root / "leaf.pyi").write_bytes(b"def stub_api() -> None: ...\n")
    subprocess.run([sys.executable, str(scan_index), "--root", str(root)], check=True, capture_output=True)
    (root / "leaf.py").unlink()
    _query(scan_query, root, "central")
    refreshed = json.loads(index_path.read_text())
    subprocess.run([sys.executable, str(scan_index), "--root", str(root)], check=True, capture_output=True)
    full = json.loads(index_path.read_text())
    assert refreshed["modules"] == full["modules"]
    leaf = next(module for module in refreshed["modules"] if module["name"] == "leaf")
    assert leaf["path"] == "leaf.pyi"
    assert [symbol["qualified_name"] for symbol in leaf["symbols"]] == ["stub_api"]


@pytest.mark.parametrize("representation", ["crlf", "pruned", "untracked"])
@pytest.mark.integration
def test_legacy_representation_migration_keeps_actual_edit_visible(
    working_tree, scan_index: Path, scan_query: Path, representation: str
) -> None:
    """Legacy CRLF, pruned and indexed-untracked differences never exhaust the real-change budget."""
    root, index_path = working_tree
    folder = root / (".hidden" if representation == "pruned" else "extras")
    folder.mkdir()
    if representation == "crlf":
        (root / ".gitattributes").write_bytes(b"*.py text eol=crlf\n")
    for number in range(60):
        (folder / f"m{number}.py").write_bytes(b"VALUE = 1\r\n" if representation == "crlf" else b"VALUE = 1\n")
    if representation != "untracked":
        _git(root, "add", ".")
    subprocess.run([sys.executable, str(scan_index), "--root", str(root)], check=True, capture_output=True)
    index = json.loads(index_path.read_text())
    records = _git(root, "ls-files", "-s", "-z", "--", "*.py", "*.pyi", "*.rst", "docs/**/*.md")
    legacy = {}
    for record in records.split("\0"):
        if record:
            meta, path = record.split("\t")
            legacy[path] = meta.split()[1]
    index["file_shas"] = legacy
    index.pop("source_fingerprint")
    index_path.write_text(json.dumps(index))
    (root / "caller.py").write_bytes(b"import leaf\n")
    before = index_path.read_bytes()
    frozen = _query(scan_query, root, "rdeps", "leaf", frozen=True)
    assert frozen["imported_by"] == []
    assert index_path.read_bytes() == before
    flagged = _query(scan_query, root, "--no-heal", "rdeps", "leaf")
    assert flagged["imported_by"] == []
    assert index_path.read_bytes() == before
    custom = root / "snapshot.json"
    custom.write_bytes(before)
    snapshot = _query(scan_query, root, "--index", str(custom), "rdeps", "leaf")
    assert snapshot["imported_by"] == []
    assert custom.read_bytes() == index_path.read_bytes() == before
    result = _query(scan_query, root, "rdeps", "leaf")
    assert result["imported_by"] == ["caller"]
    assert result["index"]["query_complete"] is True
    refreshed = json.loads(index_path.read_text())
    assert refreshed["source_fingerprint"]


@pytest.mark.integration
def test_explicit_root_coverage_ignores_invoking_checkout(working_tree, tmp_path: Path, scan_query: Path) -> None:
    """Every completeness diagnostic for an explicit root belongs to that selected checkout."""
    root, _ = working_tree
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _git(unrelated, "init", "-q")
    (unrelated / "rogue.py").write_bytes(b"VALUE = 1\n")
    expected = _query(scan_query, root, "rdeps", "leaf")
    actual = _query(scan_query, unrelated, "--root", str(root), "rdeps", "leaf")
    assert actual["index"] == expected["index"]


@pytest.mark.parametrize("config", ["service.yaml", "service.yml", "pyproject.toml", "setup.cfg", "setup.py"])
@pytest.mark.integration
def test_consumed_config_only_changes_refresh_graph(
    working_tree, scan_index: Path, scan_query: Path, config: str
) -> None:
    """A config-only change adds then removes references without injecting non-Python module entries."""
    root, index_path = working_tree
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_bytes(b"")
    (root / "pkg" / "mod.py").write_bytes(b"VALUE = 1\n")
    config_path = root / config
    config_path.write_bytes(b'target = "other.module"\n')
    if config == "setup.py":
        (root / ".codemapignore").write_bytes(b"[s]etup.py\n")
    subprocess.run([sys.executable, str(scan_index), "--root", str(root)], check=True, capture_output=True)
    before = json.loads(index_path.read_text())["source_fingerprint"]
    config_path.write_bytes(b'target = "pkg.mod"\n')
    _query(scan_query, root, "rdeps", "pkg.mod")
    refreshed = json.loads(index_path.read_text())
    assert refreshed["source_fingerprint"] != before
    module = next(module for module in refreshed["modules"] if module["name"] == "pkg.mod")
    assert [record["file"] for record in module["config_refs"]] == [config]
    assert all(module["path"].endswith((".py", ".pyi")) for module in refreshed["modules"])
    if config == "setup.py":
        assert not any(module["path"] == "setup.py" for module in refreshed["modules"])
    config_path.unlink()
    _query(scan_query, root, "rdeps", "pkg.mod")
    refreshed = json.loads(index_path.read_text())
    module = next(module for module in refreshed["modules"] if module["name"] == "pkg.mod")
    assert not module.get("config_refs")


@pytest.mark.integration
def test_source_root_config_change_relabels_unchanged_sources(working_tree, scan_index: Path, scan_query: Path) -> None:
    """A changed root configuration rebuilds names for unchanged sources and agrees with a full scan."""
    root, index_path = working_tree
    source_dir = root / "library"
    source_dir.mkdir()
    (source_dir / "mod.py").write_bytes(b"VALUE = 1\n")
    config = root / "pyproject.toml"
    config.write_bytes(b"[tool.codemap]\nsrc_roots = []\n")
    subprocess.run([sys.executable, str(scan_index), "--root", str(root)], check=True, capture_output=True)
    config.write_bytes(b'[tool.codemap]\nsrc_roots = ["library"]\n')
    _query(scan_query, root, "list")
    refreshed = json.loads(index_path.read_text())
    subprocess.run([sys.executable, str(scan_index), "--root", str(root)], check=True, capture_output=True)
    full = json.loads(index_path.read_text())
    assert refreshed["modules"] == full["modules"]
    assert any(module["name"] == "mod" for module in refreshed["modules"])


@pytest.mark.integration
def test_ignore_file_is_part_of_source_fingerprint(working_tree, scan_query: Path) -> None:
    """Changing ignore policy refreshes the published input fingerprint even when no path currently matches."""
    root, index_path = working_tree
    before = json.loads(index_path.read_text())["source_fingerprint"]
    (root / ".codemapignore").write_bytes(b"future_generated/\n")
    _query(scan_query, root, "list")
    assert json.loads(index_path.read_text())["source_fingerprint"] != before


@pytest.mark.integration
def test_unchanged_legacy_query_publishes_migration_fingerprint(working_tree, scan_query: Path) -> None:
    """An unchanged legacy graph gains current-input provenance so the next currency consumer can trust it."""
    root, index_path = working_tree
    index = json.loads(index_path.read_text())
    expected = index.pop("source_fingerprint")
    index_path.write_text(json.dumps(index))
    _query(scan_query, root, "list")
    updated = json.loads(index_path.read_text())
    assert updated["source_fingerprint"] == expected
    assert updated["modules"] == index["modules"]


@pytest.mark.parametrize(
    ("doc_path", "reference", "role", "source"),
    [
        pytest.param("reference.rst", ":func:`leaf.target`\n", "func", "sphinx", id="rst"),
        pytest.param("docs/reference.md", "[`leaf.target`][]\n", "mkdocs", "mkdocs", id="markdown"),
    ],
)
@pytest.mark.parametrize(
    "operation",
    [pytest.param("add", id="add"), pytest.param("modify", id="modify"), pytest.param("delete", id="delete")],
)
@pytest.mark.integration
def test_excluded_consumed_document_refreshes_xrefs_and_currency(
    working_tree,
    scan_index: Path,
    scan_query: Path,
    doc_path: str,
    reference: str,
    role: str,
    source: str,
    operation: str,
) -> None:
    """Excluded documents still consumed by the graph invalidate xrefs on addition, modification and deletion."""
    root, index_path = working_tree
    (root / ".codemapignore").write_text("*.rst\ndocs/*.md\nexcluded*.py\n", encoding="utf-8")
    (root / "excluded.py").write_text("import leaf\n", encoding="utf-8")
    doc = root / doc_path
    doc.parent.mkdir(exist_ok=True)
    if operation != "add":
        doc.write_text(reference if operation == "delete" else "No references.\n", encoding="utf-8")
    subprocess.run([sys.executable, str(scan_index), "--root", str(root)], check=True, capture_output=True)
    before = json.loads(index_path.read_text(encoding="utf-8"))
    if operation == "delete":
        doc.unlink()
    else:
        doc.write_text(reference, encoding="utf-8")
    currency_argv = [
        sys.executable,
        str(scan_index.with_name("check-index-currency")),
        "--index-path",
        str(index_path),
        "--root",
        str(root),
    ]
    stale_currency = subprocess.run(currency_argv, cwd=root, capture_output=True, text=True)
    result = _query(scan_query, root, "xrefs", "leaf::target")
    expected = (
        []
        if operation == "delete"
        else [{"role": role, "target": "leaf::target", "file": doc_path, "line": 1, "source": source}]
    )
    assert result["target"] == "leaf::target"
    assert result["refs"] == [{key: value for key, value in ref.items() if key != "target"} for ref in expected]
    assert result["index"]["stale"] is False
    assert result["index"]["query_complete"] is True
    assert stale_currency.returncode == 1
    assert json.loads(stale_currency.stdout)["status"] == "stale"
    refreshed = json.loads(index_path.read_text(encoding="utf-8"))
    assert refreshed["source_fingerprint"] != before["source_fingerprint"]
    assert {module["name"] for module in refreshed["modules"]} == {"leaf", "caller"}
    current_currency = subprocess.run(currency_argv, cwd=root, capture_output=True, text=True)
    assert current_currency.returncode == 0
    assert json.loads(current_currency.stdout)["status"] == "current"
    subprocess.run([sys.executable, str(scan_index), "--root", str(root)], check=True, capture_output=True)
    full = json.loads(index_path.read_text(encoding="utf-8"))
    assert refreshed["doc_xrefs"] == full["doc_xrefs"] == expected
    assert refreshed["modules"] == full["modules"]
    assert refreshed["source_fingerprint"] == full["source_fingerprint"]


@pytest.mark.parametrize("policy", [".codemapignore", "pyproject.toml"])
@pytest.mark.integration
def test_exclusion_policy_removes_and_restores_unchanged_modules(working_tree, scan_query: Path, policy: str) -> None:
    """Changing either supported exclusion policy changes the graph without editing source bytes."""
    root, index_path = working_tree
    config = root / policy
    content = b"caller*.py\n" if policy == ".codemapignore" else b'[tool.codemap]\nexclude = ["caller*.py"]\n'
    config.write_bytes(content)
    _query(scan_query, root, "list")
    assert {module["name"] for module in json.loads(index_path.read_text())["modules"]} == {"leaf"}
    config.unlink()
    _query(scan_query, root, "list")
    assert {module["name"] for module in json.loads(index_path.read_text())["modules"]} == {"caller", "leaf"}
