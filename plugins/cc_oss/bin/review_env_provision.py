#!/usr/bin/env python
"""Provision an ephemeral Python environment for reviewing one pull request, outside the reviewed repository.

## Purpose

    A review must not stop at "package not installed". This helper builds (or reuses) a private virtual environment
    for one repository and PR head, installs the dependencies the project declares, and reports every requirement it
    did not install with a cause from a closed set. It never blocks: every failure becomes data in its JSON document,
    and the review continues with whatever the environment holds.

## Trust model

    The project is never installed and the PR worktree is never read: manifests are read from git objects at the base
    and head commits (``git show``), the installer runs from the environment directory with ``--no-config``, and the
    project reaches a run only through ``PYTHONPATH`` inside the contributor-code gate. Only planned wheels are
    installed, with transitive dependency resolution disabled. A
    requirement is installed only when it is *trusted*: every declaration of its name at the base ref is a plain
    official-index spec, its head specifier is a plain version range, and project and host settings establish routing
    to the official index (https://pypi.org/simple). Everything else goes into ``skipped_gated`` with ``cause: gated``
    and a ``gate_reason``:

    - ``added-by-pr`` -- the name is new at the head, or the contributor-code gate reports it as new;
    - ``direct-reference`` -- a URL, VCS or path reference;
    - ``lookalike`` -- a new name one or two edits from a known one, as the gate reports it;
    - ``unsafe-spec`` -- a specifier, extras or marker outside the plain grammar this helper passes to an installer;
    - ``index-override`` -- the base, the head or a lock file routes the name to another index or source;
    - ``index-routing-undetermined`` -- installer routing could not be established from a manifest, project scan or
      the selected interpreter;
    - ``user-index-setting`` / ``uv-environment-override`` -- the user's own installer configuration routes names
      elsewhere; it is never stripped or replaced to reach the official index.

    Fetching such a name from the official index could install a same-name package the project never meant to use.
    An index setting found in a file this helper does not parse (such as a CI workflow or notebook) is recorded in
    ``notes`` and gates every name because the scan cannot establish which dependencies the setting affects.
    The gated names stay with the existing contributor-code gate (``untrusted_code_guard.py``), which this helper
    never weakens: it reads the gate's dependency verdict and gates every name the gate flags.

## Selection

    Runtime dependencies, every optional-dependency extra, dependency groups whose name names tests or development
    (``dev``, ``ci`` or containing ``test``), ``[tool.uv] dev-dependencies``, root and ``requirements/`` requirement
    files except documentation ones, plus optional packages the head's tests import when a manifest declares them
    (``cv2`` from an OpenCV wheel, ``pycocotools``, ``matplotlib``, ``pandas``, ``sklearn`` and others, then
    ``pytest`` and declared ``pytest-*`` plugins). Only one OpenCV wheel is installed, headless first, because every
    variant provides the same ``cv2`` module. A self-reference (``pkg[extra]`` inside ``pkg``) expands to that extra's
    requirements. Lock files are read for routing only; versions resolve from the declared ranges.

## Usage

    python review_env_provision.py --repo DIR --base REF --head REF [--out FILE] [--python PY]
        [--group NAME ...] [--all-groups] [--no-extras] [--exclude NAME ...] [--probe-import MODULE ...]
        [--plan-only] [--timeout SEC] [--lock-wait SEC] [--budget SEC]

## Outputs

    One JSON document on stdout (and in ``--out``): ``status`` (``ready``, ``partial`` or ``unavailable``),
    ``env_python``, ``installed``, ``skipped_gated``, ``failed`` (each with a ``cause``), ``skipped``,
    ``import_probes``, ``host`` (OS, architecture, CPU, cores, RAM, accelerators as observed facts) and
    ``gate_environment``: the ``VIRTUAL_ENV``, the ``PATH_PREPEND`` entries (the environment's script directory and
    the base interpreter prefix) and the ``PYTHONPATH`` (``src`` and ``.`` relative to the run's copy of the tree)
    that launch a PR-code run, each already in this host's path form and separator.

    The environment lives under ``${TMPDIR:-/tmp}/oss-review-env-<uid>/<repo>-<key>/<head>/`` (no ``-<uid>`` suffix
    on native Windows, whose temp directory is per user), outside the reviewed tree, and is reused when the same head
    is provisioned again with the same plan and verified interpreter. The root and each repository directory are
    created owner-only (``0700``) and checked before anything under them is probed, reused, locked or pruned. Only
    the invoking user can enter them. Each environment directory must belong to that user and be a real directory.
    Otherwise nothing runs and ``failed`` carries ``env-create-failed``: another user who could
    write there could plant an interpreter or a cache entry that would run as the reviewer. The installer caches live
    inside that root. Each environment carries a marker file (``.oss-review-env.json``) whose mtime is its last use.
    Every provisioning run (not ``--plan-only``) first deletes environments under that root unused for
    ``PRUNE_AGE_DAYS`` (7) days: only marker-verified directories, never the current run's, never one whose lock is
    held, never through a symbolic link. ``pruned`` reports the count and bytes freed. The lock is an operating-system
    advisory lock on ``<head>.lock``, released when its process ends, so a run killed mid-install never blocks the
    next one and two runs never hold it at once.

## Failure

    Install failures are causes, not exit codes: ``network``, ``resolver-conflict``, ``build-failure``,
    ``unsupported-platform``, ``timeout``, ``no-installer`` (the installer or its ``pip`` module cannot start),
    ``env-create-failed``, ``manifest-unparsed``, ``unknown``, each with the installer's last output lines.
    ``--budget`` bounds the whole run: every installer call fits inside it, and names still pending when it runs out
    carry ``timeout``, so a caller's own time cap never kills the run before the document is written. A file-system
    error becomes ``env-create-failed``; any other unexpected error still prints a document (status
    ``unavailable``, cause ``unknown``).

## Used by

    oss:review ``modes/provision-env.md`` (Step 2 reviewer environment) and ``review_claim_repro.py``.

Exit codes:
    0 -- a JSON document was printed, whatever was installed
    2 -- bad arguments: the repository or a ref cannot be resolved (a JSON document still names the cause)
"""

from __future__ import annotations

import argparse
import configparser
import errno
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import IO

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import untrusted_code_guard as guard

try:
    from packaging.requirements import InvalidRequirement
    from packaging.requirements import Requirement as Pep508Requirement
except ImportError:
    try:
        from pip._vendor.packaging.requirements import InvalidRequirement
        from pip._vendor.packaging.requirements import Requirement as Pep508Requirement
    except ImportError:
        Pep508Requirement = None

SCHEMA = 1
KIND = "review-env-provision"
#: The only index this helper ever fetches from; every other route is gated.
DEFAULT_INDEX = "https://pypi.org/simple"
#: Directory under the temp root holding every reviewer environment and the shared installer caches.
ENV_ROOT_NAME = "oss-review-env"
#: Seconds one installer call may take before its requirements are reported with cause ``timeout``.
DEFAULT_TIMEOUT = 1800
#: Seconds for one whole provisioning run; names still pending when it runs out are reported with cause ``timeout``.
DEFAULT_BUDGET = 3600
#: Seconds to wait for another process provisioning the same environment.
DEFAULT_LOCK_WAIT = 900
#: Lines of installer output kept as the evidence for one failure.
ERROR_TAIL_LINES = 15
ERROR_TAIL_CHARS = 2000
#: Largest manifest or test file read from git; larger ones are skipped (a manifest is then ``manifest-unparsed``).
MAX_BLOB_BYTES = 1024 * 1024
#: Test files read for imports; a larger suite is sampled in path order.
MAX_TEST_FILES = 3000
#: Includes followed from one requirements file before the rest is reported as unparsed.
MAX_INCLUDE_DEPTH = 8
_GIT = shutil.which("git") or "git"
_GIT_TIMEOUT = 60
_PROBE_TIMEOUT = 60


class Cause(str, Enum):
    """Why a requirement is not in the environment."""

    NETWORK = "network"
    RESOLVER_CONFLICT = "resolver-conflict"
    BUILD_FAILURE = "build-failure"
    DIRECT_REFERENCE = "direct-reference"
    GATED = "gated"
    UNSUPPORTED_PLATFORM = "unsupported-platform"
    TIMEOUT = "timeout"
    NO_INSTALLER = "no-installer"
    ENV_CREATE_FAILED = "env-create-failed"
    MANIFEST_UNPARSED = "manifest-unparsed"
    UNKNOWN = "unknown"


class GateReason(str, Enum):
    """Why a requirement stays with the contributor-code gate instead of being installed."""

    ADDED_BY_PR = "added-by-pr"
    DIRECT_REFERENCE = "direct-reference"
    LOOKALIKE = "lookalike"
    UNSAFE_SPEC = "unsafe-spec"
    INDEX_OVERRIDE = "index-override"
    ROUTING_UNDETERMINED = "index-routing-undetermined"
    USER_INDEX_SETTING = "user-index-setting"
    UV_ENV_OVERRIDE = "uv-environment-override"


class SkipReason(str, Enum):
    """Why a selected or test-imported requirement is deliberately not installed, without being gated."""

    PROJECT_ITSELF = "project-itself"
    MARKER_EXCLUDES_HOST = "marker-excludes-host"
    CONFLICTING_PROVIDER = "conflicting-provider"
    UNDECLARED_IMPORT = "undeclared-import"
    EXCLUDED_BY_CALLER = "excluded-by-caller"


class ProvisionError(Exception):
    """A caller error (unresolvable repository or ref) that ends the run with exit status 2."""


# --------------------------------------------------------------------------------------------------------------------
# requirement grammar
# --------------------------------------------------------------------------------------------------------------------

_REQ = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[(?P<extras>[^\]]*)\])?\s*(?P<rest>.*?)\s*$",
    re.DOTALL,
)
#: A PEP 440 version range: letters, digits, spaces and the operator characters only (no quote, ``@``, ``/``, ``:``).
_SAFE_SPEC = re.compile(r"(?:[<>=!~][A-Za-z0-9 .,*+!_=<>~-]*)?")
_SAFE_EXTRAS = re.compile(r"^[A-Za-z0-9._, -]*$")
_DIRECT = re.compile(r"@|://|^(?:git|hg|svn|bzr)\+|^(?:\.{1,2}/|/|file:)", re.IGNORECASE)
_MARKER_TOKEN = re.compile(
    r"\s*(?:(?P<str>'[^'\\\n]*'|\"[^\"\\\n]*\")|(?P<op>===|==|!=|<=|>=|~=|<|>|\(|\))|(?P<word>[A-Za-z_][A-Za-z0-9_.]*))"
)
#: Marker variables of PEP 508; any other identifier makes the marker ``unsafe-spec``.
MARKER_VARIABLES = frozenset(
    {
        "python_version", "python_full_version", "os_name", "sys_platform", "platform_release", "platform_system",
        "platform_version", "platform_machine", "platform_python_implementation", "implementation_name",
        "implementation_version", "extra",
    }
)  # fmt: skip
_VERSION_VARIABLES = frozenset({"python_version", "python_full_version", "implementation_version", "platform_release"})
_MARKER_OPS = frozenset({"===", "==", "!=", "<=", ">=", "~=", "<", ">", "in", "not in"})


def normalize_name(name: str) -> str:
    """Normalize a distribution name as PEP 503 does.

    Examples:
        >>> normalize_name("Opencv_Python.Headless")
        'opencv-python-headless'
    """
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Requirement:
    """One declared requirement, split into the parts an installer receives and the provenance this helper reports."""

    name: str
    text: str
    spec: str
    extras: tuple[str, ...]
    marker: str
    direct: bool
    safe: bool
    origin: str


def _plain_for_name(text: str, name: str) -> bool:
    """Verify that an entire installer argument denotes the approved index distribution."""
    if Pep508Requirement is None:
        return False
    try:
        parsed = Pep508Requirement(text)
    except InvalidRequirement:
        return False
    return parsed.url is None and normalize_name(parsed.name) == name


def parse_requirement(raw: str, origin: str) -> Requirement | None:
    """Parse one PEP 508 string; ``None`` for a blank line or text that is not a requirement.

    ``text`` is rebuilt from validated parts only, so an unsafe head string never reaches an installer verbatim.

    Examples:
        >>> r = parse_requirement("numpy>=1.21 ; python_version>='3.10'", "pyproject.toml")
        >>> (r.name, r.spec, r.marker, r.safe, r.direct)
        ('numpy', '>=1.21', "python_version>='3.10'", True, False)
        >>> parse_requirement("evil @ https://example.invalid/evil.whl", "x").direct
        True
        >>> parse_requirement("pkg>=1; os.system('x')", "x").safe
        False
    """
    declaration = raw.split(" #", 1)[0].strip()
    match = _REQ.fullmatch(declaration)
    if not match:
        return None
    rest = match.group("rest")
    spec, _, marker = rest.partition(";")
    spec, marker = spec.strip(), marker.strip()
    extras_raw = match.group("extras") or ""
    extras = tuple(sorted(normalize_name(e.strip()) for e in extras_raw.split(",") if e.strip()))
    name = normalize_name(match.group("name"))
    direct = bool(_DIRECT.search(spec))
    safe = (
        not direct
        and bool(_SAFE_SPEC.fullmatch(spec))
        and bool(_SAFE_EXTRAS.fullmatch(extras_raw))
        and _plain_for_name(declaration, name)
        and (not marker or marker_is_safe(marker))
    )
    text = name + (f"[{','.join(extras)}]" if extras else "") + spec + (f"; {marker}" if marker else "")
    return Requirement(name, text, spec, extras, marker, direct, safe, origin)


def _marker_tokens(marker: str) -> list[tuple[str, str]] | None:
    """Split a marker into ``(kind, value)`` tokens; ``None`` when any character falls outside the grammar."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(marker):
        match = _MARKER_TOKEN.match(marker, pos)
        if not match or match.end() == pos:
            return None if marker[pos:].strip() else tokens
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    return tokens


def marker_is_safe(marker: str) -> bool:
    """Tell whether a marker uses only PEP 508 variables, quoted literals, comparisons, ``and``/``or``/``not``/``in``.

    Examples:
        >>> marker_is_safe("python_version < '3.13' and sys_platform != 'win32'")
        True
        >>> marker_is_safe("__import__('os')")
        False
    """
    tokens = _marker_tokens(marker)
    if not tokens:
        return False
    return all(kind != "word" or value in MARKER_VARIABLES | {"and", "or", "not", "in"} for kind, value in tokens)


def _version_tuple(value: str) -> tuple[int, ...] | None:
    """Return a release tuple for a plain dotted version, or ``None``."""
    return tuple(int(p) for p in value.split(".")) if re.fullmatch(r"\d+(?:\.\d+)*", value) else None


def _compare(left: str, op: str, right: str, versioned: bool) -> bool:
    """Compare two marker values: release tuples for version variables, strings otherwise."""
    lv, rv = (_version_tuple(left), _version_tuple(right)) if versioned else (None, None)
    if lv is not None and rv is not None:
        width = max(len(lv), len(rv))
        a, b = lv + (0,) * (width - len(lv)), rv + (0,) * (width - len(rv))
        if op == "~=":
            return a >= b and lv[: len(rv) - 1] == rv[: len(rv) - 1]
        pair: tuple[object, object] = (a, b)
    else:
        pair = (left, right)
    ops: dict[str, Callable[[object, object], bool]] = {
        "==": lambda x, y: x == y, "===": lambda x, y: x == y, "!=": lambda x, y: x != y,
        "<": lambda x, y: x < y, "<=": lambda x, y: x <= y, ">": lambda x, y: x > y, ">=": lambda x, y: x >= y,  # type: ignore[operator]
        "in": lambda x, y: str(x) in str(y), "not in": lambda x, y: str(x) not in str(y),
    }  # fmt: skip
    func = ops.get(op)
    return bool(func and func(*pair))


class _MarkerParser:
    """Evaluate a validated marker against concrete environment values; any surprise raises ``ValueError``."""

    def __init__(self, tokens: list[tuple[str, str]], env: dict[str, str]) -> None:
        self.tokens, self.env, self.pos = tokens, env, 0

    def _peek(self) -> str | None:
        return self.tokens[self.pos][1] if self.pos < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        if self.pos >= len(self.tokens):
            raise ValueError("marker ended early")
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse(self) -> bool:
        value = self._or()
        if self.pos != len(self.tokens):
            raise ValueError("trailing marker tokens")
        return value

    def _or(self) -> bool:
        value = self._and()
        while self._peek() == "or":
            self._take()
            value = self._and() or value
        return value

    def _and(self) -> bool:
        value = self._atom()
        while self._peek() == "and":
            self._take()
            value = self._atom() and value
        return value

    def _value(self) -> tuple[str, bool]:
        kind, raw = self._take()
        if kind == "str":
            return raw[1:-1], False
        if kind == "word" and raw in MARKER_VARIABLES:
            return self.env.get(raw, ""), raw in _VERSION_VARIABLES
        raise ValueError(f"unexpected marker token {raw!r}")

    def _atom(self) -> bool:
        if self._peek() == "(":
            self._take()
            value = self._or()
            if self._take()[1] != ")":
                raise ValueError("unbalanced marker parenthesis")
            return value
        left, left_versioned = self._value()
        op = self._take()[1]
        if op == "not":
            op = "not " + self._take()[1]
        if op not in _MARKER_OPS:
            raise ValueError(f"unexpected marker operator {op!r}")
        right, right_versioned = self._value()
        return _compare(left, op, right, left_versioned or right_versioned)


def evaluate_marker(marker: str, env: dict[str, str]) -> bool | None:
    """Evaluate a PEP 508 marker for one environment; ``None`` when it cannot be evaluated here.

    Examples:
        >>> evaluate_marker("python_version < '3.13'", {"python_version": "3.12"})
        True
        >>> evaluate_marker("sys_platform == 'win32' or python_version >= '3.14'", {"sys_platform": "darwin",
        ...     "python_version": "3.12"})
        False
    """
    tokens = _marker_tokens(marker)
    if not tokens or not marker_is_safe(marker):
        return None
    try:
        return _MarkerParser(tokens, env).parse()
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------------------------------
# git reads (objects only; the working tree is never read)
# --------------------------------------------------------------------------------------------------------------------


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Run one read-only git command in ``repo`` and return stdout; raise ``ProvisionError`` on failure."""
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            [_GIT, "-C", str(repo), *args],
            input=stdin, capture_output=True, timeout=_GIT_TIMEOUT, check=False,
            env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvisionError(f"git {args[0]} failed: {exc}") from exc
    if done.returncode != 0:
        raise ProvisionError(f"git {args[0]} failed: {done.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return done.stdout


def resolve_commit(repo: Path, ref: str) -> str:
    """Return the full commit id of ``ref``; an option-shaped ref is refused before git sees it."""
    if not ref or ref.startswith("-"):
        raise ProvisionError(f"invalid ref {ref!r}")
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()


def tree_paths(repo: Path, sha: str) -> list[str]:
    """List every blob path of a commit."""
    raw = _git(repo, "ls-tree", "-r", "-z", "--name-only", sha)
    return [p for p in raw.decode("utf-8", "replace").split("\0") if p]


def read_blobs(repo: Path, sha: str, paths: Sequence[str]) -> dict[str, bytes]:
    """Read many blobs of one commit through one ``git cat-file --batch`` process; missing or oversized ones are
    absent."""
    wanted = [p for p in paths if "\n" not in p]
    if not wanted:
        return {}
    out = _git(repo, "cat-file", "--batch", stdin="".join(f"{sha}:{p}\n" for p in wanted).encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for path in wanted:
        end = out.find(b"\n", pos)
        if end < 0:
            break
        header = out[pos:end].split()
        pos = end + 1
        if len(header) < 3 or not header[-1].isdigit():
            continue  # "<name> missing" or "<name> ambiguous": no content follows
        size = int(header[-1])
        if header[-2] == b"blob" and size <= MAX_BLOB_BYTES:
            blobs[path] = out[pos : pos + size]
        pos += size + 1
    return blobs


# --------------------------------------------------------------------------------------------------------------------
# manifests
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingNote:
    """An index or source setting: which names it routes (``None`` = every name) and where it was found."""

    where: str
    setting: str
    names: frozenset[str] | None
    reason: GateReason = GateReason.INDEX_OVERRIDE
    #: True for a content-scan result to report separately in the plan notes.
    from_scan: bool = False


def _unparsed_routing(path: str) -> RoutingNote:
    """Gate every name when the reader could not determine a manifest's routing."""
    return RoutingNote(path, "could not parse manifest", None, GateReason.ROUTING_UNDETERMINED)


@dataclass
class Manifests:
    """The dependency declarations of one commit, as this helper reads them statically."""

    project_name: str | None = None
    runtime: list[Requirement] = field(default_factory=list)
    extras: dict[str, list[Requirement]] = field(default_factory=dict)
    groups: dict[str, list[Requirement]] = field(default_factory=dict)
    requirement_files: dict[str, list[Requirement]] = field(default_factory=dict)
    other_files: dict[str, list[Requirement]] = field(default_factory=dict)
    routing: list[RoutingNote] = field(default_factory=list)
    parsed_files: set[str] = field(default_factory=set)
    unparsed: list[str] = field(default_factory=list)

    def all_requirements(self) -> Iterable[Requirement]:
        """Yield every declared requirement from every source."""
        yield from self.runtime
        for group in (self.extras, self.groups, self.requirement_files, self.other_files):
            for reqs in group.values():
                yield from reqs

    def declared_names(self) -> set[str]:
        """Return every declared distribution name."""
        return {r.name for r in self.all_requirements()}

    def mark_unparsed(self, path: str) -> None:
        """Record an unreadable manifest and make its uncertainty part of the install decision."""
        self.unparsed.append(path)
        self.routing.append(_unparsed_routing(path))


def _toml(text: str) -> dict[str, object] | None:
    """Parse TOML with ``tomllib`` (or the ``tomli`` backport); ``None`` when no reader exists or the text is
    invalid."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        data = tomllib.loads(text)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _table(value: object) -> dict[str, object]:
    """Return ``value`` when it is a table, else an empty one."""
    return value if isinstance(value, dict) else {}


def _str_list(value: object) -> list[str]:
    """Return the string items of a list (dict items such as ``include-group`` are handled by their caller)."""
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _reqs(values: Iterable[str], origin: str) -> list[Requirement]:
    """Parse requirement strings, dropping blanks."""
    return [r for r in (parse_requirement(v, origin) for v in values) if r is not None]


def resolve_groups(raw: dict[str, object], origin: str) -> dict[str, list[Requirement]]:
    """Expand PEP 735 dependency groups, following ``include-group`` entries and ignoring cycles.

    Examples:
        >>> groups = resolve_groups({"test": ["pytest"], "dev": [{"include-group": "test"}, "ruff"]}, "p")
        >>> [r.name for r in groups["dev"]]
        ['pytest', 'ruff']
    """
    norm = {normalize_name(k): v for k, v in raw.items()}

    def expand(name: str, stack: tuple[str, ...]) -> list[Requirement]:
        out: list[Requirement] = []
        items = norm.get(name)
        for item in items if isinstance(items, list) else []:
            if isinstance(item, str):
                out += _reqs([item], f"{origin}:[dependency-groups].{name}")
            elif isinstance(item, dict) and isinstance(item.get("include-group"), str):
                child = normalize_name(str(item["include-group"]))
                if child not in stack:
                    out += expand(child, (*stack, child))
        return out

    return {name: expand(name, (name,)) for name in norm}


_UV_NON_ROUTING_KEYS = frozenset({
    "dev-dependencies", "package", "managed", "required-version", "environments", "cache-keys", "workspace", "conflicts",
})  # fmt: skip
# These tool tables do not select dependency sources for this planner. Tools that also manage environments
# are allowed only in the specific build, versioning, or publishing subtables listed below.
_NON_INSTALLER_TOOLS = frozenset({
    "bandit", "black", "bumpversion", "check-manifest", "cibuildwheel", "codespell", "commitizen", "coverage",
    "flake8", "interrogate", "isort", "mdformat", "mypy", "nbqa", "poe", "pre-commit", "pycln", "pydocstyle",
    "pylint", "pyproject-fmt", "pyright", "pytest", "pytest-ini", "ruff", "semantic_release", "setuptools",
    "setuptools_scm", "sphinx-pyproject", "sqlfluff", "towncrier", "vulture", "yapf",
})  # fmt: skip
_NON_INSTALLER_SUBTABLES = {"hatch": ("build", "metadata", "publish", "version"), "pdm": ("version",)}


def _uv_table_routing(table: dict[str, object], path: str, section: str) -> list[RoutingNote]:
    """Allow only modeled uv settings, with name-scoped sources and explicit non-default indexes."""
    if table.keys() - _UV_NON_ROUTING_KEYS - {"sources", "index"}:
        return [RoutingNote(path, f"{section} declares unmodeled installer settings", None)]
    indexes = table.get("index", [])
    if not isinstance(indexes, list) or any(
        not isinstance(index, dict) or index.get("explicit") is not True or index.get("default", False) is not False
        for index in indexes
    ):
        return [RoutingNote(path, f"{section} declares an index that is not explicit and non-default", None)]
    sources = table.get("sources", {})
    if not isinstance(sources, dict):
        return [_unparsed_routing(path)]
    return (
        [RoutingNote(path, f"{section}.sources", frozenset(normalize_name(name) for name in sources))]
        if sources
        else []
    )


def _uv_routing(uv: object, path: str, section: str, *, host: bool = False) -> list[RoutingNote]:
    """Read uv settings and treat every host index declaration as global routing."""
    if not isinstance(uv, dict) or not isinstance(uv.get("pip", {}), dict):
        return [_unparsed_routing(path)]
    if host and (uv.get("index") or uv.get("pip", {}).get("index")):
        return [RoutingNote(path, f"{section} declares a host index", None)]
    notes = _uv_table_routing({key: val for key, val in uv.items() if key != "pip"}, path, section)
    notes += _uv_table_routing(uv.get("pip", {}), path, f"{section}.pip")
    return notes


def _installer_routing(tool: object, path: str) -> list[RoutingNote]:
    """Read uv routing and gate every name when a project tool table falls outside the allowlist."""
    if not isinstance(tool, dict):
        return [_unparsed_routing(path)]
    notes = _uv_routing(tool.get("uv", {}), path, "tool.uv")
    for name, settings in tool.items():
        if name == "uv" or (name in _NON_INSTALLER_TOOLS and isinstance(settings, dict)):
            continue
        allowed = _NON_INSTALLER_SUBTABLES.get(name, ())
        if (
            isinstance(settings, dict)
            and settings
            and all(key in allowed and isinstance(value, dict) for key, value in settings.items())
        ):
            continue
        notes.append(RoutingNote(path, f"tool.{name} declares unmodeled installer settings", None))
    return notes


def _apply_pyproject(m: Manifests, text: str, path: str, *, include_requirements: bool = True) -> None:
    """Read installer routing and optionally select requirements from a pyproject."""
    data = _toml(text)
    if data is None:
        m.mark_unparsed(path)
        return
    m.parsed_files.add(path)
    tool = data.get("tool", {})
    m.routing += _installer_routing(tool, path)
    if not include_requirements:
        return
    project = _table(data.get("project"))
    name = project.get("name")
    m.project_name = normalize_name(name) if isinstance(name, str) else m.project_name
    m.runtime += _reqs(_str_list(project.get("dependencies")), f"{path}:[project.dependencies]")
    for extra, values in _table(project.get("optional-dependencies")).items():
        key = normalize_name(extra)
        m.extras[key] = _reqs(_str_list(values), f"{path}:[project.optional-dependencies].{key}")
    m.groups.update(resolve_groups(_table(data.get("dependency-groups")), path))
    uv = _table(_table(tool).get("uv"))
    if uv.get("dev-dependencies"):
        m.groups["uv-dev-dependencies"] = _reqs(_str_list(uv.get("dev-dependencies")), f"{path}:[tool.uv]")


def _apply_setup_cfg(m: Manifests, text: str, path: str) -> None:
    """Read ``setup.cfg`` install and extras requirements into ``m``."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        m.mark_unparsed(path)
        return
    m.runtime += _reqs(parser.get("options", "install_requires", fallback="").splitlines(), f"{path}:install_requires")
    if parser.has_section("options.extras_require"):
        for extra in parser["options.extras_require"]:
            key = normalize_name(extra)
            lines = parser.get("options.extras_require", extra).splitlines()
            m.extras.setdefault(key, []).extend(_reqs(lines, f"{path}:extras_require.{key}"))


@dataclass
class _RequirementsRead:
    """What one requirements file and its includes contribute: requirements, routing and the files read."""

    reqs: list[Requirement] = field(default_factory=list)
    notes: list[RoutingNote] = field(default_factory=list)
    parsed: set[str] = field(default_factory=set)
    unparsed: list[str] = field(default_factory=list)


def _read_option_line(
    read: Callable[[str], str | None], path: str, line: str, state: tuple[int, frozenset[str]], out: _RequirementsRead
) -> None:
    """Apply one option line: follow includes, record routing, skip editables.

    Options are read the way pip's option parser reads them (the contributor-code guard's
    ``requirements_line_options``), so ``-rfile``, ``-iURL`` and long-option prefixes count like their spaced forms, and
    an option the guard does not model routes every name, never passes silently.
    """
    depth, seen = state
    options = guard.requirements_line_options(line)
    if not options:
        out.unparsed.append(path)
    for option, kind, value in options:
        if kind in {"route", "editable"}:
            label = option if option in guard._REQUIREMENTS_OPTIONS else "unmodeled requirements option"
            out.notes.append(RoutingNote(path, label, None))
        elif kind == "include":
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), value))
            if not value or target.startswith("../") or target == ".." or posixpath.isabs(target):
                out.unparsed.append(path)
                continue
            sub = parse_requirements_file(read, target, depth + 1, seen | {path})
            if option == "--requirement":
                out.reqs += sub[0]
            elif names := frozenset(req.name for req in sub[0] if req.direct or not req.safe):
                out.notes.append(RoutingNote(target, "constraint specifies an untrusted source or specifier", names))
            out.notes += sub[1]
            out.parsed |= sub[2]
            out.unparsed += sub[3]


def parse_requirements_file(
    read: Callable[[str], str | None], path: str, depth: int = 0, seen: frozenset[str] = frozenset()
) -> tuple[list[Requirement], list[RoutingNote], set[str], list[str]]:
    """Parse one requirements file and the files it includes, reading lines and options as pip does.

    Returns ``(requirements, routing notes, parsed paths, unparsed paths)``; constraint files contribute routing only,
    editable and anonymous sources have unknown name scope. Continuation lines join their requirement and the per-
    requirement options (``--hash`` and the like) are dropped before the requirement is classified.
    """
    text = read(path)
    if text is None or depth > MAX_INCLUDE_DEPTH or path in seen or not guard._requirements_utf8(text):
        return [], [], set(), [path]
    out = _RequirementsRead(parsed={path})
    for line in guard.requirements_logical_lines(text):
        if guard._REQUIREMENTS_VARIABLE.search(line):
            out.unparsed.append(path)
        elif line.startswith(("-", "'", '"')):
            _read_option_line(read, path, line, (depth, seen), out)
        else:
            declaration = guard.requirement_part(line)
            req = parse_requirement(declaration, path) if guard._named_requirement(declaration) else None
            if req is None:
                out.unparsed.append(path)
            else:
                out.reqs.append(req)
    return out.reqs, out.notes, out.parsed, out.unparsed


def is_requirements_file(path: str) -> bool:
    """Tell whether ``path`` is a requirements-format file (``requirements*.txt``, ``*.in`` or under
    ``requirements/``)."""
    pure = PurePosixPath(path)
    name = pure.name.lower()
    return (name.startswith("requirements") and name.endswith((".txt", ".in"))) or (
        "requirements" in (p.lower() for p in pure.parts[:-1]) and name.endswith((".txt", ".in"))
    )


def is_selected_requirements_file(path: str) -> bool:
    """Tell whether a requirements file belongs to the project's own runtime or test set (root, ``requirements/``,
    ``tests/``), never documentation.

    Examples:
        >>> [is_selected_requirements_file(p) for p in ("requirements.txt", "requirements/test.txt",
        ...     "docs/requirements.txt", "requirements-docs.txt", "examples/demo/requirements.txt")]
        [True, True, False, False, False]
    """
    parts = PurePosixPath(path).parts
    if not is_requirements_file(path) or "doc" in parts[-1].lower():
        return False
    return len(parts) == 1 or (len(parts) == 2 and parts[0].lower() in {"requirements", "tests", "test"})


def _lock_routing(text: str, path: str, project: str | None) -> tuple[list[RoutingNote], bool]:
    """Classify recognizable lock packages and report when their name scope cannot be established."""
    data = _toml(text)
    if data is None or type(data.get("version")) is not int or data["version"] != 1:
        return [], False
    packages = data.get("package", [])
    if not isinstance(packages, list):
        return [], False
    names = set()
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str) or not package["name"].strip():
            return [], False
        name = normalize_name(package["name"])
        source = _table(package.get("source"))
        if source.keys() == {"registry"} and str(source["registry"]).rstrip("/") == DEFAULT_INDEX:
            continue
        if name != project or source.keys() not in ({"virtual"}, {"editable"}, {"directory"}):
            names.add(name)
    return ([RoutingNote(path, "lock source other than the official index", frozenset(names))] if names else []), True


def _pip_config_routing(text: str, path: str) -> list[RoutingNote]:
    """Accept only the shared guard's known neutral pip settings."""
    marks, parsed = guard._pip_config_routing(path, text)
    if not parsed:
        return [_unparsed_routing(path)]
    return [RoutingNote(path, "unmodeled pip option", None)] if marks else []


def _pipfile_lock_routing(text: str, path: str) -> list[RoutingNote]:
    """Allow a Pipenv lock only when it declares the official source and plain locked packages."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return [_unparsed_routing(path)]
    sources = _table(_table(data).get("_meta")).get("sources")
    if not isinstance(data, dict) or not isinstance(sources, list) or not sources:
        return [_unparsed_routing(path)]
    if any(str(_table(source).get("url", "")).rstrip("/") != DEFAULT_INDEX for source in sources):
        return [RoutingNote(path, "lock source other than the official index", None)]
    source_names = {source["name"] for source in sources if isinstance(source.get("name"), str)}
    for group, entries in data.items():
        if group == "_meta":
            continue
        if not isinstance(entries, dict) or any(
            not _plain_pipfile_lock_entry(entry, source_names) for entry in entries.values()
        ):
            return [RoutingNote(path, "unmodeled locked Pipenv source", None)]
    return []


def _plain_pipfile_lock_entry(entry: object, source_names: set[str]) -> bool:
    """Allow plain locked entries that refer only to a declared official index."""
    if not isinstance(entry, dict) or entry.keys() - {"version", "extras", "markers", "hashes", "index"}:
        return False
    index = entry.get("index")
    return "index" not in entry or (isinstance(index, str) and index in source_names)


def _pipfile_has_routing(data: dict[str, object]) -> bool:
    """Recognize source declarations and package specs beyond plain version requirements."""
    if data.keys() & {"source", "sources", "index", "indexes"}:
        return True
    # Pipenv permits custom dependency groups. Inspect each group's package values, never the package names.
    return any(
        isinstance(spec, dict) and spec.keys() - {"version", "extras", "markers"}
        for group, specs in data.items()
        if group not in {"requires", "scripts", "pipenv"}
        for spec in _table(specs).values()
    )


_UNMODELED_INSTALLER_CONFIGS = frozenset({"pdm.toml", ".pdm.toml", "pixi.toml", "poetry.toml"})
_UNMODELED_LOCK_FILES = frozenset({"poetry.lock", "pdm.lock", "pylock.toml"})
_CONFIG_FILES = _UNMODELED_INSTALLER_CONFIGS | {"uv.toml", "pip.conf", "pip.ini", "pipfile", "pipfile.lock"}


def _is_unmodeled_lock(name: str) -> bool:
    """Recognize Python lock formats whose source declarations this reader cannot establish."""
    return name in _UNMODELED_LOCK_FILES or (name.startswith("pylock.") and name.endswith(".toml"))


def _config_routing(text: str, path: str, *, host: bool = False) -> list[RoutingNote]:
    """Read modeled configuration and gate every name when an unmodeled installer file is present."""
    name = PurePosixPath(path).name.lower()
    if name in _UNMODELED_INSTALLER_CONFIGS:
        return [RoutingNote(path, "unmodeled installer configuration", None)]
    if name in {"pip.conf", "pip.ini"}:
        return _pip_config_routing(text, path)
    if name == "pipfile.lock":
        return _pipfile_lock_routing(text, path)
    data = _toml(text)
    if data is None:
        return [_unparsed_routing(path)]
    if name == "uv.toml":
        return _uv_routing(data, path, "uv", host=host)
    return [RoutingNote(path, "Pipfile declares installer routing", None)] if _pipfile_has_routing(data) else []


def _apply_project_manifest(m: Manifests, text: str, path: str) -> None:
    """Keep direct sources from every project manifest in the routing decision for root requirements."""
    declarations, _, parsed = guard._read_dependency_file(path, text)
    if not parsed:
        m.mark_unparsed(path)
        return
    if names := frozenset(name for name, _, direct in declarations if direct):
        m.routing.append(RoutingNote(path, "direct URL, VCS or path reference", names, GateReason.DIRECT_REFERENCE))
    if PurePosixPath(path).name.lower() == "pyproject.toml":
        _apply_pyproject(m, text, path, include_requirements=path == "pyproject.toml")
    elif path == "setup.cfg":
        _apply_setup_cfg(m, text, path)


def read_manifests(repo: Path, sha: str, paths: Sequence[str]) -> Manifests:
    """Read every dependency declaration of one commit from git objects.

    Dependencies come from the root project tables and requirements files. Nested pyprojects, locks and installer
    configuration files also contribute routing, so a nested uv source can retain its name scope.
    """
    lower = {p: PurePosixPath(p).name.lower() for p in paths}
    wanted = [
        p
        for p in paths
        if lower[p] in {"pyproject.toml", "setup.cfg", "uv.lock"} | _CONFIG_FILES or is_requirements_file(p)
    ]
    m = Manifests()
    m.routing += [RoutingNote(p, "unmodeled Python lock format", None) for p in paths if _is_unmodeled_lock(lower[p])]
    raw = read_blobs(repo, sha, wanted)
    blobs = {}
    for path in wanted:
        try:
            blobs[path] = raw[path].decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            m.mark_unparsed(path)
    for path in (p for p in blobs if lower[p] in {"pyproject.toml", "setup.cfg"}):
        _apply_project_manifest(m, blobs[path], path)
    _apply_requirement_files(m, repo, sha, blobs)
    for path in (p for p in blobs if lower[p] == "uv.lock"):
        notes, ok = _lock_routing(blobs[path], path, m.project_name if path == "uv.lock" else None)
        m.routing += notes
        (m.parsed_files.add(path) if ok else m.mark_unparsed(path))
    for path in (p for p in blobs if lower[p] in _CONFIG_FILES):
        notes = _config_routing(blobs[path], path)
        m.routing += notes
        if _unparsed_routing(path) in notes:
            m.unparsed.append(path)
        else:
            m.parsed_files.add(path)
    return m


def _apply_requirement_files(m: Manifests, repo: Path, sha: str, blobs: dict[str, str]) -> None:
    """Parse every requirements-format file; includes outside the first read are fetched on demand."""
    cache = dict(blobs)

    def read(path: str) -> str | None:
        if path not in cache:
            try:
                cache[path] = read_blobs(repo, sha, [path])[path].decode("utf-8")
            except (KeyError, UnicodeDecodeError):
                cache[path] = None
        return cache[path]

    for path in sorted(p for p in blobs if is_requirements_file(p)):
        reqs, notes, parsed, unparsed = parse_requirements_file(read, path)
        target = m.requirement_files if is_selected_requirements_file(path) else m.other_files
        target[path] = reqs
        m.routing += notes
        m.parsed_files |= parsed
        for missing in unparsed:
            m.mark_unparsed(missing)


def routing_content_scan(repo: Path, sha: str, parsed: set[str]) -> list[RoutingNote]:
    """Search the whole commit for index settings; a hit outside a parsed file makes every name's routing unknown.

    Mirrors the review's missing-package procedure: CI workflows, Dockerfiles, ``tox.ini``, scripts and docs can route
    installs elsewhere, and this helper cannot tell which names they cover.
    """
    marks = guard.project_routing_scan(str(repo), sha, parsed)
    return [RoutingNote(mark.removesuffix(": installer routing outside modeled declarations"),
                        "installer routing outside modeled declarations", None, GateReason.ROUTING_UNDETERMINED, True)
            for mark in sorted(marks)]  # fmt: skip


#: Environment names a uv invocation may carry; any other ``UV_`` name changes resolution in ways a list cannot know.
UV_ENV_ALLOWED = frozenset(
    {"UV_CACHE_DIR", "UV_NO_CACHE", "UV_NO_PROGRESS", "UV_NO_CONFIG", "UV_LINK_MODE", "UV_PYTHON",
     "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON", "UV_RUN_RECURSION_DEPTH"}
)  # fmt: skip
_USER_INDEX_ENV = (
    "UV_INDEX", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS", "UV_CONFIG_FILE",
)  # fmt: skip


def user_config_files(env: dict[str, str], home: Path, system: str, python_prefix: Path | None) -> list[Path]:
    """List host installer config paths, including pip's site file for the selected interpreter."""
    xdg = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    files = [xdg / "uv" / "uv.toml", xdg / "pip" / "pip.conf", home / ".pip" / "pip.conf"]
    if python_prefix is not None:
        files.append(python_prefix / ("pip.ini" if system == "Windows" else "pip.conf"))
    if system != "Windows":
        files.append(Path("/etc/uv/uv.toml"))
        config_dirs = (env.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(os.pathsep)
        files += [Path(root) / "uv" / "uv.toml" for root in config_dirs if root]
    if system == "Darwin":
        files += [
            home / ".config/pip/pip.conf",
            home / "Library/Application Support/pip/pip.conf",
            Path("/Library/Application Support/pip/pip.conf"),
        ]
        data_roots = [env.get("XDG_DATA_HOME", ""), *env.get("XDG_DATA_DIRS", "").split(os.pathsep)]
        files += [Path(root) / "pip" / "pip.conf" for root in data_roots if root]
    elif system == "Windows":
        appdata = env.get("APPDATA")
        programdata = Path(env.get("PROGRAMDATA") or env.get("ALLUSERSPROFILE") or "C:/ProgramData")
        files += [home / "pip" / "pip.ini", programdata / "uv" / "uv.toml", programdata / "pip" / "pip.ini"]
        if appdata:
            files += [Path(appdata) / "uv" / "uv.toml", Path(appdata) / "pip" / "pip.ini"]
    else:
        files += [Path("/etc/pip.conf")]
        files += [Path(root) / "pip" / "pip.conf" for root in config_dirs if root]
    return files


_PIP_CONFIG_PROBE = """\
import json, site, sys, sysconfig
from pathlib import Path
active = sys.argv[1]
checkout = Path(sys.argv[2]).resolve()
install = sysconfig.get_config_var('prefix') or sys.base_prefix
roots = [*site.getsitepackages([active, sys.base_prefix]), sysconfig.get_path('purelib')]
base_data = sysconfig.get_path('data', vars={'base': sys.base_prefix})
paths = [root for root in roots if root and (Path(root) / 'pip').is_dir()]
def trusted(path):
    path = Path(path)
    if path.absolute().is_relative_to(checkout) or path.resolve().is_relative_to(checkout):
        raise ValueError('pip configuration discovery would load code from the reviewed tree')

for root in paths:
    for child in ('', 'pip', 'pip/__init__.py', 'pip/_internal', 'pip/_internal/configuration.py', 'pip/_vendor'):
        trusted(Path(root) / child)
if not paths:
    import ensurepip
    wheel_root = Path(sysconfig.get_config_var('WHEEL_PKG_DIR') or Path(ensurepip.__file__).parent / '_bundled')
    paths = [str(path) for path in sorted(wheel_root.glob('pip-*.whl'), reverse=True)]
    for path in paths:
        trusted(path)
sys.path.extend(paths)
from pip._internal.configuration import get_configuration_files
files = set()
for sys.prefix in (active, sys.base_prefix, install):
    for sources in get_configuration_files().values():
        files.update(sources)
if sys.platform == 'darwin':
    for root in (base_data, sysconfig.get_path('data')):
        files.add(str(Path(root) / 'share' / 'pip' / 'pip.conf'))
print(json.dumps(sorted(files)))
"""


def _pip_config_files(python: str, prefix: Path, env: dict[str, str], checkout: Path) -> list[Path]:
    """Ask the selected interpreter's pip for config locations without loading its site hooks."""
    result = subprocess.run(  # noqa: S603 - explicitly selected interpreter in isolated mode
        [python, "-I", "-S", "-c", _PIP_CONFIG_PROBE, str(prefix), str(checkout)],
        cwd=Path(__file__).resolve().parent, env=env, capture_output=True, check=True, timeout=_PROBE_TIMEOUT,
    )  # fmt: skip
    paths = json.loads(result.stdout)
    if not isinstance(paths, list) or not all(isinstance(path, str) and Path(path).is_absolute() for path in paths):
        raise ValueError("pip did not report absolute configuration paths")
    return [Path(path) for path in paths]


def user_routing(
    env: dict[str, str], home: Path, system: str, python_prefix: Path | None, python: str | None, checkout: Path
) -> list[RoutingNote]:
    """Report the user's own index settings and uv overrides; each gates every name, and none is ever stripped.

    Only setting names are reported, never values: an index URL may carry credentials.
    """
    pip_overrides = {
        name
        for name in env
        if name.startswith("PIP_") and name[4:].lower().replace("_", "-") not in guard._NEUTRAL_PIP_OPTIONS
    }
    overrides = pip_overrides | (set(_USER_INDEX_ENV) & env.keys())
    notes = [RoutingNote("environment", name, None, GateReason.USER_INDEX_SETTING) for name in sorted(overrides)]
    extra_uv = sorted(n for n in env if n.startswith("UV_") and n not in UV_ENV_ALLOWED and n not in overrides)
    if extra_uv:
        notes.append(RoutingNote("environment", ", ".join(extra_uv), None, GateReason.UV_ENV_OVERRIDE))
    configs = user_config_files(env, home, system, python_prefix)
    if python_prefix is None or python is None:
        notes.append(RoutingNote("pip site", "selected interpreter prefix unavailable", None,
                                 GateReason.ROUTING_UNDETERMINED))  # fmt: skip
    else:
        try:
            configs += _pip_config_files(python, python_prefix, env, checkout)
        except (OSError, ValueError, subprocess.SubprocessError):
            notes.append(
                RoutingNote("pip", "configuration discovery unavailable", None, GateReason.ROUTING_UNDETERMINED)
            )
    return notes + _host_config_routing(configs)


def _host_config_routing(files: Sequence[Path]) -> list[RoutingNote]:
    """Read host config sources and redact their values from the resulting routing notes."""
    notes = []
    for path in dict.fromkeys(files):
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, UnicodeDecodeError):
            notes.append(RoutingNote(str(path), "could not read installer config", None, GateReason.USER_INDEX_SETTING))
            continue
        notes += [RoutingNote(str(path), n.setting, None, GateReason.USER_INDEX_SETTING) for n in
                  _config_routing(text, path.name, host=True)]  # fmt: skip
    return notes


# --------------------------------------------------------------------------------------------------------------------
# selection and classification
# --------------------------------------------------------------------------------------------------------------------

#: Test-imported modules whose distributions are commonly missing from a reviewer environment, in preference order.
IMPORT_DISTRIBUTIONS: dict[str, tuple[str, ...]] = {
    "cv2": ("opencv-python-headless", "opencv-contrib-python-headless", "opencv-python", "opencv-contrib-python"),
    "pycocotools": ("pycocotools",),
    "matplotlib": ("matplotlib",),
    "pandas": ("pandas",),
    "sklearn": ("scikit-learn",),
    "skimage": ("scikit-image",),
    "PIL": ("pillow",),
    "yaml": ("pyyaml",),
    "pytest": ("pytest",),
}
#: Distribution families that install the same import package; only one member may be installed.
EXCLUSIVE_FAMILIES = (IMPORT_DISTRIBUTIONS["cv2"],)
_TEST_FILE = re.compile(r"(?:^|/)(?:tests?/.*\.py|test_[^/]*\.py|[^/]*_test\.py|conftest\.py)$")
_IMPORT_LINE = re.compile(
    r"^\s*(?:import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*)|from\s+([A-Za-z_][\w.]*)\s+import\b)", re.MULTILINE
)
_IMPORTORSKIP = re.compile(r"importorskip\(\s*['\"]([A-Za-z_][\w.]*)['\"]")
_TEST_GROUP = re.compile(r"test|^dev$|^ci$|^develop(?:meant)?$")


def scan_test_imports(repo: Path, sha: str, paths: Sequence[str]) -> set[str]:
    """Return the top-level modules the commit's test files import (static text scan; nothing is executed)."""
    files = sorted(p for p in paths if _TEST_FILE.search(p))[:MAX_TEST_FILES]
    modules: set[str] = set()
    for data in read_blobs(repo, sha, files).values():
        text = data.decode("utf-8", "replace")
        for plain, source in _IMPORT_LINE.findall(text):
            modules.update(part.strip().split(".")[0] for part in (plain.split(",") if plain else [source]))
        modules.update(m.split(".")[0] for m in _IMPORTORSKIP.findall(text))
    return modules


@dataclass
class Planned:
    """One distribution this run means to install: every requirement string that names it and where each came from."""

    name: str
    texts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)


@dataclass
class Plan:
    """The classified selection: what to install, what stays gated, what is deliberately skipped."""

    install: dict[str, Planned] = field(default_factory=dict)
    gated: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def skip(self, name: str, reason: SkipReason, detail: str) -> None:
        """Record a deliberate skip once per name and reason."""
        entry = {"name": name, "reason": reason.value, "detail": detail}
        if entry not in self.skipped:
            self.skipped.append(entry)


@dataclass(frozen=True)
class Selection:
    """Caller choices that widen or narrow the default selection."""

    groups: tuple[str, ...] = ()
    all_groups: bool = False
    extras: bool = True
    exclude: tuple[str, ...] = ()


def select_requirements(head: Manifests, choice: Selection, imports: set[str]) -> list[tuple[Requirement, str]]:
    """Return ``(requirement, source)`` pairs the head declares for runtime, tests, extras and test imports."""
    picked: list[tuple[Requirement, str]] = [(r, "runtime") for r in head.runtime]
    if choice.extras:
        picked += [(r, f"extra:{name}") for name, reqs in sorted(head.extras.items()) for r in reqs]
    wanted_groups = {normalize_name(g) for g in choice.groups}
    for name, reqs in sorted(head.groups.items()):
        if choice.all_groups or name in wanted_groups or _TEST_GROUP.search(name):
            picked += [(r, f"group:{name}") for r in reqs]
    picked += [(r, f"requirements:{path}") for path, reqs in sorted(head.requirement_files.items()) for r in reqs]
    picked += _import_driven(head, imports, {r.name for r, _ in picked})
    return picked


def _import_driven(head: Manifests, imports: set[str], already: set[str]) -> list[tuple[Requirement, str]]:
    """Add declared distributions the tests import but the default selection missed (one per exclusive family)."""
    declared: dict[str, Requirement] = {}
    for req in head.all_requirements():
        declared.setdefault(req.name, req)
    out: list[tuple[Requirement, str]] = []
    for module in sorted(imports):
        family = IMPORT_DISTRIBUTIONS.get(module, (normalize_name(module),))
        if any(name in already or name == head.project_name for name in family):
            continue  # already selected, or the project's own package (run from source, never installed)
        hit = next((declared[name] for name in family if name in declared), None)
        if hit is not None:
            out.append((hit, f"test-import:{module}"))
            already.add(hit.name)
    if "pytest" in imports or "pytest" in already:
        out += [(r, "test-import:pytest-plugin") for n, r in sorted(declared.items())
                if n.startswith("pytest-") and n not in already]  # fmt: skip
    return out


def expand_self_references(
    picked: list[tuple[Requirement, str]], head: Manifests, plan: Plan
) -> list[tuple[Requirement, str]]:
    """Replace ``project[extra]`` with that extra's requirements; the project itself is never installed."""
    out: list[tuple[Requirement, str]] = []
    queue = list(picked)
    seen_extras: set[str] = set()
    while queue:
        req, source = queue.pop(0)
        if head.project_name is None or req.name != head.project_name:
            out.append((req, source))
            continue
        plan.skip(req.name, SkipReason.PROJECT_ITSELF, f"{req.origin}: the reviewed project is run from its source")
        for extra in req.extras:
            if extra not in seen_extras:
                seen_extras.add(extra)
                queue += [(r, f"{source}>{extra}") for r in head.extras.get(extra, [])]
    return out


def base_plain_names(base: Manifests) -> set[str]:
    """Return the names every base declaration of which is a plain official-index spec.

    Only these count as base-declared for trust: a name the base also points at a URL, VCS or path anywhere, or
    declares outside the plain grammar, is not exempt from any check.

    Example:
        >>> m = Manifests(runtime=[parse_requirement("foo>=1", "p"), parse_requirement("bar", "p")])
        >>> m.other_files["docs/requirements.txt"] = [parse_requirement("foo @ git+https://x.invalid/f.git", "d")]
        >>> sorted(base_plain_names(m))
        ['bar']
    """
    declared = list(base.all_requirements())
    return {r.name for r in declared} - {r.name for r in declared if r.direct or not r.safe}


def _gate_reason(
    req: Requirement, base_plain: set[str], verdict: dict[str, set[str]], routing: Sequence[RoutingNote]
) -> tuple[GateReason, str] | None:
    """Return the gate reason for unsafe or untrusted requirements or unresolved routing."""
    if req.direct or req.name in verdict["direct"]:
        return GateReason.DIRECT_REFERENCE, f"{req.origin}: direct URL, VCS or path reference"
    if not req.safe:
        return GateReason.UNSAFE_SPEC, f"{req.origin}: specifier, extras or marker outside the plain grammar"
    if req.name not in base_plain or req.name in verdict["added"]:
        return GateReason.ADDED_BY_PR, f"{req.origin}: not declared at the base ref as a plain official-index spec"
    if req.name in verdict["lookalike"]:
        return GateReason.LOOKALIKE, f"{req.origin}: the gate reports a lookalike of a known name"
    for note in routing:
        if note.names is None or req.name in note.names:
            return note.reason, f"{note.where}: {note.setting}"
    return None


def _first_member(family: tuple[str, ...], present: Iterable[str]) -> str | None:
    """Return the most preferred family member among ``present``."""
    names = set(present)
    return next((n for n in family if n in names), None)


def build_plan(
    picked: list[tuple[Requirement, str]],
    base_plain: set[str],
    verdict: dict[str, set[str]],
    routing: Sequence[RoutingNote],
    choice: Selection,
    plan: Plan | None = None,
) -> Plan:
    """Classify every selected requirement into install, gated or skipped (into ``plan`` when given)."""
    plan = plan if plan is not None else Plan()
    excluded = {normalize_name(n) for n in choice.exclude}
    for req, source in picked:
        if req.name in excluded:
            plan.skip(req.name, SkipReason.EXCLUDED_BY_CALLER, source)
            continue
        gate = _gate_reason(req, base_plain, verdict, routing)
        if gate:
            entry = {"name": req.name, "requirement": req.text if req.safe else req.origin, "cause": Cause.GATED.value,
                     "gate_reason": gate[0].value, "detail": gate[1], "source": source}  # fmt: skip
            if entry not in plan.gated:
                plan.gated.append(entry)
            continue
        item = plan.install.setdefault(req.name, Planned(req.name))
        if req.text not in item.texts:
            item.texts.append(req.text)
            item.markers.append(req.marker)
        item.sources.append(source)
    for family in EXCLUSIVE_FAMILIES:
        keep = _first_member(family, plan.install)
        for name in [n for n in family if n in plan.install and n != keep]:
            del plan.install[name]
            plan.skip(name, SkipReason.CONFLICTING_PROVIDER, f"provides the same module as {keep}")
    return plan


def report_undeclared_imports(plan: Plan, imports: set[str], declared: set[str]) -> None:
    """Name commonly missing optional modules the tests import that no manifest declares (never installed)."""
    for module in sorted(imports & set(IMPORT_DISTRIBUTIONS)):
        if not any(name in declared for name in IMPORT_DISTRIBUTIONS[module]):
            plan.skip(module, SkipReason.UNDECLARED_IMPORT, "imported by tests, declared by no manifest at the head")


def gate_verdict(repo: Path, base: str, head: str) -> tuple[dict[str, set[str]], list[RoutingNote]]:
    """Read the contributor-code gate's dependency verdict, deferring scoped uv routing to the manifest reader."""
    verdict: dict[str, set[str]] = {"added": set(), "direct": set(), "lookalike": set()}
    try:
        deps = guard.build_report(str(repo), head, base, False)["dependencies"]
    except (guard.GuardError, OSError, KeyError, ValueError) as exc:
        return verdict, [RoutingNote("contributor-code gate", f"dependency scan failed: {exc}", None,
                                     GateReason.ROUTING_UNDETERMINED)]  # fmt: skip
    deps = deps if isinstance(deps, dict) else {}
    verdict["added"] = {normalize_name(str(n)) for n in deps.get("added", [])}
    verdict["direct"] = {normalize_name(str(n)) for n in deps.get("non_index_references", [])}
    verdict["lookalike"] = {normalize_name(str(p).split("~", 1)[0]) for p in deps.get("lookalikes", [])}
    overrides = set(deps.get("index_overrides", []))
    # The manifest reader checks these at every pyproject path and preserves which names are routed.
    overrides = {mark for mark in overrides if not mark.startswith("uv-source: ")}
    notes = [RoutingNote("contributor-code gate", "index override added", None)] if overrides else []
    notes += [RoutingNote("contributor-code gate", f"unparsed manifest {p}", None, GateReason.ROUTING_UNDETERMINED)
              for p in deps.get("unparsed", [])]  # fmt: skip
    return verdict, notes


# --------------------------------------------------------------------------------------------------------------------
# host facts
# --------------------------------------------------------------------------------------------------------------------


def _command_text(argv: Sequence[str], timeout: int = 10) -> str | None:
    """Return a probe command's stripped stdout, or ``None`` when it is absent or fails."""
    exe = shutil.which(argv[0]) or (argv[0] if os.path.isabs(argv[0]) and os.path.exists(argv[0]) else None)
    if exe is None:
        return None
    try:
        done = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _sysctl(key: str) -> str | None:
    """Read one macOS ``sysctl`` value."""
    return _command_text([shutil.which("sysctl") or "/usr/sbin/sysctl", "-n", key])


def _proc_field(path: str, key: str) -> str | None:
    """Read the first ``key: value`` line of a Linux ``/proc`` file."""
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.split(":", 1)[0].strip() == key:
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _ram_bytes(system: str) -> int | None:
    """Return physical memory in bytes as the OS reports it."""
    if system == "Darwin":
        value = _sysctl("hw.memsize")
        return int(value) if value and value.isdigit() else None
    if system == "Linux":
        value = _proc_field("/proc/meminfo", "MemTotal")
        return int(value.split()[0]) * 1024 if value else None
    if system == "Windows":
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("pt", ctypes.c_ulonglong), ("pa", ctypes.c_ulonglong),
                        ("vt", ctypes.c_ulonglong), ("va", ctypes.c_ulonglong), ("ve", ctypes.c_ulonglong)]  # fmt: skip

        status = _Status()
        status.length = ctypes.sizeof(_Status)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return int(status.total) if ok else None
    return None


def _cpu_model(system: str) -> str | None:
    """Return the CPU model string the OS reports."""
    if system == "Darwin":
        return _sysctl("machdep.cpu.brand_string")
    if system == "Linux":
        return _proc_field("/proc/cpuinfo", "model name") or _proc_field("/proc/cpuinfo", "Hardware")
    return platform.processor() or None


def _accelerators(system: str, cpu_model: str | None) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Probe accelerator classes; return the present devices and, per absent class, the evidence of absence."""
    present: list[dict[str, str]] = []
    absent: dict[str, str] = {}
    cuda = _command_text(["nvidia-smi", "-L"])
    if cuda:
        present += [{"kind": "cuda", "name": ln.strip(), "evidence": "nvidia-smi -L"} for ln in cuda.splitlines()]
    else:
        reason = "nvidia-smi not on PATH" if shutil.which("nvidia-smi") is None else "nvidia-smi -L failed"
        absent["cuda"] = reason
        absent["tensorrt"] = f"needs CUDA: {reason}"
    rocm = _command_text(["rocm-smi", "--showproductname"])
    if rocm:
        present.append({"kind": "rocm", "name": rocm.splitlines()[0][:120], "evidence": "rocm-smi"})
    else:
        absent["rocm"] = "rocm-smi not on PATH" if shutil.which("rocm-smi") is None else "rocm-smi failed"
    if system == "Darwin" and cpu_model and cpu_model.startswith("Apple"):
        present.append({"kind": "mps", "name": f"{cpu_model} integrated GPU",
                        "evidence": "sysctl machdep.cpu.brand_string (Apple silicon SoC)"})  # fmt: skip
    else:
        absent["mps"] = f"no Apple silicon GPU (os {system}, cpu {cpu_model or 'unknown'})"
    if system == "Linux" and Path("/dev/accel0").exists():
        present.append({"kind": "tpu", "name": "/dev/accel0", "evidence": "/dev/accel0 exists"})
    else:
        absent["tpu"] = "no /dev/accel0 device" if system == "Linux" else f"no TPU probe on {system}"
    return present, absent


#: OS classes shared with the Codex Rig claim record, by ``platform.system()`` value.
OS_CLASSES = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}


def host_class_of(spec: dict[str, object]) -> dict[str, object]:
    """Return the host class recorded per run: OS class, machine, native architecture and accelerator classes.

    Example:
        >>> host_class_of({"os": "Darwin", "machine": "arm64", "native_arch": "arm64", "accelerators": ["mps"]})
        {'os': 'macos', 'machine': 'arm64', 'native_arch': 'arm64', 'accelerators': ['mps']}
    """
    system = str(spec.get("os") or "")
    return {"os": OS_CLASSES.get(system, system.lower()), "machine": spec.get("machine"),
            "native_arch": spec.get("native_arch") or spec.get("machine"),
            "accelerators": spec.get("accelerators") or []}  # fmt: skip


def host_spec() -> dict[str, object]:
    """Report this host as observed facts: OS, architecture (native and as Python sees it), CPU, cores, RAM, and
    accelerators.

    The keys ``os``, ``os_release``, ``machine``, ``cpu_model``, ``cpu_count``, ``memory_bytes``, ``python`` and
    ``accelerators`` (class names from ``cuda``, ``rocm``, ``mps``, ``tpu``) are shared with the Codex Rig benchmark
    claim record, so both harnesses report a host the same way; the rest are this helper's detail.

    Example:
        >>> sorted(k for k in host_spec() if k in {"os", "machine", "cpu_count", "accelerators"})
        ['accelerators', 'cpu_count', 'machine', 'os']
    """
    system = platform.system()
    cpu = _cpu_model(system)
    present, absent = _accelerators(system, cpu)
    native = platform.machine()
    translated = None
    if system == "Darwin":
        translated = _sysctl("sysctl.proc_translated") == "1"
        native = "arm64" if _sysctl("hw.optional.arm64") == "1" else native
    physical = _sysctl("hw.physicalcpu") if system == "Darwin" else None
    evidence = {a["kind"]: a["evidence"] for a in present} | {k: v for k, v in absent.items() if k != "tensorrt"}
    spec: dict[str, object] = {
        "os": system,
        "os_release": platform.release(),
        "os_version": platform.mac_ver()[0] if system == "Darwin" else platform.version(),
        "machine": platform.machine(),
        "native_arch": native,
        "translated": translated,
        "cpu_model": cpu,
        "cpu_count": os.cpu_count(),
        "cores_physical": int(physical) if physical and physical.isdigit() else None,
        "memory_bytes": _ram_bytes(system),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "accelerators": sorted({a["kind"] for a in present}),
        "accelerator_devices": present,
        "accelerator_evidence": evidence,
        "absent_accelerators": absent,
    }
    spec["host_class"] = host_class_of(spec)
    return spec


# --------------------------------------------------------------------------------------------------------------------
# environment, installer, causes
# --------------------------------------------------------------------------------------------------------------------


def env_root(env: dict[str, str] | None = None) -> Path:
    """Return this user's root for reviewer environments: ``${TMPDIR:-/tmp}/oss-review-env-<uid>``, links resolved.

    The user id keeps two users of one host on separate roots, so another user's pre-created directory is never the one
    this run would have to refuse. The temp directory is resolved first (macOS ``/tmp`` is a link), so the no-link check
    of :func:`private_directory_problem` judges only the root and what lies below it.
    """
    source = os.environ if env is None else env
    base = source.get("TMPDIR") or (tempfile.gettempdir() if os.name == "nt" else "/tmp")  # noqa: S108
    user = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return Path(base).resolve() / f"{ENV_ROOT_NAME}{user}"


def private_directory_problem(path: Path, *, owner_only: bool) -> str | None:
    """Return why ``path`` cannot hold reviewer environments, or ``None`` when it can.

    It must be a real directory (not a symbolic link, with no link above it) owned by the invoking user. With
    ``owner_only`` no other user may enter it at all (mode ``0700``), as for the root and each repository directory
    this helper creates; without it the directory sits inside such a parent, so its own mode bits cannot matter.
    Ownership and mode bits are POSIX facts; on native Windows only the link checks apply, since its temp directory
    is already per user.

    Example:
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     private_directory_problem(Path(directory).resolve(), owner_only=True) is None
        True
    """
    try:
        info = path.lstat()
        real = path.resolve(strict=True)
    except OSError as exc:
        return f"{path}: {exc.strerror or exc}"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or real != path:
        return f"{path} is a symbolic link or lies below one"
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return f"{path} is owned by another user"
    if hasattr(os, "getuid") and owner_only and info.st_mode & 0o077:
        return f"{path} is open to other users (mode {stat.S_IMODE(info.st_mode):o})"
    return None


def prepare_env_parents(env_dir: Path) -> None:
    """Create the root and repository directories of ``env_dir`` owner-only and verify both before any use.

    Raises:
        PermissionError: Either directory is not private to the invoking user; nothing under it may be run or reused.
    """
    root, repo_dir = env_dir.parent.parent, env_dir.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (root, repo_dir):
        directory.mkdir(mode=0o700, exist_ok=True)
        problem = private_directory_problem(directory, owner_only=True)
        if problem:
            raise PermissionError(f"unsafe environment directory: {problem}")


def check_env_dir(env_dir: Path) -> None:
    """Verify an existing environment directory before its interpreter is probed or reused.

    Raises:
        PermissionError: The directory is a link or another user's; its interpreter must never run.
    """
    if env_dir.exists() or env_dir.is_symlink():
        problem = private_directory_problem(env_dir, owner_only=False)
        if problem:
            raise PermissionError(f"unsafe environment directory: {problem}")


def env_dir_for(repo: Path, head: str, root: Path) -> Path:
    """Return the environment directory for one repository and head: the same pair always maps to the same directory."""
    common = Path(_git(repo, "rev-parse", "--git-common-dir").decode().strip())
    common = (repo / common).resolve() if not common.is_absolute() else common.resolve()
    toplevel = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip())
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", toplevel.name)[:40] or "repo"
    key = hashlib.sha256(str(common).encode()).hexdigest()[:10]
    return root / f"{label}-{key}" / head[:16]


def is_inside(path: Path, parent: Path) -> bool:
    """Tell whether ``path`` resolves inside ``parent``."""
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class RunResult:
    """One installer or probe process: exit status and captured text."""

    returncode: int
    output: str
    timed_out: bool = False


def run_process(argv: Sequence[str], env: dict[str, str], cwd: Path, timeout: int) -> RunResult:
    """Run one process with a bounded time; output is stdout and stderr joined."""
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            list(argv),
            env=env,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )  # undecodable installer output must never cost the JSON document
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return RunResult(124, text + f"\ntimed out after {timeout}s", timed_out=True)
    except OSError as exc:
        return RunResult(127, f"cannot start {argv[0]}: {exc}")
    return RunResult(done.returncode, (done.stdout or "") + "\n" + (done.stderr or ""))


_CAUSE_PATTERNS: tuple[tuple[Cause, re.Pattern[str]], ...] = (
    # Definitive before any download is attempted: the installer itself, or the environment's pip, cannot start.
    (Cause.NO_INSTALLER, re.compile(r"^cannot start |no module named pip\b", re.IGNORECASE | re.MULTILINE)),
    (Cause.DIRECT_REFERENCE, re.compile(r"direct reference refused", re.IGNORECASE)),
    (Cause.NETWORK, re.compile(
        r"failed to fetch|could not connect|connection (?:refused|reset|aborted)|name or service not known"
        r"|nodename nor servname|temporary failure in name resolution|network is unreachable|dns error"
        r"|error sending request|max retries exceeded|failed to establish a new connection|proxyerror"
        r"|certificate verify failed|tls handshake|could not fetch url|no route to host|operation not permitted.*connect",
        re.IGNORECASE)),
    (Cause.UNSUPPORTED_PLATFORM, re.compile(
        r"matching platform tag|wheel for the current platform|only has wheels for|not a supported wheel on this platform"
        r"|requires-python|requires a different python|python version .* does not satisfy|from versions: none"
        r"|unsupported platform|no usable wheels|building from source is disabled|only-binary", re.IGNORECASE)),
    (Cause.RESOLVER_CONFLICT, re.compile(
        r"no solution found when resolving|resolutionimpossible|conflicting dependencies|cannot install .* because",
        re.IGNORECASE)),
    (Cause.BUILD_FAILURE, re.compile(
        r"failed to build|subprocess-exited-with-error|build backend|failed to prepare distributions"
        r"|failed building wheel|metadata-generation-failed|error: command .* failed", re.IGNORECASE)),
)  # fmt: skip


def classify_failure(output: str, timed_out: bool = False) -> Cause:
    """Map installer output to one cause; network wins over the rest because an offline host fails every name.

    Examples:
        >>> classify_failure("error: Failed to fetch: `https://pypi.org/simple/x/` dns error")
        <Cause.NETWORK: 'network'>
        >>> classify_failure("x has no wheels with a matching platform tag (e.g. `macosx_14_0_arm64`)")
        <Cause.UNSUPPORTED_PLATFORM: 'unsupported-platform'>
        >>> classify_failure("No solution found when resolving dependencies: Because a==1 depends on b<1 ...")
        <Cause.RESOLVER_CONFLICT: 'resolver-conflict'>
    """
    if timed_out:
        return Cause.TIMEOUT
    for cause, pattern in _CAUSE_PATTERNS:
        if pattern.search(output):
            return cause
    return Cause.UNKNOWN


def error_tail(output: str) -> str:
    """Keep the last lines of installer output as the exact evidence of a failure."""
    lines = [ln for ln in output.strip().splitlines() if ln.strip()][-ERROR_TAIL_LINES:]
    return "\n".join(lines)[-ERROR_TAIL_CHARS:]


#: Variables an installer process inherits: locale, temp, proxies and certificates (needed to reach the index).
#: Installs are wheel-only and never build, so no compiler or build setting is passed. Everything else, credentials
#: included, is dropped, and so is every ``UV_``/``PIP_`` setting, which this helper replaces with its own.
_INSTALLER_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT",
    "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HTTP_PROXY",
    "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy", "SSL_CERT_FILE",
    "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
)  # fmt: skip


def installer_env(source: dict[str, str], cache_root: Path) -> dict[str, str]:
    """Build the installer environment from an allowlist plus this helper's own cache and config settings."""
    env = {k: source[k] for k in _INSTALLER_ENV if k in source}
    env.update(
        {"UV_CACHE_DIR": str(cache_root / "uv-cache"), "UV_NO_PROGRESS": "1", "PIP_CONFIG_FILE": os.devnull,
         "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1", "PYTHONNOUSERSITE": "1"}
    )  # fmt: skip
    return env


@dataclass(frozen=True)
class Installer:
    """The tool that creates the environment and installs into it: ``uv`` when present, else ``venv`` plus ``pip``."""

    kind: str
    uv: str | None = None

    def create_argv(self, python: str, env_dir: Path) -> list[str]:
        """Return the command that creates the environment."""
        if self.uv:
            return [
                self.uv,
                "venv",
                "--no-config",
                "--no-python-downloads",
                "--quiet",
                "--python",
                python,
                str(env_dir),
            ]
        return [python, "-m", "venv", str(env_dir)]

    def install_argv(self, env_python: str, cache_root: Path, requirements: Sequence[str]) -> list[str]:
        """Return the install command; it names only requirement strings, never a path into the reviewed tree.

        Wheels only (``--only-binary :all:``): building a source distribution runs downloaded code at install time,
        outside any sandbox. ``--no-deps`` limits each installer to the distributions whose routing was checked.
        """
        if self.uv:
            return [self.uv, "pip", "install", "--no-config", "--python", env_python, "--default-index", DEFAULT_INDEX,
                    "--only-binary", ":all:", "--no-deps", "--cache-dir", str(cache_root / "uv-cache"),
                    *requirements]  # fmt: skip
        return [env_python, "-I", "-m", "pip", "--isolated", "install", "--no-input", "--index-url",
                DEFAULT_INDEX, "--only-binary", ":all:", "--no-deps", "--cache-dir", str(cache_root / "pip-cache"),
                *requirements]  # fmt: skip


def find_installer() -> Installer:
    """Prefer ``uv``; fall back to the standard library ``venv`` and the environment's own ``pip``."""
    uv = shutil.which("uv")
    return Installer("uv", uv) if uv else Installer("pip")


def env_python_path(env_dir: Path) -> Path:
    """Return the interpreter path inside a virtual environment on this OS."""
    return env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


_PROBE = (
    "import json, os, platform, sys\n"
    "import importlib.metadata as md\n"
    "dists = {}\n"
    "for d in md.distributions():\n"
    "    name = d.metadata['Name']\n"
    "    if name:\n"
    "        dists[name] = d.version\n"
    "impl = sys.implementation.version\n"
    "print(json.dumps({'markers': {'python_version': '%d.%d' % sys.version_info[:2],"
    " 'python_full_version': platform.python_version(), 'os_name': os.name, 'sys_platform': sys.platform,"
    " 'platform_release': platform.release(), 'platform_system': platform.system(),"
    " 'platform_version': platform.version(), 'platform_machine': platform.machine(),"
    " 'platform_python_implementation': platform.python_implementation(),"
    " 'implementation_name': sys.implementation.name,"
    " 'implementation_version': '%d.%d.%d' % (impl.major, impl.minor, impl.micro)},"
    " 'base_prefix': sys.base_prefix, 'prefix': sys.prefix, 'distributions': dists}))\n"
)


def minimal_env() -> dict[str, str]:
    """Keep the host's executable search and Windows process bootstrap settings."""
    keys = ("PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    return {k: os.environ[k] for k in keys if k in os.environ}


@dataclass(frozen=True)
class PythonIdentity:
    """Identify the base Python binary even when a launcher path stays the same."""

    version: str
    base_executable: str
    device: int
    inode: int
    size: int
    mtime_ns: int


_PYTHON_IDENTITY_PROBE = (
    "import json, sys\nprint(json.dumps([sys.version.split()[0], sys.prefix, sys.executable, sys._base_executable]))\n"
)


def _probe_python(python: str, cwd: Path) -> tuple[Path, PythonIdentity, Path] | None:
    """Read the prefix, base binary and actual launcher without executing interpreter site hooks."""
    done = run_process([python, "-I", "-S", "-c", _PYTHON_IDENTITY_PROBE], minimal_env(), cwd, _PROBE_TIMEOUT)
    if done.returncode != 0:
        return None
    try:
        version, prefix, launcher, base = json.loads(done.output.partition("\n")[0])
        if (
            not isinstance(version, str)
            or not version
            or not all(Path(p).is_absolute() for p in (prefix, launcher, base))
        ):
            return None
        # Before Python 3.14, site initialization sets the virtual environment's prefix.
        launcher_dir = Path(launcher).parent
        if any((p / "pyvenv.cfg").is_file() for p in (launcher_dir, launcher_dir.parent)):
            prefix = launcher_dir.parent
        executable = Path(base).resolve(strict=True)
        binary = executable.stat()
        identity = PythonIdentity(version, executable.as_posix(), binary.st_dev, binary.st_ino, binary.st_size,
                                  binary.st_mtime_ns)  # fmt: skip
        return Path(prefix), identity, Path(launcher)
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def probe_env(env_python: Path, cwd: Path) -> dict[str, object] | None:
    """Run the environment's interpreter in isolated mode, outside the reviewed tree, and read its markers and installed
    distributions."""
    done = run_process([str(env_python), "-I", "-c", _PROBE], minimal_env(), cwd, _PROBE_TIMEOUT)
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.output.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return data if isinstance(data, dict) else None


_IMPORT_PROBE = (
    "import importlib, json, sys\n"
    "out = []\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        mod = importlib.import_module(name)\n"
    "        out.append({'module': name, 'ok': True, 'version': str(getattr(mod, '__version__', '')),"
    " 'file': str(getattr(mod, '__file__', ''))})\n"
    "    except Exception as exc:\n"
    "        out.append({'module': name, 'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)[:300]})\n"
    "print(json.dumps(out))\n"
)


def probe_imports(env_python: Path, modules: Sequence[str], cwd: Path) -> list[dict[str, object]]:
    """Import third-party modules in the environment from a directory outside the reviewed tree (never project code)."""
    safe = [m for m in modules if re.fullmatch(r"[A-Za-z_][\w.]*", m)]
    if not safe:
        return []
    done = run_process([str(env_python), "-I", "-c", _IMPORT_PROBE, *safe], minimal_env(), cwd, _PROBE_TIMEOUT)
    try:
        data = json.loads(done.output.strip().splitlines()[0])
    except (ValueError, IndexError):
        return [{"module": m, "ok": False, "error": error_tail(done.output)} for m in safe]
    return data if isinstance(data, list) else []


#: Errors a non-blocking lock attempt raises while another process holds the lock.
_LOCK_BUSY = (errno.EACCES, errno.EAGAIN, errno.EDEADLK)


def _try_lock(fileno: int) -> None:
    """Take an exclusive, non-blocking advisory lock that the operating system releases when the process ends."""
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fileno: int) -> None:
    """Release a lock taken by :func:`_try_lock`."""
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fileno, fcntl.LOCK_UN)


class EnvLock:
    """Exclusive lock for one environment directory, held by at most one process at a time.

    The lock is an operating-system advisory lock on ``<env>.lock``, never the file's existence: the system releases it
    when its process ends, so a run killed by a caller's time cap never blocks the next one, and the file is never
    deleted, so two waiters can never each hold a lock on a different file of the same name.
    """

    def __init__(self, env_dir: Path, wait: int) -> None:
        self.path = env_dir.parent / f"{env_dir.name}.lock"
        self.wait = wait
        self._handle: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        """Tell whether this object holds the lock now."""
        return self._handle is not None

    def acquire(self) -> bool:
        """Take the lock, waiting up to ``wait`` seconds; ``False`` when another run still holds it."""
        handle = self.path.open("a+b")
        handle.seek(0)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                _try_lock(handle.fileno())
            except OSError as exc:
                if exc.errno not in _LOCK_BUSY:
                    handle.close()
                    raise
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
                continue
            self._handle = handle
            return True

    def release(self) -> None:
        """Release the lock when this object holds it; the lock file stays."""
        if self._handle is not None:
            try:
                _unlock(self._handle.fileno())
            except OSError:
                pass  # closing the handle releases the lock as well
            self._handle.close()
            self._handle = None


# --------------------------------------------------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------------------------------------------------


#: Days after which an environment no run has used is deleted when the next provisioning run starts.
PRUNE_AGE_DAYS = 7
#: File written into every environment this helper creates; its mtime is the last use. Only directories carrying it
#: are ever pruned, so nothing else under the temp root (caches, another tool's files) is touched.
MARKER_NAME = ".oss-review-env.json"
MARKER_TOOL = "review_env_provision"


def mark_env(env_dir: Path) -> None:
    """Write (or refresh) the ownership marker of an environment this helper created or reused; its mtime is now."""
    (env_dir / MARKER_NAME).write_text(json.dumps({"tool": MARKER_TOOL, "schema": 1}) + "\n", encoding="utf-8")


def _owned_marker_mtime(env_dir: Path) -> float | None:
    """Return the marker's mtime when ``env_dir`` carries this helper's marker as a regular file, else ``None``."""
    marker = env_dir / MARKER_NAME
    try:
        info = os.lstat(marker)
        if not stat.S_ISREG(info.st_mode):
            return None  # a symlinked marker proves nothing about the directory
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info.st_mtime if isinstance(data, dict) and data.get("tool") == MARKER_TOOL else None


def _tree_bytes(path: Path) -> int:
    """Sum the sizes of every entry under ``path`` without following a symbolic link."""
    total = 0
    for top, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            try:
                total += os.lstat(os.path.join(top, name)).st_size
            except OSError:
                continue
    return total


def _prune_one(env_dir: Path, cutoff: float, result: dict[str, object]) -> None:
    """Delete one owned, unused environment older than ``cutoff`` and add it to ``result``."""
    used = _owned_marker_mtime(env_dir)
    if used is None or used > cutoff:
        return
    lock = EnvLock(env_dir, 0)
    if not lock.acquire():
        return  # another run is provisioning or using it right now
    try:
        size = _tree_bytes(env_dir)
        shutil.rmtree(env_dir)
    except OSError as exc:
        result["errors"].append(f"{env_dir.name}: {type(exc).__name__}: {exc}")  # type: ignore[union-attr]
        return
    finally:
        lock.release()
    result["count"] = int(result["count"]) + 1  # type: ignore[call-overload]
    result["bytes"] = int(result["bytes"]) + size  # type: ignore[call-overload]


def prune_envs(
    root: Path, keep: Path, max_age_days: int = PRUNE_AGE_DAYS, now: float | None = None
) -> dict[str, object]:
    """Delete this helper's environments under ``root`` unused for ``max_age_days``; report the count and bytes freed.

    Only ``root/<repository>/<head>`` directories carrying this helper's marker as a regular file are considered;
    ``keep`` (the current run's environment) and any environment whose lock is held are never deleted. Symbolic links
    are never followed: a linked root, repository or environment directory is skipped, and ``shutil.rmtree`` removes a
    link inside an environment without touching its target. Never raises; a failure is listed in ``errors``.
    """
    result: dict[str, object] = {"max_age_days": max_age_days, "count": 0, "bytes": 0, "errors": []}
    cutoff = (time.time() if now is None else now) - max_age_days * 86400
    keep_abs = os.path.abspath(keep)
    try:
        if root.is_symlink() or not root.is_dir():
            return result
        repos = [Path(e.path) for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
        for repo_dir in repos:
            envs = [Path(e.path) for e in os.scandir(repo_dir) if e.is_dir(follow_symlinks=False)]
            for env_dir in (e for e in envs if os.path.abspath(e) != keep_abs):
                _prune_one(env_dir, cutoff, result)
    except OSError as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")  # type: ignore[union-attr]
    return result


@dataclass
class Outcome:
    """What the environment holds after provisioning."""

    env_python: str | None = None
    python: dict[str, object] = field(default_factory=dict)
    reused: bool = False
    installed: list[dict[str, object]] = field(default_factory=list)
    failed: list[dict[str, object]] = field(default_factory=list)
    import_probes: list[dict[str, object]] = field(default_factory=list)
    batch_failure: dict[str, object] | None = None
    plan_digest: str | None = None


def plan_digest(plan: Plan, installer: Installer, python: str, identity: PythonIdentity) -> str:
    """Bind the approved requirements, installer policy and selected interpreter to one digest."""
    body = {"install": {n: sorted(p.texts) for n, p in sorted(plan.install.items())}, "installer": installer.kind,
            "launcher": Path(python).as_posix(), "python": asdict(identity), "dependency_mode": "no-deps"}  # fmt: skip
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _create_env(installer: Installer, python: str, env_dir: Path, env: dict[str, str], timeout: int) -> RunResult:
    """Create a fresh environment inside its verified repository directory, removing a broken one made before."""
    if env_dir.exists():
        shutil.rmtree(env_dir, ignore_errors=True)
    return run_process(installer.create_argv(python, env_dir), env, env_dir.parent, timeout)


def _install(
    installer: Installer, env_python: Path, items: Sequence[Planned], ctx: tuple[Path, dict[str, str], int, float]
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    """Install all items in one call; on failure retry one by one so each failure gets its own cause.

    Returns ``(per-requirement failures, joint-install failure or None)``. A network failure or a timeout is not
    retried per requirement: every name would fail the same way. Every call fits the overall deadline; names left
    when it passes are reported with cause ``timeout`` instead of being cut off mid-run by the caller.
    """
    cache_root, env, timeout, deadline = ctx
    rejected = [item for item in items if any(not _plain_for_name(text, item.name) for text in item.texts)]
    if rejected:
        joint = {"cause": Cause.GATED.value, "error_tail": "installer argument does not match an approved requirement"}
        return [{"name": item.name, "requirement": [], **joint} for item in rejected], joint
    texts = [t for item in items for t in item.texts]
    if not texts:
        return [], None
    left = _call_timeout(timeout, deadline)
    if left is None:
        return [_budget_failure(i) for i in items], None
    batch = run_process(installer.install_argv(str(env_python), cache_root, texts), env, env_python.parent, left)
    if batch.returncode == 0:
        return [], None
    cause = classify_failure(batch.output, batch.timed_out)
    joint = {"cause": cause.value, "error_tail": error_tail(batch.output)}
    if cause in {Cause.NETWORK, Cause.TIMEOUT} or len(items) == 1:
        return [{"name": i.name, "requirement": i.texts, **joint} for i in items], joint
    failed = []
    for item in items:
        left = _call_timeout(timeout, deadline)
        if left is None:
            failed.append(_budget_failure(item))
            continue
        one = run_process(installer.install_argv(str(env_python), cache_root, item.texts), env, env_python.parent,
                          left)  # fmt: skip
        if one.returncode != 0:
            failed.append({"name": item.name, "requirement": item.texts,
                           "cause": classify_failure(one.output, one.timed_out).value,
                           "error_tail": error_tail(one.output)})  # fmt: skip
    return failed, joint


def _call_timeout(timeout: int, deadline: float) -> int | None:
    """Return the seconds one call may take (its own limit, capped by the deadline); ``None`` when none remain."""
    remaining = int(deadline - time.monotonic())
    return min(timeout, remaining) if remaining >= 1 else None


def _budget_failure(item: Planned) -> dict[str, object]:
    """Report a name the overall budget left no time to install."""
    return {"name": item.name, "requirement": item.texts, "cause": Cause.TIMEOUT.value,
            "error_tail": "overall provisioning budget exhausted before this name; run again to continue"}  # fmt: skip


def _marker_filter(plan: Plan, markers: dict[str, str]) -> list[Planned]:
    """Drop requirement strings whose marker excludes this environment; record names left with none."""
    kept = []
    for item in plan.install.values():
        texts = [t for t, m in zip(item.texts, item.markers) if not m or evaluate_marker(m, markers) is not False]
        if texts:
            kept.append(Planned(item.name, texts, item.sources, [""] * len(texts)))
        else:
            plan.skip(item.name, SkipReason.MARKER_EXCLUDES_HOST, "; ".join(m for m in item.markers if m))
    return kept


def _installed_records(items: Sequence[Planned], dists: dict[str, str]) -> list[dict[str, object]]:
    """Report each planned name present in the environment with its installed version."""
    versions = {normalize_name(k): v for k, v in dists.items()}
    return [{"name": i.name, "version": versions[i.name], "requirement": i.texts, "sources": sorted(set(i.sources))}
            for i in items if i.name in versions]  # fmt: skip


@dataclass(frozen=True)
class ProvisionRequest:
    """Where and how to provision: the environment directory, interpreter, timeouts and import probes."""

    env_dir: Path
    python: str
    timeout: int
    lock_wait: int
    probe_modules: tuple[str, ...] = ()
    budget: int = DEFAULT_BUDGET
    selected_identity: PythonIdentity | None = None


def provision(plan: Plan, installer: Installer, request: ProvisionRequest) -> Outcome:
    """Create or reuse the environment, install the trusted plan, and report what it holds; never raises on install
    failures."""
    outcome = Outcome()
    deadline = time.monotonic() + request.budget
    env = installer_env(dict(os.environ), request.env_dir.parent.parent)
    try:
        prepare_env_parents(request.env_dir)
        lock = EnvLock(request.env_dir, request.lock_wait)
        if not lock.acquire():
            outcome.failed.append({"name": "*", "requirement": [], "cause": Cause.TIMEOUT.value,
                                   "error_tail": f"another run still provisions {request.env_dir}"})  # fmt: skip
            return outcome
        try:
            return _provision_locked(plan, installer, request, env, outcome, deadline)
        finally:
            lock.release()
    except OSError as exc:  # a full disk or an unwritable temp root is a cause in the document, never a traceback
        outcome.failed.append({"name": "*", "requirement": [], "cause": Cause.ENV_CREATE_FAILED.value,
                               "error_tail": f"{type(exc).__name__}: {exc}"})  # fmt: skip
        return outcome


def _ensure_env(
    installer: Installer,
    request: ProvisionRequest,
    env: dict[str, str],
    state: tuple[str, Path, float, PythonIdentity],
    outcome: Outcome,
) -> dict[str, object] | None:
    """Reuse a working environment built for the same plan, else create a fresh one; return its probe or ``None``.

    ``outcome.reused`` is set only when the earlier run completed, so an incomplete one installs again in place. A
    creation failure is recorded in ``outcome.failed`` and ``None`` is returned.
    """
    digest, state_file, deadline, identity = state
    env_python = env_python_path(request.env_dir)
    check_env_dir(request.env_dir)
    previous = _read_json(state_file)
    if previous.get("plan_digest") == digest and previous.get("python") == asdict(identity):
        probe = _matching_env_probe(env_python, request.env_dir, identity)
        if probe is not None:
            outcome.reused = previous.get("complete") is True
            mark_env(request.env_dir)
            return probe
    created = _create_env(
        installer, request.python, request.env_dir, env, _call_timeout(request.timeout, deadline) or 1
    )
    probe = _matching_env_probe(env_python, request.env_dir, identity) if created.returncode == 0 else None
    if probe is not None:
        mark_env(request.env_dir)
    if probe is None:
        cause = Cause.TIMEOUT if created.timed_out else Cause.ENV_CREATE_FAILED
        outcome.failed.append(
            {
                "name": "*",
                "requirement": [],
                "cause": cause.value,
                "error_tail": error_tail(created.output)
                or "environment interpreter could not be verified against the selection",
            }
        )
    return probe  # fmt: skip


def _matching_env_probe(env_python: Path, env_dir: Path, identity: PythonIdentity) -> dict[str, object] | None:
    """Read installed packages only when the environment runs the selected base interpreter."""
    python = _probe_python(str(env_python), env_dir)
    if python is None or python[1] != identity or python[0].resolve() != env_dir.resolve():
        return None
    return probe_env(env_python, env_dir)


def _provision_locked(
    plan: Plan, installer: Installer, request: ProvisionRequest, env: dict[str, str], outcome: Outcome, deadline: float
) -> Outcome:
    """Provision while holding the environment lock.

    The same plan reuses a working environment: a complete earlier run skips the installer entirely, an incomplete
    one installs again into the same environment (already-present wheels cost nothing), so a network failure is
    retried on the next call instead of being remembered. A different plan gets a fresh environment, so a name the
    caller now excludes cannot linger from an earlier run.
    """
    env_python = env_python_path(request.env_dir)
    selected = _probe_python(request.python, request.env_dir.parent)
    if selected is None or (request.selected_identity is not None and selected[1] != request.selected_identity):
        outcome.failed.append({"name": "*", "requirement": [], "cause": Cause.ENV_CREATE_FAILED.value,
                               "error_tail": "selected interpreter is unavailable or changed after planning"})  # fmt: skip
        return outcome
    identity = selected[1]
    digest = plan_digest(plan, installer, request.python, identity)
    outcome.plan_digest = digest
    state_file = request.env_dir / "provision-state.json"
    probe = _ensure_env(installer, request, env, (digest, state_file, deadline, identity), outcome)
    if probe is None:
        return outcome
    markers = {k: str(v) for k, v in dict(probe.get("markers") or {}).items()}  # type: ignore[call-overload]
    items = _marker_filter(plan, markers)
    needs_install = not outcome.reused
    if needs_install:
        ctx = (request.env_dir.parent.parent, env, request.timeout, deadline)
        failed, outcome.batch_failure = _install(installer, env_python, items, ctx)
        outcome.failed += failed
        probe = probe_env(env_python, request.env_dir) or probe
    outcome.env_python = str(env_python)
    outcome.python = {"markers": markers, "base_prefix": probe.get("base_prefix"), "prefix": probe.get("prefix"),
                      "version": identity.version, "base_executable": identity.base_executable}  # fmt: skip
    outcome.installed = _installed_records(items, dict(probe.get("distributions") or {}))  # type: ignore[call-overload]
    _report_missing_installs(items, outcome)
    if needs_install or outcome.failed:
        receipt = {"plan_digest": digest, "python": asdict(identity),
                   "complete": not outcome.failed and outcome.batch_failure is None}  # fmt: skip
        state_file.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    if request.probe_modules:
        outcome.import_probes = probe_imports(env_python, request.probe_modules, request.env_dir)
    return outcome


def _report_missing_installs(items: Sequence[Planned], outcome: Outcome) -> None:
    """Explain planned distributions absent after the installer reported success."""
    installed = {r["name"] for r in outcome.installed}
    failed = {r["name"] for r in outcome.failed}
    for item in items:
        if item.name not in installed and item.name not in failed:
            outcome.failed.append(
                {
                    "name": item.name,
                    "requirement": item.texts,
                    "cause": Cause.UNKNOWN.value,
                    "error_tail": "installer succeeded but the distribution is not in the environment",
                }
            )


def _read_json(path: Path) -> dict[str, object]:
    """Read a JSON object, or an empty one when it is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------------------------


def classify_selection(
    repo: Path,
    base: str,
    head: str,
    choice: Selection,
    *,
    python_prefix: Path | None = Path(sys.prefix),
    python: str | None = sys.executable,
    checkout: Path | None = None,
) -> tuple[Plan, list[RoutingNote]]:
    """Read both commits, select what the head needs, and classify every requirement as install, gated or skipped."""
    head_paths, base_paths = tree_paths(repo, head), tree_paths(repo, base)
    head_m, base_m = read_manifests(repo, head, head_paths), read_manifests(repo, base, base_paths)
    routing = list(base_m.routing) + routing_content_scan(repo, base, base_m.parsed_files)
    routing += head_m.routing
    if head != base:
        routing += routing_content_scan(repo, head, head_m.parsed_files)
    routing = list(dict.fromkeys(routing))
    routing += user_routing(dict(os.environ), Path.home(), platform.system(), python_prefix, python, checkout or repo)
    verdict, gate_notes = gate_verdict(repo, base, head)
    routing += gate_notes
    # The gate's report keeps one declaration per name, so a direct reference in one manifest can hide behind a plain
    # spec in another; a URL, VCS or path reference for a name in any base or head manifest gates that name here.
    verdict["direct"] |= {r.name for m in (head_m, base_m) for r in m.all_requirements() if r.direct}
    imports = scan_test_imports(repo, head, head_paths)
    plan = Plan()
    picked = expand_self_references(select_requirements(head_m, choice, imports), head_m, plan)
    build_plan(picked, base_plain_names(base_m), verdict, routing, choice, plan)
    plan.notes += [f"content scan: {n.where}: {n.setting}" for n in routing if n.from_scan]
    report_undeclared_imports(plan, imports, head_m.declared_names())
    unparsed = sorted(set(head_m.unparsed + base_m.unparsed))
    plan.notes += [f"{Cause.MANIFEST_UNPARSED.value}: {p}" for p in unparsed]
    if "setup.py" in head_paths:
        plan.notes.append("setup.py is never executed: requirements it computes are not read")
    return plan, routing


def _status(outcome: Outcome, plan: Plan) -> str:
    """Summarize: ``unavailable`` without an interpreter, ``partial`` with any gap, else ``ready``."""
    if outcome.env_python is None:
        return "unavailable"
    return "partial" if outcome.failed or outcome.batch_failure is not None or plan.gated else "ready"


#: Source roots a PR-code run imports the uninstalled project from, relative to the run's copy of the tree.
SOURCE_ROOTS = ("src", ".")


def gate_environment(outcome: Outcome) -> dict[str, str] | None:
    """Return the launch environment of a PR-code run in this host's path form, or ``None`` without an interpreter.

    ``PATH_PREPEND`` lists the environment's script directory and the base interpreter prefix, joined with this host's
    separator, for the front of ``PATH``; the guard's sandbox makes only ``PATH`` entries, ``VIRTUAL_ENV`` and
    interpreter prefixes readable. ``PYTHONPATH`` joins :data:`SOURCE_ROOTS` the same way, so a native Windows run reads
    two entries instead of one invalid ``src:.``.
    """
    if outcome.env_python is None:
        return None
    env_dir = Path(outcome.env_python).parent.parent
    entries = [str(Path(outcome.env_python).parent), str(outcome.python.get("base_prefix") or "")]
    return {
        "VIRTUAL_ENV": str(env_dir),
        "PATH_PREPEND": os.pathsep.join(entry for entry in entries if entry),
        "PYTHONPATH": os.pathsep.join(SOURCE_ROOTS),
    }


def build_document(
    repo: Path, base: str, head: str, plan: Plan, outcome: Outcome | None, extra: dict[str, object]
) -> dict[str, object]:
    """Assemble the JSON document this helper prints."""
    outcome = outcome or Outcome()
    return {
        "schema": SCHEMA,
        "kind": KIND,
        "status": "planned" if extra.get("plan_only") else _status(outcome, plan),
        "repo": str(repo),
        "base": base,
        "head": head,
        **extra,
        "env_python": outcome.env_python,
        "python": outcome.python,
        "reused": outcome.reused,
        "planned": {n: p.texts for n, p in sorted(plan.install.items())},
        "installed": outcome.installed,
        "skipped_gated": plan.gated,
        "failed": outcome.failed,
        "joint_install_failure": outcome.batch_failure,
        "skipped": plan.skipped,
        "import_probes": outcome.import_probes,
        "notes": plan.notes,
        "gate_environment": gate_environment(outcome),
        "host": host_spec(),
    }


def _parser() -> argparse.ArgumentParser:
    """Build the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--repo", required=True, help="PR worktree or clone; read through git objects only")
    parser.add_argument("--base", required=True, help="base ref or commit")
    parser.add_argument("--head", required=True, help="PR head ref or commit")
    parser.add_argument("--out", help="also write the JSON document here")
    parser.add_argument("--python", default=sys.executable, help="interpreter or uv version request for the env")
    parser.add_argument("--group", action="append", default=[], help="extra dependency group to include")
    parser.add_argument("--all-groups", action="store_true", help="include every dependency group")
    parser.add_argument("--no-extras", action="store_true", help="skip optional-dependency extras")
    parser.add_argument("--exclude", action="append", default=[], help="distribution never to install")
    parser.add_argument("--probe-import", action="append", default=[], help="module to import after install")
    parser.add_argument("--plan-only", action="store_true", help="classify and print the plan; install nothing")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per installer call")
    parser.add_argument("--lock-wait", type=int, default=DEFAULT_LOCK_WAIT, help="seconds to wait for another run")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="seconds for the whole provisioning run")
    return parser


def _emit(document: dict[str, object], out: str | None) -> None:
    """Print the document and write it to ``out`` when given."""
    text = json.dumps(document, indent=2, sort_keys=False) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    sys.stdout.write(text)


def _in_review_tree(path: Path, toplevel: Path) -> bool:
    """Identify both executables in the review tree and symlinks leading into it."""
    return path.absolute().is_relative_to(toplevel) or is_inside(path, toplevel)


def _find_python(python: str, installer: Installer, toplevel: Path) -> str | None:
    """Find a local interpreter without searching the review project's config or PATH directories."""
    requested = Path(python)
    if requested.name != python:
        return str(requested.absolute())
    if not installer.uv:
        return shutil.which(python)
    env = minimal_env()
    search = (Path(entry) for entry in env.get("PATH", os.defpath).split(os.pathsep))
    env["PATH"] = os.pathsep.join(
        str(path) for path in search if path.is_absolute() and not _in_review_tree(path, toplevel)
    )
    args = [installer.uv, "--offline", "--no-config", "--no-python-downloads", "python", "find", "--no-project",
            "--", python]  # fmt: skip
    found = run_process(args, env, Path(sys.prefix), _PROBE_TIMEOUT)
    candidate = Path(found.output.partition("\n")[0])
    return str(candidate) if found.returncode == 0 and candidate.is_absolute() else None


def _selected_python(
    python: str, installer: Installer, toplevel: Path
) -> tuple[str | None, Path | None, PythonIdentity | None]:
    """Check and pin the interpreter used for pip site routing and later venv creation."""
    executable = python if python == sys.executable else _find_python(python, installer, toplevel)
    if executable is None:
        return python, None, None
    # Preserve venv symlinks: resolving the real path can change the interpreter's sys.prefix.
    path = Path(executable).absolute()
    if _in_review_tree(path, toplevel):
        return None, None, None
    probe = _probe_python(str(path), Path(sys.prefix))
    if probe is None:
        return str(path), None, None
    prefix, identity, launcher = probe
    if any(_in_review_tree(p, toplevel) for p in (prefix, launcher, Path(identity.base_executable))):
        return None, None, None
    return str(launcher), prefix, identity


def _run(args: argparse.Namespace) -> int:
    """Classify, provision and print the document; raise ``ProvisionError`` for a caller error."""
    repo = Path(args.repo).resolve()
    choice = Selection(tuple(args.group), args.all_groups, not args.no_extras, tuple(args.exclude))
    base, head = resolve_commit(repo, args.base), resolve_commit(repo, args.head)
    toplevel = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip())
    env_dir = env_dir_for(repo, head, env_root())
    installer = find_installer()
    if installer.uv and _in_review_tree(Path(installer.uv), toplevel):
        installer = Installer("pip")
    python, prefix, identity = _selected_python(args.python, installer, toplevel)
    plan, _ = classify_selection(repo, base, head, choice, python_prefix=prefix, python=python, checkout=toplevel)
    extra: dict[str, object] = {"env_dir": str(env_dir), "installer": installer.kind, "plan_only": args.plan_only}
    if args.plan_only:
        _emit(build_document(repo, base, head, plan, None, extra), args.out)
        return 0
    unsafe = f"environment path {env_dir} is inside the reviewed repository" if is_inside(env_dir, toplevel) else None
    if python is None:
        unsafe = "selected interpreter is inside the reviewed repository"
    elif identity is None:
        unsafe = "selected interpreter is unavailable or could not be identified"
    if unsafe is None:
        try:
            prepare_env_parents(env_dir)  # before the prune walks the root or any lock is taken
        except OSError as exc:
            unsafe = f"{type(exc).__name__}: {exc}"
    if unsafe:
        refused = {"name": "*", "requirement": [], "cause": Cause.ENV_CREATE_FAILED.value, "error_tail": unsafe}
        _emit(build_document(repo, base, head, plan, Outcome(failed=[refused]), extra), args.out)
        return 0
    extra["pruned"] = prune_envs(env_dir.parent.parent, env_dir)
    request = ProvisionRequest(env_dir, python, args.timeout, args.lock_wait, tuple(args.probe_import),
                               args.budget, identity)  # fmt: skip
    outcome = provision(plan, installer, request)
    extra["plan_digest"] = outcome.plan_digest
    _emit(build_document(repo, base, head, plan, outcome, extra), args.out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Classify, provision and print one JSON document; exit 2 only for an unresolvable repository or ref.

    Any other failure still prints a document (status ``unavailable``, cause ``unknown``) and exits 0: the review
    continues with the project interpreter and names the cause, rather than losing it in a traceback.
    """
    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")  # JSON consumers compare bytes; Windows text mode would add CR
    try:
        return _run(args)
    except ProvisionError as exc:
        _emit({"schema": SCHEMA, "kind": KIND, "status": "unavailable", "error": str(exc), "host": host_spec()},
              args.out)  # fmt: skip
        return 2
    except Exception as exc:
        _emit({"schema": SCHEMA, "kind": KIND, "status": "unavailable", "cause": Cause.UNKNOWN.value,
               "error": f"{type(exc).__name__}: {exc}", "host": host_spec()}, args.out)  # fmt: skip
        return 0


if __name__ == "__main__":
    sys.exit(main())
