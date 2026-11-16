"""Run the import-edge census: clone pinned repositories, index them with a pinned scanner revision, score, report.

Isolation:
    - Each repository is fetched at its full commit into its own empty directory under a throwaway work directory.
      Nothing from a clone is executed: the truth is built by static resolution and the index by codemap-py's static
      scanner.
    - The scanner is checked out from this repository at the named revision into a detached git worktree under the
      same work directory and removed afterwards, so a dirty working tree never leaks into a measurement.
    - Every index is built from scratch and its recorded generation must equal the revision's ``SCAN_VERSION``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from _bench_census.classify import (
    ExtraClass,
    MissClass,
    classify_false_negatives,
    classify_false_positives,
    count_classes,
)
from _bench_census.report import CensusRow, census_table, class_table
from _bench_census.score import RepoScore, pool, read_scanner_edges, score_repo
from _bench_census.truth import build_truth

__all__ = [
    "CensusError",
    "CensusResult",
    "RepoCoordinate",
    "RepoResult",
    "Scanner",
    "build_index",
    "fetch_repository",
    "load_coordinates",
    "measure_repository",
    "read_scan_version",
    "remove_tree",
    "render_markdown",
    "run_census",
    "scanner_checkout",
    "scanner_directory",
    "scanner_tree_digest",
    "write_results",
]

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_SCAN_VERSION = re.compile(r"^SCAN_VERSION\s*:\s*int\s*=\s*(\d+)", re.MULTILINE)
_PLUGIN_DIR = Path("plugins") / "codemap-py"
_SCHEMA_FILE = Path("src") / "codemap_py" / "schema.py"
_ENTRY_FILE = Path("scripts") / "codemap_py_entry.py"
_INDEX_DIR = Path(".cache") / "codemap"
_GIT_TIMEOUT_S = 600
_INDEX_TIMEOUT_S = 1800
#: ``Scanner.ref`` of a scanner used as it stands in a directory rather than checked out at a git revision.
WORKING_TREE_REF = "working-tree"
#: Plugin subdirectories whose ``.py`` files decide what a directory scanner indexes; their digest names the scanner.
_DIGEST_DIRS = ("src", "scripts")
#: Headline group: its pooled recall and precision render bold.
HEADLINE_GROUP = "held-out"
_CLASS_COLUMNS = (
    (f"fp:{ExtraClass.PARENT_PACKAGE_INIT.value}", "Parent-package FP"),
    (f"fp:{ExtraClass.OTHER.value}", "Other FP"),
    (f"fn:{MissClass.SCRIPT_DIRECTORY.value}", "Script-directory FN"),
    (f"fn:{MissClass.NON_PACKAGE_PATH.value}", "Non-package-path FN"),
    (f"fn:{MissClass.OTHER.value}", "Other FN"),
)


class CensusError(RuntimeError):
    """A census step failed; the message names the step and what to check."""


@dataclass(frozen=True)
class RepoCoordinate:
    """A pinned public repository.

    Attributes:
        name: Directory name of the clone, also the index project name.
        label: Short display label.
        group: Group label, for example ``held-out`` or ``tuned``.
        url: Clone URL.
        commit: Full 40-character commit SHA.
    """

    name: str
    label: str
    group: str
    url: str
    commit: str


@dataclass(frozen=True)
class Scanner:
    """A codemap-py scanner checked out at one revision.

    Attributes:
        ref: Revision as requested.
        sha: Full commit SHA the revision resolved to.
        plugin_root: Plugin directory inside the checkout.
        scan_version: Index generation the scanner writes.
    """

    ref: str
    sha: str
    plugin_root: Path
    scan_version: int


@dataclass(frozen=True)
class RepoResult:
    """Census result of one repository.

    Attributes:
        coordinate: The pinned repository.
        score: Counts and disagreement lists.
        parse_failures: Sources the truth builder could not parse.
        miss_classes: Class per false negative.
        extra_classes: Class per false positive.
    """

    coordinate: RepoCoordinate
    score: RepoScore
    parse_failures: tuple[str, ...]
    miss_classes: dict[tuple[str, str], MissClass]
    extra_classes: dict[tuple[str, str], ExtraClass]

    def class_counts(self) -> tuple[tuple[str, int], ...]:
        """Return class counts keyed ``fp:<class>`` and ``fn:<class>``."""
        extra = {f"fp:{key}": value for key, value in count_classes(self.extra_classes).items()}
        miss = {f"fn:{key}": value for key, value in count_classes(self.miss_classes).items()}
        return tuple(sorted({**extra, **miss}.items()))

    def to_dict(self) -> dict[str, object]:
        """Serialize for the JSON result, with repository-relative paths only."""
        return {
            "name": self.coordinate.name,
            "label": self.coordinate.label,
            "group": self.coordinate.group,
            "url": self.coordinate.url,
            "commit": self.coordinate.commit,
            **self.score.tally.to_dict(),
            "indexed": self.score.indexed,
            "not_indexed": self.score.tally.sources - self.score.indexed,
            "out_of_universe_scanner_edges": self.score.out_of_universe,
            "parse_failures": list(self.parse_failures),
            "class_counts": dict(self.class_counts()),
            "false_negatives": [
                {"source": s, "target": t, "class": self.miss_classes[(s, t)].value}
                for s, t in self.score.false_negatives
            ],
            "false_positives": [
                {"source": s, "target": t, "class": self.extra_classes[(s, t)].value}
                for s, t in self.score.false_positives
            ],
        }


@dataclass(frozen=True)
class CensusResult:
    """A complete census run.

    Attributes:
        scanner_ref: Revision as requested.
        scanner_sha: Full commit SHA of the scanner.
        scan_version: Index generation of every index in the run.
        generated_at: UTC timestamp, ISO 8601.
        repos: Per-repository results in coordinate order.
    """

    scanner_ref: str
    scanner_sha: str
    scan_version: int
    generated_at: str
    repos: tuple[RepoResult, ...]

    def rows(self) -> list[CensusRow]:
        """Return table rows: each group's repositories, then that group's pooled row."""
        groups: dict[str, list[RepoResult]] = {}
        for repo in self.repos:
            groups.setdefault(repo.coordinate.group, []).append(repo)
        rows: list[CensusRow] = []
        for group, members in groups.items():
            rows.extend(
                CensusRow(group, repo.coordinate.label, repo.score.tally, classes=repo.class_counts())
                for repo in members
            )
            pooled_classes: dict[str, int] = {}
            for repo in members:
                for key, value in repo.class_counts():
                    pooled_classes[key] = pooled_classes.get(key, 0) + value
            rows.append(
                CensusRow(
                    group,
                    "pooled",
                    pool(repo.score.tally for repo in members),
                    pooled=True,
                    headline=group == HEADLINE_GROUP,
                    classes=tuple(sorted(pooled_classes.items())),
                )
            )
        return rows

    def to_dict(self) -> dict[str, object]:
        """Serialize for the JSON result."""
        return {
            "scanner": {"ref": self.scanner_ref, "sha": self.scanner_sha, "scan_version": self.scan_version},
            "generated_at": self.generated_at,
            "pooled": {row.group: row.tally.to_dict() for row in self.rows() if row.pooled},
            "repos": [repo.to_dict() for repo in self.repos],
        }


def load_coordinates(path: Path) -> list[RepoCoordinate]:
    """Load and validate the pinned repository coordinates.

    Args:
        path: JSON file with a ``repositories`` list of coordinate objects.

    Returns:
        Coordinates in file order.

    Raises:
        CensusError: When a coordinate lacks a field, carries a short or malformed commit, or repeats a name.

    Examples:
        >>> import tempfile
        >>> entry = {"name": "o__r", "label": "r", "group": "tuned", "url": "https://example.invalid/r.git"}
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     path = Path(tmp) / "repos.json"
        ...     _ = path.write_text(json.dumps({"repositories": [{**entry, "commit": "a" * 40}]}))
        ...     load_coordinates(path)[0].label
        'r'
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    coordinates: list[RepoCoordinate] = []
    for item in raw.get("repositories", []):
        try:
            coordinate = RepoCoordinate(**{field: str(item[field]) for field in RepoCoordinate.__dataclass_fields__})
        except KeyError as missing:
            raise CensusError(
                f"{path}: a repository entry lacks {missing}; every entry needs name, label, group, url, commit"
            ) from None
        if not _FULL_SHA.fullmatch(coordinate.commit):
            raise CensusError(f"{path}: {coordinate.name} commit must be a full 40-character lowercase SHA")
        if any(existing.name == coordinate.name for existing in coordinates):
            raise CensusError(f"{path}: repository name {coordinate.name} appears twice")
        coordinates.append(coordinate)
    if not coordinates:
        raise CensusError(f"{path}: no repositories listed")
    return coordinates


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run git non-interactively and return its standard output; raise :class:`CensusError` on failure."""
    git = shutil.which("git")
    if git is None:
        raise CensusError("git is not on PATH; the census needs git to fetch repositories and check out the scanner")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    completed = subprocess.run(  # noqa: S603 - fixed git argv, no shell
        [git, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False
    )
    if completed.returncode != 0:
        raise CensusError(f"git {' '.join(args)} failed ({completed.returncode}): {completed.stderr.strip()}")
    return completed.stdout.strip()


def read_scan_version(plugin_root: Path) -> int:
    """Read the index generation a codemap-py checkout writes.

    Args:
        plugin_root: The ``plugins/codemap-py`` directory of a checkout.

    Returns:
        The ``SCAN_VERSION`` integer declared in its schema module.

    Raises:
        CensusError: When the schema module or its declaration is missing.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     schema = Path(tmp) / "src" / "codemap_py" / "schema.py"
        ...     schema.parent.mkdir(parents=True)
        ...     _ = schema.write_text("SCAN_VERSION: int = 15\\n")
        ...     read_scan_version(Path(tmp))
        15
    """
    schema = plugin_root / _SCHEMA_FILE
    try:
        match = _SCAN_VERSION.search(schema.read_text(encoding="utf-8"))
    except OSError as error:
        raise CensusError(f"cannot read {schema}: {error}") from None
    if match is None:
        raise CensusError(f"{schema} declares no SCAN_VERSION")
    return int(match.group(1))


@contextmanager
def scanner_checkout(repo_root: Path, ref: str, workdir: Path) -> Iterator[Scanner]:
    """Check the scanner out at *ref* into a detached worktree under *workdir*, removing it on exit.

    Args:
        repo_root: Root of this repository.
        ref: Any git revision (``HEAD``, a branch, a SHA).
        workdir: Throwaway directory that receives the worktree.

    Yields:
        The checked-out scanner.

    Raises:
        CensusError: When *ref* does not resolve or the checkout carries no codemap-py plugin.

    Examples:
        >>> callable(scanner_checkout)
        True
    """
    sha = _git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=repo_root)
    destination = workdir / f"scanner-{sha[:12]}"
    _git("worktree", "add", "--detach", str(destination), sha, cwd=repo_root)
    try:
        plugin_root = destination / _PLUGIN_DIR
        if not (plugin_root / _ENTRY_FILE).is_file():
            raise CensusError(f"revision {ref} ({sha}) has no {(_PLUGIN_DIR / _ENTRY_FILE).as_posix()}")
        yield Scanner(ref=ref, sha=sha, plugin_root=plugin_root, scan_version=read_scan_version(plugin_root))
    finally:
        _git("worktree", "remove", "--force", str(destination), cwd=repo_root)


def scanner_tree_digest(plugin_root: Path) -> str:
    """Return a SHA-256 over the scanner's Python sources, naming a scanner that has no commit.

    Every ``.py`` file under the plugin's ``src/`` and ``scripts/`` contributes its POSIX relative path and its bytes,
    in sorted path order; byte-code caches are skipped. Two directories holding the same sources get the same digest.

    Args:
        plugin_root: The ``plugins/codemap-py`` directory.

    Returns:
        64 lowercase hex digits.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     source = Path(tmp) / "src" / "m.py"
        ...     source.parent.mkdir()
        ...     _ = source.write_text("x = 1\\n")
        ...     first = scanner_tree_digest(Path(tmp))
        ...     _ = source.write_text("x = 2\\n")
        ...     (len(first), first == scanner_tree_digest(Path(tmp)))
        (64, False)
    """
    digest = hashlib.sha256()
    files = sorted(
        path
        for directory in _DIGEST_DIRS
        for path in (plugin_root / directory).rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )
    for path in files:
        digest.update(path.relative_to(plugin_root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


@contextmanager
def scanner_directory(source_root: Path) -> Iterator[Scanner]:
    """Use the scanner as it stands in a directory, uncommitted edits included, with no git checkout.

    The census then measures a working tree (or a copy of one) instead of a revision. ``Scanner.ref`` is
    :data:`WORKING_TREE_REF` and ``Scanner.sha`` is :func:`scanner_tree_digest` of the plugin, not a commit SHA.

    Args:
        source_root: Repository root (or a copy of one) holding ``plugins/codemap-py``.

    Yields:
        The scanner in place; nothing is created or removed.

    Raises:
        CensusError: When the directory carries no codemap-py entry point or schema.

    Examples:
        >>> callable(scanner_directory)
        True
    """
    plugin_root = source_root.resolve() / _PLUGIN_DIR
    if not (plugin_root / _ENTRY_FILE).is_file():
        raise CensusError(
            f"{source_root} has no {(_PLUGIN_DIR / _ENTRY_FILE).as_posix()}; pass the repository root holding the scanner"
        )
    yield Scanner(
        ref=WORKING_TREE_REF,
        sha=scanner_tree_digest(plugin_root),
        plugin_root=plugin_root,
        scan_version=read_scan_version(plugin_root),
    )


def fetch_repository(coordinate: RepoCoordinate, destination: Path) -> Path:
    """Fetch one pinned commit into an empty directory and check it out detached.

    Args:
        coordinate: The pinned repository.
        destination: Directory that must not exist yet or must be empty.

    Returns:
        *destination*, holding the checkout.

    Raises:
        CensusError: When *destination* is not empty, a git step fails, or the checkout is not the pinned commit.

    Examples:
        >>> callable(fetch_repository)
        True
    """
    if destination.exists() and any(destination.iterdir()):
        raise CensusError(f"{destination} is not empty; the census clones only into empty directories")
    destination.mkdir(parents=True, exist_ok=True)
    _git("init", "--quiet", str(destination))
    _git("fetch", "--quiet", "--depth", "1", coordinate.url, coordinate.commit, cwd=destination)
    _git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=destination)
    head = _git("rev-parse", "HEAD", cwd=destination)
    if head != coordinate.commit:
        raise CensusError(f"{coordinate.name}: checked out {head}, expected {coordinate.commit}")
    return destination


def _scanner_env() -> dict[str, str]:
    """Return the process environment without codemap-py overrides, so the index lands in its default place."""
    return {key: value for key, value in os.environ.items() if not key.startswith("CODEMAP_")}


def build_index(scanner: Scanner, clone: Path) -> Path:
    """Build a fresh codemap-py index of *clone* with *scanner* and check its generation.

    Args:
        scanner: The checked-out scanner.
        clone: Repository checkout with no existing index.

    Returns:
        Path of the index JSON.

    Raises:
        CensusError: When an index already exists, the build fails, it writes other than one index, or the index
            generation differs from the scanner's ``SCAN_VERSION``.

    Examples:
        >>> callable(build_index)
        True
    """
    index_dir = clone / _INDEX_DIR
    if index_dir.exists():
        raise CensusError(f"{index_dir} exists; the census builds every index from scratch")
    command = [sys.executable, str(scanner.plugin_root / _ENTRY_FILE), "index"]
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command, cwd=clone, env=_scanner_env(), capture_output=True, text=True, timeout=_INDEX_TIMEOUT_S, check=False
    )
    if completed.returncode != 0:
        raise CensusError(
            f"codemap-py index failed in {clone.name} ({completed.returncode}): {completed.stderr.strip()[-2000:]}"
        )
    indexes = sorted(index_dir.glob("*.json"))
    if len(indexes) != 1:
        raise CensusError(f"expected one index under {index_dir}, found {len(indexes)}")
    generation = json.loads(indexes[0].read_text(encoding="utf-8")).get("scan_version")
    if generation != scanner.scan_version:
        raise CensusError(
            f"{clone.name}: index generation {generation}, scanner {scanner.sha[:12]} writes {scanner.scan_version}"
        )
    return indexes[0]


def measure_repository(coordinate: RepoCoordinate, clone: Path, index_path: Path) -> RepoResult:
    """Score one indexed repository against its truth and class every disagreement.

    Args:
        coordinate: The pinned repository.
        clone: Its checkout.
        index_path: Its codemap-py index.

    Returns:
        The repository result.

    Examples:
        >>> callable(measure_repository)
        True
    """
    truth = build_truth(clone)
    scanner_edges, indexed = read_scanner_edges(index_path)
    first = score_repo(truth.sources, truth.edges, scanner_edges, indexed)
    extra = classify_false_positives(first.false_positives, truth.edges)
    parent = sum(1 for value in extra.values() if value == ExtraClass.PARENT_PACKAGE_INIT)
    score = score_repo(truth.sources, truth.edges, scanner_edges, indexed, parent_package=parent)
    miss = classify_false_negatives(score.false_negatives, truth.sources, truth.script_directory_only)
    return RepoResult(coordinate, score, truth.parse_failures, miss, extra)


#: Fetches one coordinate into a directory and returns the checkout.
Fetcher = Callable[[RepoCoordinate, Path], Path]
#: Builds the index of one checkout and returns the index path.
Indexer = Callable[[Scanner, Path], Path]


def run_census(
    coordinates: list[RepoCoordinate],
    scanner: Scanner,
    workdir: Path,
    fetch: Fetcher = fetch_repository,
    index: Indexer = build_index,
) -> CensusResult:
    """Fetch, index and score every coordinate.

    Args:
        coordinates: Pinned repositories in display order.
        scanner: The scanner revision every index is built with.
        workdir: Throwaway directory; each clone goes to ``<workdir>/repos/<name>``.
        fetch: Repository fetcher (injected in offline tests).
        index: Index builder (injected in offline tests).

    Returns:
        The census result.

    Examples:
        >>> callable(run_census)
        True
    """
    results = []
    for coordinate in coordinates:
        clone = fetch(coordinate, workdir / "repos" / coordinate.name)
        results.append(measure_repository(coordinate, clone, index(scanner, clone)))
    return CensusResult(
        scanner_ref=scanner.ref,
        scanner_sha=scanner.sha,
        scan_version=scanner.scan_version,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        repos=tuple(results),
    )


def render_markdown(result: CensusResult) -> str:
    """Render the census as a Markdown document: provenance line, main table, class table.

    Args:
        result: A complete census.

    Returns:
        Markdown text ending with a newline; only the provenance line carries the timestamp.

    Examples:
        >>> empty = CensusResult("HEAD", "a" * 40, 15, "2026-01-01T00:00:00Z", ())
        >>> render_markdown(empty).splitlines()[0]
        '# Import-edge census'
    """
    rows = result.rows()
    lines = [
        "# Import-edge census",
        "",
        f"Scanner `{result.scanner_ref}` at `{result.scanner_sha}`, index generation {result.scan_version}. "
        f"Generated {result.generated_at}.",
    ]
    if rows:
        lines += ["", census_table(rows), "", "Disagreement classes:", "", class_table(rows, _CLASS_COLUMNS)]
    return "\n".join(lines) + "\n"


def write_results(result: CensusResult, out_dir: Path) -> tuple[Path, Path]:
    """Write ``census.json`` and ``census.md`` into *out_dir*.

    Args:
        result: A complete census.
        out_dir: Output directory, created when missing.

    Returns:
        ``(json path, markdown path)``.

    Examples:
        >>> callable(write_results)
        True
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = out_dir / "census.json", out_dir / "census.md"
    json_path.write_text(json.dumps(result.to_dict(), indent=1) + "\n", encoding="utf-8", newline="\n")
    markdown_path.write_text(render_markdown(result), encoding="utf-8", newline="\n")
    return json_path, markdown_path


def remove_tree(path: Path) -> None:
    """Delete a work directory, clearing read-only bits git sets on object files (needed on Windows).

    Args:
        path: Directory to delete; a missing directory is a no-op.

    Examples:
        >>> remove_tree(Path("/nonexistent-census-dir"))
    """
    if not path.exists():
        return
    for entry in path.rglob("*"):
        if not entry.is_symlink():
            entry.chmod(entry.stat().st_mode | stat.S_IWRITE)
    shutil.rmtree(path)
