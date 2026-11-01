"""Fixtures shared by more than one foundry hook test module."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from _hook_env import _hook_tmp_base


@pytest.fixture(name="sid")
def _sid(tmp_path: Path) -> Iterator[str]:
    """Yield a unique session id; clean its ``claude-state-<id>`` dir on teardown.

    Base resolved via ``_hook_tmp_base()`` so teardown targets the same directory the hook's ``getSentinelDir()`` writes
    on this platform.
    """
    s = f"pytest-{tmp_path.name}-{uuid4().hex}"
    yield s
    shutil.rmtree(_hook_tmp_base() / f"claude-state-{s}", ignore_errors=True)
