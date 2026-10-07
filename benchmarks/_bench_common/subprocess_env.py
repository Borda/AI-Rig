"""Minimal child environments that still let an interpreter start on every host.

A scoring boundary that micro-executes model-authored code hands the child the smallest environment that can run it.
"Smallest" is not the same set on every platform: a Windows CPython reads ``SystemRoot`` while seeding its hash
randomization and aborts before the payload runs when the variable is absent, so an environment pruned to ``PATH`` alone
is not minimal there — it is broken.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

#: Windows-only names a freshly spawned CPython needs before it reaches the payload:
#: ``SystemRoot``/``SYSTEMROOT``/``SystemDrive`` locate the CSPRNG used by
#: ``_Py_HashRandomization_Init``, ``COMSPEC``/``PATHEXT`` keep executable resolution
#: intact, and ``TEMP``/``TMP`` keep ``tempfile`` usable. None of them carry user
#: configuration, so forwarding them does not widen what the child can observe.
_WINDOWS_STARTUP_NAMES: tuple[str, ...] = (
    "SystemRoot",
    "SYSTEMROOT",
    "SystemDrive",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
)


def minimal_child_env(source: Mapping[str, str] | None = None, *, platform: str = sys.platform) -> dict[str, str]:
    """Return the smallest environment a spawned interpreter still needs to start.

    Args:
        source: Environment to draw from; defaults to the current process environment.
        platform: Target platform identifier, in :data:`sys.platform` form. Passed
            explicitly by tests so Windows behaviour is exercised from any host.

    Returns:
        A new mapping holding ``PATH`` plus, on Windows, the startup variables a child
        CPython requires. Absent names are omitted rather than forwarded as empty.

    Examples:
        >>> minimal_child_env({"PATH": "/usr/bin", "SECRET": "x"}, platform="linux")
        {'PATH': '/usr/bin'}
        >>> minimal_child_env({"PATH": "C:\\\\Windows", "SystemRoot": "C:\\\\Windows",
        ...                    "SECRET": "x"}, platform="win32")
        {'PATH': 'C:\\\\Windows', 'SystemRoot': 'C:\\\\Windows'}
    """
    environ = os.environ if source is None else source
    env = {"PATH": environ.get("PATH", "")}
    if platform == "win32":
        env.update({name: environ[name] for name in _WINDOWS_STARTUP_NAMES if name in environ})
    return env
