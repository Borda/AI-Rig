"""Disposable Codex runtime bundles and input-snapshot ledgers shared by the structural runner tests."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path


def make_direct_runtime_bundle(root: Path) -> Path:
    """Create the minimum source-shaped direct CLI runtime used by isolation tests.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     launcher = make_direct_runtime_bundle(Path(directory))
        ...     launcher.name, (launcher.parents[1] / "src/codemap_py/__init__.py").is_file()
        ('codemap-py', True)
    """
    runtime = root / "codemap-runtime"
    launcher = runtime / "bin" / "codemap-py"
    exclusions = runtime / "bin" / "_exclusions.py"
    entrypoint = runtime / "scripts" / "codemap_py_entry.py"
    package = runtime / "src" / "codemap_py"
    launcher.parent.mkdir(parents=True)
    entrypoint.parent.mkdir(parents=True)
    package.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    exclusions.write_text("EXCLUSION_PATTERNS = ()\n", encoding="utf-8")
    entrypoint.write_text("raise SystemExit(0)\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    return launcher


def write_files(root: Path, files: dict[str, str]) -> None:
    """Write each relative path in ``files`` under ``root``, creating parent directories."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def snapshot_entry(snapshot_root: Path, role: str, path: Path) -> dict[str, object]:
    """Normalise one archived file's mode and describe it as an input-ledger entry.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     root = Path(directory)
        ...     _ = (root / "a.txt").write_text("a")
        ...     snapshot_entry(root, "role", root / "a.txt")["archived_path"]
        'a.txt'
    """
    archived_mode = 0o700 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o600
    path.chmod(archived_mode)
    return {
        "role": role,
        "archived_path": path.relative_to(snapshot_root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "mode": archived_mode,
    }


def write_runtime_snapshot_metadata(
    snapshot_root: Path,
    sources: dict[str, Path],
    *,
    locked_files: dict[str, Path] | None = None,
) -> None:
    """Write the minimal input identity ledger required by snapshot binding tests.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     root = Path(directory)
        ...     write_runtime_snapshot_metadata(root, {})
        ...     json.loads((root / "input-snapshot.json").read_text())
        {'files': []}
    """
    files = [
        snapshot_entry(snapshot_root, role, path)
        for role, source in sources.items()
        for path in sorted(source.rglob("*"))
        if path.is_file()
    ]
    files += [snapshot_entry(snapshot_root, role, path) for role, path in (locked_files or {}).items()]
    (snapshot_root / "input-snapshot.json").write_text(json.dumps({"files": files}), encoding="utf-8")
