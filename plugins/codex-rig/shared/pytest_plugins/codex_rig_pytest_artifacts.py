"""Route pytest's selection history and requested coverage reports into workflow evidence.

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
cache. Requested file reports retain run-local destinations or go beside the raw coverage data. Tests that launch
another pytest inherit the stock cache and raw coverage settings and own their child process's file reports.

## Failure

An unsafe selection-cache entry fails pytest startup. File, configuration and coverage errors surface in pytest.

## Used by

The targeted PR remediation pytest launcher and the canonical gate runner share these pytest startup hooks.
"""

from __future__ import annotations

import errno
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from execnet import XSpec

_PLUGIN_OPTION = "-p codex_rig_pytest_artifacts"


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
        os.environ["PYTEST_ADDOPTS"] = f"{inherited}-o {shlex.quote(f'cache_dir={cache}')}"
        early_config.add_cleanup(lambda: os.environ.__setitem__("PYTEST_ADDOPTS", options))


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_setupnodes(specs: list[XSpec]) -> None:
    """Protect each local worker even when xdist replays a command-line cache override."""
    for spec in specs:
        if spec.popen:
            options = spec.env.get("PYTEST_ADDOPTS", os.environ.get("PYTEST_ADDOPTS", ""))
            spec.env["PYTEST_ADDOPTS"] = f"{options} {_PLUGIN_OPTION}".strip()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Redirect only selected file reports, using the active pytest-cov controller's resolved coverage settings."""
    plugin = session.config.pluginmanager.getplugin("_cov")
    if plugin is None or plugin.cov_controller is None or hasattr(session.config, "workerinput"):
        return
    controller = plugin.cov_controller
    reports = controller.cov_report
    allowed = Path(os.environ["CODEX_RIG_PYTEST_ARTIFACTS"])
    output = Path(os.environ["COVERAGE_FILE"]).parent
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
