"""Regression tests for deterministic fresh-machine Codemap index preparation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path, PureWindowsPath
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "prepare-codex-index.py"


def _load_script() -> ModuleType:
    """Import the index utility's definitions without executing its command-line entry point.

    >>> _load_script().__name__
    'prepare_codex_index'
    """
    spec = importlib.util.spec_from_file_location("prepare_codex_index", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_files(tmp_path: Path, expected_sha256: str, *, scan_version: int = 12) -> tuple[Path, Path, Path, bytes]:
    """Write a native-root scan and a canonical-root oracle, returning both files and expected locked bytes.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     index, root, manifest, locked = _fixture_files(Path(directory), "digest", scan_version=13)
    ...     json.loads(index.read_text())["scan_root"] == str(root)
    ...     json.loads(manifest.read_text())["index"]["raw_sha256"], json.loads(locked)["scan_version"]
    True
    ('digest', 13)
    """
    # Resolved once: prepare_index rewrites against source_root.resolve(), so an unresolved
    # fixture root (a Windows 8.3 short name, a macOS /tmp symlink) would spell the payload
    # prefix differently from the prefix under rewrite and defeat the relocation.
    source_root = (tmp_path / "fresh-target").resolve()
    source_root.mkdir(exist_ok=True)
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "scan_version": scan_version,
                "scanned_at": "fresh",
                "project": "fresh-target",
                "scan_root": str(source_root),
                "modules": [{"mock_patches": [{"file": f"{source_root}/tests/test_demo.py"}]}],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    locked_payload = {
        "scan_version": scan_version,
        "scanned_at": "2026-07-29T18:51:27.926212+00:00",
        "project": "codemap-provider-parity-pl-2.6.5",
        "scan_root": "/private/tmp/codemap-provider-parity-pl-2.6.5",
        "modules": [{"mock_patches": [{"file": "/private/tmp/codemap-provider-parity-pl-2.6.5/tests/test_demo.py"}]}],
    }
    locked_bytes = json.dumps(locked_payload, separators=(",", ":")).encode("utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "index": {
                    "raw_sha256": expected_sha256,
                    "scan_version": scan_version,
                    "scan_root": locked_payload["scan_root"],
                    "project": locked_payload["project"],
                    "scanned_at": locked_payload["scanned_at"],
                }
            }
        ),
        encoding="utf-8",
    )
    return index_path, source_root, manifest_path, locked_bytes


def _schema_file(tmp_path: Path, scan_version: int) -> Path:
    """Write a scanner-version assignment into a minimal UTF-8 Python schema module.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     _schema_file(Path(directory), 13).read_text(encoding="utf-8").splitlines()
    ['SCAN_VERSION: int = 13']
    """
    path = tmp_path / "schema.py"
    path.write_text(f"SCAN_VERSION: int = {scan_version}\n", encoding="utf-8")
    return path


def test_prepare_index_rewrites_only_environment_metadata_to_exact_locked_bytes(tmp_path: Path) -> None:
    """A fresh scan becomes portable only when its complete normalized hash matches."""
    module = _load_script()
    expected_payload = _fixture_files(tmp_path, "placeholder")[-1]
    expected_sha256 = hashlib.sha256(expected_payload).hexdigest()
    index_path, source_root, manifest_path, locked_bytes = _fixture_files(tmp_path, expected_sha256)

    digest = module.prepare_index(index_path, source_root, manifest_path)

    assert digest == expected_sha256
    assert index_path.read_bytes() == locked_bytes


def _rooted_payload(root: Path) -> dict[str, object]:
    """Return one scan payload whose every embedded path is spelled natively under *root*.

    Both roots' payloads are built from ``Path`` joins rather than by relocating one payload's JSON text:
    ``json.dumps(...).replace(str(root), ...)`` cannot match a Windows root, because the JSON text escapes each
    separator as ``\\\\``.

    >>> root = Path("example")
    >>> payload = _rooted_payload(root)
    >>> payload["scan_root"] == str(root)
    True
    >>> payload["modules"][0]["imports"] == [str(root / "src" / "helper.py")]
    True
    """
    return {
        "scan_version": 13,
        "scanned_at": "different-runtime-times-are-not-graph-identity",
        "project": "fixture",
        "scan_root": str(root),
        "modules": [{"file": str(root / "src" / "demo.py"), "imports": [str(root / "src" / "helper.py")]}],
    }


def test_semantic_index_identity_is_stable_across_runtime_roots(tmp_path: Path) -> None:
    """Equivalent scans keep one semantic identity without sharing a runtime path."""
    module = _load_script()
    # Resolved: semantic_index_sha256 strips str(source_root.resolve()), so an unresolved root
    # (a Windows 8.3 short name, a macOS /tmp symlink) spells the payload prefix differently
    # from the prefix being stripped and no root would be removed from either digest.
    first_root = (tmp_path / "first" / "target").resolve()
    second_root = (tmp_path / "second" / "target").resolve()
    payload = _rooted_payload(first_root)
    second_payload = _rooted_payload(second_root)

    assert module.semantic_index_sha256(payload, first_root) == module.semantic_index_sha256(
        second_payload, second_root
    )
    assert payload["scan_root"] != second_payload["scan_root"]


def _patch_bundle_source(tmp_path: Path) -> tuple[Path, str, Path]:
    """Create a one-commit source checkout plus a stub scanner; return source, commit, scanner."""
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    (source / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "demo.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Bench",
            "-c",
            "user.email=bench@example.invalid",
            "commit",
            "-m",
            "baseline",
        ],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    scanner = tmp_path / "scan-index"
    scanner.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[sys.argv.index('--root') + 1]).resolve()\n"
        "path = root / '.cache/codemap' / f'{root.name}.json'\n"
        "path.parent.mkdir(parents=True)\n"
        "path.write_text(json.dumps({'scan_version': 13, 'scanned_at': 'fresh', 'project': root.name, "
        "'scan_root': str(root), 'modules': [{'file': str(root / 'demo.py')}]}))\n",
        encoding="utf-8",
    )
    scanner.chmod(0o755)
    return source, commit, scanner


def _locked_semantic_sha256(module: ModuleType, source: Path) -> str:
    """Hash the stub scanner graph after normalizing its source-root-dependent paths.

    >>> module = _load_script()
    >>> _locked_semantic_sha256(module, Path("one")) == _locked_semantic_sha256(module, Path("two"))
    True
    """
    return module.semantic_index_sha256(
        {
            "scan_version": 13,
            "scanned_at": "locked",
            "project": "provider-parity-PT-01",
            "scan_root": str(source.resolve()),
            # Native join, not an f-string "/" literal: the installed index carries the
            # separator the scanner emitted, so a POSIX-spelled oracle never matches on Windows.
            "modules": [{"file": str(source.resolve() / "demo.py")}],
        },
        source,
    )


def _write_patch_locks(path: Path, *, commit: str, canonical_root: str, semantic_sha256: str) -> Path:
    """Replace a single-task lock document with explicit commit, root, and semantic identity.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = Path(directory) / "locks.json"
    ...     _ = _write_patch_locks(path, commit="abc", canonical_root="example", semantic_sha256="digest")
    ...     task, = json.loads(path.read_text())["tasks"].values()
    ...     task["baseline_commit"], task["semantic_sha256"]
    ('abc', 'digest')
    """
    path.write_text(
        json.dumps(
            {
                "schema_version": "provider-parity-patch-index-locks-v1",
                "canonical_scan_root": canonical_root,
                "tasks": {
                    "PT-01": {
                        "baseline_commit": commit,
                        "module_count": 1,
                        "raw_sha256_at_canonical_root": "0" * 64,
                        "scan_version": 13,
                        "scanned_at": "locked",
                        "semantic_sha256": semantic_sha256,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_patch_index_bundle_builds_each_exact_historical_graph_at_the_runtime_root(tmp_path: Path) -> None:
    """Patch tasks must never reuse the current-revision graph for historical source."""
    module = _load_script()
    source, commit, scanner = _patch_bundle_source(tmp_path)
    locks = _write_patch_locks(
        tmp_path / "locks.json",
        commit=commit,
        canonical_root="/different/canonical/root",
        semantic_sha256=_locked_semantic_sha256(module, source),
    )

    installed = module.prepare_patch_index_bundle(source, locks, scanner)

    index_path = source / ".cache/codemap/patch/PT-01.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert installed == {"PT-01": hashlib.sha256(index_path.read_bytes()).hexdigest()}
    assert payload["scan_root"] == str(source.resolve())
    assert payload["modules"][0]["file"] == str(source.resolve() / "demo.py")
    assert (
        subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )


def test_patch_index_bundle_warns_when_the_raw_byte_lock_cannot_be_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Off the canonical checkout path the skipped byte-identity check must be announced."""
    module = _load_script()
    source, commit, scanner = _patch_bundle_source(tmp_path)
    locks = _write_patch_locks(
        tmp_path / "locks.json",
        commit=commit,
        canonical_root="/different/canonical/root",
        semantic_sha256=_locked_semantic_sha256(module, source),
    )

    module.prepare_patch_index_bundle(source, locks, scanner)

    assert "raw byte-identity check skipped for PT-01" in capsys.readouterr().err


def test_patch_index_bundle_reports_graph_drift_even_when_cleanup_also_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing worktree release must not displace the drift that invalidated the run."""
    module = _load_script()
    source, commit, scanner = _patch_bundle_source(tmp_path)
    locks = _write_patch_locks(
        tmp_path / "locks.json", commit=commit, canonical_root="/different/canonical/root", semantic_sha256="d" * 64
    )
    released = module._release_worktree
    monkeypatch.setattr(
        module,
        "_release_worktree",
        lambda root, worktree, task_id: released(root, worktree, task_id) or "cleanup exploded",
    )

    with pytest.raises(ValueError, match="semantic SHA-256 drifted"):
        module.prepare_patch_index_bundle(source, locks, scanner)

    assert "cleanup exploded" in capsys.readouterr().err


def test_patch_index_bundle_raises_when_cleanup_is_the_only_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked worktree on an otherwise clean pass must still fail the bundle."""
    module = _load_script()
    source, commit, scanner = _patch_bundle_source(tmp_path)
    locks = _write_patch_locks(
        tmp_path / "locks.json",
        commit=commit,
        canonical_root="/different/canonical/root",
        semantic_sha256=_locked_semantic_sha256(module, source),
    )
    released = module._release_worktree
    monkeypatch.setattr(
        module,
        "_release_worktree",
        lambda root, worktree, task_id: released(root, worktree, task_id) or "cleanup failed for PT-01",
    )

    with pytest.raises(ValueError, match="cleanup failed for PT-01"):
        module.prepare_patch_index_bundle(source, locks, scanner)


def test_replace_root_rewrites_path_prefixes_without_touching_content(tmp_path: Path) -> None:
    """Only the scan root and paths beneath it are relocated; prose and siblings stay verbatim."""
    module = _load_script()
    payload = {
        "scan_root": "/scan/repo",
        "modules": [
            {
                "file": "/scan/repo/src/demo.py",
                "docstring_first_line": "Reads defaults from /scan/repo/setup.cfg at import time.",
                "neighbour": "/scan/repo-other/src/demo.py",
                "imports": ["/scan/repo/src/helper.py", "relative/path.py"],
            }
        ],
    }

    rewritten = module._replace_root(payload, "/scan/repo", "/locked/root")

    assert rewritten == {
        "scan_root": "/locked/root",
        "modules": [
            {
                "file": "/locked/root/src/demo.py",
                "docstring_first_line": "Reads defaults from /scan/repo/setup.cfg at import time.",
                "neighbour": "/scan/repo-other/src/demo.py",
                "imports": ["/locked/root/src/helper.py", "relative/path.py"],
            }
        ],
    }


def test_replace_root_relocates_a_simulated_windows_spelled_scan_under_either_separator() -> None:
    """A Windows-rooted scan relocates on any host, whichever separator spells its children.

    Windows accepts both separators, so a scan taken there may carry either below the same root; both must therefore
    delimit the tree, while a sibling that merely extends the root name and prose that only mentions the checkout stay
    verbatim. Inputs are built with ``PureWindowsPath`` so the Windows contract is exercised on every platform.
    """
    module = _load_script()
    root = str(PureWindowsPath("C:/scan/repo"))
    locked = str(PureWindowsPath("D:/locked/root"))
    payload = {
        "scan_root": root,
        "modules": [
            {
                "file": str(PureWindowsPath("C:/scan/repo/src/demo.py")),
                "forward_slash_child": f"{root}/src/helper.py",
                "neighbour": str(PureWindowsPath("C:/scan/repo-other/src/demo.py")),
                "docstring_first_line": f"Reads defaults from {root}\\setup.cfg at import time.",
            }
        ],
    }

    rewritten = module._replace_root(payload, root, locked)

    assert rewritten == {
        "scan_root": locked,
        "modules": [
            {
                "file": str(PureWindowsPath("D:/locked/root/src/demo.py")),
                "forward_slash_child": f"{locked}/src/helper.py",
                "neighbour": str(PureWindowsPath("C:/scan/repo-other/src/demo.py")),
                "docstring_first_line": f"Reads defaults from {root}\\setup.cfg at import time.",
            }
        ],
    }


def test_replace_root_keeps_a_backslash_named_sibling_of_a_posix_root_verbatim() -> None:
    """A backslash is a legal POSIX filename character, so it never delimits a POSIX scan tree."""
    module = _load_script()

    rewritten = module._replace_root({"file": "/scan/repo\\sibling/demo.py"}, "/scan/repo", "/locked/root")

    assert rewritten == {"file": "/scan/repo\\sibling/demo.py"}


@pytest.mark.parametrize(
    ("canonical_root", "absolute"),
    [
        pytest.param("/checkouts/repo", True, id="posix-root"),
        pytest.param(str(PureWindowsPath("C:/checkouts/repo")), True, id="windows-drive-root"),
        pytest.param(str(PureWindowsPath("//server/share/repo")), True, id="windows-unc-root"),
        pytest.param("checkouts/repo", False, id="relative-root"),
    ],
)
def test_patch_index_locks_accept_a_canonical_root_recorded_on_either_platform(
    tmp_path: Path, canonical_root: str, absolute: bool
) -> None:
    """A lock is portable data: its canonical root is judged by both path flavours, not the host.

    A run reads a lock recorded on the other OS — a POSIX root on Windows, a drive or UNC root off Windows — and
    validation based on only the native path flavour rejected the foreign spelling as relative.
    """
    module = _load_script()
    locks = _write_patch_locks(
        tmp_path / "locks.json", commit="a" * 40, canonical_root=canonical_root, semantic_sha256="c" * 64
    )

    if not absolute:
        with pytest.raises(ValueError, match="absolute canonical_scan_root"):
            module._patch_index_locks(locks)
        return
    assert module._patch_index_locks(locks)["PT-01"]["baseline_commit"] == "a" * 40


def test_patch_index_bundle_fails_before_scanning_when_baseline_object_is_missing(tmp_path: Path) -> None:
    """A paid patch scope cannot begin when its exact source object is unavailable."""
    module = _load_script()
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    locks = tmp_path / "locks.json"
    locks.write_text(
        json.dumps(
            {
                "schema_version": "provider-parity-patch-index-locks-v1",
                "canonical_scan_root": "/canonical",
                "tasks": {
                    "PT-01": {
                        "baseline_commit": "a" * 40,
                        "module_count": 1,
                        "raw_sha256_at_canonical_root": "b" * 64,
                        "scan_version": 13,
                        "scanned_at": "locked",
                        "semantic_sha256": "c" * 64,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    scanner = tmp_path / "scan-index"
    scanner.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    scanner.chmod(0o755)

    with pytest.raises(ValueError, match="fetch the exact commit"):
        module.prepare_patch_index_bundle(source, locks, scanner)

    assert not (source / ".cache/codemap/patch/PT-01.json").exists()


def test_patch_index_bundle_rejects_incomplete_lock_rows_before_scanning(tmp_path: Path) -> None:
    """A partial provenance row cannot become a runnable historical index."""
    module = _load_script()
    locks = tmp_path / "locks.json"
    locks.write_text(
        json.dumps(
            {
                "schema_version": "provider-parity-patch-index-locks-v1",
                "canonical_scan_root": "/canonical",
                "tasks": {"PT-01": {"baseline_commit": "a" * 40}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="scan_version"):
        module._patch_index_locks(locks)


def test_prepare_index_uses_semantic_lock_without_rewriting_runtime_root(tmp_path: Path) -> None:
    """A prospective lock accepts equivalent scans at distinct roots and keeps each queryable path."""
    module = _load_script()
    # Resolved for the same reason as the identity test above: prepare_index hashes through
    # semantic_index_sha256, which strips the resolved spelling of the runtime root.
    first_root = (tmp_path / "first" / "target").resolve()
    second_root = (tmp_path / "second" / "target").resolve()
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    first_index = tmp_path / "first-index.json"
    second_index = tmp_path / "second-index.json"
    payload = _rooted_payload(first_root)
    first_index.write_text(json.dumps(payload), encoding="utf-8")
    second_payload = _rooted_payload(second_root)
    second_index.write_text(json.dumps(second_payload), encoding="utf-8")
    semantic_sha256 = module.semantic_index_sha256(payload, first_root)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "index": {
                    "raw_sha256": "0" * 64,
                    "semantic_sha256": semantic_sha256,
                    "scan_version": 13,
                    "scan_root": "<runtime-root>",
                    "project": "fixture",
                    "scanned_at": "locked-time",
                }
            }
        ),
        encoding="utf-8",
    )

    module.prepare_index(first_index, first_root, manifest)
    module.prepare_index(second_index, second_root, manifest)

    assert json.loads(first_index.read_text(encoding="utf-8"))["scan_root"] == str(first_root)
    assert json.loads(second_index.read_text(encoding="utf-8"))["scan_root"] == str(second_root)
    assert module.verify_index(first_index, manifest, source_root=first_root) is None
    assert module.verify_index(second_index, manifest, source_root=second_root) is None


def test_prepare_index_preserves_fresh_scan_when_normalized_graph_does_not_match(tmp_path: Path) -> None:
    """Metadata normalization must never conceal a changed graph or scanner result."""
    module = _load_script()
    index_path, source_root, manifest_path, _locked_bytes = _fixture_files(tmp_path, "0" * 64)
    original = index_path.read_bytes()

    with pytest.raises(ValueError, match="normalized index SHA-256 mismatch"):
        module.prepare_index(index_path, source_root, manifest_path)

    assert index_path.read_bytes() == original


def test_old_lock_was_accepted_without_schema_context_but_fails_against_current_schema(tmp_path: Path) -> None:
    """An archived v11 lock remains reproducible only outside the active v12 contract."""
    module = _load_script()
    expected_payload = _fixture_files(tmp_path, "placeholder", scan_version=11)[-1]
    expected_sha256 = hashlib.sha256(expected_payload).hexdigest()
    index_path, source_root, manifest_path, _locked_bytes = _fixture_files(tmp_path, expected_sha256, scan_version=11)

    assert module.prepare_index(index_path, source_root, manifest_path) == expected_sha256
    with pytest.raises(ValueError, match="does not match current Codemap schema"):
        module.index_contract(manifest_path, schema_path=_schema_file(tmp_path, 12))


def test_current_v12_lock_and_index_pass_schema_contract(tmp_path: Path) -> None:
    """The active v12 scan is accepted when manifest, bytes, and scanner agree."""
    module = _load_script()
    expected_payload = _fixture_files(tmp_path, "placeholder", scan_version=12)[-1]
    expected_sha256 = hashlib.sha256(expected_payload).hexdigest()
    index_path, source_root, manifest_path, locked_bytes = _fixture_files(tmp_path, expected_sha256, scan_version=12)
    schema_path = _schema_file(tmp_path, 12)

    assert module.index_contract(manifest_path, schema_path=schema_path)["scan_version"] == 12
    assert module.verify_index(index_path, manifest_path, schema_path=schema_path) is None
    assert module.prepare_index(index_path, source_root, manifest_path, schema_path=schema_path) == expected_sha256
    assert index_path.read_bytes() == locked_bytes


def test_verify_index_rejects_wrong_bytes_with_current_schema(tmp_path: Path) -> None:
    """A structurally valid v12 graph still requires the exact locked bytes."""
    module = _load_script()
    expected_payload = _fixture_files(tmp_path, "placeholder", scan_version=12)[-1]
    expected_sha256 = hashlib.sha256(expected_payload).hexdigest()
    index_path, _source_root, manifest_path, _locked_bytes = _fixture_files(tmp_path, expected_sha256, scan_version=12)
    index_path.write_text(json.dumps({"scan_version": 12, "modules": [{"wrong": True}]}), encoding="utf-8")

    with pytest.raises(ValueError, match="index SHA-256 mismatch"):
        module.verify_index(manifest_path=manifest_path, index_path=index_path, require_hash=True)


def _paired_manifests(tmp_path: Path, *, source_manifest: object) -> tuple[Path, Path, Path]:
    """Write matching index locks into two manifests while preserving the supplied source-manifest reference.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     manifest, methodology, schema = _paired_manifests(Path(directory), source_manifest="source.json")
    ...     left, right = json.loads(manifest.read_text()), json.loads(methodology.read_text())
    ...     left["index"] == right["index"], left["source_manifest"], schema.name
    (True, 'source.json', 'schema.py')
    """
    methodology_path = tmp_path / "provider-parity-methodology.json"
    methodology_path.write_text(
        json.dumps(
            {
                "index": {
                    "raw_sha256": "0" * 64,
                    "scan_version": 12,
                    "project": "fixture",
                    "scan_root": "/fixture",
                    "scanned_at": "now",
                }
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "codex-integration.json"
    manifest_path.write_text(
        json.dumps({"index": json.loads(methodology_path.read_text())["index"], "source_manifest": source_manifest}),
        encoding="utf-8",
    )
    return manifest_path, methodology_path, _schema_file(tmp_path, 12)


def test_index_contract_requires_source_manifest_metadata(tmp_path: Path) -> None:
    """Codex must identify the exact provider-neutral methodology bytes."""
    module = _load_script()
    manifest_path, methodology_path, schema_path = _paired_manifests(tmp_path, source_manifest=None)

    with pytest.raises(ValueError, match="missing source_manifest"):
        module.index_contract(manifest_path, methodology_path=methodology_path, schema_path=schema_path)


def test_index_contract_rejects_tampered_source_manifest_metadata(tmp_path: Path) -> None:
    """A stale or tampered methodology source cannot supply an index lock."""
    module = _load_script()
    manifest_path, methodology_path, schema_path = _paired_manifests(
        tmp_path,
        source_manifest={"path": str(tmp_path / "provider-parity-methodology.json"), "sha256": "f" * 64},
    )

    with pytest.raises(ValueError, match="source_manifest SHA-256"):
        module.index_contract(manifest_path, methodology_path=methodology_path, schema_path=schema_path)
