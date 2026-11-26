#!/usr/bin/env python
"""Provision declared project dependencies in a private temporary virtual environment.

Purpose: Recover a local Python check whose normal interpreter lacks a declared distribution. Scope: Read working-tree
declarations and install index wheels into a venv outside the project. The provisioner     skips local and direct
references and has no dependency on another plugin. It never installs the local project. Usage: Run `python
dev_env_provision.py --repo DIR --out RECEIPT`; add `--plan-only` to inspect selection offline. Outputs: One JSON
receipt on stdout and, optionally, an identical copy at a caller-selected path outside the project. Failure: Selection,
installation and probe failures become receipt data; only invalid CLI arguments exit nonzero. Used by: Develop fix,
feature and refactor targeted pytest recovery and the closing check named by develop review.
"""

from __future__ import annotations

import argparse
import configparser
import errno
import hashlib
import json
import math
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from itertools import chain
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import InvalidSdistFilename, InvalidWheelFilename, parse_sdist_filename, parse_wheel_filename
except ModuleNotFoundError as exc:
    if exc.name != "packaging":
        raise
    try:
        from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
        from pip._vendor.packaging.utils import (
            InvalidSdistFilename,
            InvalidWheelFilename,
            parse_sdist_filename,
            parse_wheel_filename,
        )
    except ModuleNotFoundError as exc:
        if exc.name != "pip":
            raise
        Requirement = None

if os.name == "nt":
    import msvcrt
else:
    import fcntl

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ModuleNotFoundError as exc:
        if exc.name != "tomli":
            raise
        tomllib = None

INDEX = "https://pypi.org/simple"
_NAME = r"[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?"
_DIRECT = re.compile(r"@|[/\\]|^[a-z][a-z0-9+.-]*:|\.(?:whl|zip|tgz|tar(?:\.gz|\.bz2|\.xz)?)$", re.I)
_IMPORT = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_FAILURES = {
    "direct-reference": r"direct reference refused|url dependenc|transitive url",
    "network": r"failed to fetch|could not fetch|dns|proxyerror|could not connect|connection|certificate verify",
    "unsupported-platform": r"platform tag|current platform|not a supported wheel|requires-python|different python",
    "no-wheel": r"no wheels|no matching distribution|could not find a version|no versions|no usable wheels|only-binary",
    "resolver-conflict": r"resolutionimpossible|conflict|no solution found|unsatisfiable|invalid requirement",
}


def _name(value: str) -> str:
    """Normalize a distribution or dependency-group name.

    Examples:
        >>> _name("My_Package.Extras")
        'my-package-extras'
    """
    return re.sub(r"[-_.]+", "-", value.lower())


def _repo_root() -> Path:
    """Find the enclosing worktree marker without executing Git or reading its objects."""
    cwd = Path.cwd()
    return next((path for path in (cwd, *cwd.parents) if (path / ".git").exists()), cwd)


def _requirement_files(repo: Path) -> list[Path]:
    """Find root and requirements-directory files while ignoring documentation requirements."""
    paths = sorted(
        path
        for suffix in ("*.txt", "*.in")
        for path in chain(repo.glob(f"requirements{suffix}"), (repo / "requirements").rglob(suffix))
    )
    return [
        path
        for path in paths
        if path.is_file() and not {"doc", "docs", "documentation"}.intersection(_name(path.stem).split("-"))
    ]


def _group_items(groups: dict, name: str, ancestors: tuple = ()) -> Iterator[object]:
    """Expand group includes while reporting cyclic, missing or malformed declarations as unsafe."""
    entries = groups.get(name)
    if name in ancestors or not isinstance(entries, list):
        yield {"include-group": name}
        return
    for entry in entries:
        if isinstance(entry, dict) and entry.keys() == {"include-group"} and isinstance(entry["include-group"], str):
            yield from _group_items(groups, _name(entry["include-group"]), (*ancestors, name))
        else:
            yield entry


def _toml_items(data: dict, args: argparse.Namespace) -> Iterator[object]:
    """Select project dependencies, extras and requested development groups."""
    project = data.get("project", {})
    extras = () if args.no_extras else project.get("optional-dependencies", {}).values()
    for entries in (project.get("dependencies", []), *extras):
        yield from entries if isinstance(entries, list) else (None,)
    groups = {_name(name): items for name, items in data.get("dependency-groups", {}).items()}
    selected = {name for name in groups if args.all_groups or name in {"dev", "ci"} or "test" in name}
    selected.update(map(_name, args.group))
    for name in sorted(selected):
        yield from _group_items(groups, name)
    entries = data.get("tool", {}).get("uv", {}).get("dev-dependencies", [])
    yield from entries if isinstance(entries, list) else (None,)


#: pip's requirements-file options by long spelling, with the class this provisioner gives each and whether it takes a
#: value: ``route`` sends names to another index or source, ``include`` reads another file, ``editable`` names a local
#: or direct project, ``neutral`` only picks wheels, pre-releases or hashes. It mirrors pip's own option set (and the
#: contributor-code guard's table in the oss and Codex Rig plugins), kept here because develop depends on no other
#: plugin. An option outside it counts as ``route``, so an unmodelled spelling never passes as harmless.
_REQUIREMENTS_OPTIONS: dict[str, tuple[str, bool]] = {
    "--index-url": ("route", True), "--pypi-url": ("route", True), "--extra-index-url": ("route", True),
    "--find-links": ("route", True), "--trusted-host": ("route", True), "--no-index": ("route", False),
    "--requirement": ("include", True), "--constraint": ("include", True), "--build-constraint": ("include", True),
    "--editable": ("editable", True),
    "--no-binary": ("neutral", True), "--only-binary": ("neutral", True), "--prefer-binary": ("neutral", False),
    "--require-hashes": ("neutral", False), "--no-require-hashes": ("neutral", False), "--pre": ("neutral", False),
    "--all-releases": ("neutral", True), "--only-final": ("neutral", True), "--use-feature": ("neutral", True),
    "--hash": ("neutral", True), "--config-settings": ("neutral", True), "--global-option": ("neutral", True),
}  # fmt: skip
#: Short spellings pip accepts; each takes a value, attached (``-iURL``) or as the next token.
_SHORT_OPTIONS = {
    "-i": "--index-url", "-f": "--find-links", "-r": "--requirement", "-c": "--constraint", "-e": "--editable",
    "-C": "--config-settings",
}  # fmt: skip
#: A comment as pip strips it: ``#`` at the line start or after whitespace.
_COMMENT = re.compile(r"(^|\s+)#.*$")
_ENCODING = re.compile(r"coding[:=][ \t]*([-\w.]+)", re.ASCII)
_ENV_REFERENCE = re.compile(r"\$\{[A-Z0-9_]+\}")
#: Receipt cause for each option class that keeps a line out of the plan (``neutral`` options are simply ignored).
_OPTION_CAUSES = {"route": "index-routing", "include": "requirement-include", "editable": "direct-reference"}


def _logical_lines(text: str) -> list[str]:
    r"""Return a requirements file's logical lines as pip reads them: continuations joined, comments dropped.

    Examples:
        >>> _logical_lines("requests==2.32.3 \\\n    --hash=sha256:aa\n# note\nidna==3.7  # pinned\n")
        ['requests==2.32.3     --hash=sha256:aa', 'idna==3.7']
    """
    lines: list[str] = []
    pending = ""
    for raw in text.removeprefix("\ufeff").splitlines():
        comment = _COMMENT.match(raw)
        if raw.endswith("\\") and not comment:
            pending += raw.strip("\\")
            continue
        line = _COMMENT.sub("", pending + (" " if comment else "") + raw).strip()
        pending = ""
        lines += [line] if line else []
    tail = _COMMENT.sub("", pending).strip()
    return lines + ([tail] if tail else [])


def _line_options(line: str) -> list[tuple[str, str, str]]:
    """Read canonical pip option names, classes and values from one option line."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        return [("requirements option", "route", "")]
    options, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith("--"):
            name, equals, attached = token.partition("=")
            matches = [option for option in _REQUIREMENTS_OPTIONS if option.startswith(name)] if len(name) > 2 else []
            option = name if name in _REQUIREMENTS_OPTIONS or len(matches) != 1 else matches[0]
            value = attached if equals else None
        elif token.startswith("-"):
            option, value = _SHORT_OPTIONS.get(token[:2], token), token[2:] or None
        else:
            continue  # a stray value; its option was already classified
        kind, takes_value = _REQUIREMENTS_OPTIONS.get(option, ("route", False))
        if takes_value and value is None and index < len(tokens):
            value, index = tokens[index], index + 1
        label = option if option in _REQUIREMENTS_OPTIONS else "requirements option"
        options.append((label, kind, value or ""))
    return options or [("requirements option", "route", "")]


def _requirement_lines(text: str, receipt: dict) -> tuple[list[str], set[str], bool]:
    """Read requirement texts and flag files that do not establish default-index routing.

    Per-requirement options (``--hash`` and the like) are dropped, as pip splits a requirement line at its first option.
    An option line is recorded under ``skipped`` with the cause its class names and contributes no name.
    """
    requirements: list[str] = []
    sources: set[str] = set()
    routed = False
    if "\0" in text or any(
        match[1].lower().replace("_", "-") not in {"utf-8", "utf8"}
        for line in text.removeprefix("\ufeff").split("\n", 2)[:2]
        if line.lstrip().startswith("#") and (match := _ENCODING.search(line))
    ):
        receipt["skipped"].append({"requirement": "unmodeled requirements input", "cause": "index-routing"})
        return requirements, sources, True
    for line in _logical_lines(text):
        if _ENV_REFERENCE.search(line):
            receipt["skipped"].append({"requirement": "unresolved requirements variable", "cause": "index-routing"})
            routed = True
            continue
        if not line.lstrip("'\"\\").startswith("-"):
            requirements.append(re.split(r"\s+(?=-)", line, maxsplit=1)[0].strip())
            continue
        names, file_routed = _requirements_option_routing(line, receipt)
        sources.update(names)
        routed |= file_routed
    return requirements, sources, routed


def _requirements_option_routing(line: str, receipt: dict) -> tuple[set[str], bool]:
    """Record safe option labels and scope an editable source only when its name is known."""
    sources = set()
    routed = False
    for option, kind, value in _line_options(line):
        if kind == "neutral":
            continue
        receipt["skipped"].append({"requirement": option, "cause": _OPTION_CAUSES[kind]})
        if kind == "editable" and (name := _direct_name(value)):
            sources.add(name)
        else:
            routed = True
    return sources, routed


_UV_NEUTRAL_KEYS = {
    "cache-keys",
    "conflicts",
    "dev-dependencies",
    "environments",
    "managed",
    "package",
    "required-version",
    "workspace",
}
#: Tools whose settings do not choose sources for the project's dependencies.
_NON_INSTALLER_TOOLS = {
    "bandit", "black", "bumpversion", "check-manifest", "cibuildwheel", "codespell", "commitizen", "coverage", "flake8",
    "interrogate", "isort", "mdformat", "mypy", "nbqa", "poe", "pre-commit", "pycln", "pydocstyle", "pylint",
    "pyproject-fmt", "pyright", "pytest", "pytest-ini", "ruff", "semantic_release", "setuptools", "setuptools_scm",
    "sphinx-pyproject", "sqlfluff", "towncrier", "vulture", "yapf",
}  # fmt: skip
#: Only these subtables of multipurpose tools are independent of dependency installation.
_NON_INSTALLER_SECTIONS = {"hatch": {"version", "build", "metadata", "publish"}, "pdm": {"version"}}
_UNMODELED_INSTALLER_FILES = {
    "pdm.toml", ".pdm.toml", "pixi.toml", "poetry.toml", "tox.ini", "tox.toml", "Pipfile", "Pipfile.lock",
    "pdm.lock", "poetry.lock", "pylock.toml",
}  # fmt: skip
_INSTALLER_SCRIPT_SUFFIXES = {".yml", ".yaml", ".cfg", ".ini", ".sh", ".ps1", ".bat", ".cmd"}
_ROUTING_DIRECTIVE = re.compile(
    r"(?i)\b(?:extra[-_]index[-_]?url|index[-_]?url|pypi[-_]url|find[-_]links|no[-_]index|trusted[-_]host)\b"
    r"|--(?:default-index|index|build-constraint|constraint|requirement|config-file)(?=[\s=])"
    r"|(?:^|(?:pip|uv)[^#\n]*\s)-[ifrc](?=[\s=\w./~])",
    re.MULTILINE,
)
_INSTALLER_VARIABLE = re.compile(r"\b(?:PIP|UV)_[A-Z0-9_]+\b")

_NEUTRAL_UV_ENV = {
    "UV_CACHE_DIR", "UV_NO_CACHE", "UV_NO_PROGRESS", "UV_NO_CONFIG", "UV_LINK_MODE", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR",
    "UV_PYTHON_INSTALL_DIR", "UV_COMPILE_BYTECODE", "UV_VENV_CLEAR", "UV_RUN_RECURSION_DEPTH",
}  # fmt: skip
_NEUTRAL_PIP_CONFIG = {
    name.removeprefix("--") for name, (kind, _) in _REQUIREMENTS_OPTIONS.items() if kind == "neutral"
} | {
    "break-system-packages", "cache-dir", "cert", "client-cert", "disable-pip-version-check", "log", "no-cache-dir",
    "no-color", "no-input", "progress-bar", "proxy", "quiet", "resume-retries", "retries", "timeout", "verbose",
}  # fmt: skip


def _tool_routing(tools: dict) -> bool:
    """Refuse tool settings unless their role or routing is understood."""
    for tool, settings in tools.items():
        if tool == "uv" or tool in _NON_INSTALLER_TOOLS:
            continue
        sections = _NON_INSTALLER_SECTIONS.get(tool)
        if sections is None or not isinstance(settings, dict) or not settings or settings.keys() - sections:
            return True
        if any(not isinstance(section, dict) for section in settings.values()):
            return True
    return False


def _uv_routing(settings: object, *, top_level: bool = True) -> tuple[set[str], bool]:
    """Allow known neutral uv settings and classify source declarations by scope."""
    allowed = _UV_NEUTRAL_KEYS | {"sources", "index"}
    if top_level:
        allowed.add("pip")
    if not isinstance(settings, dict) or settings.keys() - allowed:
        return set(), True

    sources = settings.get("sources", {})
    indexes = settings.get("index", [])
    # Only explicitly selected indexes that preserve the default can be scoped by source name.
    if (
        not isinstance(sources, dict)
        or not isinstance(indexes, list)
        or any(
            not isinstance(index, dict) or index.get("explicit") is not True or index.get("default", False) is not False
            for index in indexes
        )
    ):
        return set(), True
    names = {_name(name) for name in sources}
    if not top_level:
        return names, False
    pip_names, pip_routed = _uv_routing(settings.get("pip", {}), top_level=False)
    return names | pip_names, pip_routed


def _uv_lock_routing(lock: dict | None) -> tuple[set[str], bool]:
    """Classify sources consumed by frozen uv sync before planning a default-index install."""
    if lock is None:
        return set(), False
    version, packages = lock.get("version"), lock.get("package", [])
    if type(version) is not int or version != 1 or not isinstance(packages, list):
        return set(), True
    names = set()
    for package in packages:
        if not isinstance(package, dict):
            return set(), True
        name, source = package.get("name"), package.get("source")
        if (
            not isinstance(name, str)
            or not re.fullmatch(_NAME, name)
            or not isinstance(source, dict)
            or len(source) != 1
        ):
            return set(), True
        if source.get("registry") not in (INDEX, f"{INDEX}/"):
            names.add(_name(name))
    return names, False


def _toml_routing(data: dict, uv_config: dict) -> tuple[set[str], bool]:
    """Combine pyproject and standalone uv declarations before planning any requirements."""
    tools = data.get("tool", {})
    names, routed = _uv_routing(tools.get("uv", {}))
    config_names, config_routed = _uv_routing(uv_config)
    return names | config_names, routed or config_routed or _tool_routing(tools)


def _project_routing(
    data: dict, uv_config: dict, requirements: list[str], receipt: dict
) -> tuple[list[str], set[str], bool]:
    """Read requirements and combine their routing with the project's TOML declarations."""
    names, routed = _toml_routing(data, uv_config)
    lines = []
    for text in requirements:
        items, file_names, file_routed = _requirement_lines(text, receipt)
        lines.extend(items)
        names.update(file_names)
        routed = routed or file_routed
    return lines, names, routed


def _installer_variable_routes(name: str) -> bool:
    """Treat any pip or uv variable without a modeled neutral role as a routing input."""
    name = name.upper()
    if name.startswith("UV_"):
        return name not in _NEUTRAL_UV_ENV
    return name.startswith("PIP_") and name[4:].lower().replace("_", "-") not in _NEUTRAL_PIP_CONFIG


def _unmodeled_project_config(repo: Path) -> bool:
    """Refuse opaque installer manifests and routing directives in project automation."""
    if any((repo / name).exists() or (repo / name).is_symlink() for name in _UNMODELED_INSTALLER_FILES):
        return True
    root_scripts = (path for path in repo.iterdir() if path.suffix.lower() in _INSTALLER_SCRIPT_SUFFIXES)
    automation = (repo / name for name in (".github", ".circleci", ".buildkite", "ci", "scripts"))
    scripts = (path for directory in automation for path in directory.rglob("*"))
    for path in chain(root_scripts, scripts):
        if path.suffix.lower() not in _INSTALLER_SCRIPT_SUFFIXES or path.name in {"pip.conf", "pip.ini"}:
            continue
        if path.is_dir():
            continue
        content = _read_declaration(repo, path).decode("utf-8-sig")
        if _ROUTING_DIRECTIVE.search(content) or any(
            _installer_variable_routes(name) for name in _INSTALLER_VARIABLE.findall(content)
        ):
            return True
    return False


def _python_prefix(python: str, timeout: float) -> Path:
    """Find the selected interpreter's site prefix without loading startup hooks."""
    base = Path(shutil.which(python) or python).absolute()
    if base == Path(sys.executable):
        return Path(sys.prefix)
    probe = "import json, sys; print(json.dumps([sys.executable, sys.prefix, sys.version_info < (3, 14)]))"
    output = _run([str(base), "-I", "-S", "-B", "-c", probe], Path(sys.prefix), timeout)
    executable, prefix, legacy_venv = json.loads(output)
    if legacy_venv:
        # Before Python 3.14, -S bypasses site.py's pyvenv.cfg lookup and prefix assignment.
        directory = Path(executable).parent
        if any((parent / "pyvenv.cfg").is_file() for parent in (directory, directory.parent)):
            return directory.parent
    if not Path(prefix).is_absolute():
        raise ValueError("selected interpreter did not report an absolute prefix")
    return Path(prefix)


def _host_config_files(prefix: Path) -> list[Path]:
    """List the site, user and system uv and pip configuration paths for this host."""
    env, home, system = os.environ, Path.home(), platform.system()
    xdg = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    site_config = "pip.ini" if system == "Windows" else "pip.conf"
    files = [prefix / site_config, xdg / "uv/uv.toml", xdg / "pip/pip.conf", home / ".pip/pip.conf"]
    if system == "Windows":
        program_data = Path(env.get("PROGRAMDATA") or "C:/ProgramData")
        files += [program_data / "uv/uv.toml", program_data / "pip/pip.ini", home / "pip/pip.ini"]
        if app_data := env.get("APPDATA"):
            files += [Path(app_data) / "uv/uv.toml", Path(app_data) / "pip/pip.ini"]
        return files
    config_dirs = (env.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(os.pathsep)
    files.extend(Path(directory) / "uv/uv.toml" for directory in config_dirs if directory)
    files.append(Path("/etc/uv/uv.toml"))
    if system == "Darwin":
        files += [root / "Library/Application Support/pip/pip.conf" for root in (home, Path("/"))]
        return [*files, home / ".config/pip/pip.conf"]
    files.append(Path("/etc/pip.conf"))
    files += [
        Path(directory) / "pip/pip.conf" for directory in config_dirs if directory
    ]  # fmt: skip
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
    root = Path(sysconfig.get_config_var("WHEEL_PKG_DIR") or Path(ensurepip.__file__).parent / "_bundled")
    paths = [str(path) for path in sorted(root.glob("pip-*.whl"), reverse=True)]
sys.path.extend(paths)
from pip._internal.configuration import get_configuration_files
files = set()
for sys.prefix in (active, install):
    for sources in get_configuration_files().values():
        files.update(sources)
print(json.dumps(sorted(files)))
"""


def _pip_config_files(python: str, prefix: Path, timeout: float) -> list[Path]:
    """Ask the selected interpreter's pip where it loads config, with site startup disabled."""
    base = Path(shutil.which(python) or python).absolute()
    cwd = Path(__file__).resolve().parent
    env = _installer_env(cwd)
    # Windows pip expands %SystemDrive%\ProgramData and the user data directories from these; unset, they stay literal.
    locations = {"SYSTEMDRIVE", "PROGRAMDATA", "ALLUSERSPROFILE", "APPDATA", "LOCALAPPDATA"}
    env.update(
        {key: value for key, value in os.environ.items() if key.upper().startswith("XDG_") or key.upper() in locations}
    )
    try:
        result = subprocess.run(  # noqa: S603 - explicitly selected interpreter, isolated startup
            [str(base), "-I", "-S", "-B", "-c", _PIP_CONFIG_PROBE, str(prefix)],
            cwd=cwd, env=env, capture_output=True, encoding="utf-8", check=True, timeout=timeout,
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise _Issue("timeout", "pip configuration discovery timed out") from exc
    except (OSError, subprocess.CalledProcessError):
        raise _Issue("index-routing", "pip configuration discovery unavailable") from None
    files = json.loads(result.stdout)
    if not isinstance(files, list) or not all(isinstance(path, str) and Path(path).is_absolute() for path in files):
        relative = [path for path in files if not isinstance(path, str) or not Path(path).is_absolute()]
        raise ValueError(
            f"pip did not return absolute configuration paths: {relative if isinstance(files, list) else files!r}"
        )
    return [Path(path) for path in files]


def _host_config_routing(path: Path) -> bool:
    """Treat any host routing or unreadable installer configuration as global."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return True
        content = path.read_text(encoding="utf-8")
        if path.name == "uv.toml":
            settings = tomllib.loads(content)
            names, routed = _uv_routing(settings)
            return routed or bool(names or settings.get("index") or settings.get("pip", {}).get("index"))
        config = configparser.RawConfigParser()
        config.read_string(content)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError, configparser.Error):
        return True
    keys = {key.replace("_", "-").removeprefix("--") for section in config.values() for key in section}
    # pip config can supply an alias or a file of requirements with its own sources.
    return bool(keys - _NEUTRAL_PIP_CONFIG)


def _host_routing(python: str, timeout: float) -> bool:
    """Detect host settings before a clean installer invocation can discard them."""
    if any(map(_installer_variable_routes, os.environ)):
        return True
    deadline = time.monotonic() + timeout
    prefix = _python_prefix(python, timeout)
    files = _host_config_files(prefix)
    if any(map(_host_config_routing, files)):
        return True
    return any(map(_host_config_routing, _pip_config_files(python, prefix, deadline - time.monotonic())))


def _parse_requirement(spec: str) -> Requirement | None:
    """Validate the full installer grammar before trusting the name it declares."""
    if Requirement is None:
        raise _Issue("installer-missing", "a PEP 508 requirement parser is unavailable")
    try:
        return Requirement(spec)
    except InvalidRequirement:
        return None


def _direct_name(spec: str) -> str | None:
    """Identify a named direct reference or a distribution archive without opening its source."""
    parsed = _parse_requirement(spec)
    if parsed and parsed.url:
        return _name(parsed.name)
    try:
        filename = Path(unquote(urlsplit(spec.replace("\\", "/")).path)).name
        if filename.lower().endswith(".whl"):
            return parse_wheel_filename(filename)[0]
        return parse_sdist_filename(filename)[0]
    except (InvalidWheelFilename, InvalidSdistFilename, ValueError):
        return None


def _select(items: Iterator[object], excluded: set[str], receipt: dict) -> tuple[dict[str, str], set[str], bool]:
    """Collect valid plain requirements and scope direct references to defensibly identified names."""
    candidates = {}
    sources: set[str] = set()
    routed = False
    for entry in items:
        spec = entry.strip() if isinstance(entry, str) else ""
        parsed = _parse_requirement(spec) if spec else None
        if parsed and not parsed.url and not _DIRECT.search(parsed.name):
            name = _name(parsed.name)
            if name not in excluded:
                candidates[spec] = name
        elif parsed or _DIRECT.search(spec):
            name = _direct_name(spec)
            if name:
                sources.add(name)
            else:
                routed = True
            receipt["skipped"].append({"requirement": name or "direct requirement", "cause": "direct-reference"})
        else:
            label = spec if re.fullmatch(r"[\w .<>=!,*+~-]+", spec) else "invalid requirement"
            receipt["skipped"].append({"requirement": label, "cause": "unsafe-spec"})
    return candidates, sources, routed


def _read_declaration(repo: Path, path: Path) -> bytes:
    """Read only regular declaration files contained by the checkout."""
    if not path.resolve().is_relative_to(repo) or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"not a regular project declaration: {path.name}")
    return path.read_bytes()


def _project_declarations(repo: Path) -> tuple[str, dict[str, dict], list[str]]:
    """Read and hash the same bytes used to decide project routing and requirements."""
    toml_paths = (repo / "pyproject.toml", repo / "uv.toml", repo / "uv.lock")
    digest = hashlib.sha256(os.fsencode(repo.as_posix()))
    toml = {}
    requirements = []
    for path in (*toml_paths, *_requirement_files(repo)):
        try:
            path.lstat()
        except FileNotFoundError:
            if path not in toml_paths:
                raise
            continue
        content = _read_declaration(repo, path)
        digest.update(json.dumps([path.relative_to(repo).as_posix(), hashlib.sha256(content).hexdigest()]).encode())
        if path in toml_paths:
            toml[path.name] = tomllib.loads(content.decode("utf-8"))
        else:
            requirements.append(content.decode("utf-8-sig"))
    return digest.hexdigest(), toml, requirements


def _plan(repo: Path, args: argparse.Namespace, receipt: dict, deadline: float) -> str:
    """Plan only names whose project and host routing permits the default index."""
    digest, toml, requirements = _project_declarations(repo)
    data, uv_config = toml.get("pyproject.toml", {}), toml.get("uv.toml", {})
    excluded = set(map(_name, args.exclude))
    own_name = data.get("project", {}).get("name")
    if isinstance(own_name, str):
        excluded.add(_name(own_name))
    lines, source_names, routed = _project_routing(data, uv_config, requirements, receipt)
    locked_names, lock_routed = _uv_lock_routing(toml.get("uv.lock"))
    candidates, direct_names, direct_routed = _select(chain(_toml_items(data, args), lines), excluded, receipt)
    source_names.update(locked_names | direct_names)
    timeout = min(args.timeout, deadline - time.monotonic())
    routed = routed or lock_routed or direct_routed or _unmodeled_project_config(repo)
    if candidates and not routed:
        routed = _host_routing(args.python, timeout)
    for spec, name in candidates.items():
        if routed or name in source_names:
            receipt["skipped"].append({"requirement": spec, "cause": "index-routing"})
        else:
            receipt["planned"].append(spec)
    return digest


def environment_root() -> Path:
    """Return the current user's environment root beneath the portable temp directory."""
    temp = os.environ.get("TMPDIR") or (tempfile.gettempdir() if os.name == "nt" else "/tmp")  # noqa: S108
    user = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return Path(temp).resolve() / f"cc-develop-env{user}"


def _owned_directory(path: Path) -> None:
    """Refuse symlinked or broadly writable cache directories before using their contents."""
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.resolve() != path:
        raise ValueError(f"not a private environment directory: {path}")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError(f"not a private environment directory: {path}")


def _env_python(venv: Path) -> Path:
    """Locate the environment interpreter using the native venv layout."""
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def receipt_python(receipt_file: Path) -> str:
    """Resolve a caller-owned receipt to a private, local provisioner interpreter."""
    with receipt_file.open(encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (
            hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o022)
        ):
            raise ValueError("environment receipt is not a caller-owned regular file")
        data = json.load(handle)
    if not isinstance(data, dict) or data.get("status") not in ("ready", "partial"):
        raise ValueError("environment receipt has no usable status")
    python = Path(data["env_python"])
    root = environment_root()
    bucket = python.parents[2]
    if bucket.parent != root or not re.fullmatch(r"[a-f0-9]{64}", bucket.name):
        raise ValueError("interpreter is outside the provisioner environment root")
    _owned_directory(root)
    _owned_directory(bucket)
    if python != _env_python(bucket / "venv") or python.resolve(strict=True) != python or not python.is_file():
        raise ValueError("receipt does not name a provisioned interpreter")
    return str(python)


def retry_pythonpath(project: Path, existing: str | None) -> str:
    """Return the ``PYTHONPATH`` of a provisioned retry: the project's source roots first, then any existing entries.

    The provisioner never installs the project, while the original interpreter usually had it installed (editable),
    so a src-layout package would not import in the retry and its import error would read as a missing package.
    ``src`` (when the project has one) and the project root lead, joined with this host's separator.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     (Path(directory) / "src").mkdir()
        ...     entries = retry_pythonpath(Path(directory), "extra").split(os.pathsep)
        ...     [Path(entry).name for entry in entries] == ["src", Path(directory).name, "extra"]
        True
    """
    roots = ([project / "src"] if (project / "src").is_dir() else []) + [project]
    if any(os.pathsep in str(root) for root in roots):
        raise ValueError("project source path contains the PYTHONPATH separator")
    return os.pathsep.join([*(str(root) for root in roots), *([existing] if existing else [])])


def _try_lock(handle) -> None:
    """Take a native nonblocking lock that the OS releases on process exit."""
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def _lock(bucket: Path, deadline: float) -> Iterator[None]:
    """Serialize reads and writes for one environment within the call's budget."""
    with (bucket / ".lock").open("a+b") as handle:
        while True:
            try:
                _try_lock(handle)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("another run holds the environment lock") from exc
                time.sleep(min(remaining, 0.1))
        yield


def _installer_env(cwd: Path) -> dict[str, str]:
    """Keep platform and TLS defaults while ignoring Python and installer overrides."""
    keep = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update(PIP_CONFIG_FILE=os.devnull, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(cwd), TEMP=str(cwd), TMP=str(cwd))
    return env


def _failure(output: str) -> str:
    """Classify common installer diagnostics into the closed set.

    Examples:
        >>> _failure("error: no wheels with the matching platform tag")
        'unsupported-platform'
        >>> _failure("ERROR: No matching distribution found for demo")
        'no-wheel'
    """
    return next(
        (cause for cause, pattern in _FAILURES.items() if re.search(pattern, output, re.I)), "installer-missing"
    )


class _Issue(Exception):
    """Carry a bounded failure diagnostic through an operation that cannot continue."""

    def __init__(self, cause: str, detail: str):
        """Store the closed cause and a short diagnostic."""
        super().__init__(detail[-1500:])
        self.cause = cause


def _run(argv: list[str], cwd: Path, timeout: float) -> str:
    """Run a bounded command without a shell and translate subprocess failures."""
    if timeout <= 0:
        raise _Issue("timeout", "provisioning budget exhausted")
    try:
        result = subprocess.run(  # noqa: S603 - validated argv, no shell
            argv,
            cwd=cwd,
            env=_installer_env(cwd),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise _Issue("timeout", str(exc)) from exc
    if result.returncode:
        output = f"{result.stdout}\n{result.stderr}".strip()
        raise _Issue(_failure(output), output)
    return result.stdout


def _install_command(uv: str | None, python: Path, specs: list[str]) -> list[str]:
    """Build a wheel-only default-index install for the exact validated declarations.

    Examples:
        >>> _install_command("uv", Path("python"), ["pytest"])[-3:]
        ['--only-binary', ':all:', 'pytest']
    """
    if any(not (req := _parse_requirement(spec)) or req.url or _DIRECT.search(req.name) for spec in specs):
        raise _Issue("unsafe-spec", "installer received an invalid or direct requirement")
    if uv:
        command = [
            uv,
            "pip",
            "install",
            "--no-config",
            "--no-sources",
            "--no-cache",
            "--no-python-downloads",
            "--python",
            str(python),
            "--default-index",
            INDEX,
        ]
    else:
        command = [
            str(python),
            "-I",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-input",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--index-url",
            INDEX,
        ]
    # Wheel metadata can introduce names and sources that the project never approved for default-index resolution.
    return [*command, "--no-deps", "--only-binary", ":all:", *specs]


def _create(run: Callable, uv: str | None, base: str, python: Path) -> None:
    """Create the environment and keep its entry-point executable inside the private root."""
    venv = python.parent.parent
    if venv.resolve() != venv:
        raise ValueError("environment directory redirects outside its cache slot")
    if uv:
        run(
            [
                uv,
                "venv",
                "--no-config",
                "--no-project",
                "--no-cache",
                "--no-python-downloads",
                "--allow-existing",
                "--python",
                base,
                str(venv),
            ]
        )
    else:
        run([base, "-I", "-m", "venv", "--copies", str(venv)])
    if python.is_symlink():
        # uv normally links to the base executable on POSIX. Keep the venv path independently verifiable.
        copy = python.with_name(".python-copy")
        shutil.copy2(python, copy)
        copy.replace(python)
    if python.resolve(strict=True) != python or not python.is_file():
        raise ValueError("venv did not create an interpreter inside its cache slot")


def _record(receipt: dict, issue: _Issue, **context: object) -> None:
    """Add a failure with its closed cause and bounded evidence."""
    receipt["failed"].append({**context, "cause": issue.cause, "detail": str(issue)})


def _install(run: Callable, uv: str | None, python: Path, receipt: dict) -> bool:
    """Install jointly, then recover unaffected packages after a package-specific failure."""
    planned = receipt["planned"]
    if not planned:
        return True
    try:
        run(_install_command(uv, python, planned))
    except _Issue as issue:
        _record(receipt, issue, requirements=planned)
        if len(planned) > 1 and issue.cause in {"no-wheel", "unsupported-platform", "direct-reference"}:
            for spec in planned:
                try:
                    run(_install_command(uv, python, [spec]))
                except _Issue as individual:
                    _record(receipt, individual, requirement=spec)
        return False
    return True


#: Inspect installed distributions and the PEP 508 environment without importing any installed packages.
_INSPECT = """\
import importlib.metadata as m, json, os, platform, sys
version = sys.implementation.version
implementation_version = f"{version.major}.{version.minor}.{version.micro}"
if version.releaselevel != "final":
    implementation_version += version.releaselevel[0] + str(version.serial)
environment = {
    "implementation_name": sys.implementation.name,
    "implementation_version": implementation_version,
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_python_implementation": platform.python_implementation(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "python_version": ".".join(platform.python_version_tuple()[:2]),
    "sys_platform": sys.platform,
}
installed = [(d.metadata["Name"], d.version) for d in m.distributions() if d.metadata["Name"]]
print(json.dumps({"installed": installed, "environment": environment}))
"""


def _check_missing(receipt: dict, present: set[str], environment: dict) -> bool:
    """Report absent applicable specs and unevaluable markers as distinct failures."""
    failed = {f.get("requirement") for f in receipt["failed"]}
    failed |= {spec for f in receipt["failed"] for spec in f.get("requirements", [])}
    complete = True
    for spec in receipt["planned"]:
        requirement = Requirement(spec)
        if _name(requirement.name) in present or spec in failed:
            continue
        try:
            if requirement.marker and not requirement.marker.evaluate(environment=environment):
                continue
        except (ValueError, KeyError):
            issue = _Issue("marker-undetermined", "could not evaluate the marker for this interpreter")
        else:
            issue = _Issue("not-installed", "planned but absent from the environment")
        _record(receipt, issue, requirement=spec)
        complete = False
    return complete


def _inspect(run: Callable, python: Path, args: argparse.Namespace, receipt: dict) -> bool:
    """Report actual requested distributions, check requested imports, and return whether every planned name is there.

    ``ready`` must mean the environment holds every planned name that applies to this host: a reused environment whose
    distribution was removed, or an install that skipped a name, records ``not-installed`` for it.
    """
    complete = False
    try:
        report = json.loads(run([str(python), "-I", "-c", _INSPECT]))
        installed = report["installed"]
        requested = {_name(Requirement(spec).name) for spec in receipt["planned"]}
        receipt["installed"] = sorted(
            ({"name": name, "version": version} for name, version in installed if _name(name) in requested),
            key=lambda item: _name(item["name"]),
        )
        complete = _check_missing(receipt, {_name(name) for name, _ in installed}, report["environment"])
    except (ValueError, KeyError, TypeError, _Issue) as exc:
        issue = exc if isinstance(exc, _Issue) else _Issue("installer-missing", str(exc))
        _record(receipt, issue, operation="inspect")
    for module in args.probe_import:
        try:
            run([str(python), "-I", "-c", "import importlib, sys; importlib.import_module(sys.argv[1])", module])
        except _Issue as issue:
            receipt["import_probes"][module] = {"imports": False, "detail": str(issue)}
        else:
            receipt["import_probes"][module] = {"imports": True}
    return complete


def _provision(repo: Path, digest: str, args: argparse.Namespace, receipt: dict, deadline: float) -> None:
    """Build or reuse the locked environment keyed by declarations, plan and interpreter."""
    root = environment_root()
    if root.is_relative_to(repo):
        raise ValueError("temporary environment root is inside the repository")
    base = Path(shutil.which(args.python) or args.python).absolute()
    info = base.stat()
    identity = [digest, receipt["planned"], "--no-deps", base.resolve().as_posix(), info.st_mtime_ns]
    bucket = root / hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    for directory in (root, bucket):
        directory.mkdir(mode=0o700, exist_ok=True)
        _owned_directory(directory)
    python = _env_python(bucket / "venv")
    uv = shutil.which("uv")

    def run(command: list[str]) -> str:
        """Give each child the smaller of the command timeout and remaining budget."""
        return _run(command, bucket, min(args.timeout, deadline - time.monotonic()))

    ready = bucket / "installed"
    with _lock(bucket, deadline):
        exists = python.is_file() and python.resolve() == python and (python.parent.parent / "pyvenv.cfg").is_file()
        if not exists:
            ready.unlink(missing_ok=True)
            _create(run, uv, str(base), python)
        receipt["env_python"] = str(python)
        receipt["reused"] = ready.is_file()
        if not receipt["reused"] and _install(run, uv, python, receipt):
            ready.touch()
        if not _inspect(run, python, args, receipt):
            ready.unlink(missing_ok=True)  # a planned name is absent: the next call installs again


def _host() -> dict:
    """Report observed host attributes and accelerator indicators without launching probes."""
    system, machine = platform.system(), platform.machine()
    accelerators = []
    if system == "Darwin" and machine == "arm64":
        accelerators.append({"kind": "apple-silicon-gpu", "evidence": "Darwin arm64"})
    if system == "Linux":
        devices = {"nvidia": "/dev/nvidia0", "amd": "/dev/kfd", "accelerator": "/dev/accel/accel0"}
        accelerators.extend(
            {"kind": kind, "evidence": device} for kind, device in devices.items() if Path(device).exists()
        )
    return {
        "os": system,
        "architecture": machine,
        "cpu_count": os.cpu_count(),
        "accelerators": accelerators,
        "accelerator_observation": "OS architecture and device nodes only",
    }


def _args(argv: list[str] | None) -> argparse.Namespace:
    """Parse CLI options before touching a project or environment."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--python", default=sys.executable)
    for option in ("--group", "--exclude", "--probe-import"):
        parser.add_argument(option, action="append", default=[])
    for option in ("--all-groups", "--no-extras", "--plan-only"):
        parser.add_argument(option, action="store_true")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--budget", type=float, default=90)
    args = parser.parse_args(argv)
    if any(not math.isfinite(value) or value <= 0 for value in (args.timeout, args.budget)):
        parser.error("--timeout and --budget must be positive and finite")
    if any(not _IMPORT.fullmatch(module) for module in args.probe_import):
        parser.error("--probe-import must be a dotted module name")
    return args


def _emit(receipt: dict, target: Path | None, repo: Path | None) -> None:
    """Write identical JSON to stdout and to an optional atomic, caller-private receipt."""
    payload = json.dumps(receipt, indent=2) + "\n"
    if target is not None:
        try:
            destination = target.resolve()
            if repo is None or destination.is_relative_to(repo) or target.is_symlink():
                raise ValueError("receipt must name a regular path outside the repository")
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                staging = Path(handle.name)
                try:
                    handle.write(payload.encode("utf-8"))
                    handle.close()
                    staging.replace(destination)
                finally:
                    staging.unlink(missing_ok=True)
        except (OSError, ValueError, RuntimeError) as exc:
            _record(receipt, _Issue("unsafe-spec", str(exc)), operation="receipt")
            if receipt["env_python"]:
                receipt["status"] = "partial"
            payload = json.dumps(receipt, indent=2) + "\n"
    sys.stdout.write(payload)


def main(argv: list[str] | None = None) -> int:
    """Read declarations and emit a receipt even when provisioning cannot finish."""
    args = _args(argv)
    receipt = {
        "status": "unavailable",
        "env_python": None,
        "planned": [],
        "installed": [],
        "skipped": [],
        "failed": [],
        "import_probes": dict.fromkeys(args.probe_import),
        "host": _host(),
        "plan_only": args.plan_only,
        "reused": False,
    }
    repo = None
    try:
        deadline = time.monotonic() + args.budget
        repo = (args.repo or _repo_root()).resolve(strict=True)
        if not repo.is_dir():
            raise ValueError("repository must be a directory")
        if tomllib is None:
            raise _Issue("no-toml-reader", "Python 3.10 requires the optional tomli reader")
        digest = _plan(repo, args, receipt, deadline)
        if not args.plan_only:
            _provision(repo, digest, args, receipt, deadline)
    except _Issue as issue:
        _record(receipt, issue)
    except TimeoutError as exc:
        _record(receipt, _Issue("timeout", str(exc)))
    except OSError as exc:
        _record(receipt, _Issue("installer-missing", str(exc)))
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        _record(receipt, _Issue("unsafe-spec", str(exc)))
    if receipt["env_python"]:
        failed_probe = any(not probe or not probe["imports"] for probe in receipt["import_probes"].values())
        receipt["status"] = "partial" if receipt["skipped"] or receipt["failed"] or failed_probe else "ready"
    _emit(receipt, args.out, repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
