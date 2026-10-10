"""Keep the Claude approval-record builder and the Codex contract's grant record on one schema.

Claude Code's ``plugins/cc_foundry/hooks/lib/approval-grants.js`` writes ``claude-git-approval.json``; Codex Rig writes
``codex-git-approval.json`` by following the JSON example in ``plugins/codex-rig/shared/native-skill-contract.md``
(§Local Git approval grant, the fence after "Write the grant only after"). Both are one ``approval-record`` family at
version 1 with the same common fields, so a reader of either host reads the other's record shape. This lives in the
repository's root ``tests/`` because it reads two plugins; neither plugin's own suite may reach into the other.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "plugins" / "cc_foundry" / "hooks" / "lib" / "approval-grants.js"
CONTRACT = ROOT / "plugins" / "codex-rig" / "shared" / "native-skill-contract.md"
ANCHOR = "Write the grant only after"


def _codex_example() -> dict:
    """Return the grant record the Codex contract prescribes: the first ``json`` fence after the anchor sentence.

    Examples:
        >>> _codex_example()["scope"]
        'local-git-non-destructive'
    """
    tail = CONTRACT.read_text(encoding="utf-8").split(ANCHOR, 1)[1]
    return json.loads(tail.split("```json\n", 1)[1].split("\n```", 1)[0])


@pytest.fixture(name="claude_record")
def _claude_record() -> dict:
    """Build the local Git record with the Claude lib's own builder, for the contract example's question text."""
    example = _codex_example()
    script = (
        f"const g = require({json.dumps(str(LIB))});"
        "const [question, at] = process.argv.slice(1);"
        "const r = g.buildRecord('local-git-non-destructive', question, 'Approve always', new Date(at));"
        "process.stdout.write(JSON.stringify(r));"
    )
    proc = subprocess.run(
        ["node", "-e", script, example["question"], example["created_at"]],
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=15,
    )
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="requires node to run the Claude record builder")
class TestLocalGitRecordParity:
    """The record Claude writes and the record Codex is told to write agree on schema and values."""

    def test_same_keys_in_the_same_order(self, claude_record: dict) -> None:
        """Both hosts write the five common fields in the contract's order and nothing else.

        A field one host adds and the other's reader does not expect is exactly the drift the shared family forbids.
        """
        assert list(claude_record) == list(_codex_example())

    def test_same_version_scope_and_answer(self, claude_record: dict) -> None:
        """Version, scope and the recording answer are the values each host's reader validates."""
        example = _codex_example()
        assert {k: claude_record[k] for k in ("version", "scope", "answer")} == {
            k: example[k] for k in ("version", "scope", "answer")
        }

    def test_same_created_at_format(self, claude_record: dict) -> None:
        """``created_at`` serializes the same instant identically: an ISO UTC timestamp with milliseconds."""
        assert claude_record["created_at"] == _codex_example()["created_at"]
