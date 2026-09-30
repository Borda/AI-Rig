#!/usr/bin/env python3
"""Assemble the deterministic codemap-py distribution package.

Packages the REAL tracked plugin tree (not a synthetic mini-candidate): both
runtime manifest directories, every ``bin/`` executable and support module, the
``scripts/`` entrypoints, the Claude skill roster, the hook wiring, and the
top-level product documents. Identity (name, version) is read from
``.claude-plugin/plugin.json`` at build time — never hardcoded — so a single
manifest edit is the sole source of truth.

Determinism: stable file order, LF-terminated generated manifest, fixed on-disk
modes, no timestamps. The per-file ``exec`` flag is a platform-neutral boolean
taken from git's tracked mode (``100755``), never from the build host's
``st_mode`` — a Windows and a POSIX build of the same commit therefore emit a
byte-identical ``package-manifest.json``. ``--check``
rebuilds to a temporary directory and byte-compares.

Mode authority: by default the exec-mode map is derived from ``source_root``'s
OWN git index (``git ls-files --stage``), which is authoritative for a direct
build in the real tracked checkout. A build from a DISPOSABLE copy (as the
install probes use) cannot trust that copy's own synthesized index — a fresh
``git add -A`` on a ``core.filemode=false`` host records ``100644`` for every
file regardless of its real on-disk bit. ``--mode-map <path>`` overrides the
git-derived map with an externally supplied ``{relative_posix_path: bool}``
JSON file (the caller derives it from the REAL repository before any copy is
made).

Payload membership: within each ``_INCLUDE_DIRS`` subtree, membership is drawn
from the SAME map used for modes, not a separate filesystem walk — a shipped
candidate is a key of the effective exec-mode map under that subtree's prefix,
so "pure function of the tracked sources" (below) is literally true. An
untracked file under an include dir (e.g. a concurrent work-in-progress from
another wave) is therefore simply invisible to the payload rather than a
build error; a *tracked* candidate absent from the working tree still raises.
Top-level required documents are still admitted by filesystem presence (they
sit outside ``_INCLUDE_DIRS``), but one present on disk yet untracked still
raises via the mode-map lookup below.

CLI (install probes depend on this contract — do not deviate)::

    python plugins/codemap-py/scripts/build_package.py --out <dir> [--check] [--mode-map <path>]

Exit ``0`` on success; ``1`` on a ``--check`` mismatch; ``2`` on usage or a
payload-closure error (missing required document, symlink, case collision,
missing mode-map entry).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = "package-manifest.json"
_SCHEMA = 2
_MODE_EXEC = 0o755
_MODE_DATA = 0o644

# Directory subtrees copied whole (minus the exclusions below).
_INCLUDE_DIRS: tuple[str, ...] = (
    ".claude-plugin",
    ".codex-plugin",
    "bin",
    "scripts",
    "src",
    "claude-skills",
    "codex-skills",
    "shared",
    "hooks",
)
# Top-level product documents — all required; a missing one is a closure error.
_REQUIRED_DOCS: tuple[str, ...] = ("README.md", "LICENSE", "NOTICE", "CHANGELOG.md")
# Path components that are never shipped (runtime caches, evidence, state).
_EXCLUDE_COMPONENTS: frozenset[str] = frozenset(
    {"__pycache__", ".cache", ".reports", ".temp", ".pytest_cache", ".claude", "tests"}
)
_EXCLUDE_NAMES: frozenset[str] = frozenset({".DS_Store"})
# Human-readable exclusion policy recorded in the manifest.
_EXCLUSIONS: tuple[str, ...] = (
    "__pycache__/",
    ".cache/",
    ".reports/",
    ".temp/",
    ".pytest_cache/",
    ".claude/",
    "tests/",
    "*.pyc",
    ".coverage*",
    ".DS_Store",
)


def _sha256(payload: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact bytes.

    Examples:
        >>> _sha256(b"")[:8]
        'e3b0c442'
    """
    return hashlib.sha256(payload).hexdigest()


def _encode_json(value: dict[str, Any]) -> bytes:
    """Encode a stable, LF-terminated, key-sorted JSON document.

    Examples:
        >>> _encode_json({"b": 1, "a": 2})
        b'{\\n  "a": 2,\\n  "b": 1\\n}\\n'
    """
    return (json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")


def _is_excluded(relative: str) -> bool:
    """Return whether a source-relative posix path is excluded from the payload.

    Examples:
        >>> _is_excluded("bin/__pycache__/x.pyc")
        True
        >>> _is_excluded("bin/scan-index")
        False
    """
    parts = relative.split("/")
    if any(part in _EXCLUDE_COMPONENTS for part in parts):
        return True
    name = parts[-1]
    return name in _EXCLUDE_NAMES or name.endswith(".pyc") or name.startswith(".coverage")


def _load_identity(source_root: Path) -> tuple[str, str]:
    """Return ``(name, version)`` read from the Claude manifest (single source)."""
    manifest = json.loads((source_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return str(manifest["name"]), str(manifest["version"])


def _skill_roster(source_root: Path, skills_subdir: str) -> list[str]:
    """Return the sorted skill names (directories holding a ``SKILL.md``) under *skills_subdir*."""
    skills_dir = source_root / skills_subdir
    if not skills_dir.is_dir():
        return []
    return sorted(child.name for child in skills_dir.iterdir() if (child / "SKILL.md").is_file())


def _git_exec_modes(source_root: Path) -> dict[str, bool]:
    """Return ``{relative_posix_path: is_executable}`` from git's tracked modes.

    The executable bit is read from git (``100755``) rather than the working
    tree's ``st_mode`` so the flag is identical on every OS. This is a hard
    dependency: a deterministic, faithful candidate cannot be built without git's
    tracked modes, so a missing git or a non-git ``SOURCE_ROOT`` raises rather than
    silently marking every payload file non-executable (a determinism hole that
    would strip the launcher's executable bit).

    Raises:
        ValueError: when git is unavailable or ``source_root`` is not a git working
            tree, so the executable-mode metadata cannot be derived.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "ls-files", "--stage", "--", "."],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise ValueError("git not found; the deterministic build requires git to derive tracked modes") from error
    except subprocess.CalledProcessError as error:
        raise ValueError(
            f"cannot derive tracked executable modes: {source_root} is not a git working tree "
            f"({error.stderr.strip() or error})"
        ) from error
    except subprocess.SubprocessError as error:
        raise ValueError(f"cannot derive tracked executable modes: git ls-files failed: {error}") from error
    modes: dict[str, bool] = {}
    for line in completed.stdout.splitlines():
        meta, _, relative = line.partition("\t")
        if relative:
            modes[relative] = meta.split(" ", 1)[0] == "100755"
    return modes


def _load_mode_map(path: Path) -> dict[str, bool]:
    """Load an externally supplied ``{relative_posix_path: is_executable}`` map.

    Used in place of ``_git_exec_modes`` when the build runs against a
    disposable copy whose own synthesized git index is not authoritative (see
    the module docstring's "Mode authority" section).

    Raises:
        ValueError: when the file is missing, not valid JSON, or not a JSON
            object mapping strings to booleans.

    Examples:
        >>> import tempfile
        >>> with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        ...     _ = f.write('{"bin/x": true}')
        ...     name = f.name
        >>> _load_mode_map(Path(name))
        {'bin/x': True}
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read mode map {path}: {error}") from error
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, bool) for k, v in raw.items()):
        raise ValueError(f"mode map {path} must be a JSON object of {{relative_posix_path: bool}}")
    return raw


def _iter_source_payload(source_root: Path, tracked: dict[str, bool]) -> list[tuple[Path, str]]:
    """Return ``(absolute_source, relative_posix)`` payload pairs in canonical order.

    ``_INCLUDE_DIRS`` membership comes from ``tracked``'s keys (the same map the
    caller uses for exec modes), not a filesystem walk: a path under an include-dir
    prefix that has no entry in ``tracked`` is untracked and simply excluded from the
    payload, rather than admitted and later failing the mode-map lookup. This is what
    keeps membership and mode authority from diverging.

    Raises:
        ValueError: on a symlink in the payload, a case-folding collision, a missing
            required document, or a tracked include-dir path absent from the working
            tree (git index and working tree disagree — not silently skipped).
    """
    pairs: list[tuple[Path, str]] = []
    folded: set[str] = set()
    for name in _REQUIRED_DOCS:
        doc = source_root / name
        if not doc.is_file():
            raise ValueError(f"missing required document: {name}")
        _admit(doc, name, folded, pairs)
    # MCP wiring is optional in historical source layouts; current declared wiring
    # is checked with its provider by package validation before publication.
    question_config = source_root / ".codex-mcp.json"
    if question_config.is_file():
        _admit(question_config, ".codex-mcp.json", folded, pairs)
    for dir_name in _INCLUDE_DIRS:
        prefix = f"{dir_name}/"
        for relative in sorted(rel for rel in tracked if rel.startswith(prefix)):
            if _is_excluded(relative):
                continue
            path = source_root / relative
            if path.is_symlink():
                raise ValueError(f"symlink payload forbidden: {relative}")
            if not path.is_file():
                raise ValueError(f"tracked payload path missing from working tree: {relative}")
            _admit(path, relative, folded, pairs)
    return pairs


def _admit(path: Path, relative: str, folded: set[str], pairs: list[tuple[Path, str]]) -> None:
    """Record one payload pair, rejecting a case-folding collision."""
    key = relative.casefold()
    if key in folded:
        raise ValueError(f"case-colliding payload path: {relative}")
    folded.add(key)
    pairs.append((path, relative))


def _write_file(path: Path, data: bytes, executable: bool) -> None:
    """Write bytes with a fixed deterministic mode, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(_MODE_EXEC if executable else _MODE_DATA)


def build_package(source_root: Path, out: Path, mode_map: dict[str, bool] | None = None) -> dict[str, Any]:
    """Build the package into ``out`` and return its manifest.

    The destination is cleared first, so a build is a pure function of the
    tracked sources plus the effective exec-mode map — that same map is also the
    ``_INCLUDE_DIRS`` membership authority (see the module docstring's "Payload
    membership" section), so an untracked file under an include dir cannot leak
    into the shipped payload.

    Args:
        source_root: Plugin root holding the tracked tree.
        out: Destination directory (created fresh).
        mode_map: Authoritative ``{relative_posix_path: is_executable}`` map.
            When ``None`` (direct builds in the real tracked checkout), the map
            is derived from ``source_root``'s own git index. When supplied
            (disposable-copy builds), it overrides the copy's own index — see
            the module docstring's "Mode authority" section.

    Returns:
        The ``package-manifest.json`` document as a dict.

    Raises:
        ValueError: when a required top-level document is present on disk but
            has no entry in the effective mode map (untracked) — never silently
            defaulted to non-executable. (An ``_INCLUDE_DIRS`` file with no map
            entry cannot reach this: it is excluded from membership itself.)
    """
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    name, version = _load_identity(source_root)
    exec_modes = _git_exec_modes(source_root) if mode_map is None else mode_map
    records: list[dict[str, Any]] = []
    for src, relative in _iter_source_payload(source_root, exec_modes):
        data = src.read_bytes()
        # Structurally unreachable for _INCLUDE_DIRS candidates (membership is already
        # exec_modes-filtered above); still live for a required top-level document that
        # is present on disk but untracked (no mode-map entry at all).
        if relative not in exec_modes:
            raise ValueError(f"missing mode-map entry for shipped payload path: {relative}")
        executable = exec_modes[relative]
        _write_file(out / relative, data, executable)
        records.append({"path": relative, "sha256": _sha256(data), "exec": executable})
    manifest = {
        "schema": _SCHEMA,
        "name": name,
        "version": version,
        "skills": {
            "claude": _skill_roster(source_root, "claude-skills"),
            "codex": _skill_roster(source_root, "codex-skills"),
        },
        "files": records,
        "exclusions": list(_EXCLUSIONS),
    }
    _write_file(out / _MANIFEST, _encode_json(manifest), executable=False)
    return manifest


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Return every file's bytes keyed by package-relative posix path."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _compare(built: Path, reference: Path) -> list[str]:
    """Return byte-difference descriptions between two package trees."""
    left, right = _tree_bytes(built), _tree_bytes(reference)
    diffs = [f"only in rebuild: {name}" for name in sorted(set(left) - set(right))]
    diffs += [f"only in reference: {name}" for name in sorted(set(right) - set(left))]
    diffs += [f"bytes differ: {name}" for name in sorted(set(left) & set(right)) if left[name] != right[name]]
    return diffs


def _exec_mode_mismatches(out: Path, manifest: dict[str, Any]) -> list[str]:
    """On POSIX, return files whose on-disk executable bit disagrees with the manifest.

    The manifest ``exec`` flag is the platform-neutral source of truth; this verifies the build actually applied it. On
    non-POSIX hosts the executable bit is unreliable, so no mismatch is reported.
    """
    if os.name != "posix":
        return []
    mismatches: list[str] = []
    for record in manifest["files"]:
        disk = out / record["path"]
        if disk.is_file() and bool(disk.stat().st_mode & 0o111) != bool(record["exec"]):
            mismatches.append(record["path"])
    return mismatches


def _run_check(source_root: Path, out: Path, mode_map: dict[str, bool] | None = None) -> int:
    """Rebuild to a temp dir, byte-compare, and verify on-disk executable modes."""
    with tempfile.TemporaryDirectory() as tmp:
        rebuild = Path(tmp) / "rebuild"
        manifest = build_package(source_root, rebuild, mode_map)
        if out.exists() and any(out.iterdir()):
            reference = out
        else:
            reference = Path(tmp) / "reference"
            build_package(source_root, reference, mode_map)
        diffs = _compare(rebuild, reference)
        # Verify executable modes on BOTH the temp rebuild and the ``--out`` reference: a
        # byte-identical file whose mode was tampered in ``--out`` is invisible to _compare.
        mode_mismatches = sorted(
            set(_exec_mode_mismatches(rebuild, manifest)) | set(_exec_mode_mismatches(reference, manifest))
        )
    if diffs:
        sys.stderr.write("package rebuild is not deterministic:\n" + "\n".join(diffs) + "\n")
        return 1
    if mode_mismatches:
        sys.stderr.write("on-disk executable modes disagree with manifest:\n" + "\n".join(mode_mismatches) + "\n")
        return 1
    print("package build is deterministic")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the builder CLI arguments."""
    parser = argparse.ArgumentParser(description="Build the deterministic codemap-py package.")
    parser.add_argument("--out", required=True, type=Path, help="package output directory")
    parser.add_argument("--check", action="store_true", help="rebuild to temp and byte-compare")
    parser.add_argument(
        "--mode-map",
        type=Path,
        help="JSON {relative_posix_path: bool} overriding source_root's own git-derived exec modes",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build the package, or verify determinism with ``--check``."""
    args = _parse_args(argv)
    mode_map = _load_mode_map(args.mode_map) if args.mode_map else None
    if args.check:
        return _run_check(SOURCE_ROOT, args.out, mode_map)
    manifest = build_package(SOURCE_ROOT, args.out, mode_map)
    print(f"built {manifest['name']} {manifest['version']}: {len(manifest['files'])} files -> {args.out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        sys.stderr.write(f"build-package-error: {error}\n")
        raise SystemExit(2) from error
