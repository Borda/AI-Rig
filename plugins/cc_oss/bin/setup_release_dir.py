#!/usr/bin/env python
"""Create a release directory and protect existing draft artifacts before prepare writes.

Purpose: Prepare release artifact paths while preserving the previous drafts. Scope: One local release directory and a
validated changelog target. Usage: ``setup_release_dir.py RELEASE_DIR CHANGELOG_FILE`` from the release prepare step.
Outputs: Creates RELEASE_DIR and its parents, links CHANGELOG.md, and copies existing artifacts to ``.bak`` siblings
before later phases overwrite them. ``--validate-only`` checks all paths without writes before the changelog audit.
Refuses linked changelog inputs, unrelated changelog names, linked artifact sources, and occupied release changelog
paths before changing any release artifact. Failure: Returns 1 for missing or unsafe arguments and occupied release
changelog paths. Ordinary filesystem errors propagate. Used by: ``skills/release/modes/prepare.md`` before its artifact
writing phases.

Re-running prepare for the same version is legitimate (post-audit-fix retry); silently overwriting hand-edited notes is
destructive, hence the backups. A prior CHANGELOG.md symlink may be re-linked on re-run; a regular file is preserved and
blocks setup.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ARTIFACTS: tuple[str, ...] = (
    "HIGHLIGHTS.md",
    "DRAFT.md",
    "SUMMARY.md",
    "MIGRATION.md",
    "demo.py",
    "waived-changes.md",
)


def _allowed_abs_roots() -> tuple[Path, ...]:
    """Return the allowlist of absolute-path roots, computed at call time.

    Computed lazily rather than at import time so test runs that ``chdir`` after import still see the up-to-date project
    root.  ``tempfile.gettempdir()`` is included to support pytest's ``tmp_path`` fixture and other sandboxed runs.
    """
    return (
        Path.cwd().resolve(),
        (Path(os.path.expanduser("~")) / ".claude").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    )


def _is_within(target: Path, root: Path) -> bool:
    """Return ``True`` when ``target`` resolves under ``root`` (post-resolution).

    Args:
        target: Resolved path to test.
        root: Resolved allowed-root path.

    Returns:
        ``True`` when ``target`` is identical to or nested under ``root``.

    Examples:
        >>> _is_within(Path("/opt/x/y"), Path("/opt"))
        True
        >>> _is_within(Path("/etc/passwd"), Path("/opt"))
        False
    """
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_path_arg(raw: str, label: str) -> Path:
    """Resolve and validate a path argument, rejecting unsafe absolute paths.

    Relative paths must resolve under the caller's cwd. Absolute paths must
    resolve under :func:`_allowed_abs_roots`. Any
    ``..`` traversal token in the raw input is rejected.

    Args:
        raw: Path string from argv.
        label: Argument label used in error messages.

    Returns:
        ``Path`` object suitable for use by the caller.

    Raises:
        ValueError: When the path is rejected for any of the above reasons.
    """
    if not raw:
        raise ValueError(f"{label} must not be empty")
    if ".." in Path(raw).parts:
        raise ValueError(f"{label} must not contain '..': {raw!r}")
    p = Path(raw)
    if not p.is_absolute():
        if not _is_within(p.resolve(), Path.cwd().resolve()):
            raise ValueError(f"{label} resolves outside project root: {raw!r}")
        return p
    resolved = p.expanduser().resolve()
    for root in _allowed_abs_roots():
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return p
    raise ValueError(f"{label} resolves outside project root, ~/.claude, and temp dir: {raw!r} → {resolved.as_posix()}")


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``setup_release_dir.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 1 on unsafe input or backup destination; 0 on success.

    Examples:
        No doctest — filesystem I/O; covered by pytest with ``tmp_path``.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(
        prog="setup_release_dir.py",
        description="Create release dir, symlink changelog, back up artifacts.",
    )
    # nargs="*" keeps the per-arg "release_dir required" / "changelog_file required"
    # stderr messages and exit-1 contract (vs argparse's exit 2).
    parser.add_argument(
        "--validate-only", action="store_true", help="Check paths without creating or changing artifacts."
    )
    parser.add_argument("paths", nargs="*", help="RELEASE_DIR CHANGELOG_FILE (2 paths).")
    args_ns = parser.parse_args(argv)
    positional = args_ns.paths

    if len(positional) < 1:
        print("setup_release_dir: release_dir required", file=sys.stderr)
        return 1
    if len(positional) < 2:
        print("setup_release_dir: changelog_file required", file=sys.stderr)
        return 1

    try:
        release_dir = _validate_path_arg(positional[0], "release_dir")
        changelog_file = _validate_path_arg(positional[1], "changelog_file")
        if any(component.is_symlink() for component in (release_dir, *release_dir.parents)):
            raise ValueError("release_dir must not traverse a symlink")
        if not changelog_file.name.startswith("CHANGELOG"):
            raise ValueError("changelog_file must have a CHANGELOG name")
        if any(component.is_symlink() for component in (changelog_file, *changelog_file.parents)):
            raise ValueError("changelog_file must not traverse a symlink")
        if changelog_file.exists() and not changelog_file.is_file():
            raise ValueError("changelog_file must be a regular file")
        resolved_target = changelog_file.resolve()
        if not any(_is_within(resolved_target, root) for root in _allowed_abs_roots()):
            raise ValueError(f"resolved changelog target outside allowed roots: {resolved_target.as_posix()}")
        # Release sources belong to the project that owns the version directory, even in temporary test projects.
        project_root = release_dir.parent.parent if release_dir.parent.name == "releases" else release_dir.parent
        if not _is_within(resolved_target, project_root.resolve()):
            raise ValueError("changelog_file must be inside the release project")
        link_path = release_dir / "CHANGELOG.md"
        if changelog_file.parent.resolve() / changelog_file.name == release_dir.resolve() / "CHANGELOG.md":
            raise ValueError("changelog_file must not be the release changelog link")
        if link_path.exists() and not link_path.is_symlink():
            raise ValueError("release CHANGELOG.md exists and is not a symlink")
    except ValueError as exc:
        print(f"setup_release_dir: {exc}", file=sys.stderr)
        return 1

    # Validate the complete artifact set before relinking the changelog or making any backup.
    for name in _ARTIFACTS:
        target = release_dir / name
        if target.is_symlink():
            print(f"setup_release_dir: artifact source must not be a symlink: {target}", file=sys.stderr)
            return 1
        backup = release_dir / f"{name}.bak"
        if backup.is_symlink():
            print(f"setup_release_dir: backup destination must not be a symlink: {backup}", file=sys.stderr)
            return 1

    if args_ns.validate_only:
        return 0

    release_dir.mkdir(parents=True, exist_ok=True)

    if link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(resolved_target)

    for name in _ARTIFACTS:
        target = release_dir / name
        if target.is_file():
            # Replace the directory entry atomically so a changed link or hardlink cannot redirect the copy.
            fd, temporary_name = tempfile.mkstemp(prefix=f".{name}.bak-", dir=release_dir)
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                shutil.copy2(target, temporary)
                os.replace(temporary, release_dir / f"{name}.bak")
            finally:
                temporary.unlink(missing_ok=True)
            print(f"⚠ {target} exists — backed up to {name}.bak before overwrite")

    return 0


if __name__ == "__main__":
    sys.exit(main())
