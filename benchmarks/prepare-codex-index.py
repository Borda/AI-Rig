#!/usr/bin/env python3
"""Normalize a fresh Codemap scan to the manifest-locked benchmark bytes.

The scanner's timestamp and absolute root are environment-dependent even when
the indexed repository is identical. This utility rewrites only those declared
metadata values and embedded root prefixes, then replaces the new scan only if
its complete SHA-256 matches the reviewed manifest.

Usage:
    python3 benchmarks/prepare-codex-index.py \
        --index-path <managed-repo>/.cache/codemap/codemap-provider-parity-pl-2.6.5.json \
        --source-root <managed-repo> \
        --manifest-path benchmarks/manifests/codex-integration.json \
        --methodology-path benchmarks/manifests/provider-parity-methodology.json \
        --schema-path plugins/codemap-py/src/codemap_py/schema.py
"""

from __future__ import annotations

import ast
import hashlib
import json
import ntpath
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from _bench_common.edit_patch_contracts import semantic_index_sha256
from _bench_common.mutation_isolation import relocate_frozen_index_for_worktree


def _root_boundary_separators(source_root: str) -> tuple[str, ...]:
    r"""Return the separators that can delimit a path *beneath* ``source_root``.

    The convention follows the recorded path data, not the running interpreter: an index
    and its lock are portable JSON, so a scan taken on Windows carries a drive-rooted
    (or backslash-bearing) root whose children may be spelled with either separator, and
    both therefore delimit the tree. A POSIX root admits ``/`` only — a backslash is a
    legal POSIX filename character, so accepting it would fold ``…/repo\sibling`` into
    the locked tree. Anchoring on :data:`os.sep` instead made the relocation a silent
    no-op whenever the payload's separator differed from the host's, which is what left
    a Windows run hashing unrewritten checkout paths.

    Args:
        source_root: Scan root exactly as it is spelled in the index or lock.

    Returns:
        Separator strings that mark the start of a path below *source_root*.

    Examples:
        >>> _root_boundary_separators("/scan/repo")
        ('/',)
        >>> "\\" in _root_boundary_separators("C:\\scan\\repo")
        True
    """
    if ntpath.splitdrive(source_root)[0] or "\\" in source_root:
        return ("\\", "/")
    return ("/",)


def _is_absolute_path(value: str) -> bool:
    r"""Return whether *value* is an absolute path under POSIX *or* Windows rules.

    Lock documents travel between machines: a POSIX-rooted canonical checkout path must
    still validate when the lock is read on Windows, where ``Path`` becomes
    ``WindowsPath`` and rejects a driveless root; a recorded ``C:\repo`` must likewise
    validate off Windows. ``os.path.isabs`` cannot serve as the check either —
    ``ntpath.isabs("/x")`` changed answer in Python 3.13.

    Args:
        value: Path string exactly as recorded in the lock document.

    Returns:
        True when either path flavour considers *value* absolute.

    Examples:
        >>> _is_absolute_path("/scan/repo")
        True
        >>> _is_absolute_path("C:\\repo"), _is_absolute_path("repo/sub")
        (True, False)
    """
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _cli_error(message: str) -> None:
    """Report one CLI usage error and exit with argparse's usage status.

    Args:
        message: Human-readable description of the invalid invocation.

    Raises:
        SystemExit: Always, with status 2 (argparse's usage-error status).

    Examples:
        >>> _cli_error("--manifest-path is required")
        Traceback (most recent call last):
        ...
        SystemExit: 2
    """
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def _load_json(path: Path) -> dict[str, Any]:
    """Load one manifest object and reject malformed/non-object JSON."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"manifest root must be an object: {path}")
    return value


def _scanner_scan_version(schema_path: Path) -> int:
    """Read the persisted-index schema version from the checked-out scanner source."""
    try:
        tree = ast.parse(schema_path.read_text(encoding="utf-8"), filename=str(schema_path))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"cannot read Codemap schema version from {schema_path}: {exc}") from exc
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        else:
            continue
        if name == "SCAN_VERSION" and isinstance(value, ast.Constant) and isinstance(value.value, int):
            if isinstance(value.value, bool) or value.value < 1:
                break
            return value.value
    raise ValueError(f"Codemap schema does not declare a valid SCAN_VERSION: {schema_path}")


def index_contract(
    manifest_path: Path,
    *,
    methodology_path: Path | None = None,
    schema_path: Path | None = None,
) -> dict[str, Any]:
    """Return and validate the active index lock and scanner schema identity.

    The Codex manifest must carry the same index identity as the provider-neutral methodology source. When the schema
    source is supplied, an older manifest lock is rejected even if an old index's bytes still match that lock.
    """
    manifest = _load_json(manifest_path)
    expected = manifest.get("index")
    if not isinstance(expected, dict):
        raise ValueError(f"active manifest has no index contract: {manifest_path}")
    raw_sha = expected.get("raw_sha256")
    semantic_sha = expected.get("semantic_sha256")
    scan_version = expected.get("scan_version")
    if not isinstance(raw_sha, str) or len(raw_sha) != 64 or any(char not in "0123456789abcdef" for char in raw_sha):
        raise ValueError("active index contract requires a 64-character raw_sha256")
    if not isinstance(scan_version, int) or isinstance(scan_version, bool):
        raise ValueError("active index contract requires an integer scan_version")
    if semantic_sha is not None and (
        not isinstance(semantic_sha, str)
        or len(semantic_sha) != 64
        or any(char not in "0123456789abcdef" for char in semantic_sha)
    ):
        raise ValueError("active index semantic_sha256 must be a 64-character digest")

    if methodology_path is not None:
        methodology = _load_json(methodology_path)
        methodology_index = methodology.get("index")
        if not isinstance(methodology_index, dict):
            raise ValueError(f"provider-neutral methodology has no index contract: {methodology_path}")
        if expected != methodology_index:
            raise ValueError(
                "Codex/index lock disagrees with provider-neutral methodology; regenerate both manifests "
                f"(codex={expected.get('raw_sha256')}, methodology={methodology_index.get('raw_sha256')})"
            )
        source = manifest.get("source_manifest")
        if not isinstance(source, dict):
            raise ValueError("Codex manifest is missing source_manifest metadata")
        source_sha = source.get("sha256")
        actual_sha = hashlib.sha256(methodology_path.read_bytes()).hexdigest()
        root = Path(__file__).resolve().parents[1]
        try:
            relative_methodology = methodology_path.resolve().relative_to(root).as_posix()
        except ValueError:
            relative_methodology = ""
        if source.get("path") not in {str(methodology_path), relative_methodology}:
            raise ValueError("Codex source_manifest path does not identify the active methodology")
        if source_sha != actual_sha:
            raise ValueError("Codex source_manifest SHA-256 does not match the active methodology")

    if schema_path is not None:
        current_version = _scanner_scan_version(schema_path)
        if scan_version != current_version:
            raise ValueError(
                f"active index scan_version {scan_version} does not match current Codemap schema {current_version}; "
                "rebuild and relock the index before running the benchmark"
            )
    return expected


def verify_index(
    index_path: Path,
    manifest_path: Path,
    *,
    source_root: Path | None = None,
    methodology_path: Path | None = None,
    schema_path: Path | None = None,
    require_hash: bool = False,
) -> None:
    """Fail closed unless an index has the active lock's schema metadata."""
    expected = index_contract(manifest_path, methodology_path=methodology_path, schema_path=schema_path)
    try:
        payload = _load_json(index_path)
    except ValueError as exc:
        raise ValueError(f"index is not valid JSON: {index_path}: {exc}") from exc
    if payload.get("scan_version") != expected["scan_version"]:
        raise ValueError(
            f"index scan_version mismatch: expected {expected['scan_version']}, got {payload.get('scan_version')!r}"
        )
    if not isinstance(payload.get("modules"), list):
        raise ValueError("index schema mismatch: modules must be a list")
    if expected.get("semantic_sha256") is not None:
        runtime_root = source_root or Path(str(payload.get("scan_root", "")))
        if not runtime_root:
            raise ValueError("semantic index verification requires source_root or index scan_root")
        actual_semantic_sha256 = semantic_index_sha256(payload, runtime_root)
        if actual_semantic_sha256 != expected["semantic_sha256"]:
            raise ValueError(
                f"index semantic SHA-256 mismatch: expected {expected['semantic_sha256']}, got {actual_semantic_sha256}"
            )
    if require_hash:
        actual_sha = hashlib.sha256(index_path.read_bytes()).hexdigest()
        if actual_sha != expected["raw_sha256"]:
            raise ValueError(f"index SHA-256 mismatch: expected {expected['raw_sha256']}, got {actual_sha}")


def _replace_root(value: Any, source_root: str, locked_root: str) -> Any:
    """Replace scanner-root path prefixes recursively without changing structure.

    Only a string that *is* the scan root, or a path beneath it, is rewritten. The previous bare ``str.replace`` rewrote
    every occurrence in every string, so a content field that merely mentions the checkout path (a docstring first line,
    a captured subprocess argument) was edited too — and those rewritten bytes are the ones hashed and installed, which
    makes the installed index disagree with the source it was scanned from. Anchoring on a separator boundary
    additionally keeps a sibling directory whose name merely extends the root (``…/repo-other`` beside ``…/repo``) out
    of the locked tree; a bare ``startswith`` would fold it in. Which separators bound the tree is decided by the root's
    own spelling (:func:`_root_boundary_separators`), never by the host, so the same payload relocates identically on
    every OS.
    """
    if isinstance(value, str):
        if value == source_root:
            return locked_root
        for separator in _root_boundary_separators(source_root):
            if value.startswith(source_root + separator):
                return locked_root + value[len(source_root) :]
        return value
    if isinstance(value, list):
        return [_replace_root(item, source_root, locked_root) for item in value]
    if isinstance(value, dict):
        return {key: _replace_root(item, source_root, locked_root) for key, item in value.items()}
    return value


def _patch_index_locks(path: Path) -> dict[str, dict[str, Any]]:
    """Load the reviewed per-task historical index identities."""
    payload = _load_json(path)
    if payload.get("schema_version") != "provider-parity-patch-index-locks-v1":
        raise ValueError("patch index locks use an unsupported schema")
    canonical_root = payload.get("canonical_scan_root")
    if not isinstance(canonical_root, str) or not _is_absolute_path(canonical_root):
        raise ValueError("patch index locks require an absolute canonical_scan_root")
    tasks = payload.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise ValueError("patch index locks require a non-empty tasks object")
    for task_id, lock in tasks.items():
        if not isinstance(task_id, str) or not task_id.startswith("PT-") or not isinstance(lock, dict):
            raise ValueError("patch index locks require PT task object entries")
        if not isinstance(lock.get("baseline_commit"), str) or len(lock["baseline_commit"]) != 40:
            raise ValueError(f"patch index lock {task_id} requires an exact baseline commit")
        if not isinstance(lock.get("scan_version"), int) or isinstance(lock["scan_version"], bool):
            raise ValueError(f"patch index lock {task_id} requires an integer scan_version")
        if not isinstance(lock.get("module_count"), int) or lock["module_count"] < 1:
            raise ValueError(f"patch index lock {task_id} requires a positive module_count")
        for digest_field in ("raw_sha256_at_canonical_root", "semantic_sha256"):
            value = lock.get(digest_field)
            if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"patch index lock {task_id} requires a lowercase {digest_field}")
        if not isinstance(lock.get("scanned_at"), str) or not lock["scanned_at"]:
            raise ValueError(f"patch index lock {task_id} requires scanned_at")
    return tasks


def _git(source_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one bounded Git operation against the explicit source object store."""
    return subprocess.run(
        ["git", "-C", str(source_root), *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _release_worktree(source_root: Path, worktree: Path, task_id: str) -> str:
    """Detach one temporary patch worktree; return a failure description, or "" when clean.

    Every Git call here is deliberately unchecked and the outcome is *returned* rather
    than raised: this runs on the cleanup path, where a raising subprocess replaces
    whatever failure is already unwinding (a semantic-drift ValueError surfaced as
    "cleanup failed", hiding the drift that actually invalidated the run). The caller
    decides whether the cleanup failure is the only failure and may be raised.
    """
    _git(worktree, "reset", "--hard", "HEAD", check=False)
    _git(worktree, "clean", "-fdx", check=False)
    removal = _git(source_root, "worktree", "remove", str(worktree), check=False)
    registered = str(worktree) in _git(source_root, "worktree", "list", "--porcelain", check=False).stdout
    if removal.returncode != 0 or worktree.exists() or registered:
        return f"patch index worktree cleanup failed for {task_id}: {removal.stderr.strip()[:300]}"
    return ""


def _install_patch_task_index(
    # config object would only rename them
    *,
    source_root: Path,
    worktree: Path,
    task_id: str,
    lock: dict[str, Any],
    scan_index_bin: Path,
    bundle_dir: Path,
    canonical_root: str,
) -> str:
    """Scan one prepared worktree, verify it against *lock*, install it, and return its SHA-256.

    The scanner is launched through the running interpreter rather than executed directly:
    ``scan-index`` is an extension-less Python script whose shebang only selects an
    interpreter on POSIX, so a direct launch raises ``OSError`` on Windows. This matches
    how the sibling lane runs the same binary (``benchmarks/run-codemap-cli.py``).

    Raises:
        ValueError: If the scanner fails or the graph drifts from the reviewed lock.
    """
    scan = subprocess.run(
        [sys.executable, str(scan_index_bin), "--root", str(worktree)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if scan.returncode != 0:
        raise ValueError(f"scan-index failed for {task_id}: {scan.stderr.strip()[:500]}")
    scanned_path = worktree / ".cache" / "codemap" / f"{worktree.name}.json"
    payload = _load_json(scanned_path)
    payload = _replace_root(payload, str(worktree), str(source_root))
    payload.update(
        project=f"provider-parity-{task_id}",
        scan_root=str(source_root),
        scanned_at=lock.get("scanned_at"),
    )
    if payload.get("scan_version") != lock.get("scan_version"):
        raise ValueError(f"patch index scan_version drifted for {task_id}")
    if len(payload.get("modules", [])) != lock.get("module_count"):
        raise ValueError(f"patch index module count drifted for {task_id}")
    semantic_sha = semantic_index_sha256(payload, source_root)
    if semantic_sha != lock.get("semantic_sha256"):
        raise ValueError(
            f"patch index semantic SHA-256 drifted for {task_id}: "
            f"expected {lock.get('semantic_sha256')}, got {semantic_sha}"
        )
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    # The raw byte lock is root-dependent, so it can only be checked on the machine whose
    # checkout path the lock was recorded at. Off that path the check used to disappear
    # silently, leaving the operator believing a byte-identity guarantee that never ran.
    if str(source_root) == canonical_root:
        if digest != lock.get("raw_sha256_at_canonical_root"):
            raise ValueError(f"patch index canonical raw SHA-256 drifted for {task_id}")
    else:
        print(
            f"WARNING: raw byte-identity check skipped for {task_id}: source root {source_root} is not "
            f"the canonical root {canonical_root}; only semantic graph identity was verified",
            file=sys.stderr,
        )
    destination = bundle_dir / f"{task_id}.json"
    with tempfile.NamedTemporaryFile(dir=bundle_dir, prefix=f".{task_id}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return digest


def prepare_patch_index_bundle(source_root: Path, locks_path: Path, scan_index_bin: Path) -> dict[str, str]:
    """Build and verify one source-matched frozen Codemap index per patch task.

    Historical tasks intentionally use different Git commits. Each graph is
    therefore scanned in a detached temporary worktree, relocated to the clean
    orchestration checkout, and admitted only when its semantic identity equals
    the reviewed lock. The operation never changes the orchestration checkout's
    HEAD or tracked files.

    Args:
        source_root: Clean Git checkout whose object store contains every baseline.
        locks_path: Reviewed task-to-index identity JSON.
        scan_index_bin: Checked-out ``scan-index`` executable.

    Returns:
        Mapping from task ID to the installed runtime index SHA-256.

    Raises:
        ValueError: If source, commit, scanner output, or index identity is invalid.
    """
    source_root = source_root.resolve(strict=True)
    locks_path = locks_path.resolve(strict=True)
    scan_index_bin = scan_index_bin.resolve(strict=True)
    if not (_git(source_root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"):
        raise ValueError("patch index source must be a Git worktree")
    if _git(source_root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise ValueError("patch index source must have no tracked changes")
    locks = _patch_index_locks(locks_path)
    installed: dict[str, str] = {}
    bundle_dir = source_root / ".cache" / "codemap" / "patch"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    canonical_root = str(_load_json(locks_path)["canonical_scan_root"])
    root = Path(tempfile.mkdtemp(prefix="codemap-patch-index-bundle-")).resolve(strict=True)
    try:
        for task_id, lock in sorted(locks.items()):
            commit = lock.get("baseline_commit")
            if not isinstance(commit, str) or len(commit) != 40:
                raise ValueError(f"patch index lock {task_id} has no exact baseline commit")
            if _git(source_root, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode != 0:
                raise ValueError(
                    f"patch baseline {commit} for {task_id} is unavailable; fetch the exact commit, then rerun "
                    "python3 benchmarks/prepare-codex-index.py --prepare-patch-bundle"
                )
            worktree = root / task_id
            created = False
            try:
                _git(source_root, "worktree", "add", "--detach", str(worktree), commit)
                created = True
                installed[task_id] = _install_patch_task_index(
                    source_root=source_root,
                    worktree=worktree,
                    task_id=task_id,
                    lock=lock,
                    scan_index_bin=scan_index_bin,
                    bundle_dir=bundle_dir,
                    canonical_root=canonical_root,
                )
            except BaseException:
                # A cleanup failure must never displace the failure already unwinding:
                # the drift/scan error is the diagnosis the operator needs, so it is
                # re-raised and the cleanup problem is reported alongside it.
                if created and (failure := _release_worktree(source_root, worktree, task_id)):
                    print(f"WARNING: {failure}", file=sys.stderr)
                raise
            else:
                # Nothing else in flight, so a leaked worktree is itself the failure:
                # leaving it registered would dirty the orchestration checkout.
                if failure := _release_worktree(source_root, worktree, task_id):
                    raise ValueError(failure)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return installed


def prepare_index(
    index_path: Path,
    source_root: Path,
    manifest_path: Path,
    *,
    methodology_path: Path | None = None,
    schema_path: Path | None = None,
) -> str:
    """Normalize one new scan and atomically install it only on a locked identity match."""
    expected = index_contract(manifest_path, methodology_path=methodology_path, schema_path=schema_path)
    payload = _load_json(index_path)
    if payload.get("scan_version") != expected["scan_version"]:
        raise ValueError(
            "fresh index schema mismatch: "
            f"expected scan_version {expected['scan_version']}, got {payload.get('scan_version')!r}"
        )
    if not isinstance(payload.get("modules"), list):
        raise ValueError("fresh index schema mismatch: modules must be a list")
    runtime_root = source_root.resolve()
    semantic_sha256 = expected.get("semantic_sha256")
    if semantic_sha256 is not None:
        payload.update(project=expected["project"], scan_root=str(runtime_root), scanned_at=expected["scanned_at"])
        actual_semantic_sha256 = semantic_index_sha256(payload, runtime_root)
        if actual_semantic_sha256 != semantic_sha256:
            raise ValueError(
                f"normalized index semantic SHA-256 mismatch: expected {semantic_sha256}, got {actual_semantic_sha256}"
            )
    else:
        locked_root = str(expected["scan_root"])
        payload = _replace_root(payload, str(runtime_root), locked_root)
        payload.update(project=expected["project"], scan_root=locked_root, scanned_at=expected["scanned_at"])
    normalized = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(normalized).hexdigest()
    if semantic_sha256 is None and digest != expected["raw_sha256"]:
        raise ValueError(f"normalized index SHA-256 mismatch: expected {expected['raw_sha256']}, got {digest}")

    with tempfile.NamedTemporaryFile(dir=index_path.parent, prefix=f".{index_path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(normalized)
    try:
        os.replace(temporary, index_path)
    finally:
        temporary.unlink(missing_ok=True)
    return digest


def relocate_index_for_run(
    frozen_index_path: Path,
    worktree_root: Path,
    manifest_path: Path,
    *,
    methodology_path: Path | None = None,
    schema_path: Path | None = None,
) -> dict[str, Any]:
    """Install the locked index into one run's own worktree and return its relocation provenance.

    A run outside the canonical clone still owes the graph the study was locked against. Scanning the
    worktree would build a new graph with no provenance link to that lock, so the frozen bytes are
    copied and only ``scan_root`` is moved — the same relocation the executable stages perform for
    their disposable checkouts. The returned provenance is what an admission gate checks instead of
    the byte hash, which reproduces only at the path the lock recorded.

    Args:
        frozen_index_path: Path to the locked index inside the canonical clone.
        worktree_root: Root of the worktree receiving the relocated copy.
        manifest_path: Path to the active manifest carrying the index lock.
        methodology_path: Path to the provider-neutral methodology manifest, when cross-checked.
        schema_path: Path to the checked-out scanner schema source, when version-checked.

    Returns:
        The relocation provenance, with the derived index's path added as ``derived_index_path``.

    Raises:
        ValueError: If the frozen index does not satisfy the active lock, or relocation would change
            graph content.

    Examples:
        >>> relocate_index_for_run.__name__
        'relocate_index_for_run'
    """
    expected = index_contract(manifest_path, methodology_path=methodology_path, schema_path=schema_path)
    frozen_bytes = frozen_index_path.read_bytes()
    frozen_sha256 = hashlib.sha256(frozen_bytes).hexdigest()
    if frozen_sha256 != expected["raw_sha256"]:
        raise ValueError(
            f"relocation source is not the locked index: expected {expected['raw_sha256']}, got {frozen_sha256}"
        )
    source_root = Path(expected["scan_root"])
    derived_bytes, provenance = relocate_frozen_index_for_worktree(
        frozen_bytes, source_root=source_root, worktree_root=worktree_root
    )
    derived_path = worktree_root / ".cache" / "codemap" / f"{worktree_root.name}.json"
    derived_path.parent.mkdir(parents=True, exist_ok=True)
    derived_path.write_bytes(derived_bytes)
    if hashlib.sha256(derived_path.read_bytes()).hexdigest() != provenance["derived_index_sha256"]:
        raise ValueError("relocated index write is incomplete")
    return {**provenance, "derived_index_path": str(derived_path)}


def main(
    index_path: Path = None,
    source_root: Path = None,
    manifest_path: Path = None,
    methodology_path: Path = None,
    schema_path: Path = None,
    verify: bool = False,
    require_hash: bool = False,
    print_contract: bool = False,
    prepare_patch_bundle: bool = False,
    patch_locks_path: Path = None,
    scan_index_bin: Path = None,
    relocate_into: Path = None,
    provenance_path: Path = None,
) -> None:
    """Prepare, verify, or describe one manifest-locked Codemap index.

    Args:
        index_path: Path to the fresh scan to normalize, verify, or install.
        source_root: Root the fresh scan was taken from; its prefix is rewritten to the locked root.
        manifest_path: Path to the active manifest carrying the index lock. Required on every path.
        methodology_path: Path to the provider-neutral methodology manifest; when given, its index
            lock and source_manifest identity are cross-checked against the Codex manifest.
        schema_path: Path to the checked-out scanner schema source; when given, a manifest lock
            older than the current ``SCAN_VERSION`` is rejected.
        verify: Check an existing index against the active lock instead of installing a new one.
        require_hash: With ``--verify``, also require the exact manifest-locked index bytes.
        print_contract: Print the validated index contract as sorted JSON and exit.
        prepare_patch_bundle: Build every manifest-locked historical patch index.
        patch_locks_path: Reviewed per-task patch index identity JSON.
        scan_index_bin: Checked-out scanner executable used for the patch bundle.
        relocate_into: Worktree root receiving the locked index; installs the relocated copy and
            prints its relocation provenance instead of scanning or normalizing.
        provenance_path: File to also write that relocation provenance to, for a runner to load.

    Raises:
        SystemExit: With status 2 when a required argument for the selected mode is missing.

    Examples:
        >>> main.__name__
        'main'
    """
    # fire passes CLI string args regardless of type annotation — coerce Path args explicitly.
    if manifest_path is not None:
        manifest_path = Path(manifest_path)
    if index_path is not None:
        index_path = Path(index_path)
    if source_root is not None:
        source_root = Path(source_root)
    if methodology_path is not None:
        methodology_path = Path(methodology_path)
    if schema_path is not None:
        schema_path = Path(schema_path)
    if patch_locks_path is not None:
        patch_locks_path = Path(patch_locks_path)
    if scan_index_bin is not None:
        scan_index_bin = Path(scan_index_bin)

    if prepare_patch_bundle:
        if source_root is None or patch_locks_path is None or scan_index_bin is None:
            _cli_error("--source-root, --patch-locks-path, and --scan-index-bin are required for patch preparation")
        print(json.dumps(prepare_patch_index_bundle(source_root, patch_locks_path, scan_index_bin), sort_keys=True))
        return

    if relocate_into is not None:
        relocate_into = Path(relocate_into)
    if provenance_path is not None:
        provenance_path = Path(provenance_path)

    if manifest_path is None:
        _cli_error("--manifest-path is required unless --prepare-patch-bundle is used")

    if relocate_into is not None:
        if index_path is None:
            _cli_error("--index-path is required with --relocate-into")
        provenance = relocate_index_for_run(
            index_path,
            relocate_into,
            manifest_path,
            methodology_path=methodology_path,
            schema_path=schema_path,
        )
        if provenance_path is not None:
            provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(provenance, sort_keys=True))
        return

    if print_contract:
        print(
            json.dumps(
                index_contract(manifest_path, methodology_path=methodology_path, schema_path=schema_path),
                sort_keys=True,
            )
        )
        return
    if verify:
        if index_path is None:
            _cli_error("--index-path is required with --verify")
        verify_index(
            index_path,
            manifest_path,
            source_root=source_root,
            methodology_path=methodology_path,
            schema_path=schema_path,
            require_hash=require_hash,
        )
        print(f"verified: {index_path}")
        return
    if index_path is None or source_root is None:
        _cli_error("--index-path and --source-root are required unless --print-contract is used")
    print(
        prepare_index(
            index_path,
            source_root,
            manifest_path,
            methodology_path=methodology_path,
            schema_path=schema_path,
        )
    )


if __name__ == "__main__":
    from fire import Fire

    Fire(main)
