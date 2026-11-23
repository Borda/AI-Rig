"""Route pytest's selection history and requested JUnit and coverage reports into workflow evidence.

## Purpose

Preserve pytest's configured last-failed, failed-first, new-first and stepwise selections when protecting a checkout.

## Scope

Runs inside the project's pytest process, after pytest has resolved its root, configuration and options. Only the built-
in selection keys are seeded. The project owns test selection, plugins, coverage settings and exit status.

## Usage

The targeted launcher and the quality gate load this module with pytest's explicit plugin option. They pass the allowed
run directory and absolute raw coverage path through the child environment, including for local xdist workers.

## Outputs

The first protected cache takes its selection state from the configured cache. Subsequent invocations reuse the run's
cache. Requested file reports retain run-local destinations or go beside the raw coverage data. A direct subprocess
launch of the same interpreter with -m pytest or -B -m pytest examines its own options. A child requesting JUnit gets a
separate evidence directory. Other direct pytest children retain their stock cache and coverage settings. Shell
commands, other interpreters and wrappers keep their own output behavior and the surrounding integrity checks.

## Failure

An unsafe selection-cache entry fails pytest startup. File, configuration and coverage errors surface in pytest.

## Used by

The targeted PR remediation pytest launcher and the canonical gate runner share these pytest startup hooks.
"""

from __future__ import annotations

import errno
import functools
import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from execnet import XSpec

_PLUGIN_OPTION = "-p codex_rig_pytest_artifacts"
_CHILD_BOOTSTRAP_KEYS = ("PYTHONPATH", "PYTEST_ADDOPTS", "CODEX_RIG_PYTEST_ARTIFACTS", "PYTHONDONTWRITEBYTECODE")


def _protect_pytest_children(config: pytest.Config, root: Path) -> None:
    """Bind direct nested pytest launches to separate evidence while preserving the child's own project options."""
    plugin_path = str(Path(__file__).resolve().parent)
    interpreter = (sys.executable, Path(sys.executable))
    scripts = Path(sysconfig.get_path("scripts"))
    suffix = ".exe" if os.name == "nt" else ""
    consoles = {scripts / f"{name}{suffix}" for name in ("pytest", "py.test")}
    original = subprocess.Popen.__init__
    signature = inspect.signature(original)

    @functools.wraps(original)
    def initialize(process: subprocess.Popen, args: Any, *positional: Any, **keywords: Any) -> None:
        if not isinstance(args, (list, tuple)) or not args or not isinstance(args[0], (str, Path)):
            return original(process, args, *positional, **keywords)
        program = Path(args[0])
        # Recognize only this interpreter's pytest launches. Other programs retain the caller's environment.
        module = 2 if len(args) > 1 and args[1] == "-B" else 1
        module_launch = args[0] in interpreter and tuple(args[module : module + 2]) == ("-m", "pytest")
        if not module_launch and program.name.lower() not in {"pytest", "py.test", "pytest.exe", "py.test.exe"}:
            return original(process, args, *positional, **keywords)
        # Resolve positional shell, executable and env arguments with the running interpreter's Popen signature.
        call = signature.bind(process, args, *positional, **keywords)
        override = call.arguments.get("executable")
        if call.arguments.get("shell") or (override is not None and os.fsdecode(override) != os.fspath(args[0])):
            return original(process, args, *positional, **keywords)
        selected = call.arguments.get("env")
        environment = dict(os.environ if selected is None else selected)
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items()):
            return original(process, args, *positional, **keywords)
        if not module_launch:
            # POSIX uses the child's cwd and PATH; on Windows shell=False resolves in the launching process.
            directory = (
                Path.cwd() if os.name == "nt" else Path(os.fsdecode(call.arguments.get("cwd") or os.curdir)).absolute()
            )
            if os.path.dirname(args[0]):
                executable = directory / program
            else:
                search = os.get_exec_path(None if os.name == "nt" else environment)
                found = shutil.which(str(program), path=os.pathsep.join(str(directory / entry) for entry in search))
                executable = Path(found) if found else None
            # The same installed console can be reached through a symlink or a noncanonical PATH entry.
            if executable is None or not any(
                console.is_file() and executable.is_file() and executable.samefile(console) for console in consoles
            ):
                return original(process, args, *positional, **keywords)

        options = environment.get("PYTEST_ADDOPTS", "")
        previous_path = environment.get("PYTHONPATH")
        paths = previous_path.split(os.pathsep) if previous_path else []
        environment.update(
            CODEX_RIG_PYTEST_ARTIFACTS=str(root),
            CODEX_RIG_PYTEST_CHILD=json.dumps({key: environment.get(key) for key in _CHILD_BOOTSTRAP_KEYS}),
            PYTEST_ADDOPTS=f"{options} {_PLUGIN_OPTION}".strip(),
            PYTHONPATH=os.pathsep.join([plugin_path, *(path for path in paths if path != plugin_path)]),
            PYTHONDONTWRITEBYTECODE="1",
        )
        call.arguments["env"] = environment
        original(*call.args, **call.kwargs)

    patch = pytest.MonkeyPatch()
    patch.setattr(subprocess.Popen, "__init__", initialize)
    config.add_cleanup(patch.undo)


def _seed_selection(source: Path, cache: Path) -> None:
    """Publish a complete initial cache without following links or replacing another process's history."""
    if cache.exists() or not source.exists():
        return
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache.parent) as staging:
        pending = Path(staging) / "pytest-cache"
        history = pending / "v/cache"
        history.mkdir(parents=True)
        for name in ("lastfailed", "nodeids", "stepwise"):
            entry = source / "v/cache" / name
            if entry.resolve() != Path(os.path.abspath(entry)):
                raise pytest.UsageError(f"cannot seed pytest selection through a symbolic link: {entry}")
            if entry.exists():
                if not entry.is_file():
                    raise pytest.UsageError(f"expected a regular pytest selection file: {entry}")
                shutil.copyfile(entry, history / name)
        try:
            pending.rename(cache)
        except OSError as error:
            # Another controller may have published its fully initialized cache while these files were read.
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not cache.is_dir():
                raise


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    """Prepare selection history and a stock child environment before importing the project's conftests."""
    nested = os.environ.pop("CODEX_RIG_PYTEST_CHILD", None)
    if nested is not None:
        previous = json.loads(nested)
        root = Path(os.environ["CODEX_RIG_PYTEST_ARTIFACTS"])
        if not getattr(early_config.known_args_namespace, "xmlpath", None):
            # Only the child knows its ini and CLI. Drop the bootstrap before importing project conftests.
            plugin_path = str(Path(__file__).resolve().parent)
            if plugin_path not in (previous["PYTHONPATH"] or "").split(os.pathsep) and plugin_path in sys.path:
                sys.path.remove(plugin_path)
            for key in _CHILD_BOOTSTRAP_KEYS:
                value = previous[key]
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            _protect_pytest_children(early_config, root)
            early_config.pluginmanager.unregister(sys.modules[__name__])
            return
        if root.resolve() != root:
            raise pytest.UsageError(f"nested pytest evidence directory was redirected: {root}")
        root.mkdir(parents=True, exist_ok=True)
        child = Path(tempfile.mkdtemp(prefix="pytest-child-", dir=root))
        os.environ.update(CODEX_RIG_PYTEST_ARTIFACTS=str(child), COVERAGE_FILE=str(child / ".coverage"))
    cache = Path(os.environ["COVERAGE_FILE"]).with_name("pytest-cache")
    if early_config.pluginmanager.hasplugin("cacheprovider"):
        source = pytest.Cache.cache_dir_from_config(early_config, _ispytest=True)
        if source.resolve().is_relative_to(Path(os.environ["CODEX_RIG_PYTEST_ARTIFACTS"])):
            cache = source
        else:
            if not early_config.known_args_namespace.cacheclear:
                _seed_selection(source, cache)
            # Pytest has already parsed ini overrides. Its config has no public scalar ini setter at this hook.
            early_config._inicache["cache_dir"] = str(cache)

    # Project conftests can launch another pytest during import with PYTHONPATH intentionally cleared.
    options = os.environ.get("PYTEST_ADDOPTS", "")
    if options.endswith(_PLUGIN_OPTION):
        inherited = options.removesuffix(_PLUGIN_OPTION)
        child_options = f"{inherited}-o {shlex.quote(f'cache_dir={cache}')}"
        os.environ["PYTEST_ADDOPTS"] = child_options
        early_config.add_cleanup(lambda: os.environ.__setitem__("PYTEST_ADDOPTS", options))
        _protect_pytest_children(early_config, Path(os.environ["CODEX_RIG_PYTEST_ARTIFACTS"]))


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_setupnodes(specs: list[XSpec]) -> None:
    """Protect each local worker even when xdist replays a command-line cache override."""
    for spec in specs:
        if spec.popen:
            options = spec.env.get("PYTEST_ADDOPTS", os.environ.get("PYTEST_ADDOPTS", ""))
            spec.env["PYTEST_ADDOPTS"] = f"{options} {_PLUGIN_OPTION}".strip()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Redirect requested file reports after their plugins have resolved the project's settings."""
    allowed = Path(os.environ["CODEX_RIG_PYTEST_ARTIFACTS"])
    output = Path(os.environ["COVERAGE_FILE"]).parent
    # Pytest expands user and environment variables in the JUnit path and creates a writer only on the controller.
    junit = session.config.pluginmanager.getplugin("junitxml")
    writer = session.config.stash.get(junit.xml_key, None) if junit is not None else None
    if writer is not None and not Path(writer.logfile).resolve().is_relative_to(allowed):
        destination = output / "junit.xml"
        if not destination.resolve().is_relative_to(allowed):
            raise pytest.UsageError(f"JUnit report destination escapes run directory: {destination}")
        writer.logfile = str(destination)

    plugin = session.config.pluginmanager.getplugin("_cov")
    if plugin is None or plugin.cov_controller is None or hasattr(session.config, "workerinput"):
        return
    controller = plugin.cov_controller
    reports = controller.cov_report
    # Coverage resolves XML/JSON/HTML/LCOV defaults from its own config. pytest-cov resolves Markdown names itself.
    for kind, option, default in (
        ("xml", "xml:output", "coverage.xml"),
        ("json", "json:output", "coverage.json"),
        ("html", "html:directory", "htmlcov"),
        ("lcov", "lcov:output", "coverage.lcov"),
        ("annotate", None, "annotated"),
        ("markdown", None, "coverage.md"),
        ("markdown-append", None, "coverage-append.md"),
    ):
        if kind not in reports:
            continue
        selected = reports[kind] or (controller.cov.get_option(option) if option else None)
        if selected == "-" and kind in ("xml", "json", "lcov"):
            continue
        if selected and Path(selected).resolve().is_relative_to(allowed):
            continue
        destination = output / default
        if not destination.resolve().is_relative_to(allowed):
            raise pytest.UsageError(f"coverage report destination escapes run directory: {destination}")
        reports[kind] = str(destination)
