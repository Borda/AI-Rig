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

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "plugins" / "cc_foundry" / "hooks" / "lib" / "approval-grants.js"
CONTRACT = ROOT / "plugins" / "codex-rig" / "shared" / "native-skill-contract.md"
ANCHOR = "Write the grant only after"
_COMMON_FIELDS = ("version", "scope", "answer")


def _codex_example() -> dict:
    """Return the grant record the Codex contract prescribes: the first ``json`` fence after the anchor sentence.

    Examples:
        >>> _codex_example()["scope"]
        'local-git-non-destructive'
    """
    tail = CONTRACT.read_text(encoding="utf-8").split(ANCHOR, 1)[1]
    return json.loads(tail.split("```json\n", 1)[1].split("\n```", 1)[0])


@pytest.fixture(scope="module", name="codex_example")
def _codex_example_fixture() -> dict:
    """Parse the contract's example record once; the tests only read it."""
    return _codex_example()


@pytest.fixture(scope="module", name="claude_record")
def _claude_record(codex_example: dict) -> dict:
    """Build the local Git record with the Claude lib's own builder, for the contract example's question text.

    The real Node builder is the unit under test, so it runs once per module; the checks only read the record.
    """
    script = (
        f"const g = require({json.dumps(str(LIB))});"
        "const [question, at] = process.argv.slice(1);"
        "const r = g.buildRecord('local-git-non-destructive', question, 'Approve always', new Date(at));"
        "process.stdout.write(JSON.stringify(r));"
    )
    proc = subprocess.run(
        ["node", "-e", script, codex_example["question"], codex_example["created_at"]],
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=15,
    )
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="requires node to run the Claude record builder")
class TestLocalGitRecordParity:
    """The record Claude writes and the record Codex is told to write agree on schema and values."""

    def test_keys_match_in_order(self, claude_record: dict, codex_example: dict) -> None:
        """Both hosts write the five common fields in the contract's order and nothing else.

        A Claude-built record and the Codex contract example start from the same question and timestamp. A field one
        host adds and the other's reader does not expect is exactly the drift the shared family forbids.
        """
        assert list(claude_record) == list(codex_example)

    def test_version_scope_and_answer_match(self, claude_record: dict, codex_example: dict) -> None:
        """Version, scope and the recording answer are the values each host's reader validates.

        A reader on either host rejects a record whose version, scope or answer differs from what it expects, so the two
        hosts must agree on these three literals.
        """
        assert {k: claude_record[k] for k in _COMMON_FIELDS} == {k: codex_example[k] for k in _COMMON_FIELDS}

    def test_created_at_format_matches(self, claude_record: dict, codex_example: dict) -> None:
        """``created_at`` serializes the same instant identically: an ISO UTC timestamp with milliseconds.

        The builder is handed the contract example's own timestamp, so any difference is a serialization-format drift.
        """
        assert claude_record["created_at"] == codex_example["created_at"]
