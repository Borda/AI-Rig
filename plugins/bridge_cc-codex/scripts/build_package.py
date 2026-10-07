#!/usr/bin/env python3
"""Build a disposable, install-shaped copy of the bridge plugin.

Purpose:
    Copy the bridge's installable payload into an explicitly selected output
    directory so validation exercises the same relative paths that an
    installed plugin exposes.

Scope:
    This helper performs a local, deterministic copy only. It excludes tests,
    interpreter caches, coverage files, and project-private artifact trees.
    It never contacts a marketplace, writes to a user home, or publishes a
    package.

Usage:
    Run ``python scripts/build_package.py --output <temporary-directory>`` from
    the source checkout. The output directory must not already exist and may be
    passed directly to ``validate_package.py``.

Outputs:
    A complete relative-path copy of the bridge payload, including manifests,
    skills, rules, schemas, assets, runtime scripts, and package helpers.

Failure:
    Invalid source/output relationships, symlink payloads, or an existing
    output directory exit non-zero before any package is produced.

Used by:
    Maintainer package gates and bridge packaging tests use this command to
    prove that validation does not depend on the source-tree working directory.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

#: Plugin directory that is copied into the distributable package, derived from this script's location.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
#: Directory names left out of the package copy at any depth: scratch output, caches and tests.
EXCLUDED_DIRECTORIES = frozenset({".plans", ".reports", ".temp", ".pytest_cache", "__pycache__", "tests"})
#: File names left out of the package copy: coverage data and macOS metadata.
EXCLUDED_FILES = frozenset({".coverage", ".DS_Store"})


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the required disposable output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory receiving the package copy")
    return parser.parse_args(argv)


def _check_output(source: Path, output: Path) -> None:
    """Reject output paths that could overwrite or recursively copy the source."""
    source = source.resolve()
    output = output.resolve()
    if output == source or source in output.parents:
        raise ValueError("output must be outside the source plugin")
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")


def _copy_payload(source: Path, output: Path) -> int:
    """Copy regular payload files while preserving relative paths and modes."""
    count = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
            continue
        if path.name in EXCLUDED_FILES:
            continue
        if path.is_symlink():
            raise ValueError(f"symlink payload forbidden: {relative.as_posix()}")
        if not path.is_file():
            continue
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    """Build one disposable package copy and report its file count."""
    args = _parse_args(argv)
    try:
        _check_output(PACKAGE_ROOT, args.output)
        args.output.mkdir(parents=True)
        count = _copy_payload(PACKAGE_ROOT, args.output)
    except (OSError, ValueError) as error:
        print(f"package-build-error: {error}", file=sys.stderr)
        return 2
    print(f"Built bridge package: {args.output} ({count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
