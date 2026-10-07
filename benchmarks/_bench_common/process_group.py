"""Process-group launch and teardown shared by the provider transports.

A wall-clock kill must reach every process the agent spawned, not only the one the runner launched. Killing the direct
child alone leaves grandchildren alive, still consuming paid budget outside the measured window and still holding the
pipes the runner is trying to drain.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from typing import Any

#: POSIX gets its own session, so the child leads a new process group and ``killpg``
#: reaches every descendant. Windows gets CREATE_NEW_PROCESS_GROUP, its nearest
#: equivalent for signalling a spawned tree.
NEW_PROCESS_GROUP: dict[str, Any] = (
    {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    if os.name == "nt"
    else {"start_new_session": True}
)


def terminate_process_group(process: subprocess.Popen) -> None:
    """Kill a timed-out child and every descendant it spawned.

    Falls back to killing the direct child alone when the platform has no process
    groups, or when the group has already exited.

    Args:
        process: The child launched with :data:`NEW_PROCESS_GROUP`.

    Examples:
        >>> import subprocess, sys
        >>> proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
        ...                         **NEW_PROCESS_GROUP)
        >>> terminate_process_group(proc)
        >>> _ = proc.wait()
    """
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    if killpg is not None and getpgid is not None:
        try:
            killpg(getpgid(process.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()
