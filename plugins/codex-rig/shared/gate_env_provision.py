#!/usr/bin/env python3
"""Plan and build a reviewer-owned gate environment from the review base's trusted dependency declarations.

## Purpose

A missing Python package is an environment gap, never a reason to leave a review or remediation gate unverified. This
helper turns the reviewed change's dependency declarations into one disposable virtual environment that the canonical
gate runner then uses through ``run_gates.py --project-env``, and a writable pre-commit and uv cache that it uses
through ``run_gates.py --tool-cache``. Both live outside the reviewed checkout and outside the project's own ``.venv``.

## Scope

Trust comes from the contributor-code guard's own dependency report (``untrusted_code_guard.py``), never from a second
parser. A package the review base already declares, resolved from the default index with a plain version range, is
trusted by name and installed from wheels only. A package only the head declares, a direct URL, VCS or path reference,
or a specifier that is not a version range stays with the contributor-code gate and is listed under ``gated`` with its
reason. Index overrides, requirement includes, unparsable manifests, base or user index routing block the whole plan,
because a name routed off the default index could be squatted there. The project itself is never installed: its build
backend is contributor code. Source imports come from the checkout through the ``PYTHONPATH`` in
``gate_environment`` instead.
The pre-commit hook cache is seeded only when ``.pre-commit-config.yaml`` is unchanged between base and head, and
then from a copy of the base configuration bytes outside the checkout, never from the working-tree file.

The environment root must be a real directory owned by, and writable only by, the invoking user, inside a parent no
other user can rename it out of: another user who could plant an interpreter or a cache entry there would run code as
the reviewer. A missing root is created owner-only. A cached environment is reused only when it is the invoking
user's real directory, others cannot write it (or cannot enter the root at all), and it still holds its interpreter.

## Usage

``python gate_env_provision.py plan --repo <checkout> --base <sha> --head <sha> --python <abs-interpreter>
--env-root <abs-dir-outside-checkout> --out <plan.json> [--group <name>]... [--extra <name>]...
[--requirements <path>]... [--exclude <name>]...`` probes the selected interpreter and writes the plan offline.
The lane is the project's runtime dependencies plus the named groups, extras and requirement files of the test or CI
lane; ``available_lanes`` lists what the project declares.
``python gate_env_provision.py apply --repo <checkout> --base <sha> --head <sha> --python <abs-interpreter>
--env-root <abs-dir-outside-checkout> --plan <plan.json> --plan-digest <approved-digest> --out <result.json>``
runs the planned uv and pre-commit commands. The install step reads the default package index, so in a sandbox
without network the workflow runs ``apply`` under its existing networked-command approval.

``apply`` checks the file against the approved digest and the explicit checkout, base, head, interpreter and environment
root before running an interpreter or creating an environment root. It re-derives the plan using those arguments and
the lane bound by the digest, and refuses with cause ``plan-modified`` unless the derivation equals the file.
It runs the derived commands under an allowlisted environment that omits caller ``UV_*``, ``PIP_*`` and credential
variables.

## Outputs

The plan records the environment and cache paths, the trusted requirement strings, the gated names with reasons, any
blocking cause, the exact commands and its ``plan_digest``. Every path is in the host's native form.
``run_gates_args`` (``--tool-cache``) applies to every gate run; ``worktree_run_gates_args`` (``--project-env``)
applies only to a worktree-bound run, which ``run_gates.py`` requires for that option; ``gate_environment`` gives the
``VIRTUAL_ENV``, the directory to put first on ``PATH`` and the ``PYTHONPATH`` that makes the uninstalled project
importable from the checkout, for the execution tool's environment. The apply result records each step's exit status
and a fixed cause for any failure. Place ``--out`` in the retained review or remediation run directory. Apply also
writes the result there as ``gate-provisioning.json``. A plan mismatch reports the refusal on stdout when the
output directory does not exist.

## Failure

Exit 0 means a ready plan or environment, 1 a blocked plan, a refused apply or a failed or partial build (its cause is
recorded), and 2 invalid input such as an environment root inside the checkout or one not private to the caller.

## Used by

Code Review and Code Remediate use the plan to prepare a gate environment. ``run_gap_records.py`` reads the retained
apply receipt to distinguish a failed provisioning attempt from a skipped step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.pycache_prefix = str(Path(__file__).resolve())
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import untrusted_code_guard as guard  # noqa: E402

try:
    from packaging.requirements import InvalidRequirement, Requirement
except ImportError:
    try:
        from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
    except ImportError:
        Requirement = None

#: Version of the plan and result records this helper writes.
SCHEMA_VERSION = 2
#: The only index a trusted requirement is fetched from.
DEFAULT_INDEX = "https://pypi.org/simple"
#: A head specifier enters a command only when it is a plain PEP 440 version range written with these characters.
_VERSION_RANGE = re.compile(r"(?:[<>=!~][A-Za-z0-9 .,*+!_=<>~-]*)?")
#: Extras attached to a declared name, which pull only further default-index packages.
_EXTRAS = re.compile(r"\[[A-Za-z0-9,_ .-]*\]")
#: An environment marker, which uv evaluates for this interpreter; no URL, path or option characters.
_MARKER = re.compile(r"[A-Za-z0-9_ .'\"<>=!~(),-]+")
#: The hook configuration whose base bytes seed the pre-commit cache.
HOOK_CONFIG = ".pre-commit-config.yaml"
#: User-level index routing that sends names off the default index (presence alone counts, even when empty).
USER_ROUTING_VARIABLES = (
    "UV_INDEX",
    "UV_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_CONFIG_FILE",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "PIP_NO_INDEX",
    "PIP_CONFIG_FILE",
)
#: Host uv settings with understood effects that leave default-index provenance unchanged.
_NEUTRAL_UV_ENV = frozenset({
    "UV_CACHE_DIR", "UV_NO_CACHE", "UV_NO_PROGRESS", "UV_NO_CONFIG", "UV_LINK_MODE", "UV_PYTHON",
    "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON", "UV_PROJECT_ENVIRONMENT", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR",
    "UV_PYTHON_INSTALL_DIR", "UV_COMPILE_BYTECODE", "UV_NO_SYNC", "UV_FROZEN", "UV_LOCKED", "UV_VENV_CLEAR",
    "UV_RUN_RECURSION_DEPTH",
})  # fmt: skip
_NEUTRAL_PIP_ENV = frozenset(
    {
        "PIP_CACHE_DIR",
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "PIP_NO_CACHE_DIR",
        "PIP_NO_INPUT",
        "PIP_PROGRESS_BAR",
    }
)
#: Variables an apply step's tools inherit: platform process settings, locale, temp, proxies and certificates (needed
#: to reach the index). Everything else is dropped, credentials and every caller ``UV_*`` or ``PIP_*`` setting
#: included, so no override such as ``UV_OVERRIDE`` or ``UV_NO_BINARY`` steers resolution; each step adds its own
#: cache settings.
INSTALLER_ENVIRONMENT = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT",
    "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HTTP_PROXY",
    "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy", "SSL_CERT_FILE",
    "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
)  # fmt: skip
#: Causes an apply step or a blocked plan can record.
CAUSES = (
    "dependency-unparsed",
    "requirement-include",
    "index-override",
    "base-index-routing",
    "user-index-routing",
    "plan-modified",
    "plan-invalid",
    "env-root-unsafe",
    "uv-unavailable",
    "network-unavailable",
    "no-compatible-wheel",
    "resolver-conflict",
    "permission-denied",
    "hook-config-changed",
    "pre-commit-unavailable",
    "install-failed",
)
_STEP_CAUSES = (
    (
        "network-unavailable",
        re.compile(
            r"Could not resolve host|dns error|failed to lookup address|Network is unreachable|Failed to fetch", re.I
        ),
    ),
    ("no-compatible-wheel", re.compile(r"no wheels? with a matching|has no wheels|no matching distribution", re.I)),
    ("resolver-conflict", re.compile(r"No solution found|unsatisfiable|ResolutionImpossible", re.I)),
    ("permission-denied", re.compile(r"Permission denied|Operation not permitted", re.I)),
)


class ApplyRefused(Exception):
    """A plan ``apply`` will not run, with the fixed cause the result records."""

    def __init__(self, cause: str, detail: str) -> None:
        """Keep the cause from :data:`CAUSES` and a one-line detail."""
        super().__init__(detail)
        self.cause = cause


def requirement_text(name: str, rest: str) -> str | None:
    """Return the installable requirement for one trusted declaration, or ``None`` when it is not a version range.

    Example:
        >>> requirement_text("numpy", ">=1.21,<3")
        'numpy>=1.21,<3'
        >>> requirement_text("opencv-python", "[contrib] >=4.8 ; python_version >= '3.10'")
        "opencv-python[contrib]>=4.8; python_version >= '3.10'"
        >>> requirement_text("pkg", "==1.0 --hash=sha256:abc") is None
        True
    """
    if Requirement is None:
        return None
    try:
        parsed = Requirement(f"{name} {rest}")
    except InvalidRequirement:
        return None
    if parsed.url is not None or guard._norm(parsed.name) != name:
        return None
    return _render_requirement(name, rest)


def _render_requirement(name: str, rest: str) -> str | None:
    """Render a validated plain requirement while retaining the declared marker and version order."""
    specifier, _, marker = rest.strip().partition(";")
    match = _EXTRAS.match(specifier)
    extras = ""
    if match:
        extras = match.group(0).replace(" ", "")
        specifier = specifier.strip()[match.end() :]
    specifier = specifier.strip()
    marker = marker.strip()
    if not _VERSION_RANGE.fullmatch(specifier) or "--" in specifier or (marker and not _MARKER.fullmatch(marker)):
        return None
    return f"{name}{extras}{specifier.replace(' ', '')}" + (f"; {marker}" if marker else "")


def _outside(path: Path, checkout: Path) -> bool:
    """Return whether ``path`` lies outside the checkout, which also keeps it outside the project's ``.venv``."""
    return not path.resolve().is_relative_to(checkout.resolve())


def private_directory_problem(path: Path, *, enclosed: bool = False) -> str | None:
    """Return why ``path`` is not a directory only the invoking user can write, or ``None`` when it is one.

    The directory itself must be a real directory (not a symbolic link), owned by the invoking user and not writable by
    group or others. Its parent must be owned by the invoking user or the superuser and, when others can write it, carry
    the sticky bit, so no other user can rename the directory away and put their own in its place. With ``enclosed``
    the parent admits no other user at all, so the directory's own group and other write bits (a ``002`` umask gives
    every new directory group write) cannot matter and are not checked. Ownership and mode bits are POSIX facts; on
    native Windows only the link and directory checks apply, since its per-user temporary directory already carries an
    access list for that user alone.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     private_directory_problem(Path(directory)) is None
        True
    """
    try:
        info = path.lstat()
        parent = path.parent.stat()
    except OSError as error:
        return f"{path}: {error.strerror or error}"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return f"{path} is not a real directory"
    if not hasattr(os, "getuid"):
        return None
    if info.st_uid != os.getuid() or (info.st_mode & 0o022 and not enclosed):
        return f"{path} is not owned by and writable only by the invoking user"
    shared_parent = parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX
    if parent.st_uid not in {os.getuid(), 0} or shared_parent:
        return f"{path.parent} lets another user replace {path.name}"
    return None


def env_root_problem(env_root: Path, checkout: Path) -> str | None:
    """Create the environment root when missing and return why it cannot hold reviewer environments, if it cannot.

    The root must be absolute, outside the reviewed checkout, not a symbolic link, and private to the invoking user
    (:func:`private_directory_problem`) after its links are resolved. A missing root is created owner-only.
    """
    if not env_root.is_absolute():
        return "invalid-path: --env-root must be absolute"
    if not _outside(env_root, checkout):
        return "invalid-env-root: the environment must live outside the reviewed checkout"
    if env_root.is_symlink():
        return f"unsafe-env-root: {env_root} is a symbolic link"
    try:
        env_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        return f"unsafe-env-root: {env_root}: {error.strerror or error}"
    problem = private_directory_problem(env_root.resolve())
    return f"unsafe-env-root: {problem}" if problem else None


def _user_config_files(environment: dict[str, str], home: Path, platform: str) -> list[Path]:
    """List user and system uv and pip configuration locations for the host."""
    xdg = Path(environment.get("XDG_CONFIG_HOME") or home / ".config")
    files = [xdg / "uv" / "uv.toml", xdg / "pip" / "pip.conf"]
    if platform == "win32":
        files.append(home / "pip" / "pip.ini")
        appdata = Path(environment.get("APPDATA") or home / "AppData" / "Roaming")
        system_drive = environment.get("SystemDrive") or "C:"
        programdata = Path(environment.get("PROGRAMDATA") or Path(f"{system_drive}/ProgramData"))
        for directory in (appdata, programdata):
            files.extend((directory / "uv" / "uv.toml", directory / "pip" / "pip.ini"))
        return files
    config_dirs = (environment.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(os.pathsep)
    files.extend(Path(directory) / "uv" / "uv.toml" for directory in config_dirs if directory)
    files.extend((home / ".pip" / "pip.conf", home / ".config/pip/pip.conf", Path("/etc/uv/uv.toml")))
    if platform == "darwin":
        files.extend(
            [home / "Library/Application Support/pip/pip.conf", Path("/Library/Application Support/pip/pip.conf")]
        )
    else:
        files.append(Path("/etc/pip.conf"))
        files.extend(Path(directory) / "pip" / "pip.conf" for directory in config_dirs if directory)
    return files


_PIP_CONFIG_PROBE = """\
import json, site, sys, sysconfig
from pathlib import Path
active = sys.argv[1]
install = sysconfig.get_config_var("prefix") or sys.base_prefix
roots = [*site.getsitepackages([active, sys.base_prefix]), sysconfig.get_path("purelib")]
paths = [root for root in roots if root and (Path(root) / "pip").is_dir()]
if not paths:
    import ensurepip
    wheel_root = Path(sysconfig.get_config_var("WHEEL_PKG_DIR") or Path(ensurepip.__file__).parent / "_bundled")
    paths = [str(path) for path in sorted(wheel_root.glob("pip-*.whl"), reverse=True)]
sys.path.extend(paths)
from pip._internal.configuration import get_configuration_files
files = set()
for sys.prefix in (active, install):
    for sources in get_configuration_files().values():
        files.update(sources)
print(json.dumps(sorted(files)))
"""


def _pip_config_files(python: str, prefix: Path, environment: dict[str, str]) -> list[Path]:
    """Ask the selected Python's pip for config paths without running site startup hooks."""
    cwd = Path(__file__).resolve().parent
    result = subprocess.run(  # noqa: S603 - selected interpreter, isolated standard library and trusted pip code
        [python, "-I", "-S", "-c", _PIP_CONFIG_PROBE, str(prefix)],
        cwd=cwd, env=environment, capture_output=True, text=True, check=True, timeout=30,
    )  # fmt: skip
    paths = json.loads(result.stdout)
    if not isinstance(paths, list) or not all(isinstance(path, str) and Path(path).is_absolute() for path in paths):
        raise ValueError("pip did not return absolute configuration paths")
    return [Path(path) for path in paths]


def _user_config_routing(environment: dict[str, str], python: str, prefix: Path) -> list[str]:
    """Report only the paths of host configs that route packages or cannot be read safely."""
    home_key = "USERPROFILE" if sys.platform == "win32" else "HOME"
    home = Path(environment.get(home_key) or Path.home())
    routed: list[str] = []
    files = _user_config_files(environment, home, sys.platform)
    try:
        files.extend(_pip_config_files(python, prefix, environment))
    except (OSError, ValueError, subprocess.SubprocessError):
        routed.append("pip configuration discovery unavailable")
    for path in dict.fromkeys(files):
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, UnicodeDecodeError):
            routed.append(str(path))
            continue
        marks, _ = guard._installer_config_routing(path.name, text)
        if any(not label.startswith(guard.UV_EXPLICIT_INDEX_MARK) for label, _ in marks):
            routed.append(str(path))
    return routed


def _blocked(
    report: dict[str, Any], base_marks: set[str], lane_marks: set[str], host_routing: list[str]
) -> dict[str, str] | None:
    """Return the first fail-closed reason the whole plan cannot proceed, or ``None``."""
    if report["unparsed"]:
        return {"cause": "dependency-unparsed", "detail": ", ".join(report["unparsed"])}
    includes = set(report["includes"]) | {mark for mark in base_marks | lane_marks if mark.startswith("include: ")}
    if includes:
        return {"cause": "requirement-include", "detail": "; ".join(sorted(includes))}
    overrides = sorted(
        mark for mark in {*report["index_overrides"], *lane_marks} if not mark.startswith(guard.UV_SOURCE_MARK)
    )
    if overrides:
        return {"cause": "index-override", "detail": "; ".join(overrides)}
    # Editable project lines are never installed here. Named source bindings are handled by _classify.
    routing = sorted(mark for mark in base_marks if not mark.startswith(("editable: ", guard.UV_SOURCE_MARK)))
    if routing:
        return {"cause": "base-index-routing", "detail": "; ".join(routing)}
    if host_routing:
        # Report provenance without exposing configured values that could contain credentials.
        return {"cause": "user-index-routing", "detail": ", ".join(host_routing)}
    return None


def _host_routing(environment: dict[str, str], python: str, prefix: Path) -> list[str]:
    """Refuse installer environment settings unless their routing effects are understood."""
    routed = sorted(
        name
        for name in environment
        if name in USER_ROUTING_VARIABLES
        or (name.upper().startswith("UV_") and name.upper() not in _NEUTRAL_UV_ENV)
        or (name.upper().startswith("PIP_") and name.upper() not in _NEUTRAL_PIP_ENV)
    )
    return routed or _user_config_routing(environment, python, prefix)


def _group_lines(groups: dict[str, Any], name: str, seen: frozenset[str] = frozenset()) -> list[str]:
    """Return one PEP 735 dependency group's requirement strings, following ``include-group`` entries once."""
    if name in seen or not isinstance(groups.get(name), list):
        raise ValueError(f"dependency-group-unavailable:{name}")
    lines: list[str] = []
    for entry in groups[name]:
        if isinstance(entry, str):
            lines.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("include-group"), str):
            lines.extend(_group_lines(groups, entry["include-group"], seen | {name}))
    return lines


def _lane(
    repo: str, head: str, groups: list[str], extras: list[str], requirement_files: list[str]
) -> tuple[dict[str, tuple[str, bool]], dict[str, list[str]], set[str]]:
    """Select the test lane's declarations: runtime dependencies plus the named groups, extras and requirement files.

    Installing every declared table would pull documentation tools, heavy optional integrations and mutually exclusive
    backends, so only the lane the caller names is planned. Each line is read with the guard's own requirement parser;
    the project's own name is skipped because the project is never installed.
    """
    data = guard._toml_loads((guard._blob(repo, head, "pyproject.toml") or b"").decode("utf-8", "replace")) or {}
    project = guard._table(data.get("project"))
    optional, declared_groups = (
        guard._table(project.get("optional-dependencies")),
        guard._table(data.get("dependency-groups")),
    )
    lines = [line for line in guard._strings(project.get("dependencies"))]
    for extra in extras:
        if not isinstance(optional.get(extra), list):
            raise ValueError(f"optional-dependency-unavailable:{extra}")
        lines.extend(guard._strings(optional[extra]))
    for group in groups:
        lines.extend(_group_lines(declared_groups, group))
    selected: dict[str, tuple[str, bool]] = {}
    marks: set[str] = set()
    for path in requirement_files:
        text = guard._blob(repo, head, path)
        if text is None:
            raise ValueError(f"requirements-file-unavailable:{path}")
        declared, options = _lane_file(text)
        for name, spec in declared.items():
            selected[name] = guard._preferred_declaration(selected.get(name), spec)
        marks.update(options)
    for line in lines:
        parsed = guard._requirement(line)
        if parsed:
            name, rest, direct = parsed
            selected[name] = guard._preferred_declaration(selected.get(name), (rest, direct))
    selected.pop(guard._norm(str(project.get("name", ""))), None)
    return selected, {"groups": sorted(declared_groups), "extras": sorted(optional)}, marks


def _lane_file(data: bytes) -> tuple[dict[str, tuple[str, bool]], set[str]]:
    """Read a selected requirements blob or mark it unmodeled when decoding fails."""
    try:
        declared, options = guard._from_requirements_txt(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return {}, {"unmodeled requirements input"}
    return declared, {guard.requirement_routing_label(option) for option in options}


def _classify(
    lane: dict[str, tuple[str, bool]], base_deps: dict[str, tuple[str, bool]], exclude: list[str], routed: set[str]
) -> tuple[list[str], list[dict[str, str]]]:
    """Split the lane into installable requirement strings and gated names with their reasons."""
    excluded = {guard._norm(name) for name in exclude}
    requirements: list[str] = []
    gated: list[dict[str, str]] = []
    for name, (rest, direct) in sorted(lane.items()):
        if name in excluded:
            gated.append({"name": name, "reason": "excluded by the caller"})
        elif name in routed:
            gated.append({"name": name, "reason": "index-routing"})
        elif name not in base_deps:
            gated.append({"name": name, "reason": "new dependency, verify provenance"})
        elif direct or base_deps[name][1]:
            gated.append({"name": name, "reason": "non-index dependency reference"})
        elif requirement_text(name, base_deps[name][0]) is None:
            gated.append({"name": name, "reason": "base specifier not a version range"})
        elif (text := requirement_text(name, rest)) is not None:
            requirements.append(text)
        else:
            gated.append({"name": name, "reason": "head specifier not a version range"})
    return requirements, gated


def _hook_step(
    arguments: argparse.Namespace, tree: list[str], changed: dict[str, str], env_root: Path, cache_env: dict[str, str]
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Return the hook-seeding command and any reason the hook cache is not seeded.

    The command reads a copy of the base configuration that ``apply`` writes into the private environment root, named by
    the digest of those bytes, so the seeded hooks are exactly the configuration compared between base and head.
    """
    if HOOK_CONFIG not in tree:
        return None, None
    if HOOK_CONFIG in changed:
        return None, {"cause": "hook-config-changed", "detail": "the reviewed change edits the hook configuration"}
    blob = guard._blob(str(arguments.repo), arguments.base, HOOK_CONFIG) or b""
    config_copy = env_root / f"pre-commit-config-{hashlib.sha256(blob).hexdigest()[:16]}.yaml"
    argv = [shutil.which("pre-commit") or "pre-commit", "install-hooks", "--config", str(config_copy)]
    return {"id": "hooks", "argv": argv, "env": cache_env, "cwd": str(arguments.repo.resolve())}, None


def plan_digest(plan: dict[str, Any]) -> str:
    """Return the digest the plan action records over every other field of the plan.

    Example:
        >>> plan_digest({"a": 1}) == plan_digest({"a": 1, "plan_digest": "x"})
        True
    """
    body = {key: value for key, value in plan.items() if key != "plan_digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def _plan_inputs(arguments: argparse.Namespace) -> dict[str, str]:
    """Return the trust inputs in the form stored in the plan."""
    return {
        "repo": str(arguments.repo.resolve()),
        "base": arguments.base,
        "head": arguments.head,
        # Invoking a venv's symlink by its resolved target would skip that venv and its pip site settings.
        "python": os.path.abspath(arguments.python),
        "env_root": str(arguments.env_root.resolve()),
    }


def _lock_marks(text: str, path: str) -> set[str] | None:
    """Scope non-registry lock sources to their names and refuse alternate registries for the whole plan."""
    data = guard._toml_loads(text)
    if data is None or type(data.get("version")) is not int or data["version"] != 1:
        return None
    packages = data.get("package", [])
    if not isinstance(packages, list):
        return None
    marks = set()
    for package in packages:
        entry = guard._table(package)
        name = entry.get("name")
        if not isinstance(name, str) or not guard._REQ_NAME.fullmatch(name):
            return None
        source = guard._table(entry.get("source"))
        registry = source.get("registry")
        if "registry" in source and (not isinstance(registry, str) or registry.rstrip("/") != DEFAULT_INDEX):
            marks.add(f"{path}: non-default registry in locked package sources")
        elif source.keys() != {"registry"}:
            marks.add(f"{guard.UV_SOURCE_MARK}{guard._norm(name)}")
    return marks


def _read_lock_routing(repo: str, sha: str, tree: list[str]) -> tuple[set[str], list[str]]:
    """Read lock routing from one committed tree without consulting the checkout."""
    marks, unparsed = set(), []
    for path in tree:
        name = Path(path).name.lower()
        if name in {"pipfile.lock", "poetry.lock", "pdm.lock", "pylock.toml"}:
            marks.add(f"{path}: locked sources are not modeled")
        if name != "uv.lock":
            continue
        data = guard._blob(repo, sha, path)
        try:
            got = _lock_marks(data.decode("utf-8"), path) if data is not None else None
        except UnicodeDecodeError:
            got = None
        if got is None:
            unparsed.append(path)
        else:
            marks.update(got)
    return marks, unparsed


def _project_provenance(
    repo: str, arguments: argparse.Namespace, tree: list[str], changed: dict[str, str]
) -> tuple[dict[str, tuple[str, bool]], set[str], dict[str, Any]]:
    """Gather Python trust and routing evidence from both compared revisions."""
    report, _ = guard._dependency_report(repo, arguments.base, arguments.head, tree, changed)
    base_tree = sorted(guard._tree_entries(repo, arguments.base))
    files = _dependency_files(base_tree)
    base_deps, base_routes, base_unparsed = guard._collect(repo, arguments.base, files)
    head_direct = guard._direct_declarations(repo, arguments.head, _dependency_files(tree))
    base_lock, base_lock_unparsed = _read_lock_routing(repo, arguments.base, base_tree)
    head_lock, head_lock_unparsed = _read_lock_routing(repo, arguments.head, tree)
    report["unparsed"] = sorted({*report["unparsed"], *base_unparsed, *base_lock_unparsed, *head_lock_unparsed})
    head_scan = _scan_project_routing(repo, arguments.head, tree, arguments.requirements)
    base_scan = (
        head_scan
        if arguments.base == arguments.head
        else _scan_project_routing(repo, arguments.base, base_tree, arguments.requirements)
    )
    report["index_overrides"] = sorted(
        set(report["index_overrides"]) | (head_lock - base_lock) | (head_scan - base_scan)
    )
    report["python_direct"] = {name for ecosystem, name in head_direct if ecosystem == "python"}
    base_marks = {label for _, label, _ in base_routes} | base_lock | base_scan
    for path in arguments.requirements:
        # The caller can select a requirements file whose basename is outside manifest discovery.
        text = guard._blob(repo, arguments.base, path)
        if text is not None:
            base_marks.update(_lane_file(text)[1])
    return {name: dep for (ecosystem, name), dep in base_deps.items() if ecosystem == "python"}, base_marks, report


def _dependency_files(tree: list[str]) -> list[str]:
    """Select tree files whose dependency declarations the guard reads."""
    return [
        path for path in tree if guard.is_dependency_manifest(path) and Path(path).name.lower() not in guard.LOCKFILES
    ]


def _scan_project_routing(repo: str, sha: str, tree: list[str], selected_files: list[str]) -> set[str]:
    """Use the shared guard to check routing in workflows and other unmodeled files."""
    modeled = {
        path
        for path in _dependency_files(tree)
        if Path(path).name.lower() not in {"setup.cfg", "setup.py", "package.json"}
    }
    modeled.update(path for path in tree if Path(path).name.lower() in guard.LOCKFILES)
    return guard.project_routing_scan(repo, sha, modeled | set(selected_files))


def _probe_python(python: str) -> tuple[str, Path, Path]:
    """Read the interpreter's full version, active prefix and underlying executable."""
    probe = "import json, sys; print(json.dumps([sys.version.split()[0], sys.prefix, sys.executable, sys._base_executable]))"
    done = subprocess.run(  # noqa: S603 - explicit selected interpreter or owned cached environment
        [python, "-I", "-S", "-c", probe], capture_output=True, text=True, check=True, timeout=30
    )
    version, prefix, launcher, executable = json.loads(done.stdout)
    if not all(isinstance(value, str) and value for value in (version, prefix, launcher, executable)):
        raise ValueError("Python probe did not report an interpreter identity")
    # Skip executable site hooks. Before Python 3.14, site also selects the venv prefix from pyvenv.cfg.
    launcher_dir = Path(launcher).parent
    venv_prefix = launcher_dir.parent
    if (launcher_dir / "pyvenv.cfg").is_file() or (venv_prefix / "pyvenv.cfg").is_file():
        prefix = str(venv_prefix)
    return version, Path(prefix).resolve(), Path(executable).resolve(strict=True)


def _environment_key(python: str, version: str, executable: Path, requirements: list[str]) -> str:
    """Identify the chosen launcher, the installed Python binary and the dependency set."""
    binary = executable.stat()
    identity = [executable.as_posix(), binary.st_dev, binary.st_ino, binary.st_size, binary.st_mtime_ns]
    selected = Path(python).as_posix()
    digest = hashlib.sha256(json.dumps([selected, version, identity, "--no-deps", requirements]).encode()).hexdigest()[
        :16
    ]
    return f"py{version.replace('.', '')}-{digest}"


def build_plan(arguments: argparse.Namespace, environment: dict[str, str]) -> dict[str, Any]:
    """Classify declared dependencies and return the environment plan without touching the network."""
    inputs = _plan_inputs(arguments)
    repo, checkout = inputs["repo"], Path(inputs["repo"])
    tree = sorted(guard._tree_entries(repo, arguments.head))
    changed = guard._changed_paths(repo, arguments.base, arguments.head)
    base_deps, base_marks, report = _project_provenance(repo, arguments, tree, changed)
    lane, available, lane_marks = _lane(repo, arguments.head, arguments.group, arguments.extra, arguments.requirements)
    routed = {
        mark.removeprefix(guard.UV_SOURCE_MARK)
        for mark in {*base_marks, *report["index_overrides"]}
        if mark.startswith(guard.UV_SOURCE_MARK) and not mark.startswith(guard.UV_EXPLICIT_INDEX_MARK)
    }
    for name in lane.keys() & report["python_direct"]:
        lane[name] = (lane[name][0], True)
    requirements, gated = _classify(lane, base_deps, arguments.exclude, routed)
    python = inputs["python"]
    version, prefix, executable = _probe_python(python)
    blocked = _blocked(report, base_marks, lane_marks, _host_routing(environment, python, prefix))
    if blocked:
        requirements = []
    env_root = Path(inputs["env_root"])
    env = env_root / _environment_key(python, version, executable, requirements)
    env_python = env / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    cache_env = {"UV_CACHE_DIR": str(env_root / "uv"), "PRE_COMMIT_HOME": str(env_root / "pre-commit")}
    uv = shutil.which("uv") or "uv"
    commands = [{"id": "venv", "argv": [uv, "venv", "--no-config", "--python", python, str(env)], "env": cache_env}]
    if requirements:
        install = [
            uv,
            "pip",
            "install",
            "--no-config",
            "--no-deps",
            "--only-binary",
            ":all:",
            "--default-index",
            DEFAULT_INDEX,
        ]
        commands.append(
            {"id": "install", "argv": [*install, "--python", str(env_python), *requirements], "env": cache_env}
        )
    hook_command, hooks = _hook_step(arguments, tree, changed, env_root, cache_env)
    commands += [hook_command] if hook_command else []
    plan = {
        "schema_version": SCHEMA_VERSION,
        **inputs,
        "python_version": version,
        "env": str(env),
        "env_python": str(env_python),
        "lane": {
            "groups": arguments.group,
            "extras": arguments.extra,
            "requirements": arguments.requirements,
            "exclude": arguments.exclude,
        },
        "available_lanes": available,
        "requirements": requirements,
        "gated": gated,
        "hooks": hooks,
        "blocked": blocked,
        "commands": commands,
        "run_gates_args": ["--tool-cache", str(env_root)],
        "worktree_run_gates_args": ["--project-env", str(env)],
        "gate_environment": {
            "VIRTUAL_ENV": str(env),
            "PATH_PREPEND": str(env / ("Scripts" if sys.platform == "win32" else "bin")),
            "PYTHONPATH": str(checkout / "src" if any(path.startswith("src/") for path in tree) else checkout),
        },
    }
    plan["plan_digest"] = plan_digest(plan)
    return plan


def _lane_arguments(lane: Any) -> dict[str, list[str]]:
    """Read the digest-bound lane selectors, refusing a malformed lane."""
    if not isinstance(lane, dict):
        raise ApplyRefused("plan-invalid", "the plan does not record its lane")
    selectors = {"group": lane.get("groups"), "extra": lane.get("extras"),
                 "requirements": lane.get("requirements"), "exclude": lane.get("exclude")}  # fmt: skip
    if not all(
        isinstance(items, list) and all(isinstance(item, str) for item in items) for items in selectors.values()
    ):
        raise ApplyRefused("plan-invalid", "the plan's lane is not a list of names")
    return selectors


def derive_validated_plan(plan: Any, arguments: argparse.Namespace, environment: dict[str, str]) -> dict[str, Any]:
    """Derive an executable plan from approved inputs and require it to match the plan file.

    Check the independent approval before probing the interpreter or creating directories. Return the fresh derivation,
    or raise ``ApplyRefused`` if any part disagrees or the environment root is unsafe.
    """
    if not isinstance(plan, dict) or plan.get("plan_digest") != arguments.plan_digest:
        raise ApplyRefused("plan-modified", "the plan file does not record the approved digest")
    if plan_digest(plan) != arguments.plan_digest:
        raise ApplyRefused("plan-modified", "the plan contents do not match the approved digest")
    try:
        if any(plan.get(key) != value for key, value in _plan_inputs(arguments).items()):
            raise ApplyRefused("plan-modified", "the plan inputs differ from the approved apply arguments")
        planning = argparse.Namespace(**vars(arguments), **_lane_arguments(plan.get("lane")))
        derived = build_plan(planning, environment)
    except (guard.GuardError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise ApplyRefused("plan-invalid", f"the approved inputs no longer derive a plan: {error}") from error
    if derived != plan:
        raise ApplyRefused("plan-modified", "the plan differs from what the approved inputs derive; run plan again")
    problem = env_root_problem(arguments.env_root, arguments.repo)
    if problem:
        raise ApplyRefused("env-root-unsafe", problem)
    return derived


def installer_environment(environment: dict[str, str], step_env: dict[str, str]) -> dict[str, str]:
    """Return one step's environment: the allowlisted caller variables plus the step's own cache settings.

    Example:
        >>> installer_environment({"PATH": "/bin", "UV_OVERRIDE": "x", "TOKEN": "y"}, {"UV_CACHE_DIR": "/c"})
        {'PATH': '/bin', 'UV_CACHE_DIR': '/c'}
    """
    return {**{key: environment[key] for key in INSTALLER_ENVIRONMENT if key in environment}, **step_env}


def _stage_hook_config(plan: dict[str, Any], command: dict[str, Any]) -> None:
    """Write the base hook configuration to the private path the hook command reads, verifying its digest."""
    target = Path(command["argv"][-1])
    blob = guard._blob(plan["repo"], plan["base"], HOOK_CONFIG) or b""
    if target.name != f"pre-commit-config-{hashlib.sha256(blob).hexdigest()[:16]}.yaml":
        raise ApplyRefused("plan-modified", "the base hook configuration no longer matches the plan")
    target.write_bytes(blob)


def _step_cause(output: str) -> str:
    """Name the cause of one failed provisioning step from the tool's own diagnostic.

    Example:
        >>> _step_cause("error: Failed to fetch: dns error: failed to lookup address information")
        'network-unavailable'
        >>> _step_cause("No solution found when resolving dependencies")
        'resolver-conflict'
        >>> _step_cause("something else")
        'install-failed'
    """
    for cause, pattern in _STEP_CAUSES:
        if pattern.search(output):
            return cause
    return "install-failed"


def _reusable_environment(plan: dict[str, Any]) -> bool:
    """Tell whether the planned environment already exists with its interpreter; refuse one others could write.

    Behind a root no other user can enter, only the environment's owner and real-directory checks apply; behind a root
    others can enter, its own mode must also keep their writes out.

    Raises:
        ApplyRefused: The environment directory exists but is not private to the invoking user.
    """
    env = Path(plan["env"])
    if not env.exists() and not env.is_symlink():
        return False
    enclosed = hasattr(os, "getuid") and not env.parent.stat().st_mode & 0o077
    problem = private_directory_problem(env, enclosed=enclosed)
    if problem:
        raise ApplyRefused("env-root-unsafe", problem)
    if not (env / "pyvenv.cfg").is_file() or not Path(plan["env_python"]).is_file():
        return False
    try:
        version, prefix, executable = _probe_python(plan["env_python"])
        key = _environment_key(plan["python"], version, executable, plan["requirements"])
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return prefix == env and key == env.name


def _run_step(command: dict[str, Any], environment: dict[str, str], timeout: int) -> dict[str, Any]:
    """Run one derived command and return its step record with a fixed cause when it failed."""
    started = time.monotonic()
    try:
        done = subprocess.run(  # noqa: S603 - argv derived by this helper, no shell
            command["argv"],
            cwd=command.get("cwd"),
            env=installer_environment(environment, command["env"]),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output, code = (done.stdout or "") + (done.stderr or ""), done.returncode
    except FileNotFoundError:
        output, code = f"executable not found: {command['argv'][0]}", 127
    except subprocess.TimeoutExpired:
        output, code = f"timeout after {timeout} seconds", 124
    step = {"id": command["id"], "exit_code": code, "duration_seconds": round(time.monotonic() - started, 3)}
    step["output_tail"] = output[-4096:]
    if code != 0:
        unavailable = "pre-commit-unavailable" if command["id"] == "hooks" else "uv-unavailable"
        step["cause"] = unavailable if code == 127 else _step_cause(output)
    return step


def _result(status: str, cause: str | None, steps: list[dict[str, Any]], detail: str | None = None) -> dict[str, Any]:
    """Return one apply result record."""
    record: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "status": status, "cause": cause, "steps": steps}
    return record if detail is None else {**record, "detail": detail}


def apply_plan(plan: Any, arguments: argparse.Namespace, environment: dict[str, str] | None = None) -> dict[str, Any]:
    """Validate the plan, run the derived commands in order, stop at the first failure, and return the step record.

    A refused plan (edited, malformed, or with an environment root or cached environment another user could write)
    returns status ``blocked`` or ``failed`` with its cause and runs nothing.
    """
    environment = dict(os.environ) if environment is None else environment
    steps: list[dict[str, Any]] = []
    try:
        plan = derive_validated_plan(plan, arguments, environment)
        if plan["blocked"] is not None:
            return _result("blocked", plan["blocked"]["cause"], steps)
        reusable = _reusable_environment(plan)
    except ApplyRefused as refused:
        status = "failed" if refused.cause == "env-root-unsafe" else "blocked"
        return _result(status, refused.cause, steps, str(refused))
    status, cause = "ready", None
    for command in plan["commands"]:
        if command["id"] == "venv" and reusable:
            steps.append({"id": "venv", "status": "reused"})
            continue
        try:
            if command["id"] == "hooks":
                _stage_hook_config(plan, command)
        except (ApplyRefused, OSError) as error:
            step_cause = error.cause if isinstance(error, ApplyRefused) else "permission-denied"
            steps.append({"id": "hooks", "exit_code": None, "cause": step_cause, "output_tail": str(error)})
            status, cause = "partial", step_cause
            break
        step = _run_step(command, environment, arguments.timeout_seconds)
        steps.append(step)
        if step["exit_code"] != 0:
            # A failed hook seed leaves a usable package environment; the hook gate then records its own cause.
            status, cause = ("partial", step["cause"]) if command["id"] == "hooks" else ("failed", step["cause"])
            break
    if status == "ready" and plan.get("hooks"):
        status, cause = "partial", plan["hooks"]["cause"]
    return _result(status, cause, steps)


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the plan and apply subcommands."""
    parser = argparse.ArgumentParser(prog="gate_env_provision.py", description=__doc__.split("\n\n")[0])
    inputs = argparse.ArgumentParser(add_help=False)
    inputs.add_argument("--repo", type=Path, required=True, help="Reviewed checkout.")
    inputs.add_argument("--base", required=True, help="Review base commit; its declarations decide trust.")
    inputs.add_argument("--head", required=True, help="Reviewed head commit.")
    inputs.add_argument("--python", type=Path, required=True, help="Absolute interpreter for the environment.")
    inputs.add_argument(
        "--env-root", type=Path, required=True, help="Absolute directory outside the checkout, private to the caller."
    )
    actions = parser.add_subparsers(dest="action", required=True)
    plan = actions.add_parser("plan", parents=[inputs], help="Classify declared dependencies and write the plan.")
    plan.add_argument("--group", action="append", default=[], help="PEP 735 dependency group of the test lane.")
    plan.add_argument("--extra", action="append", default=[], help="Optional-dependency extra of the test lane.")
    plan.add_argument(
        "--requirements", action="append", default=[], help="Repository-relative requirements file of the test lane."
    )
    plan.add_argument("--exclude", action="append", default=[], help="Declared name to leave out, such as a backend.")
    plan.add_argument("--out", type=Path, required=True, help="Plan JSON path.")
    apply = actions.add_parser("apply", parents=[inputs], help="Build the planned environment and seed the hook cache.")
    apply.add_argument("--plan", type=Path, required=True, help="Plan JSON written by the plan action.")
    apply.add_argument("--plan-digest", required=True, help="Digest recorded when the plan was approved.")
    apply.add_argument("--timeout-seconds", type=int, default=1800, help="Per-step timeout.")
    apply.add_argument("--out", type=Path, required=True, help="Result JSON path inside the retained run directory.")
    arguments = parser.parse_args(argv)
    if not arguments.python.is_absolute() or not arguments.env_root.is_absolute():
        parser.error("invalid-path: --python and --env-root must be absolute")
    if arguments.action == "plan":
        problem = env_root_problem(arguments.env_root, arguments.repo)
        if problem:
            parser.error(problem)
    return arguments


def _read_plan(path: Path) -> Any:
    """Read the plan file; an unreadable or non-JSON file becomes a value ``apply`` refuses as malformed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    """Write the action's JSON result and retain a canonical receipt when applying a plan."""
    arguments = _parse(argv)
    if arguments.action == "plan":
        try:
            record = build_plan(arguments, dict(os.environ))
        except (guard.GuardError, OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"gate-env-plan-failed:{error}", file=sys.stderr)
            return 2
        ok = record["blocked"] is None
    else:
        record = apply_plan(_read_plan(arguments.plan), arguments)
        ok = record["status"] == "ready"
    output = json.dumps(record, indent=2) + "\n"
    if record.get("cause") == "plan-modified" and not arguments.out.parent.is_dir():
        # A refusal must not create directories, even for its receipt.
        print(output, end="")
        return 1
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(output, encoding="utf-8")
    if arguments.action == "apply" and arguments.out.name != "gate-provisioning.json":
        arguments.out.with_name("gate-provisioning.json").write_text(output, encoding="utf-8")
    print(arguments.out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
