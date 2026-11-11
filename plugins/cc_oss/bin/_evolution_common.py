"""Shared contract for the ``/oss:analyse evolution`` scripts: records, identities, versions, store I/O, git runner.

Every ``evolution_*.py`` script imports this private module, so the record shapes, stable identifiers, version
ordering and store write protocol are defined exactly once. It has no command line of its own (underscore modules are
excluded from the ``bin/`` help contract and the orphan check).

Contents:

* **Versions** - :func:`parse_version` accepts PEP 440 and SemVer tags (optional ``v`` prefix) and returns a
  :class:`Version` with a total sort key; pre-releases are detected from the parsed version, never by substring.
* **Identities** - :func:`entry_ids` builds ``<release>/<entry-hash>`` identifiers with ``~2``, ``~3`` occurrence
  suffixes; :func:`commit_id`, :func:`pr_id`, :func:`patch_set_id` cover heuristic rows and columns; pseudo-column
  identifiers come from :func:`pre_window_id`, :func:`non_feature_id` and :data:`RESIDUE_COLUMNS`.
* **Records** - frozen field lists for every store file (:data:`RECORD_FIELDS`) and :func:`check_record`.
* **Hashing** - :func:`canonical_json`, :func:`fingerprint`, :func:`digest_lines`.
* **Store I/O** - :func:`read_jsonl` (drops a torn last line), :func:`last_by_key`, :func:`repair_tail`,
  :func:`append_jsonl`, :func:`atomic_write_json`, :class:`StorePaths` and :func:`resolve_store_root` (path
  containment: safe components only, no symlinked component, result inside ``.evolution/``).
* **Remotes** - :func:`parse_remote_url` splits a git remote URL into store path components.
* **Git** - :func:`run_git` runs one git command with a timeout and raises :class:`GitError` / :class:`GitTimeout`.

Schema: ``SCHEMA_VERSION`` is the single integer version of every store file. Algorithm versions in
:data:`ALGO_VERSIONS` feed the stage fingerprints, so changing an algorithm invalidates exactly the stages it
produces.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any, Final

#: Single schema version for every file in an evolution store.
SCHEMA_VERSION: Final = 1

#: Per-stage algorithm versions; each is folded into that stage's fingerprint.
ALGO_VERSIONS: Final = {
    "index": 1,
    "entries": 1,
    "evidence": 1,
    "column_map": 1,
    "scores": 1,
    "adjudication_prompt": 2,
}

#: Residue pseudo-columns, one per bucket; a row with no other evidence puts its mass on exactly one of them.
RESIDUE_COLUMNS: Final = ("unlinked", "unattributable", "timed_out", "unadjudicated", "unknown")

#: Column kinds.
KIND_FEATURE: Final = "feature"
KIND_CHANGE: Final = "change"
KIND_PRE_WINDOW: Final = "pre-window"
KIND_NON_FEATURE: Final = "non-feature"
KIND_RESIDUE: Final = "residue"
REAL_KINDS: Final = frozenset({KIND_FEATURE, KIND_CHANGE})

#: Cell methods, in ascending precedence for conflicts (manual wins).
METHODS: Final = ("file", "heuristic", "blame-context", "text", "symbol", "blame", "inherited", "llm", "manual")

#: Entry sources.
SOURCE_CHANGELOG: Final = "changelog"
SOURCE_HEURISTIC: Final = "heuristic"

#: Default bug labels for the open-bug series (compared case-insensitively).
DEFAULT_BUG_LABELS: Final = ("bug", "type: bug", "kind/bug", "type/bug")

#: Default per-git-call timeout in seconds (the release helpers' convention).
GIT_TIMEOUT_S: Final = 60

MAX_INPUT_BYTES: Final = 10 * 1024 * 1024
MAX_ENTRY_LINE_CHARS: Final = 10000

#: Required keys per record kind. Extra keys are allowed; missing keys fail :func:`check_record`.
RECORD_FIELDS: Final = {
    # One entry of index.json["releases"].
    "release": ("tag", "version", "sha", "date", "line", "is_prerelease", "order"),
    # One changelog/heuristic item before scoring; kind is "fix", "feature" or "change".
    "entry": (
        "entry_id",
        "kind",
        "release",
        "text",
        "source",
        "refs",
        "commits",
        "link_method",
        "link_confidence",
    ),
    # One line of columns.jsonl.
    "column": (
        "schema_version",
        "column_id",
        "kind",
        "release",
        "line_introductions",
        "title",
        "footprint",
        "aliases",
        "fingerprint",
    ),
    # One line of evidence.jsonl (one record per fix).
    "evidence": ("schema_version", "fix_id", "status", "blamed", "context_blamed", "files", "symbols", "fingerprint"),
    # One cell inside a row.
    "cell": ("column_id", "release", "raw_score", "soft_share", "binary", "method", "confidence", "cross_line"),
    # One line of rows.jsonl.
    "row": (
        "schema_version",
        "fix_id",
        "fix_release",
        "line",
        "source",
        "secondary_releases",
        "cells",
        "evidence_count",
        "evidence_digest",
        "fingerprints",
    ),
    # One line of adjudications.jsonl.
    "adjudication": ("schema_version", "key", "fix_id", "model", "effort", "verdict"),
    # One parsed line of overrides.csv.
    "override": ("fix_id", "column_id", "action", "note"),
}


class EvolutionError(Exception):
    """Base class for evolution contract failures."""


class StorePathError(EvolutionError):
    """A store path failed the containment rules."""


class GitError(EvolutionError):
    """A git command exited non-zero or could not start."""


class GitTimeout(GitError):
    """A git command exceeded its timeout."""


# --------------------------------------------------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------------------------------------------------

_PEP440 = re.compile(
    r"^(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre_l>a|alpha|b|beta|c|rc|pre|preview)[-_.]?(?P<pre_n>\d*))?"
    r"(?:-(?P<post_n1>\d+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>\d*))?"
    r"(?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>\d*))?"
    r"(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?$",
    re.IGNORECASE,
)
_SEMVER = re.compile(
    r"^(?P<release>\d+\.\d+\.\d+)(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$",
)
_PRE_CANON = {"a": "a", "alpha": "a", "b": "b", "beta": "b", "c": "rc", "rc": "rc", "pre": "rc", "preview": "rc"}
_PRE_ORDER = {"a": 0, "b": 1, "rc": 2}


@dataclass(frozen=True)
class Version:
    """A parsed release version.

    Attributes:
        raw: The tag text as given.
        canonical: Normalised version text without a ``v`` prefix, used as the release identity.
        epoch: PEP 440 epoch (0 when absent).
        release: Numeric release components.
        pre: ``(kind, number)`` with kind ``a``, ``b`` or ``rc``, or None.
        post: Post-release number, or None.
        dev: Dev-release number, or None.
    """

    raw: str
    canonical: str
    epoch: int
    release: tuple[int, ...]
    pre: tuple[str, int] | None
    post: int | None
    dev: int | None

    @property
    def is_prerelease(self) -> bool:
        """Return True for alpha, beta, release-candidate and dev versions."""
        return self.pre is not None or self.dev is not None

    def sort_key(self) -> tuple[Any, ...]:
        """Return a total ordering key following PEP 440 comparison rules."""
        release = list(self.release)
        while len(release) > 1 and release[-1] == 0:
            release.pop()
        if self.pre is None and self.post is None and self.dev is not None:
            pre_key: tuple[int, int] = (-1, 0)  # 1.0.dev1 sorts before 1.0a1
        elif self.pre is None:
            pre_key = (3, 0)
        else:
            pre_key = (_PRE_ORDER[self.pre[0]], self.pre[1])
        post_key = (-1,) if self.post is None else (self.post,)
        dev_key = (float("inf"),) if self.dev is None else (self.dev,)
        return (self.epoch, tuple(release), pre_key, post_key, dev_key)


def _strip_prefix(tag: str) -> str:
    """Drop a single leading ``v``/``V`` from a tag name."""
    text = tag.strip()
    return text[1:] if text[:1] in ("v", "V") and text[1:2].isdigit() else text


def _num(text: str | None) -> int:
    """Convert an optional digit string to an int, treating empty as 0."""
    return int(text) if text else 0


def _parse_semver(text: str, raw: str) -> Version | None:
    """Parse a SemVer string with a pre-release part such as ``1.2.0-rc.1``."""
    match = _SEMVER.match(text)
    if match is None or match.group("pre") is None:
        return None
    head = re.match(r"^(?P<label>[A-Za-z]+)[.-]?(?P<num>\d*)", match.group("pre"))
    if head is None or head.group("label").lower() not in _PRE_CANON:
        return None
    kind = _PRE_CANON[head.group("label").lower()]
    release = tuple(int(part) for part in match.group("release").split("."))
    number = _num(head.group("num"))
    canonical = f"{match.group('release')}{kind}{number}"
    return Version(raw, canonical, 0, release, (kind, number), None, None)


def parse_version(tag: str) -> Version | None:
    """Parse a tag as a PEP 440 or SemVer version; return None when it is neither.

    Examples:
        >>> parse_version("v1.2.0").canonical
        '1.2.0'
        >>> parse_version("1.2.0rc1").is_prerelease, parse_version("1.2.0-rc.1").canonical
        (True, '1.2.0rc1')
        >>> parse_version("devtools-1.0") is None
        True
    """
    text = _strip_prefix(tag)
    match = _PEP440.match(text)
    if match is None:
        return _parse_semver(text, tag)
    pre = None
    if match.group("pre_l"):
        pre = (_PRE_CANON[match.group("pre_l").lower()], _num(match.group("pre_n")))
    has_post = match.group("post_n1") is not None or match.group("post_l") is not None
    post = _num(match.group("post_n1") or match.group("post_n2")) if has_post else None
    dev = _num(match.group("dev_n")) if match.group("dev_l") is not None else None
    epoch = int(match.group("epoch") or 0)
    release = tuple(int(part) for part in match.group("release").split("."))
    canonical = _canonical_text(epoch, release, pre, post, dev)
    return Version(tag, canonical, epoch, release, pre, post, dev)


def _canonical_text(
    epoch: int, release: tuple[int, ...], pre: tuple[str, int] | None, post: int | None, dev: int | None
) -> str:
    """Render the PEP 440 normal form of a parsed version."""
    text = f"{epoch}!" if epoch else ""
    text += ".".join(str(part) for part in release)
    if pre is not None:
        text += f"{pre[0]}{pre[1]}"
    if post is not None:
        text += f".post{post}"
    if dev is not None:
        text += f".dev{dev}"
    return text


# --------------------------------------------------------------------------------------------------------------------
# Hashing and identities
# --------------------------------------------------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Serialise ``obj`` deterministically: sorted keys, compact separators, UTF-8 kept."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    """Return the hex SHA-256 of UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(*parts: Any) -> str:
    """Return a stable fingerprint of any JSON-serialisable inputs."""
    return sha256_text(canonical_json(list(parts)))


def digest_lines(lines: list[str]) -> str:
    """Return the digest of exact JSONL lines (without their newlines), used to bind a row to its evidence."""
    return sha256_text("\n".join(lines))


_REF_PATTERNS = (
    re.compile(r"\[#\d+\]\([^\n)]*\)"),  # [#12](url)
    re.compile(r"\(\s*#\d+(?:\s*,\s*#\d+)*\s*\)"),  # (#12) or (#12, #13)
    re.compile(r"\(\s*[0-9a-f]{7,40}\s*\)"),  # (abc1234)
    re.compile(r"\[[0-9a-f]{7,40}\]\([^\n)]*\)"),  # [abc1234](url)
)


def normalise_entry_text(text: str) -> str:
    """Normalise changelog entry text for hashing: drop PR/SHA references and markup, fold case and whitespace.

    Adding or removing a ``(#12)`` reference therefore keeps the entry's identity.

    Examples:
        >>> normalise_entry_text("Fix **crash** in `load()` (#12)")
        'fix crash in load()'
    """
    out = unicodedata.normalize("NFKC", text)
    out = "\n".join(line[:MAX_ENTRY_LINE_CHARS] for line in out.split("\n"))
    for pattern in _REF_PATTERNS:
        out = pattern.sub(" ", out)
    out = re.sub(r"\[([^\[\]\n]*)\]\([^\n)]*\)", r"\1", out)  # markdown link -> its text
    out = re.sub(r"[*_`]+", "", out)
    return " ".join(out.lower().split())


def entry_hash(text: str) -> str:
    """Return the 12-hex-character hash of normalised entry text."""
    return sha256_text(normalise_entry_text(text))[:12]


def entry_ids(release: str, texts: list[str]) -> list[str]:
    """Return one ``<release>/<entry-hash>`` ID per entry, suffixing repeats in file order with ``~2``, ``~3``.

    Examples:
        >>> ids = entry_ids("1.0.0", ["Fix a", "Fix b", "fix A"])
        >>> ids[0] != ids[1], ids[2].endswith("~2")
        (True, True)
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for text in texts:
        base = f"{release}/{entry_hash(text)}"
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}~{seen[base]}")
    return out


def commit_id(patch_id: str) -> str:
    """Return the heuristic identity of a single-commit row or column."""
    return f"commit/{patch_id}"


def pr_id(number: int) -> str:
    """Return the heuristic identity of a PR-linked column."""
    return f"pr/{number}"


def patch_set_id(patch_ids: list[str]) -> str:
    """Return the identity of a commit-only column from its patch-id set (order-independent)."""
    return f"commit/{sha256_text(','.join(sorted(set(patch_ids))))[:16]}"


def pre_window_id(release: str | None) -> str:
    """Return the ``pre-window`` pseudo-column for a release, or ``pre-window@untagged`` when None."""
    return f"pre-window@{release or 'untagged'}"


def non_feature_id(release: str | None) -> str:
    """Return the ``non-feature`` pseudo-column for a release (``untagged`` when None)."""
    return f"non-feature@{release or 'untagged'}"


def column_kind(column_id: str) -> str:
    """Classify a column identity into one of the kinds (real columns report ``feature``-or-``change`` as ``real``)."""
    if column_id in RESIDUE_COLUMNS:
        return KIND_RESIDUE
    if column_id.startswith("pre-window@"):
        return KIND_PRE_WINDOW
    if column_id.startswith("non-feature@"):
        return KIND_NON_FEATURE
    return "real"


def check_record(kind: str, record: dict[str, Any]) -> list[str]:
    """Return the required keys missing from ``record`` for the given record kind (empty when valid)."""
    return [key for key in RECORD_FIELDS[kind] if key not in record]


# --------------------------------------------------------------------------------------------------------------------
# Store I/O
# --------------------------------------------------------------------------------------------------------------------


def read_bounded_bytes(path: Path) -> bytes:
    """Read at most ten MiB, rejecting larger files before allocation and after a bounded read."""
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"{path}: exceeds size limit of {MAX_INPUT_BYTES} bytes")
    with path.open("rb") as handle:
        data = handle.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError(f"{path}: exceeds size limit of {MAX_INPUT_BYTES} bytes")
    return data


def read_bounded_text(path: Path, *, errors: str = "strict") -> str:
    """Read UTF-8 text under the shared ten-MiB file-size ceiling."""
    text = read_bounded_bytes(path).decode("utf-8", errors=errors)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def repair_tail(path: Path) -> int:
    """Truncate a torn last line (bytes after the final newline); return the number of bytes removed."""
    if not path.exists():
        return 0
    data = read_bounded_bytes(path)
    if not data or data.endswith(b"\n"):
        return 0
    keep = data.rfind(b"\n") + 1
    with path.open("r+b") as handle:
        handle.truncate(keep)
    return len(data) - keep


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read complete JSONL records; return ``(records, dropped)``.

    A last line without a trailing newline is torn and dropped, as is any line that is not a JSON object. The file is
    never modified here; :func:`repair_tail` does that before an append.
    """
    if not path.exists():
        return [], 0
    data = read_bounded_bytes(path).decode("utf-8").replace("\r\n", "\n")
    lines = data.split("\n")
    dropped = 0
    if lines and lines[-1] != "":
        dropped += 1  # torn tail
    records: list[dict[str, Any]] = []
    for line in lines[:-1]:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            dropped += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            dropped += 1
    return records, dropped


def last_by_key(records: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """Index records by ``key``; a later record with the same key replaces an earlier one."""
    out: dict[str, dict[str, Any]] = {}
    for record in records:
        if key in record:
            out[str(record[key])] = record
    return out


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> list[str]:
    """Repair a torn tail, then append records as canonical JSON lines; return the exact lines written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    repair_tail(path)
    lines = [canonical_json(record) for record in records]
    if lines:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(f"{line}\n" for line in lines))
            handle.flush()
            os.fsync(handle.fileno())
    return lines


def atomic_write_json(path: Path, obj: Any) -> None:
    """Write JSON through a temp file in the same directory and an atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_component(part: str) -> bool:
    """Return True when ``part`` is a single safe path component (not ``.``/``..``, no separators)."""
    return bool(_SAFE_COMPONENT.fullmatch(part)) and part not in (".", "..")


@dataclass(frozen=True)
class StorePaths:
    """File layout of one repository's evolution store.

    Root-level files survive ``--full``; everything under ``store/`` is regenerable.
    """

    root: Path

    @property
    def lock(self) -> Path:
        """Store lock covering every mutation, including fetching the dedicated clone."""
        return self.root / ".lock"

    @property
    def repo(self) -> Path:
        """Dedicated full clone (used only when the target is not the current checkout)."""
        return self.root / "repo"

    @property
    def overrides(self) -> Path:
        """User-authored corrections."""
        return self.root / "overrides.csv"

    @property
    def adjudications(self) -> Path:
        """Stored LLM verdicts, kept across ``--full``."""
        return self.root / "adjudications.jsonl"

    @property
    def store(self) -> Path:
        """Regenerable store directory deleted by ``--full``."""
        return self.root / "store"

    @property
    def manifest(self) -> Path:
        """Store manifest, written last by atomic rename."""
        return self.store / "manifest.json"

    @property
    def index(self) -> Path:
        """Release index, entries, alias table and SHA-to-column map."""
        return self.store / "index.json"

    @property
    def evidence(self) -> Path:
        """Per-fix blame/overlap evidence (one record per fix)."""
        return self.store / "evidence.jsonl"

    @property
    def rows(self) -> Path:
        """Scored fix rows (one record per fix; a row marks its fix finished)."""
        return self.store / "rows.jsonl"

    @property
    def columns(self) -> Path:
        """Column records."""
        return self.store / "columns.jsonl"

    @property
    def matrix_csv(self) -> Path:
        """Binary view exported for review."""
        return self.store / "matrix.csv"


def resolve_store_root(evolution_dir: Path, parts: list[str]) -> StorePaths:
    """Validate store path components and return the store layout under ``evolution_dir``.

    Raises:
        StorePathError: a component is unsafe, ``evolution_dir`` or any existing component below it is a symlink, or
            the resolved root escapes ``evolution_dir``.
    """
    if not parts or not all(safe_component(part) for part in parts):
        raise StorePathError(f"unsafe store path components: {parts!r}")
    current = evolution_dir
    if current.is_symlink():
        raise StorePathError(f"symlinked store component: {current}")
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise StorePathError(f"symlinked store component: {current}")
    base = evolution_dir.resolve()
    root = (evolution_dir / Path(*parts)).resolve()
    try:
        root.relative_to(base)
    except ValueError as exc:
        raise StorePathError(f"store root {root} escapes {base}") from exc
    return StorePaths(root=evolution_dir / Path(*parts))


_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")


def parse_remote_url(url: str) -> list[str] | None:
    """Split a git remote URL into store path components ``[host, *path]``; return None when unparsable.

    Examples:
        >>> parse_remote_url("https://github.com/Owner/Repo.git")
        ['github.com', 'Owner', 'Repo']
        >>> parse_remote_url("git@gitlab.com:group/sub/proj.git")
        ['gitlab.com', 'group', 'sub', 'proj']
    """
    text = url.strip()
    match = re.fullmatch(r"[a-z][a-z0-9+.-]*://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)", text, re.I)
    if match is None and "://" not in text:
        match = _SCP_REMOTE.fullmatch(text)
    if match is None:
        return None
    path = match.group("path").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [match.group("host").lower(), *[part for part in path.split("/") if part]]
    if len(parts) < 3 or not all(safe_component(part) for part in parts):
        return None
    return parts


# --------------------------------------------------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------------------------------------------------


def run_git(
    repo: Path, *args: str, timeout: float = GIT_TIMEOUT_S, check: bool = True, stdin: str | None = None
) -> str:
    """Run ``git -C <repo> <args>`` and return stdout.

    Raises:
        GitTimeout: the command ran longer than ``timeout`` seconds.
        GitError: git is missing, could not start, or (with ``check``) exited non-zero.
    """
    git = which("git")
    if git is None:
        raise GitError("git executable not found on PATH")
    protocols = ["https", "ssh", "git"]
    if "GIT_ALLOW_PROTOCOL" in os.environ:
        protocols = [protocol for protocol in protocols if protocol in os.environ["GIT_ALLOW_PROTOCOL"].split(":")]
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            [git, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            input=stdin,
            env={**os.environ, "GIT_ALLOW_PROTOCOL": ":".join(protocols)},
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeout(f"git {' '.join(args)} timed out after {timeout}s") from exc
    except OSError as exc:
        raise GitError(f"git {' '.join(args)} could not start: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    return proc.stdout


def first_parent(repo: Path, sha: str, timeout: float = GIT_TIMEOUT_S) -> str | None:
    """Return the first parent of ``sha`` (the diff base for merges and ordinary commits), or None for a root commit."""
    out = run_git(repo, "rev-list", "--parents", "-n", "1", sha, timeout=timeout).split()
    return out[1] if len(out) > 1 else None


# --------------------------------------------------------------------------------------------------------------------
# Python symbols (stdlib ast; no codemap dependency)
# --------------------------------------------------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -(?P<os>\d+)(?:,(?P<oc>\d+))? \+(?P<ns>\d+)(?:,(?P<nc>\d+))? @@")


def _symbol_spans(source: str) -> list[tuple[str, int, int]]:
    """Return ``(qualified name, first line, last line)`` for every function and class in Python source."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    spans: list[tuple[str, int, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        """Collect definitions below ``node`` with dotted names."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                spans.append((name, child.lineno, getattr(child, "end_lineno", child.lineno) or child.lineno))
                visit(child, f"{name}.")

    visit(tree, "")
    return spans


def _changed_ranges(diff_text: str) -> dict[str, tuple[list[tuple[int, int]], list[tuple[int, int]]]]:
    """Map each changed new-side path to its (old ranges, new ranges) from a ``-U0`` diff."""
    out: dict[str, tuple[list[tuple[int, int]], list[tuple[int, int]]]] = {}
    old_path = new_path = None
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            old_path = None if line[4:] == "/dev/null" else line[6:]
        elif line.startswith("+++ "):
            new_path = None if line[4:] == "/dev/null" else line[6:]
            out.setdefault(new_path or old_path or "", ([], []))
        elif (match := _HUNK.match(line)) is not None:
            key = new_path or old_path or ""
            ostart, ocount = int(match.group("os")), int(match.group("oc") or 1)
            nstart, ncount = int(match.group("ns")), int(match.group("nc") or 1)
            if ocount:
                out[key][0].append((ostart, ostart + ocount - 1))
            if ncount:
                out[key][1].append((nstart, nstart + ncount - 1))
    out.pop("", None)
    return out


def _hit(spans: list[tuple[str, int, int]], ranges: list[tuple[int, int]]) -> set[str]:
    """Return names whose span overlaps any range."""
    return {name for name, lo, hi in spans for a, b in ranges if a <= hi and b >= lo}


def python_symbols_changed(
    repo: Path, sha: str, *, added_only: bool = False, timeout: float = GIT_TIMEOUT_S
) -> list[str]:
    """Return ``path:qualified.name`` for Python functions/classes a commit adds or modifies (first-parent diff).

    Args:
        repo: Repository root.
        sha: Commit to inspect; merges are compared against their first parent.
        added_only: Return only symbols absent before the commit (feature columns) instead of added or modified.
        timeout: Per-git-call timeout in seconds.
    """
    base = first_parent(repo, sha, timeout=timeout)
    if base is None:
        return []
    # Pin output shape: user config (diff.noprefix, diff.mnemonicPrefix, external drivers) would break the a/ b/ parse.
    diff = run_git(
        repo,
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "-M",
        base,
        sha,
        "--",
        "*.py",
        timeout=timeout,
    )
    found: set[str] = set()
    for path, (old_ranges, new_ranges) in _changed_ranges(diff).items():
        new_src = run_git(repo, "show", f"{sha}:{path}", timeout=timeout, check=False)
        old_src = run_git(repo, "show", f"{base}:{path}", timeout=timeout, check=False)
        new_spans, old_spans = _symbol_spans(new_src), _symbol_spans(old_src)
        old_names = {name for name, _, _ in old_spans}
        names = _hit(new_spans, new_ranges) | (set() if added_only else _hit(old_spans, old_ranges) & old_names)
        if added_only:
            names = {name for name in names if name not in old_names}
        found |= {f"{path}:{name}" for name in names}
    return sorted(found)
