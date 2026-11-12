"""Require explicit registration before blueprint blocks may call mutating helpers."""

import build_blueprint_manifest as bbm
import pytest


@pytest.mark.parametrize(
    ("helper", "arguments"),
    [
        pytest.param("resolve_commit_hooks.py", "", id="inspect-hooks"),
        pytest.param("resolve_commit_hooks.py", " --commit-merge", id="commit-merge"),
        pytest.param(
            "retro_scan.py", ' --jsonl "$RETRO_JSONL" --out-dir "$RUN_DIR" --direction higher', id="retro-default"
        ),
        pytest.param(
            "retro_scan.py",
            ' --jsonl "$RETRO_JSONL" --out-dir "$RUN_DIR" --direction lower --threshold 0.01',
            id="retro-threshold",
        ),
        pytest.param("run_config_command.py", ' --state-json "$STATE" --kind metric', id="configured-metric"),
        pytest.param(
            "run_config_command.py",
            ' --state-json "$STATE" --kind guard --command-override "$GUARD_CMD"',
            id="configured-guard-override",
        ),
    ],
)
def test_mutating_helper_block_requires_registration(
    monkeypatch: pytest.MonkeyPatch, helper: str, arguments: str
) -> None:
    """Admit registered calls and refuse the same block if its boundary registration is absent."""
    command = f'python "${{CLAUDE_PLUGIN_ROOT}}/bin/{helper}"{arguments}'
    entries, dropped = bbm.block_entries(command, "skills/demo/SKILL.md:1")
    assert dropped == 0
    assert entries[bbm.sha256_text(command)]["kind"] == "block"

    monkeypatch.delitem(bbm._MUTATING_HELPERS, helper, raising=False)
    assert bbm.unlisted_command(command) == f"mutating helper {helper}"
    assert bbm.block_entries(command, "skills/demo/SKILL.md:1") == ({}, 1)


def test_unregistered_mutating_helper_block_is_refused() -> None:
    """Adding reviewed helpers must not admit an unknown deleting helper."""
    command = 'python "${CLAUDE_PLUGIN_ROOT}/bin/delete_reports.py"'
    assert bbm.unlisted_command(command) == "mutating helper delete_reports.py"
    assert bbm.block_entries(command, "skills/demo/SKILL.md:1") == ({}, 1)
