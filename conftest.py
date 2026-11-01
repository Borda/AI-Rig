"""Disable Git auto-maintenance for pytest and every subprocess it starts.

Fixture commits can spawn detached maintenance that creates and removes ``objects/maintenance.lock`` while another
fixture copies the repository, causing ``shutil.copytree`` to fail. Set inherited Git config at import time so the
controller, xdist workers, and their children all avoid this race before fixtures run.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


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
