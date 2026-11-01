"""Regression tests for deterministic fresh-machine Codemap index preparation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "prepare-codex-index.py"
CANONICAL_ROOT = "/different/canonical/root"

_WINDOWS_ROOT = str(PureWindowsPath("C:/scan/repo"))
_WINDOWS_LOCKED_ROOT = str(PureWindowsPath("D:/locked/root"))


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


@pytest.fixture
def script() -> ModuleType:
    """Load the index utility module fresh for one test."""
    return _load_script()


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


def _locked_fixture(tmp_path: Path, scan_version: int = 12) -> tuple[Path, Path, Path, bytes, str]:
    """Write a fresh scan plus a manifest locking the SHA-256 of its normalized bytes.

    Returns the index, its source root, the manifest, the locked bytes and their SHA-256.
    """
    expected_sha256 = hashlib.sha256(_fixture_files(tmp_path, "placeholder", scan_version=scan_version)[-1]).hexdigest()
    return (*_fixture_files(tmp_path, expected_sha256, scan_version=scan_version), expected_sha256)


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


@dataclass(frozen=True)
class PatchBundle:
    """A one-commit source checkout, a stub scanner, and a lock document that matches the stub's graph."""

    source: Path
    commit: str
    scanner: Path
    locks: Path


@pytest.fixture
def patch_bundle(tmp_path: Path, script: ModuleType) -> PatchBundle:
    """Create a one-commit source checkout plus a stub scanner and matching semantic locks."""
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    (source / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "demo.py"], check=True)
    identity = ["-c", "user.name=Bench", "-c", "user.email=bench@example.invalid"]
    subprocess.run(["git", "-C", str(source), *identity, "commit", "-m", "baseline"], check=True, capture_output=True)
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
    locks = _write_patch_locks(
        tmp_path / "locks.json",
        commit=commit,
        canonical_root=CANONICAL_ROOT,
        semantic_sha256=_locked_semantic_sha256(script, source),
    )
    return PatchBundle(source=source, commit=commit, scanner=scanner, locks=locks)


def _fail_worktree_release(script: ModuleType, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    """Make every worktree release report *message* as its failure after the real release has run."""
    released = script._release_worktree
    monkeypatch.setattr(
        script, "_release_worktree", lambda root, worktree, task_id: released(root, worktree, task_id) or message
    )


class TestPrepareIndex:
    """Normalizing a fresh scan into the locked index bytes, or refusing when the graph does not match."""

    def test_rewrites_only_environment_metadata_to_exact_locked_bytes(self, tmp_path: Path, script: ModuleType) -> None:
        """A fresh scan becomes portable only when its complete normalized hash matches.

        The fresh scan differs from the locked oracle only in environment metadata (root, project, time); preparing it
        rewrites exactly those fields, and the resulting file is byte-identical to the locked bytes.
        """
        index_path, source_root, manifest_path, locked_bytes, expected_sha256 = _locked_fixture(tmp_path)

        digest = script.prepare_index(index_path, source_root, manifest_path)

        assert digest == expected_sha256
        assert index_path.read_bytes() == locked_bytes

    def test_uses_semantic_lock_without_rewriting_runtime_root(self, tmp_path: Path, script: ModuleType) -> None:
        """A prospective lock accepts equivalent scans at distinct roots and keeps each queryable path.

        Two scans of the same graph at different roots both satisfy one semantic lock, and each keeps its own scan root
        so the paths remain queryable where they were taken.
        """
        # Resolved for the same reason as the identity test: prepare_index hashes through
        # semantic_index_sha256, which strips the resolved spelling of the runtime root.
        first_root = (tmp_path / "first" / "target").resolve()
        second_root = (tmp_path / "second" / "target").resolve()
        first_root.mkdir(parents=True)
        second_root.mkdir(parents=True)
        first_index = tmp_path / "first-index.json"
        second_index = tmp_path / "second-index.json"
        payload = _rooted_payload(first_root)
        first_index.write_text(json.dumps(payload), encoding="utf-8")
        second_index.write_text(json.dumps(_rooted_payload(second_root)), encoding="utf-8")
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "index": {
                        "raw_sha256": "0" * 64,
                        "semantic_sha256": script.semantic_index_sha256(payload, first_root),
                        "scan_version": 13,
                        "scan_root": "<runtime-root>",
                        "project": "fixture",
                        "scanned_at": "locked-time",
                    }
                }
            ),
            encoding="utf-8",
        )

        script.prepare_index(first_index, first_root, manifest)
        script.prepare_index(second_index, second_root, manifest)

        assert json.loads(first_index.read_text(encoding="utf-8"))["scan_root"] == str(first_root)
        assert json.loads(second_index.read_text(encoding="utf-8"))["scan_root"] == str(second_root)
        assert script.verify_index(first_index, manifest, source_root=first_root) is None
        assert script.verify_index(second_index, manifest, source_root=second_root) is None

    def test_preserves_fresh_scan_when_normalized_graph_does_not_match(
        self, tmp_path: Path, script: ModuleType
    ) -> None:
        """Metadata normalization must never conceal a changed graph or scanner result.

        The manifest locks a hash the scan cannot reach; preparation must raise a mismatch and leave the fresh scan
        bytes untouched instead of overwriting them.
        """
        index_path, source_root, manifest_path, _locked_bytes = _fixture_files(tmp_path, "0" * 64)
        original = index_path.read_bytes()

        with pytest.raises(ValueError, match="normalized index SHA-256 mismatch"):
            script.prepare_index(index_path, source_root, manifest_path)

        assert index_path.read_bytes() == original


class TestSemanticIndexSha256:
    """The semantic hash identifies a scan graph independently of the root it was taken at."""

    def test_is_stable_across_runtime_roots(self, tmp_path: Path, script: ModuleType) -> None:
        """Equivalent scans keep one semantic identity without sharing a runtime path.

        The same graph scanned under two different roots hashes identically once each root prefix is stripped, while the
        raw payloads still differ in their recorded scan root.
        """
        # Resolved: semantic_index_sha256 strips str(source_root.resolve()), so an unresolved root
        # (a Windows 8.3 short name, a macOS /tmp symlink) spells the payload prefix differently
        # from the prefix being stripped and no root would be removed from either digest.
        first_root = (tmp_path / "first" / "target").resolve()
        second_root = (tmp_path / "second" / "target").resolve()
        payload = _rooted_payload(first_root)
        second_payload = _rooted_payload(second_root)

        assert script.semantic_index_sha256(payload, first_root) == script.semantic_index_sha256(
            second_payload, second_root
        )
        assert payload["scan_root"] != second_payload["scan_root"]


class TestPatchIndexBundle:
    """Building one historical index per patch task in a throwaway worktree, with real Git and a stub scanner."""

    def test_builds_each_exact_historical_graph_at_the_runtime_root(
        self, patch_bundle: PatchBundle, script: ModuleType
    ) -> None:
        """Patch tasks must never reuse the current-revision graph for historical source.

        The stub scanner is run inside a worktree at the locked commit; the installed index must carry the runtime root,
        be hash-reported, and leave the source checkout's tracked files unchanged. Real Git is used because worktree
        creation and release are the contract.
        """
        source = patch_bundle.source

        installed = script.prepare_patch_index_bundle(source, patch_bundle.locks, patch_bundle.scanner)

        index_path = source / ".cache/codemap/patch/PT-01.json"
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        status = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert installed == {"PT-01": hashlib.sha256(index_path.read_bytes()).hexdigest()}
        assert payload["scan_root"] == str(source.resolve())
        assert payload["modules"][0]["file"] == str(source.resolve() / "demo.py")
        assert status == ""

    def test_warns_when_the_raw_byte_lock_cannot_be_checked(
        self, patch_bundle: PatchBundle, script: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Off the canonical checkout path the skipped byte-identity check must be announced.

        The lock's canonical root differs from the runtime root, so the raw byte comparison cannot apply; the bundle
        must say so on stderr instead of silently skipping it.
        """
        script.prepare_patch_index_bundle(patch_bundle.source, patch_bundle.locks, patch_bundle.scanner)

        assert "raw byte-identity check skipped for PT-01" in capsys.readouterr().err

    def test_reports_graph_drift_even_when_cleanup_also_fails(
        self,
        patch_bundle: PatchBundle,
        script: ModuleType,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing worktree release must not displace the drift that invalidated the run.

        The lock holds a semantic hash the scanned graph cannot match and the worktree release fails too; the drift is
        raised as the error while the cleanup failure is still reported on stderr.
        """
        locks = _write_patch_locks(
            tmp_path / "drift-locks.json",
            commit=patch_bundle.commit,
            canonical_root=CANONICAL_ROOT,
            semantic_sha256="d" * 64,
        )
        _fail_worktree_release(script, monkeypatch, "cleanup exploded")

        with pytest.raises(ValueError, match="semantic SHA-256 drifted"):
            script.prepare_patch_index_bundle(patch_bundle.source, locks, patch_bundle.scanner)

        assert "cleanup exploded" in capsys.readouterr().err

    def test_raises_when_cleanup_is_the_only_failure(
        self, patch_bundle: PatchBundle, script: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A leaked worktree on an otherwise clean pass must still fail the bundle.

        The graph matches its lock, but releasing the worktree reports a failure; the bundle must raise it rather than
        report success with a leaked worktree.
        """
        _fail_worktree_release(script, monkeypatch, "cleanup failed for PT-01")

        with pytest.raises(ValueError, match="cleanup failed for PT-01"):
            script.prepare_patch_index_bundle(patch_bundle.source, patch_bundle.locks, patch_bundle.scanner)

    def test_fails_before_scanning_when_baseline_object_is_missing(self, tmp_path: Path, script: ModuleType) -> None:
        """A paid patch scope cannot begin when its exact source object is unavailable.

        The source repository holds no commits, so the locked baseline commit cannot be found; the bundle must fail
        before running the scanner (which would exit 99) and install no index.
        """
        source = tmp_path / "source"
        source.mkdir()
        subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
        locks = _write_patch_locks(
            tmp_path / "locks.json", commit="a" * 40, canonical_root="/canonical", semantic_sha256="c" * 64
        )
        scanner = tmp_path / "scan-index"
        scanner.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        scanner.chmod(0o755)

        with pytest.raises(ValueError, match="fetch the exact commit"):
            script.prepare_patch_index_bundle(source, locks, scanner)

        assert not (source / ".cache/codemap/patch/PT-01.json").exists()


class TestReplaceRoot:
    """Relocating a scan's root prefix in every embedded path while leaving prose and sibling paths verbatim."""

    @pytest.mark.parametrize(
        ("payload", "root", "locked_root", "expected"),
        [
            pytest.param(
                {
                    "scan_root": "/scan/repo",
                    "modules": [
                        {
                            "file": "/scan/repo/src/demo.py",
                            "docstring_first_line": "Reads defaults from /scan/repo/setup.cfg at import time.",
                            "neighbour": "/scan/repo-other/src/demo.py",
                            "imports": ["/scan/repo/src/helper.py", "relative/path.py"],
                        }
                    ],
                },
                "/scan/repo",
                "/locked/root",
                {
                    "scan_root": "/locked/root",
                    "modules": [
                        {
                            "file": "/locked/root/src/demo.py",
                            "docstring_first_line": "Reads defaults from /scan/repo/setup.cfg at import time.",
                            "neighbour": "/scan/repo-other/src/demo.py",
                            "imports": ["/locked/root/src/helper.py", "relative/path.py"],
                        }
                    ],
                },
                id="posix-prefixes-only",
            ),
            pytest.param(
                {
                    "scan_root": _WINDOWS_ROOT,
                    "modules": [
                        {
                            "file": str(PureWindowsPath("C:/scan/repo/src/demo.py")),
                            "forward_slash_child": f"{_WINDOWS_ROOT}/src/helper.py",
                            "neighbour": str(PureWindowsPath("C:/scan/repo-other/src/demo.py")),
                            "docstring_first_line": f"Reads defaults from {_WINDOWS_ROOT}\\setup.cfg at import time.",
                        }
                    ],
                },
                _WINDOWS_ROOT,
                _WINDOWS_LOCKED_ROOT,
                {
                    "scan_root": _WINDOWS_LOCKED_ROOT,
                    "modules": [
                        {
                            "file": str(PureWindowsPath("D:/locked/root/src/demo.py")),
                            "forward_slash_child": f"{_WINDOWS_LOCKED_ROOT}/src/helper.py",
                            "neighbour": str(PureWindowsPath("C:/scan/repo-other/src/demo.py")),
                            "docstring_first_line": f"Reads defaults from {_WINDOWS_ROOT}\\setup.cfg at import time.",
                        }
                    ],
                },
                id="windows-root-either-separator",
            ),
            pytest.param(
                {"file": "/scan/repo\\sibling/demo.py"},
                "/scan/repo",
                "/locked/root",
                {"file": "/scan/repo\\sibling/demo.py"},
                id="posix-root-backslash-sibling",
            ),
        ],
    )
    def test_relocates_only_the_scan_root_and_paths_beneath_it(
        self, script: ModuleType, payload: dict[str, object], root: str, locked_root: str, expected: dict[str, object]
    ) -> None:
        """Only the scan root and paths beneath it are relocated; prose and siblings stay verbatim.

        POSIX roots delimit children by `/` alone, so a backslash is an ordinary filename character there. A Windows-
        rooted scan relocates on any host whichever separator spells its children (Windows accepts both); its inputs are
        built with ``PureWindowsPath`` so the Windows contract runs on every platform. A sibling that merely extends the
        root name, and prose that only mentions the checkout, never change.
        """
        assert script._replace_root(payload, root, locked_root) == expected


class TestPatchIndexLocks:
    """Reading a lock document: its canonical root is portable data and its rows must be complete."""

    @pytest.mark.parametrize(
        "canonical_root",
        [
            pytest.param("/checkouts/repo", id="posix-root"),
            pytest.param(str(PureWindowsPath("C:/checkouts/repo")), id="windows-drive-root"),
            pytest.param(str(PureWindowsPath("//server/share/repo")), id="windows-unc-root"),
        ],
    )
    def test_accepts_an_absolute_canonical_root_recorded_on_either_platform(
        self, tmp_path: Path, script: ModuleType, canonical_root: str
    ) -> None:
        """A lock is portable data: its canonical root is judged by both path flavours, not the host.

        A run reads a lock recorded on the other OS — a POSIX root on Windows, a drive or UNC root off Windows — and
        validation based on only the native path flavour rejected the foreign spelling as relative.
        """
        locks = _write_patch_locks(
            tmp_path / "locks.json", commit="a" * 40, canonical_root=canonical_root, semantic_sha256="c" * 64
        )

        assert script._patch_index_locks(locks)["PT-01"]["baseline_commit"] == "a" * 40

    def test_rejects_a_relative_canonical_root(self, tmp_path: Path, script: ModuleType) -> None:
        """A canonical root that is relative under both path flavours is refused.

        A relative root names no checkout location, so the lock cannot say where its raw bytes were taken.
        """
        locks = _write_patch_locks(
            tmp_path / "locks.json", commit="a" * 40, canonical_root="checkouts/repo", semantic_sha256="c" * 64
        )

        with pytest.raises(ValueError, match="absolute canonical_scan_root"):
            script._patch_index_locks(locks)

    def test_rejects_incomplete_rows_before_scanning(self, tmp_path: Path, script: ModuleType) -> None:
        """A partial provenance row cannot become a runnable historical index.

        A task row carrying only its baseline commit lacks the scan version and the other identity fields, so reading
        the lock fails up front instead of at scan time.
        """
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
            script._patch_index_locks(locks)


class TestIndexContract:
    """The manifest index lock is checked against the current Codemap schema and the methodology source manifest."""

    def test_old_lock_is_accepted_without_schema_context_but_fails_against_current_schema(
        self, tmp_path: Path, script: ModuleType
    ) -> None:
        """An archived v11 lock remains reproducible only outside the active v12 contract.

        Without a schema the v11 index prepares to its locked bytes; checking the same manifest against a v12 schema
        must refuse it, so an archived lock cannot be mistaken for a current one.
        """
        index_path, source_root, manifest_path, _locked_bytes, expected_sha256 = _locked_fixture(
            tmp_path, scan_version=11
        )

        prepared = script.prepare_index(index_path, source_root, manifest_path)
        with pytest.raises(ValueError, match="does not match current Codemap schema"):
            script.index_contract(manifest_path, schema_path=_schema_file(tmp_path, 12))

        assert prepared == expected_sha256

    def test_current_v12_lock_and_index_pass_schema_contract(self, tmp_path: Path, script: ModuleType) -> None:
        """The active v12 scan is accepted when manifest, bytes, and scanner agree.

        With a v12 schema the contract reports version 12, the locked index verifies, and preparing the fresh scan
        reproduces the locked bytes exactly.
        """
        index_path, source_root, manifest_path, locked_bytes, expected_sha256 = _locked_fixture(tmp_path)
        schema_path = _schema_file(tmp_path, 12)

        assert script.index_contract(manifest_path, schema_path=schema_path)["scan_version"] == 12
        assert script.verify_index(index_path, manifest_path, schema_path=schema_path) is None
        assert script.prepare_index(index_path, source_root, manifest_path, schema_path=schema_path) == expected_sha256
        assert index_path.read_bytes() == locked_bytes

    def test_verify_index_rejects_wrong_bytes_with_current_schema(self, tmp_path: Path, script: ModuleType) -> None:
        """A structurally valid v12 graph still requires the exact locked bytes.

        The index is replaced by a different v12-shaped graph; byte verification must report an SHA-256 mismatch rather
        than accept it for being well formed.
        """
        index_path, _source_root, manifest_path, _locked_bytes, _sha256 = _locked_fixture(tmp_path)
        index_path.write_text(json.dumps({"scan_version": 12, "modules": [{"wrong": True}]}), encoding="utf-8")

        with pytest.raises(ValueError, match="index SHA-256 mismatch"):
            script.verify_index(manifest_path=manifest_path, index_path=index_path, require_hash=True)

    def test_requires_source_manifest_metadata(self, tmp_path: Path, script: ModuleType) -> None:
        """Codex must identify the exact provider-neutral methodology bytes.

        A Codex manifest whose `source_manifest` is null cannot say which methodology its lock came from, so the
        contract check refuses it.
        """
        manifest_path, methodology_path, schema_path = _paired_manifests(tmp_path, source_manifest=None)

        with pytest.raises(ValueError, match="missing source_manifest"):
            script.index_contract(manifest_path, methodology_path=methodology_path, schema_path=schema_path)

    def test_rejects_tampered_source_manifest_metadata(self, tmp_path: Path, script: ModuleType) -> None:
        """A stale or tampered methodology source cannot supply an index lock.

        The recorded `source_manifest` digest does not match the methodology bytes on disk, so the contract check
        refuses the lock instead of trusting the stale reference.
        """
        manifest_path, methodology_path, schema_path = _paired_manifests(
            tmp_path,
            source_manifest={"path": str(tmp_path / "provider-parity-methodology.json"), "sha256": "f" * 64},
        )

        with pytest.raises(ValueError, match="source_manifest SHA-256"):
            script.index_contract(manifest_path, methodology_path=methodology_path, schema_path=schema_path)


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
