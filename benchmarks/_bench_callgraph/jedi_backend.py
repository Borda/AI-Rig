"""Jedi reference backend: resolve each call-site position to its definition by static inference.

Jedi parses Python source with parso and infers statically; it does not import or execute the analysed repository's
Python files. It imports only compiled and builtin modules, inside its own inference subprocess, and
``load_unsafe_extensions=False`` keeps it from loading compiled extensions found in the analysed project. jedi is
imported lazily so the truth builder and its tests run without it installed.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath

from _bench_callgraph.truth import DefLocation

#: Pinned jedi release; the census refuses any other version.
JEDI_VERSION = "0.20.1"

#: Line breaks as ``ast`` and parso count them (form feeds and other ``str.splitlines`` breaks excluded).
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


class ReferenceToolError(RuntimeError):
    """The reference tool is missing, the wrong version, or failed on a file."""


@dataclass(frozen=True)
class JediBackend:
    """Resolve call sites with ``jedi.Script.goto(follow_imports=True)`` inside one repository checkout.

    Attributes:
        root: Repository checkout; also the jedi project path.
        cache_directory: Directory for jedi's parser cache, kept out of the user's home.
    """

    root: Path
    cache_directory: Path

    def _jedi(self) -> object:
        """Import jedi, check its version and apply the fixed settings."""
        try:
            import jedi
        except ImportError as error:
            raise ReferenceToolError(
                f"jedi is not installed; install jedi=={JEDI_VERSION} into the interpreter that runs the census"
            ) from error
        if jedi.__version__ != JEDI_VERSION:
            raise ReferenceToolError(f"jedi {jedi.__version__} found, the census is pinned to {JEDI_VERSION}")
        # One cache directory per process: jedi creates its cache subdirectory without tolerating a concurrent creator.
        jedi.settings.cache_directory = str(self.cache_directory / f"process-{os.getpid()}")
        return jedi

    def _location(self, module_path: Path | None, line: int | None) -> DefLocation | None:
        """Map a jedi definition to a repository-relative location, or ``None`` when it has no line."""
        if line is None:
            return None
        if module_path is None:
            return DefLocation(None, line)
        try:
            relative = PurePath(module_path).relative_to(self.root)
        except ValueError:
            return DefLocation(None, line)
        return DefLocation(relative.as_posix(), line)

    def resolve(self, path: str, positions: Sequence[tuple[int, int]]) -> list[tuple[DefLocation, ...]]:
        """Return the definitions of the name at each ``(line, column)`` of *path*.

        Args:
            path: Repository-relative POSIX path.
            positions: ``(line, column)`` pairs, line 1-based and column 0-based.

        Returns:
            One tuple of locations per position, sorted and deduplicated; empty when jedi finds nothing.

        Raises:
            ReferenceToolError: When jedi is missing or not the pinned version.
        """
        jedi = self._jedi()
        # The search path replaces the environment's: installed packages (a second copy of the analysed project in an
        # active virtualenv) are never consulted, and the environment is the census interpreter, not $VIRTUAL_ENV.
        sys_path = [str(entry) for entry in search_paths(self.root)]
        project = jedi.Project(self.root, sys_path=sys_path, load_unsafe_extensions=False, smart_sys_path=True)
        environment = jedi.create_environment(sys.executable, safe=False)
        source = (self.root / path).read_text(encoding="utf-8")
        script = jedi.Script(source, path=self.root / path, project=project, environment=environment)
        lines = _LINE_BREAK.split(source)
        answers: list[tuple[DefLocation, ...]] = []
        for line, column in positions:
            names = script.goto(line, _character_column(lines[line - 1], column), follow_imports=True)
            leaked = [str(name.module_path) for name in names if _installed_package(name.module_path)]
            if leaked:
                raise ReferenceToolError(f"{path}:{line} resolved into an installed package {leaked[0]}")
            found = {self._location(name.module_path, name.line) for name in names}
            answers.append(tuple(sorted((loc for loc in found if loc is not None), key=_location_key)))
        return answers


def search_paths(root: Path) -> tuple[Path, ...]:
    """Return the import search path of a checkout: ``src/`` when present, then the repository root.

    The census module naming follows the same roots; jedi's ``smart_sys_path`` adds the importing file's directories.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     [entry.name for entry in search_paths(Path(tmp))] == [Path(tmp).name]
        True
    """
    source_root = root / "src"
    return (source_root, root) if source_root.is_dir() else (root,)


def _installed_package(module_path: Path | None) -> bool:
    """Return whether a definition lives in an installed package other than jedi's own bundled stubs.

    Examples:
        >>> _installed_package(Path("/venv/lib/python3.12/site-packages/click/termui.py"))
        True
        >>> _installed_package(Path("/venv/lib/python3.12/site-packages/jedi/third_party/typeshed/stdlib/os.pyi"))
        False
    """
    parts = PurePath(module_path).parts if module_path is not None else ()
    if "site-packages" not in parts:
        return False
    following = parts[parts.index("site-packages") + 1 : parts.index("site-packages") + 2]
    return following != ("jedi",)


def _character_column(line: str, byte_offset: int) -> int:
    """Convert an ``ast`` UTF-8 byte offset into the code-point column jedi expects.

    Examples:
        >>> _character_column('x = ("💩", f(1))', 14)
        11
    """
    return len(line.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore"))


def _location_key(location: DefLocation) -> tuple[str, int]:
    """Sort key placing outside-repository locations first."""
    return (location.path or "", location.line)
