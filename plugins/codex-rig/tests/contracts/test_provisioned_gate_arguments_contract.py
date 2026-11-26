"""The shared contract names the gate arguments the provisioning helper actually emits, flag by flag.

``gate_env_provision.py plan`` returns ``run_gates_args`` carrying ``--tool-cache`` for every gate run and
``worktree_run_gates_args`` carrying ``--project-env`` for a worktree-bound run. A contract that attributes a flag to
the wrong field makes a workflow run its gates without the provisioned project environment.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: Each emitted argument field and the single flag it carries, as the provisioning helper's own tests pin them.
EMITTED_FIELDS = {"run_gates_args": "--tool-cache", "worktree_run_gates_args": "--project-env"}
FIELD_FLAG = re.compile(r"`([a-z_]+_args)` \(`(--[a-z-]+)`(?:, `(--[a-z-]+)`)*\)")


def _field_flags(document: str) -> dict[str, list[str]]:
    """Return every ``field (flags)`` attribution on the lines of a document that mention gate arguments."""
    attributions: dict[str, list[str]] = {}
    for line in (ROOT / document).read_text(encoding="utf-8").splitlines():
        if "run_gates_args" not in line:
            continue
        for match in FIELD_FLAG.finditer(line):
            flags = re.findall(r"`(--[a-z-]+)`", match.group(0))
            attributions.setdefault(match.group(1), []).extend(flags)
    return attributions


def test_native_contract_attributes_each_flag_to_its_emitted_field() -> None:
    """Every field the contract names carries exactly the flag the helper puts in it, and both fields are named."""
    attributions = _field_flags("shared/native-skill-contract.md")

    assert {field: flags for field, flags in attributions.items()} == {
        field: [flag] for field, flag in EMITTED_FIELDS.items()
    }
