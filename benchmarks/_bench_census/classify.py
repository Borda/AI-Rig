"""Classify every census disagreement by rule, so each false negative and false positive carries a mechanism.

False-positive classes:
    ``parent_package_init``: the extra edge points at a package ``__init__.py`` that Python executes on the way to a
    module the source imports: the source lives in that package, or another truth target of the same source does.
    ``other``: anything else.

False-negative classes:
    ``script_directory``: the source sits in a directory without ``__init__.py`` and the missed edge resolves only with
    that directory on the search path (the truth builder records which edges need it).
    ``non_package_path``: the target is reached through a directory without ``__init__.py`` below the repository root.
    ``other``: anything else.

These are rules, not hand verdicts: a class names the structural shape of the disagreement, never whether either side
is wrong.
"""

from __future__ import annotations

import posixpath
from collections import Counter
from collections.abc import Iterable
from enum import Enum

__all__ = ["ExtraClass", "MissClass", "classify_false_negatives", "classify_false_positives", "count_classes"]

_PACKAGE_MARKER = "__init__.py"


class ExtraClass(str, Enum):
    """Mechanism class of a false positive."""

    PARENT_PACKAGE_INIT = "parent_package_init"
    OTHER = "other"


class MissClass(str, Enum):
    """Mechanism class of a false negative."""

    SCRIPT_DIRECTORY = "script_directory"
    NON_PACKAGE_PATH = "non_package_path"
    OTHER = "other"


def _is_package(directory: str, sources: frozenset[str]) -> bool:
    """Return whether *directory* holds an ``__init__.py`` in the source universe."""
    return posixpath.join(directory, _PACKAGE_MARKER) in sources


def classify_false_positives(
    false_positives: Iterable[tuple[str, str]], truth: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], ExtraClass]:
    """Class every false positive edge.

    Args:
        false_positives: codemap-py edges absent from the truth.
        truth: The repository's truth edges.

    Returns:
        Class per edge.

    Examples:
        >>> fps = [("pkg/sub/a.py", "pkg/__init__.py"), ("x.py", "y.py")]
        >>> {edge[1]: cls.value for edge, cls in classify_false_positives(fps, []).items()}
        {'pkg/__init__.py': 'parent_package_init', 'y.py': 'other'}
    """
    targets_by_source: dict[str, set[str]] = {}
    for source, target in truth:
        targets_by_source.setdefault(source, set()).add(target)
    classes: dict[tuple[str, str], ExtraClass] = {}
    for source, target in false_positives:
        package_dir = posixpath.dirname(target)
        is_init = posixpath.basename(target) == _PACKAGE_MARKER
        source_dir = posixpath.dirname(source)
        # A root-level ``__init__.py`` has package_dir "": only a root-level source sits inside it, and no other
        # target path starts with "/", so a repository-root package never matches by reach.
        source_inside = source_dir == package_dir or source_dir.startswith(package_dir + "/")
        reaches = any(other.startswith(package_dir + "/") for other in targets_by_source.get(source, ()))
        parent = is_init and (source_inside or reaches)
        classes[(source, target)] = ExtraClass.PARENT_PACKAGE_INIT if parent else ExtraClass.OTHER
    return classes


def _non_package_ancestor(path: str, sources: frozenset[str]) -> bool:
    """Return whether any directory between the root and *path* lacks ``__init__.py``."""
    directory = posixpath.dirname(path)
    while directory:
        if not _is_package(directory, sources):
            return True
        directory = posixpath.dirname(directory)
    return False


def classify_false_negatives(
    false_negatives: Iterable[tuple[str, str]],
    sources: Iterable[str],
    script_directory_only: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], MissClass]:
    """Class every false negative edge.

    Args:
        false_negatives: Truth edges codemap-py misses.
        sources: The truth universe, used to tell packages from plain directories.
        script_directory_only: Truth edges that resolve only with the importing script's own directory on the search
            path (``TruthEdges.script_directory_only``).

    Returns:
        Class per edge.

    Examples:
        >>> universe = ["tools/run.py", "tools/helper.py", "lib/__init__.py", "lib/deep/m.py", "lib/x.py"]
        >>> fns = [("tools/run.py", "tools/helper.py"), ("lib/x.py", "lib/deep/m.py")]
        >>> [cls.value for cls in classify_false_negatives(fns, universe, fns[:1]).values()]
        ['script_directory', 'non_package_path']
    """
    universe = frozenset(sources)
    script_only = frozenset(script_directory_only)
    classes: dict[tuple[str, str], MissClass] = {}
    for source, target in false_negatives:
        if (source, target) in script_only:
            classes[(source, target)] = MissClass.SCRIPT_DIRECTORY
        elif _non_package_ancestor(target, universe):
            classes[(source, target)] = MissClass.NON_PACKAGE_PATH
        else:
            classes[(source, target)] = MissClass.OTHER
    return classes


def count_classes(classes: dict[tuple[str, str], Enum]) -> dict[str, int]:
    """Count edges per class value, in first-seen order of the enum members.

    Args:
        classes: Class per edge.

    Returns:
        Mapping of class value to count, without zero entries.

    Examples:
        >>> count_classes({("a", "b"): MissClass.OTHER, ("c", "d"): MissClass.OTHER})
        {'other': 2}
    """
    counts = Counter(cls.value for cls in classes.values())
    return dict(sorted(counts.items()))
