"""Repository-wide pytest setup: quiet Git maintenance, and an optional ``--shard`` split for CI.

Fixture commits can spawn detached maintenance that creates and removes ``objects/maintenance.lock`` while another
fixture copies the repository, causing ``shutil.copytree`` to fail. Set inherited Git config at import time so the
controller, xdist workers, and their children all avoid this race before fixtures run.

``--shard I/N`` runs one of N disjoint slices of the collected tests so one slow CI scope can spread across runners.
"""

from __future__ import annotations

import json
import os
import shutil
import zlib
from collections.abc import Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

#: Plugins whose helper resolver finds a plugin only through an install record or the plugin cache, by plugin name.
_CLAUDE_PLUGINS = {
    "develop": "cc_develop",
    "foundry": "cc_foundry",
    "oss": "cc_oss",
    "research": "cc_research",
    "codemap-py": "codemap-py",
}

#: Test path fragments whose subprocesses resolve plugins as installed users do.
_INSTALLED_PLUGIN_TEST_DIRS = ("/plugins/cc_", "/plugins/codemap-py/")

#: Marketplace directory the resolver's cache tier reads.
_MARKETPLACE = "borda-ai-rig"


def disable_git_maintenance(environment: Mapping[str, str]) -> dict[str, str]:
    """Return a copy with maintenance disabled, preserving other indexed Git config entries."""
    updated = dict(environment)
    count = int(updated.get("GIT_CONFIG_COUNT", "0"))
    maintenance_entries = [
        index for index in range(count) if updated[f"GIT_CONFIG_KEY_{index}"].lower() == "maintenance.auto"
    ]
    if maintenance_entries:
        for index in maintenance_entries:
            updated[f"GIT_CONFIG_VALUE_{index}"] = "false"
        return updated

    updated[f"GIT_CONFIG_KEY_{count}"] = "maintenance.auto"
    updated[f"GIT_CONFIG_VALUE_{count}"] = "false"
    updated["GIT_CONFIG_COUNT"] = str(count + 1)
    return updated


os.environ.update(disable_git_maintenance(os.environ))


def shard_index(nodeid: str, total: int) -> int:
    """Return the zero-based shard that owns a test, stable across processes, runs, and collection order.

    Hashing the node id instead of counting positions keeps the controller and every xdist worker in agreement, and
    spreads the many cases of one heavy parametrized test across shards instead of grouping them.

    Examples:
        >>> shard_index("tests/test_a.py::test_x", 1)
        0
        >>> 0 <= shard_index("tests/test_a.py::test_x", 3) < 3
        True
    """
    return zlib.crc32(nodeid.encode("utf-8")) % total


def parse_shard(value: str) -> tuple[int, int]:
    """Parse ``I/N`` into a one-based index and a shard count, rejecting anything outside ``1 <= I <= N``.

    Examples:
        >>> parse_shard("2/3")
        (2, 3)
    """
    index, _, total = value.partition("/")
    if not (index.isdecimal() and total.isdecimal() and 1 <= int(index) <= int(total)):
        raise pytest.UsageError(f"--shard expects I/N with 1 <= I <= N, got {value!r}")
    return int(index), int(total)


@pytest.fixture(scope="session")
def claude_plugin_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a home directory holding an installed copy of every Claude plugin in the plugin cache.

    Skill blocks resolve their plugin through the install record, then the plugin cache, and only then through the
    checkout that holds the executing resolver, and only while the working directory is inside that checkout. A test
    that runs a block from a temporary directory therefore finds a plugin only when its home has one installed. A
    developer machine has; a clean CI runner does not, so such tests passed locally and failed in CI. Copies leave out
    tests and bytecode, as a published install does.
    """
    home = tmp_path_factory.mktemp("claude-home")
    cache = home / ".claude" / "plugins" / "cache" / _MARKETPLACE
    for name, directory in _CLAUDE_PLUGINS.items():
        source = ROOT / "plugins" / directory
        manifest = json.loads((source / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        shutil.copytree(
            source,
            cache / name / manifest["version"],
            ignore=shutil.ignore_patterns("tests", "__pycache__", "node_modules", ".pytest_cache"),
        )
    return home


@pytest.fixture(autouse=True)
def _installed_claude_plugins(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the home of Claude plugin tests at the installed copies, so their subprocesses resolve plugins as users do.

    Applies only to tests under ``plugins/cc_*`` and ``plugins/codemap-py``; a test that sets its own ``HOME`` still
    overrides this one.
    """
    path = request.node.path.as_posix()
    if not any(fragment in path for fragment in _INSTALLED_PLUGIN_TEST_DIRS):
        return
    home = request.getfixturevalue("claude_plugin_home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--shard I/N`` for splitting one test selection across CI runners."""
    parser.addoption(
        "--shard",
        default=None,
        metavar="I/N",
        help="Run only slice I (1-based) of N disjoint slices of the collected tests.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep only the tests owned by the requested shard and report the rest as deselected."""
    value = config.getoption("--shard")
    if value is None:
        return
    index, total = parse_shard(value)
    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        (kept if shard_index(item.nodeid, total) == index - 1 else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = kept
