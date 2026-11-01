"""Real CLI regressions for automatic working-tree freshness and checkout ownership.

Every test drives the shipped ``scan-index`` / ``scan-query`` / ``check-index-currency`` entry points as subprocesses
against a disposable git repository, because the contract is end-to-end behaviour: what an unstaged edit, a deleted file
or a nested worktree does to the stored graph and to the freshness verdict a user sees. The real tools therefore stay
real here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from codemap_py import rwgate
from codemap_py.scanner.discovery import fingerprint_file_hashes

# Same file kinds the index writer records, so a legacy blob map can be rebuilt from git.
_INDEXED_PATHSPEC = ("*.py", "*.pyi", "*.rst", "docs/**/*.md")

# Document states for the excluded-document test: absent file, a file with no cross-reference, one with a reference.
_ABSENT, _CLEAN, _REFERENCING = "absent", "clean", "referencing"


def _git(root: Path, *args: str) -> str:
    """Run Git against a disposable repository and return stdout."""
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _full_scan(scan_index: Path, root: Path) -> None:
    """Run a full ``scan-index`` over *root*, asserting success."""
    result = subprocess.run([sys.executable, str(scan_index), "--root", str(root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _load(index_path: Path) -> dict:
    """Return the stored index parsed from *index_path*."""
    return json.loads(index_path.read_text(encoding="utf-8"))


def _write_files(folder: Path, files: dict[str, bytes]) -> None:
    """Write each ``name -> bytes`` entry of *files* into *folder*."""
    for name, data in files.items():
        (folder / name).write_bytes(data)


def _unlink(root: Path, *names: str) -> None:
    """Delete the named files under *root*."""
    for name in names:
        (root / name).unlink()


def _stage(root: Path, *paths: str) -> None:
    """Stage the given paths in the repository at *root*."""
    for path in paths:
        _git(root, "add", path)


def _set_document(doc: Path, text: str | None) -> None:
    """Make *doc* hold *text*, or be absent when *text* is ``None``."""
    doc.parent.mkdir(exist_ok=True)
    if text is None:
        doc.unlink(missing_ok=True)
    else:
        doc.write_text(text, encoding="utf-8")


def _legacy_blob_shas(root: Path) -> dict[str, str]:
    """Return ``path -> git blob sha`` for every indexed kind, the shape older indexes stored."""
    records = _git(root, "ls-files", "-s", "-z", "--", *_INDEXED_PATHSPEC)
    return {path: meta.split()[1] for meta, path in (record.split("\t") for record in records.split("\0") if record)}


def _tree_snapshot(folder: Path) -> dict[Path, bytes | None]:
    """Return every path under *folder* mapped to its bytes (``None`` for a directory)."""
    return {path.relative_to(folder): path.read_bytes() if path.is_file() else None for path in folder.rglob("*")}


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
    _full_scan(scan_index, root)
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


@pytest.fixture(name="busy_first_index")
def _busy_first_index(working_tree) -> Iterator[tuple[Path, Path, threading.Event, object]]:
    """Hold the writer lease for a first-index publication until the test releases it or ends.

    The index is removed, then a writer thread enters the exclusive phase of the shared gate and parks there. Yields
    ``(root, index_path, release, writer)``; teardown always releases the writer and waits for it, so a failing
    assertion never leaves the lease held.
    """
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
        assert entered.wait(10), "writer never acquired its lease"
        yield root, index_path, release, writer
        release.set()
        writer.result(timeout=10)


@pytest.mark.integration
def test_query_refreshes_unstaged_and_new_importers(working_tree, scan_query: Path) -> None:
    """Both working-tree edges appear automatically without staging or committing source.

    An unstaged edit to an existing module and a brand-new untracked module both start importing ``leaf``. A plain query
    must report both, answer a fresh and complete index, and leave the git status untouched.
    """
    root, _ = working_tree
    _write_files(root, {"caller.py": b"import leaf\n", "new.py": b"import leaf\n"})
    before = _git(root, "status", "--porcelain")

    result = _query(scan_query, root, "rdeps", "leaf")

    assert set(result["imported_by"]) == {"caller", "new"}
    assert result["index"]["stale"] is False
    assert result["index"]["query_complete"] is True
    assert _git(root, "status", "--porcelain") == before


@pytest.mark.parametrize(
    ("deleted", "remaining"),
    [
        pytest.param(("caller.py",), {"leaf"}, id="one-of-two"),
        pytest.param(("caller.py", "leaf.py"), set(), id="final-file"),
    ],
)
@pytest.mark.integration
def test_query_removes_deleted_modules(working_tree, scan_query: Path, deleted: tuple, remaining: set) -> None:
    """Deleting eligible files, including the final file, updates the stored graph.

    Deleting every indexed source is the edge case: an empty module set must be published, not mistaken for a failed
    scan that keeps the previous graph.
    """
    root, index_path = working_tree
    _unlink(root, *deleted)

    _query(scan_query, root, "list")

    assert {module["name"] for module in _load(index_path)["modules"]} == remaining


@pytest.mark.parametrize("mode", ["environment", "flag"])
@pytest.mark.integration
def test_frozen_working_tree_query_never_mutates_index(working_tree, scan_query: Path, mode: str) -> None:
    """Both freeze mechanisms retain byte-identical stored data and report unstaged staleness.

    ``SCAN_NO_AUTOBUILD=1`` and ``--no-heal`` must behave the same: the edited importer stays invisible, the answer is
    marked stale without claiming a heal was attempted, and the stored index is not touched.
    """
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
    """Sixty unchanged legacy blobs leave room to heal the one genuinely edited source.

    An index written with git blob shas (not the current byte hashes) looks changed for every file; if those counted
    against the heal cap the one real edit would be refused as an oversized change set.
    """
    root, index_path = working_tree
    _write_files(root, {f"other_{number}.py": b"VALUE = 1\n" for number in range(60)})
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "wide graph")
    _full_scan(scan_index, root)
    index = _load(index_path)
    index["file_shas"] = {**_legacy_blob_shas(root), "__coverage_mtime__": "123"}
    index_path.write_text(json.dumps(index), encoding="utf-8")
    (root / "caller.py").write_bytes(b"import leaf\n")

    result = _query(scan_query, root, "rdeps", "leaf")

    assert result["imported_by"] == ["caller"]
    assert result["index"]["stale"] is False


@pytest.mark.integration
def test_nested_worktree_bootstraps_its_own_index(working_tree, scan_query: Path) -> None:
    """An unindexed nested worktree never reads or overwrites its parent's cached graph.

    A checkout under ``.worktrees/`` has no index of its own; the query must build one there rather than borrow the
    parent's, so the edit made inside the worktree is visible and the parent's bytes are unchanged.
    """
    root, parent_index = working_tree
    nested = root / ".worktrees" / "review"
    _git(root, "worktree", "add", "--detach", str(nested), "HEAD")
    (nested / "caller.py").write_text("import leaf\n", encoding="utf-8")
    before = parent_index.read_bytes()

    result = _query(scan_query, nested, "rdeps", "leaf")

    own_index = nested / ".cache" / "codemap" / "review.json"
    assert result["imported_by"] == ["caller"]
    assert result["index"]["root_mismatch"] is False
    assert _load(own_index)["scan_root"] == str(nested.resolve())
    assert parent_index.read_bytes() == before


@pytest.mark.integration
def test_additions_count_once_toward_the_heal_cap(working_tree, scan_query: Path) -> None:
    """Thirty newly discovered files fit the fifty-path cap and all contribute their import edges.

    A new file is one change, not two (added and unlisted); counting it twice would push thirty additions past the cap
    and leave the graph stale.
    """
    root, _ = working_tree
    expected = {f"added_{number}" for number in range(30)}
    _write_files(root, {f"{module}.py": b"import leaf\n" for module in expected})

    result = _query(scan_query, root, "rdeps", "leaf")

    assert set(result["imported_by"]) == expected
    assert result["index"]["stale"] is False


@pytest.mark.integration
def test_explicit_custom_index_is_not_rewritten(working_tree, scan_query: Path) -> None:
    """A custom snapshot is answered honestly without rewriting it or the canonical cache.

    ``--index`` points at a copy of the index taken before an importer appeared. The answer must say it is stale and
    neither the copy nor the canonical cache may be healed behind the caller's back.
    """
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
    """A frozen nested worktree fails at its own missing index instead of borrowing its parent's graph.

    Both freeze mechanisms must refuse to build or borrow: the query exits non-zero, names the missing index, and
    leaves the nested tree and the parent's index byte-identical.
    """
    root, index_path = working_tree
    nested = root / ".worktrees" / "frozen"
    _git(root, "worktree", "add", "--detach", str(nested), "HEAD")
    before = index_path.read_bytes()
    tree_before = _tree_snapshot(nested)
    env = {**os.environ, "SCAN_NO_AUTOBUILD": "1" if mode == "environment" else "0"}
    flags = ["--no-heal"] if mode == "flag" else []

    result = subprocess.run(
        [sys.executable, str(scan_query), *flags, "list"], cwd=nested, env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "Index not found" in result.stdout + result.stderr
    assert not (nested / ".cache" / "codemap" / "frozen.json").exists()
    assert index_path.read_bytes() == before
    assert _tree_snapshot(nested) == tree_before


@pytest.mark.integration
def test_frozen_missing_index_waits_for_active_writer(busy_first_index, scan_query: Path) -> None:
    """A frozen query waits at the shared gate when a writer is publishing its first index.

    While a real writer thread holds the exclusive lease, a frozen query must report ``index_busy`` after the gate
    timeout instead of failing as a missing index. Once the writer publishes, the same frozen query reads the index.
    """
    root, index_path, release, writer = busy_first_index

    busy = subprocess.run(
        [sys.executable, str(scan_query), "--no-heal", "list"],
        cwd=root,
        env={**os.environ, "SCAN_NO_AUTOBUILD": "1", "CODEMAP_GATE_TIMEOUT": "0.1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    published_during_wait = index_path.exists()
    release.set()
    writer.result(timeout=10)
    after = _query(scan_query, root, "--no-heal", "rdeps", "leaf", frozen=True)

    assert busy.returncode == 1
    assert json.loads(busy.stderr)["error"] == "index_busy"
    assert not published_during_wait
    assert after["index"]["stale"] is False


@pytest.mark.integration
def test_noop_incremental_upgrades_legacy_fingerprint_header(working_tree, scan_index: Path) -> None:
    """A metadata-only legacy upgrade publishes its source fingerprint in the bounded header.

    An older index lacks ``source_fingerprint``. A no-op incremental scan must add it near the start of the file (so
    cheap header reads find it) without altering the module graph.
    """
    root, index_path = working_tree
    index = _load(index_path)
    index.pop("source_fingerprint")
    index["large_extra"] = "padding" * 1000
    index_path.write_text(json.dumps(index), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root), "--incremental"], capture_output=True, text=True
    )

    published = index_path.read_text(encoding="utf-8")
    expected = fingerprint_file_hashes(index["file_shas"])
    assert result.returncode == 0, result.stderr
    assert f'"source_fingerprint":"{expected}"' in published[:512]
    assert json.loads(published)["modules"] == index["modules"]


@pytest.mark.integration
def test_deleting_implementation_restores_unchanged_stub(working_tree, scan_index: Path, scan_query: Path) -> None:
    """Automatic deletion refresh restores a suppressed stub and matches a full scan of the same bytes.

    ``leaf.pyi`` is shadowed by ``leaf.py``. Deleting the implementation must surface the stub as the module, exactly as
    a from-scratch scan of the resulting tree would.
    """
    root, index_path = working_tree
    (root / "leaf.pyi").write_bytes(b"def stub_api() -> None: ...\n")
    _full_scan(scan_index, root)
    (root / "leaf.py").unlink()

    _query(scan_query, root, "central")
    refreshed = _load(index_path)
    _full_scan(scan_index, root)
    full = _load(index_path)

    leaf = next(module for module in refreshed["modules"] if module["name"] == "leaf")
    assert refreshed["modules"] == full["modules"]
    assert leaf["path"] == "leaf.pyi"
    assert [symbol["qualified_name"] for symbol in leaf["symbols"]] == ["stub_api"]


@pytest.mark.parametrize(
    ("folder", "line_ending", "attributes", "stage"),
    [
        pytest.param("extras", b"\r\n", {".gitattributes": b"*.py text eol=crlf\n"}, (".",), id="crlf"),
        pytest.param(".hidden", b"\n", {}, (".",), id="pruned"),
        pytest.param("extras", b"\n", {}, (), id="untracked"),
    ],
)
@pytest.mark.integration
def test_legacy_representation_migration_keeps_actual_edit_visible(
    working_tree, scan_index: Path, scan_query: Path, folder: str, line_ending: bytes, attributes: dict, stage: tuple
) -> None:
    """Legacy CRLF, pruned and indexed-untracked differences never exhaust the real-change budget.

    Sixty files whose stored hash is in an older representation (CRLF-normalised, inside a pruned directory, or
    untracked) sit beside one genuine edit. Frozen, flagged and custom-index queries must stay read-only and stale; a
    plain query must heal only the real edit and publish the current fingerprint.
    """
    root, index_path = working_tree
    (root / folder).mkdir()
    _write_files(root, attributes)
    _write_files(root / folder, {f"m{number}.py": b"VALUE = 1" + line_ending for number in range(60)})
    _stage(root, *stage)
    _full_scan(scan_index, root)
    index = _load(index_path)
    index["file_shas"] = _legacy_blob_shas(root)
    index.pop("source_fingerprint")
    index_path.write_text(json.dumps(index))
    (root / "caller.py").write_bytes(b"import leaf\n")
    before = index_path.read_bytes()
    custom = root / "snapshot.json"
    custom.write_bytes(before)

    frozen = _query(scan_query, root, "rdeps", "leaf", frozen=True)
    after_frozen = index_path.read_bytes()
    flagged = _query(scan_query, root, "--no-heal", "rdeps", "leaf")
    after_flagged = index_path.read_bytes()
    snapshot = _query(scan_query, root, "--index", str(custom), "rdeps", "leaf")
    result = _query(scan_query, root, "rdeps", "leaf")

    assert frozen["imported_by"] == flagged["imported_by"] == snapshot["imported_by"] == []
    assert after_frozen == after_flagged == before
    assert custom.read_bytes() == before
    assert result["imported_by"] == ["caller"]
    assert result["index"]["query_complete"] is True
    assert _load(index_path)["source_fingerprint"]


@pytest.mark.integration
def test_explicit_root_coverage_ignores_invoking_checkout(working_tree, tmp_path: Path, scan_query: Path) -> None:
    """Every completeness diagnostic for an explicit root belongs to that selected checkout.

    A query launched from an unrelated repository holding an untracked ``rogue.py`` but pointed at the project with
    ``--root`` must report the same index block as one launched from the project itself.
    """
    root, _ = working_tree
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _git(unrelated, "init", "-q")
    (unrelated / "rogue.py").write_bytes(b"VALUE = 1\n")
    expected = _query(scan_query, root, "rdeps", "leaf")

    actual = _query(scan_query, unrelated, "--root", str(root), "rdeps", "leaf")

    assert actual["index"] == expected["index"]


@pytest.mark.parametrize(
    ("config", "ignore_files"),
    [
        pytest.param("service.yaml", {}, id="service.yaml"),
        pytest.param("service.yml", {}, id="service.yml"),
        pytest.param("pyproject.toml", {}, id="pyproject.toml"),
        pytest.param("setup.cfg", {}, id="setup.cfg"),
        pytest.param("setup.py", {".codemapignore": b"[s]etup.py\n"}, id="setup.py"),
    ],
)
@pytest.mark.integration
def test_consumed_config_only_changes_refresh_graph(
    working_tree, scan_index: Path, scan_query: Path, config: str, ignore_files: dict
) -> None:
    """A config-only change adds then removes references without injecting non-Python module entries.

    Each supported config kind starts naming an unrelated target, is edited to name ``pkg.mod`` and later deleted. The
    graph must gain then lose the reference with no source change, and the config file must never become a module.
    """
    root, index_path = working_tree
    (root / "pkg").mkdir()
    _write_files(root, {"pkg/__init__.py": b"", "pkg/mod.py": b"VALUE = 1\n", config: b'target = "other.module"\n'})
    _write_files(root, ignore_files)
    _full_scan(scan_index, root)
    before = _load(index_path)["source_fingerprint"]

    (root / config).write_bytes(b'target = "pkg.mod"\n')
    _query(scan_query, root, "rdeps", "pkg.mod")
    added = _load(index_path)
    (root / config).unlink()
    _query(scan_query, root, "rdeps", "pkg.mod")
    removed = _load(index_path)

    added_module = next(module for module in added["modules"] if module["name"] == "pkg.mod")
    removed_module = next(module for module in removed["modules"] if module["name"] == "pkg.mod")
    assert added["source_fingerprint"] != before
    assert [record["file"] for record in added_module["config_refs"]] == [config]
    assert all(module["path"].endswith((".py", ".pyi")) for module in added["modules"])
    assert "setup.py" not in {module["path"] for module in added["modules"]}
    assert not removed_module.get("config_refs")


@pytest.mark.integration
def test_source_root_config_change_relabels_unchanged_sources(working_tree, scan_index: Path, scan_query: Path) -> None:
    """A changed root configuration rebuilds names for unchanged sources and agrees with a full scan.

    Moving ``library/mod.py`` under a configured source root changes its dotted name without touching its bytes; the
    automatic refresh must relabel it exactly as a full scan would.
    """
    root, index_path = working_tree
    (root / "library").mkdir()
    _write_files(root, {"library/mod.py": b"VALUE = 1\n", "pyproject.toml": b"[tool.codemap]\nsrc_roots = []\n"})
    _full_scan(scan_index, root)
    (root / "pyproject.toml").write_bytes(b'[tool.codemap]\nsrc_roots = ["library"]\n')

    _query(scan_query, root, "list")
    refreshed = _load(index_path)
    _full_scan(scan_index, root)
    full = _load(index_path)

    assert refreshed["modules"] == full["modules"]
    assert any(module["name"] == "mod" for module in refreshed["modules"])


@pytest.mark.integration
def test_ignore_file_is_part_of_source_fingerprint(working_tree, scan_query: Path) -> None:
    """Changing ignore policy refreshes the published input fingerprint even when no path currently matches.

    ``.codemapignore`` is an input of the graph; adding one with a pattern that matches nothing yet must still move the
    fingerprint so currency checks notice the policy change.
    """
    root, index_path = working_tree
    before = _load(index_path)["source_fingerprint"]
    (root / ".codemapignore").write_bytes(b"future_generated/\n")

    _query(scan_query, root, "list")

    assert _load(index_path)["source_fingerprint"] != before


@pytest.mark.integration
def test_unchanged_legacy_query_publishes_migration_fingerprint(working_tree, scan_query: Path) -> None:
    """An unchanged legacy graph gains current-input provenance so the next currency consumer can trust it.

    The index loses its fingerprint but no source changes; a query must restore the same fingerprint and leave the
    module graph as it was.
    """
    root, index_path = working_tree
    index = _load(index_path)
    expected = index.pop("source_fingerprint")
    index_path.write_text(json.dumps(index))

    _query(scan_query, root, "list")

    updated = _load(index_path)
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
    ("before", "after"),
    [
        pytest.param(_ABSENT, _REFERENCING, id="add"),
        pytest.param(_CLEAN, _REFERENCING, id="modify"),
        pytest.param(_REFERENCING, _ABSENT, id="delete"),
    ],
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
    before: str,
    after: str,
) -> None:
    """Excluded documents still consumed by the graph invalidate xrefs on addition, modification and deletion.

    The document is excluded from the module set by ignore patterns yet still feeds cross-references. Adding, editing or
    removing it must flip ``check-index-currency`` to stale, be healed by one query, and then agree with a full scan on
    xrefs, modules and fingerprint.
    """
    root, index_path = working_tree
    states = {_ABSENT: None, _CLEAN: "No references.\n", _REFERENCING: reference}
    _write_files(root, {".codemapignore": b"*.rst\ndocs/*.md\nexcluded*.py\n", "excluded.py": b"import leaf\n"})
    doc = root / doc_path
    _set_document(doc, states[before])
    _full_scan(scan_index, root)
    fingerprint_before = _load(index_path)["source_fingerprint"]
    _set_document(doc, states[after])
    currency_argv = [
        sys.executable,
        str(scan_index.with_name("check-index-currency")),
        "--index-path",
        str(index_path),
        "--root",
        str(root),
    ]
    entry = {"role": role, "target": "leaf::target", "file": doc_path, "line": 1, "source": source}
    expected = [entry] if after == _REFERENCING else []

    stale_currency = subprocess.run(currency_argv, cwd=root, capture_output=True, text=True)
    result = _query(scan_query, root, "xrefs", "leaf::target")
    refreshed = _load(index_path)
    current_currency = subprocess.run(currency_argv, cwd=root, capture_output=True, text=True)
    _full_scan(scan_index, root)
    full = _load(index_path)

    assert result["target"] == "leaf::target"
    assert result["refs"] == [{key: value for key, value in ref.items() if key != "target"} for ref in expected]
    assert result["index"]["stale"] is False
    assert result["index"]["query_complete"] is True
    assert stale_currency.returncode == 1
    assert json.loads(stale_currency.stdout)["status"] == "stale"
    assert refreshed["source_fingerprint"] != fingerprint_before
    assert {module["name"] for module in refreshed["modules"]} == {"leaf", "caller"}
    assert current_currency.returncode == 0
    assert json.loads(current_currency.stdout)["status"] == "current"
    assert refreshed["doc_xrefs"] == full["doc_xrefs"] == expected
    assert refreshed["modules"] == full["modules"]
    assert refreshed["source_fingerprint"] == full["source_fingerprint"]


@pytest.mark.parametrize(
    ("policy", "content"),
    [
        pytest.param(".codemapignore", b"caller*.py\n", id=".codemapignore"),
        pytest.param("pyproject.toml", b'[tool.codemap]\nexclude = ["caller*.py"]\n', id="pyproject.toml"),
    ],
)
@pytest.mark.integration
def test_exclusion_policy_removes_and_restores_unchanged_modules(
    working_tree, scan_query: Path, policy: str, content: bytes
) -> None:
    """Changing either supported exclusion policy changes the graph without editing source bytes.

    Adding an exclusion for ``caller*.py`` must drop that module from the stored graph, and removing the policy file
    must bring it back, though no source file ever changed.
    """
    root, index_path = working_tree
    (root / policy).write_bytes(content)

    _query(scan_query, root, "list")
    excluded = {module["name"] for module in _load(index_path)["modules"]}
    (root / policy).unlink()
    _query(scan_query, root, "list")
    restored = {module["name"] for module in _load(index_path)["modules"]}

    assert excluded == {"leaf"}
    assert restored == {"caller", "leaf"}
