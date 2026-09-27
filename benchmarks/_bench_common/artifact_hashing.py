"""Identity digests for benchmark runner entrypoints and their implementation packages."""

from __future__ import annotations

import hashlib
from pathlib import Path


def runner_sha256(runner_path: Path, package_dir: Path | None = None) -> str:
    """Return the identity digest of a runner, covering its package when it has one.

    A runner that still holds its whole implementation in one file hashes to that file's plain
    SHA-256, exactly as before. A runner split into a package is a thin re-export shim, so hashing
    the shim alone would stop detecting any change to the code that actually runs; passing
    ``package_dir`` folds every module of that package into the digest as well.

    The digest binds each member's path to its bytes, so moving code between two modules of the
    same package changes the result. Paths enter the digest in POSIX form and members are ordered
    by that form, keeping the value identical on Linux, macOS, and Windows.

    Args:
        runner_path: The ``run-<name>.py`` entrypoint.
        package_dir: The runner's implementation package, or ``None`` when it has not been split.

    Returns:
        A hex SHA-256 digest.

    Raises:
        ValueError: If the runner, or a declared package directory, is missing.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     runner = Path(tmp) / "run-demo.py"
        ...     _ = runner.write_text("print(1)\\n", encoding="utf-8")
        ...     plain = runner_sha256(runner)
        ...     pkg = Path(tmp) / "_bench_demo"
        ...     pkg.mkdir()
        ...     _ = (pkg / "cli.py").write_text("x = 1\\n", encoding="utf-8")
        ...     tree = runner_sha256(runner, pkg)
        ...     stable = runner_sha256(runner, pkg)
        >>> len(plain) == 64 and len(tree) == 64
        True
        >>> tree == stable, tree == plain
        (True, False)
    """
    if not runner_path.is_file():
        raise ValueError(f"required runner entrypoint is missing or not a file: {runner_path}")
    if package_dir is None:
        return hashlib.sha256(runner_path.read_bytes()).hexdigest()
    if not package_dir.is_dir():
        raise ValueError(f"declared runner package is missing or not a directory: {package_dir}")

    base = runner_path.parent
    members = [runner_path, *(p for p in package_dir.rglob("*.py") if "__pycache__" not in p.parts)]
    ordered = sorted(members, key=lambda p: p.relative_to(base).as_posix())

    digest = hashlib.sha256()
    for member in ordered:
        digest.update(member.relative_to(base).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(member.read_bytes()).digest())
    return digest.hexdigest()


def module_sha256(path: Path) -> str:
    """Return the identity digest of a pinned module, whether it is a file or a package.

    Unlike :func:`runner_sha256` there is no entrypoint shim to anchor on: a pinned module
    either still is one ``.py`` file or has become a package directory of them. Hashing the
    file that used to be there would raise once it is a directory, so the shape is detected
    and a package folds all of its modules into one digest.

    Members bind path to bytes and are ordered by POSIX-form relative path, so the value is
    identical on Linux, macOS, and Windows, and moving code between two modules of the same
    package still changes it.

    Args:
        path: The pinned ``.py`` file, or the package directory that replaced it.

    Returns:
        A hex SHA-256 digest.

    Raises:
        ValueError: If *path* is neither an existing file nor an existing directory.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     mod = Path(tmp) / "thing.py"
        ...     _ = mod.write_text("x = 1\\n", encoding="utf-8")
        ...     plain = module_sha256(mod)
        ...     pkg = Path(tmp) / "pkg"
        ...     pkg.mkdir()
        ...     _ = (pkg / "__init__.py").write_text("x = 1\\n", encoding="utf-8")
        ...     tree = module_sha256(pkg)
        >>> len(plain) == 64 and len(tree) == 64
        True
        >>> tree == plain
        False
    """
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        raise ValueError(f"required module input is missing: {path}")

    members = sorted(
        (p for p in path.rglob("*.py") if "__pycache__" not in p.parts),
        key=lambda p: p.relative_to(path).as_posix(),
    )
    digest = hashlib.sha256()
    for member in members:
        digest.update(member.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(member.read_bytes()).digest())
    return digest.hexdigest()
