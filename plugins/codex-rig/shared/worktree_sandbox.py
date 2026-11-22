"""Prepare one read-only macOS worktree gate using the native process sandbox.

## Purpose

Confine an admitted single-process gate to an existing linked worktree, selected runtime inputs and a dedicated output
directory. Git administration, sibling checkouts and parent credentials remain private.

## Scope

The caller verifies the revision, serializes repository access and selects the compatible command. Only the structured
import-bound pytest child and native Ruff check or formatting checks have profiles.

## Usage

Call prepare_pytest or prepare_ruff before starting the gate, passing the exact arguments, the runner's filtered gate
environment and canonical directories. Never pass the ambient parent environment. Preparation starts only the system
capability probe.

## Outputs

A supported route returns the sandbox launch argv and environment. The caller captures its output and exit.

## Failure

Unsupported platforms, layouts and unavailable sandbox capabilities return a fixed decline reason before workload
launch, so the caller can record why confinement did not apply. A reason never contains a path or other input text: a
contributed file name can cause a decline, and the reason is published in the gate receipt. Unexpected sandbox probe
failures raise and stop route selection. An actual workload exit is authoritative and must never trigger an unconfined
retry.

## Used by

The shared quality gate runner selects this preparation within its existing source and approval checks.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parent
_SANDBOX = Path("/usr/bin/sandbox-exec")
_CELLARS = (Path("/opt/homebrew/Cellar"), Path("/usr/local/Cellar"))
_SYSTEM = ("/usr/lib", "/usr/share", "/System/Library", "/System/Volumes/Preboot/Cryptexes", "/Library/Apple/System")
_PRIVATE = {".git", ".claude", ".codex", ".temp", ".reports", ".ssh", ".gnupg", ".aws", ".netrc", ".npmrc", ".venv"}
_DEVICES = (Path("/dev/null"), Path("/dev/urandom"), Path("/dev/random"))
# Matches the kernel's MAXSYMLINKS bound so a link cycle fails preparation instead of looping.
_MAX_LINKS = 32
Launch = tuple[list[str], dict[str, str]]

# Fixed decline reasons. Each names the failed precondition without echoing a path or other input text.
NATIVE_UNAVAILABLE = "native sandbox unavailable"
PYTEST_COMMAND = "pytest command has no matching profile"
PYTEST_RUNTIME = "pytest runtime is not the selected Homebrew framework virtualenv"
TRACKED_INVENTORY = "tracked inventory binding does not match"
RUNTIME_LIBRARY = "Homebrew runtime library resolves outside its formula"
PYTHONPATH_ESCAPE = "PYTHONPATH reaches outside the worktree and runtime"
ARTIFACT_ESCAPE = "pytest artifact path is outside private scratch"
RUFF_COMMAND = "Ruff command has no matching profile"
RUFF_INSTALL = "Ruff executable is not a known native install"
RUFF_BINARY = "Ruff executable is not a native binary"
GRANT_OVERLAP = "runtime grant overlaps the worktree or scratch"
RUFF_PRIVATE_INPUT = "Ruff input includes private, linked or nested paths"
PROFILE_SIZE = "sandbox profile exceeds its size limit"
INPUT_MISSING = "a required sandbox input is missing"
INPUT_UNVERIFIED = "a sandbox input could not be verified"


class _Declined(ValueError):
    """Stop preparation with a fixed reason; constructed only with literal messages, never with input text."""


def _decline_reason(error: Exception) -> str:
    """Name a preparation failure without echoing the paths or values carried by a standard library exception."""
    if isinstance(error, _Declined):
        return str(error)
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return INPUT_MISSING
    return INPUT_UNVERIFIED


def _git_line(path: Path) -> str:
    """Read a bounded single-line registration file without following a file alias."""
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_size > 4096 or observed.st_nlink != 1:
        raise _Declined("invalid worktree registration")
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or not lines[0]:
        raise _Declined("invalid worktree registration")
    return lines[0]


def _overlap(a: Path, b: Path) -> bool:
    """Reject aliases and ancestor grants in either direction."""
    return a.is_relative_to(b) or b.is_relative_to(a)


def _linked_main(work: Path, scratch: Path) -> Path:
    """Require reciprocal linked registration and an empty writable directory outside its source and Git metadata."""
    if work != work.resolve(strict=True) or scratch != scratch.resolve(strict=True) or _overlap(work, scratch):
        raise _Declined("overlapping or redirected gate roots")
    ownership = scratch.stat()
    if ownership.st_uid != os.getuid() or ownership.st_mode & 0o077 or any(scratch.iterdir()) or not work.is_dir():
        raise _Declined("invalid gate roots")
    pointer = work / ".git"
    prefix, sep, path = _git_line(pointer).partition("gitdir: ")
    if prefix or not sep or not path:
        raise _Declined("expected a linked worktree")
    admin = (work / path).resolve(strict=True)
    common = (admin / _git_line(admin / "commondir")).resolve(strict=True)
    if (
        common.name != ".git"
        or admin.parent != common / "worktrees"
        or (admin / _git_line(admin / "gitdir")).resolve(strict=True) != pointer
        or _overlap(work, common)
        or _overlap(scratch, common)
    ):
        raise _Declined("unexpected worktree registration")
    return common.parent


def _private_paths(work: Path, *, exclude_links: bool) -> list[Path]:
    """Exclude private inputs, nested checkouts and multiply linked files beneath the source read grant."""
    private = []

    def unreadable(error: OSError) -> None:
        raise error

    for directory, directories, files in os.walk(work, onerror=unreadable):
        root = Path(directory)
        if root != work and any(name.casefold() == ".git" for name in (*directories, *files)):
            private.append(root)
            directories.clear()
            continue
        retained = []
        for is_directory, names in ((True, directories), (False, files)):
            for name in names:
                path = root / name
                folded = name.casefold()
                if folded in _PRIVATE or folded == ".env" or folded.startswith(".env."):
                    private.append(path)
                elif is_directory:
                    if exclude_links and path.is_symlink():
                        private.append(path)
                    else:
                        retained.append(name)
                else:
                    entry = path.lstat()
                    regular_input = stat.S_ISREG(entry.st_mode) or (not exclude_links and stat.S_ISLNK(entry.st_mode))
                    if entry.st_nlink > 1 or not regular_input:
                        private.append(path)
        directories[:] = retained
    return private


def _lookup_chain(path: Path) -> set[Path]:
    """List the directories and symbolic links visited while resolving one absolute path.

    Seatbelt checks metadata on every symbolic link a lookup follows, and a confined process stats ancestor
    directories directly when it computes a real path or its working directory. The final resolved entry is excluded:
    its own read grant decides access. A missing component ends link expansion but keeps its visited ancestors.

    Example:
        >>> sorted(entry.as_posix() for entry in _lookup_chain(Path("/nonexistent-fixture/child")))
        ['/', '/nonexistent-fixture']
    """
    visited: set[Path] = set()
    current = Path(path.anchor)
    pending = list(path.parts[1:])
    links = 0
    while pending:
        part = pending.pop(0)
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        visited.add(current)
        candidate = current / part
        if not candidate.is_symlink():
            current = candidate
            continue
        links += 1
        if links > _MAX_LINKS:
            raise _Declined("symbolic link loop in sandbox grant")
        visited.add(candidate)
        target = Path(os.readlink(candidate))
        if target.is_absolute():
            current = Path(target.anchor)
            pending[:0] = target.parts[1:]
        else:
            pending[:0] = target.parts
    return visited


def _filter(kind: str, path: str | Path) -> str:
    """Quote a kernel path as a single SBPL string without introducing profile syntax."""
    value = os.fspath(path)
    if not value.isprintable() or '"' in value or "\\" in value:
        raise _Declined("unsupported sandbox path")
    return f'({kind} "{value}")'


def _probe(profile: str) -> bool:
    """Distinguish known nested sandbox unavailability from unexpected native launch or policy failures."""
    # A nested Seatbelt may forbid sandbox_apply. Probe only Apple's true, never the contributed command.
    try:
        probe = subprocess.run(  # noqa: S603
            [str(_SANDBOX), "-p", profile, "/usr/bin/true"],
            cwd="/",
            env={},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
    except OSError as error:
        # Keep process-creation errors distinct from optional profile files missing during preparation.
        raise RuntimeError(f"worktree-sandbox-probe-failed: launch: {error}") from error
    if probe.returncode == 71 and probe.stderr.strip() == b"sandbox-exec: sandbox_apply: Operation not permitted":
        return False
    if probe.returncode:
        raise RuntimeError(f"worktree-sandbox-probe-failed: exit {probe.returncode}")
    return True


@functools.lru_cache(maxsize=1)
def native_available() -> bool:
    """Probe process-local Seatbelt capability before traversing a checkout or inspecting tool configuration."""
    if sys.platform != "darwin" or not _SANDBOX.is_file():
        return False
    roots = " ".join(_filter("subpath", p) for p in _SYSTEM)
    return _probe(
        '(version 1)(deny default)(import "dyld-support.sb")(allow sysctl-read)(allow file-read-metadata)'
        '(allow process-exec file-read* file-map-executable (literal "/usr/bin/true"))'
        f"(allow file-read* file-map-executable {roots})"
    )


def _launch(
    executable: Path,
    arguments: list[str],
    work: Path,
    scratch: Path,
    environment: dict[str, str],
    roots: tuple[Path, ...] = (),
    files: tuple[Path, ...] = (),
    *,
    aliases: tuple[Path, ...] = (),
    complete_source: bool = False,
) -> Launch | str:
    """Probe the native policy with a system executable before returning a single confined launch.

    ``aliases`` are unresolved spellings the workload follows to granted content, such as a virtualenv interpreter link
    or a Homebrew ``opt`` link. They gain only lookup metadata, never content. A declined profile returns its fixed
    reason.
    """
    if any(_overlap(root, scratch) or _overlap(root, work) for root in (*roots, *files, executable)):
        return GRANT_OVERLAP
    read = " ".join(
        [*(_filter("subpath", p) for p in (*_SYSTEM, *roots, work)), *(_filter("literal", p) for p in files)]
    )
    # Metadata stays private outside the grants: only lookup ancestors and followed links become visible,
    # so a confined test cannot stat credentials or sibling checkouts. Ungranted reads report ENOENT
    # rather than EPERM, hiding existence as well as size and times, and discovery code (pytest
    # collection, upward config searches) treats a committed link that escapes the grants as broken
    # instead of failing. Seatbelt ranks operation-specific rules above file-read*, so that default
    # must stay a file-read* rule: a file-read-metadata default would override the file-read* grants
    # and hide the runtime from dyld. Filtered rules outrank it in either order. Private entries inside
    # the worktree keep metadata after their content deny, so collection can list but not read them.
    granted = (*map(Path, _SYSTEM), *roots, work, *files, scratch, executable, *_DEVICES, *aliases)
    lookup = sorted({entry for path in granted for entry in _lookup_chain(path)})
    metadata = " ".join(_filter("literal", p) for p in lookup)
    private = _private_paths(work, exclude_links=complete_source)
    # Ruff discovers files itself. Hidden or redirected inputs could silently change the lint/format result.
    if complete_source and any(path != work / ".git" for path in private):
        return RUFF_PRIVATE_INPUT
    denied = " ".join(_filter("subpath", p) for p in private)
    profile = f"""(version 1)
(deny default)
(import "dyld-support.sb")
(allow signal (target self))
(allow sysctl-read)
(deny file-read* (with errno ENOENT))
(allow file-read* file-map-executable {read})
(deny file-read* file-map-executable {denied})
(allow file-read-metadata {metadata} {_filter("subpath", work)})
(allow file-read* file-write* (literal "/dev/null") {_filter("subpath", scratch)})
(allow file-read* (literal "/dev/urandom") (literal "/dev/random"))
(allow system-socket (require-all (socket-domain AF_SYSTEM) (socket-protocol 2)))
"""

    def policy(entry: Path) -> str:
        literal = _filter("literal", entry)
        return f"{profile}(allow process-exec file-read* file-map-executable {literal})"

    if len(profile.encode("utf-8")) > 64 * 1024:
        return PROFILE_SIZE
    if not _probe(policy(Path("/usr/bin/true"))):
        return NATIVE_UNAVAILABLE
    return [str(_SANDBOX), "-p", policy(executable), str(executable), *arguments], environment


def _environment(source: dict[str, str], scratch: Path) -> dict[str, str]:
    """Retain the caller's filtered gate inputs and direct temporary output to the confined writable area."""
    env = source.copy()
    env.update(TMPDIR=str(scratch), TEMP=str(scratch), TMP=str(scratch), XDG_CACHE_HOME=str(scratch))
    return env


def prepare_pytest(argv: list[str], worktree: Path, scratch: Path, environment: dict[str, str]) -> Launch | str:
    """Prepare a digest-bound pytest child using the selected virtualenv and its exact Homebrew framework runtime.

    Returns the confined launch, or the fixed reason this invocation keeps its standard route.
    """
    if not native_available():
        return NATIVE_UNAVAILABLE
    try:
        main = _linked_main(worktree, scratch)
        if (
            len(argv) != 10
            or argv[1:5] != ["-B", str(_SHARED / "run_gates.py"), "--_python-tests-child", str(worktree)]
            or argv[8] != argv[0]
            or Path(argv[5]).parent != scratch
        ):
            return PYTEST_COMMAND
        interpreter = Path(argv[0])
        venv = interpreter.parent.parent
        framework = Path(sys.base_prefix).resolve(strict=True)
        cellar = next((root for root in _CELLARS if framework.is_relative_to(root)), None)
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        runtime = framework / "Resources/Python.app/Contents/MacOS/Python"
        if (
            not cellar
            or framework.relative_to(cellar).parts[0] != f"python@{version}"
            or not runtime.is_file()
            or interpreter.parent != venv / "bin"
            or venv != venv.resolve(strict=True)
            or not (venv / "pyvenv.cfg").is_file()
            or venv not in {Path(sys.prefix).resolve(), main / ".venv"}
            or interpreter.resolve(strict=True) != framework / "bin" / f"python{version}"
        ):
            return PYTEST_RUNTIME
        binding = json.loads(argv[9])["tracked_inventory"]
        digest = binding["sha256"]
        inventory = Path(binding["path"])
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return TRACKED_INVENTORY
        if inventory != scratch.parent / f"tracked-source-{digest}.bin":
            return TRACKED_INVENTORY
        observed = inventory.lstat()
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            return TRACKED_INVENTORY
        content = inventory.read_bytes()
        root, head, _ = content.split(b"\0", 2)
        if (
            hashlib.sha256(content).hexdigest() != digest
            or root != os.fsencode(worktree)
            or head.decode("ascii") != binding["head"]
            or not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", head)
        ):
            return TRACKED_INVENTORY
        site_packages = venv / "lib" / f"python{version}" / "site-packages"
        if site_packages != site_packages.resolve(strict=True) or not site_packages.is_dir():
            return PYTEST_RUNTIME
        runtime_roots = [framework, site_packages, _SHARED]
        # The interpreter, base prefix and library install names reach granted content through Homebrew links.
        aliases = [interpreter, Path(sys.base_prefix)]
        for formula in ("openssl@3", "xz", "mpdecimal"):
            alias = cellar.parent / "opt" / formula / "lib"
            library = alias.resolve(strict=True)
            if not library.is_relative_to(cellar / formula):
                return RUNTIME_LIBRARY
            runtime_roots.append(library)
            aliases.append(alias)
        env = _environment(environment, scratch)
        if "PYTHONPATH" in environment:
            for raw in environment["PYTHONPATH"].split(os.pathsep):
                path = (worktree / raw).resolve(strict=True)
                if not any(path.is_relative_to(root) for root in (worktree, site_packages, _SHARED)):
                    return PYTHONPATH_ESCAPE
            env["PYTHONPATH"] = environment["PYTHONPATH"]
        for key, path in (("COVERAGE_FILE", scratch / ".coverage"), ("CODEX_RIG_PYTEST_ARTIFACTS", scratch)):
            if key in environment and Path(environment[key]) != path:
                return ARTIFACT_ESCAPE
            env[key] = str(path)
        env.update(__PYVENV_LAUNCHER__=str(interpreter), PYTHONDONTWRITEBYTECODE="1")
        return _launch(
            runtime,
            argv[1:],
            worktree,
            scratch,
            env,
            tuple(runtime_roots),
            (inventory, venv / "pyvenv.cfg"),
            aliases=tuple(aliases),
        )
    except (FileNotFoundError, NotADirectoryError, ValueError, TypeError, KeyError) as error:
        return _decline_reason(error)


def prepare_ruff(
    executable: Path, arguments: list[str], worktree: Path, scratch: Path, environment: dict[str, str]
) -> Launch | str:
    """Prepare a native Ruff check from a selected environment or the Homebrew Ruff keg.

    Returns the confined launch, or the fixed reason this check keeps its standard route.
    """
    if arguments not in (["check", "--no-fix", "."], ["format", "--check", "."]):
        return RUFF_COMMAND
    if not native_available():
        return NATIVE_UNAVAILABLE
    try:
        main = _linked_main(worktree, scratch)
        binary = executable.resolve(strict=True)
        env_roots = [main / ".venv"]
        if sys.prefix != sys.base_prefix:
            env_roots.append(Path(sys.prefix).resolve(strict=True))
        known_venv = any(binary == root / "bin/ruff" and (root / "pyvenv.cfg").is_file() for root in env_roots)
        known_keg = any(binary.match(str(cellar / "ruff/*/bin/ruff")) for cellar in _CELLARS)
        if not executable.is_absolute() or executable.name != "ruff" or not (known_venv or known_keg):
            return RUFF_INSTALL
        with binary.open("rb") as stream:
            if stream.read(4) not in {
                bytes.fromhex(magic) for magic in ("cffaedfe", "feedfacf", "cafebabe", "cafebabf")
            }:
                return RUFF_BINARY
        env = _environment(environment, scratch)
        env["RUFF_CACHE_DIR"] = str(scratch / "ruff-cache")
        return _launch(binary, arguments, worktree, scratch, env, aliases=(executable,), complete_source=True)
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        return _decline_reason(error)
