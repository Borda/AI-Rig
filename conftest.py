"""Repository-wide pytest setup: quiet Git maintenance, and an optional ``--shard`` split for CI.

Fixture commits can spawn detached maintenance that creates and removes ``objects/maintenance.lock`` while another
fixture copies the repository, causing ``shutil.copytree`` to fail. Set inherited Git config at import time so the
controller, xdist workers, and their children all avoid this race before fixtures run.

``--shard I/N`` runs one of N disjoint slices of the collected tests so one slow CI scope can spread across runners.
"""

from __future__ import annotations

import os
import zlib
from collections.abc import Mapping

import pytest


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
