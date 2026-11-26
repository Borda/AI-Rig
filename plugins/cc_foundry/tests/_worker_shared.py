"""Build one expensive test artifact once per run and share it between pytest-xdist workers.

A ``scope="module"`` fixture runs once per worker, so under ``-n 4`` a 60-second build repeats up to four times. The
workers of one run share the parent of their base temporary directory, so the first worker to claim a lock file builds
the artifact there and the others read it. Without xdist the worker id is ``master`` and the artifact stays private.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

#: Seconds a waiting worker gives the building worker before it reports a stuck build.
_WAIT_SECONDS = 1800.0


def shared_artifact(
    tmp_path_factory: pytest.TempPathFactory, worker_id: str, name: str, build: Callable[[Path], None]
) -> Path:
    """Return the path of artifact ``name``, calling ``build(path)`` in exactly one worker to create it.

    Args:
        tmp_path_factory: The pytest temporary-path factory of the calling worker.
        worker_id: The xdist ``worker_id`` fixture value (``master`` without xdist).
        name: A file name unique to the artifact within the run.
        build: Writes the finished artifact to the path it is given; any exception propagates to the building worker.

    Returns:
        The artifact path, complete when returned: it appears atomically after ``build`` finishes.

    Raises:
        TimeoutError: Another worker held the build lock for longer than the wait bound.
    """
    base = tmp_path_factory.getbasetemp()
    shared = base if worker_id == "master" else base.parent
    target, lock = shared / name, shared / f"{name}.lock"
    deadline = time.monotonic() + _WAIT_SECONDS
    while not target.exists():
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"another worker did not finish building {name}") from None
            time.sleep(0.25)
            continue
        try:
            if not target.exists():
                partial = shared / f"{name}.{worker_id}.partial"
                build(partial)
                os.replace(partial, target)
        finally:
            os.close(descriptor)
            lock.unlink(missing_ok=True)
    return target
