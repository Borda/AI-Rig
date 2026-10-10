"""Exercise develop's shipped permission merges and standalone dispatcher contract."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
TRIMMED_ALLOWS = {
    "Bash(gh api repos/*)",
    "Bash(gh api graphql:*)",
    "Bash(git branch:*)",
    "Bash(git tag:*)",
}


def test_shipped_permissions_omit_broad_api_branch_and_tag_allows() -> None:
    """New installs must not auto-allow writes through the four broad rules."""
    allow = json.loads((PLUGIN / ".claude-plugin/permissions-allow.json").read_text(encoding="utf-8"))
    assert not TRIMMED_ALLOWS.intersection(allow)
    assert "Bash(gh api search/*)" in allow
    assert "Bash(git status:*)" in allow


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required to execute the shipped merge filters")
@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({}, id="fresh-install"),
        pytest.param(
            {
                "permissions": {
                    "allow": [*sorted(TRIMMED_ALLOWS), "Bash(gh api repos/*:*)", "Read(private/**)"],
                    "deny": ["Bash(gh pr merge:*)", "Bash(git branch -D:*)", "Write(private/**)"],
                },
                "theme": "dark",
            },
            id="upgrade-preserves-user-and-foundry-rules",
        ),
    ],
)
def test_shipped_merges_preserve_existing_rules_and_are_idempotent(settings: dict[str, object]) -> None:
    """Execute the actual jq filters without deleting existing broad allows or stale denies."""
    skill = (PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    result = settings
    for _ in range(2):
        previous = result
        for variable, kind in [("perms", "allow"), ("deny", "deny")]:
            match = re.search(rf"jq (--slurpfile {variable} [^\n]*) \\\n\s*'([^']+)'", skill)
            assert match is not None, f"Missing shipped {kind} merge filter"
            merged = subprocess.run(
                ["jq", *shlex.split(match[1].replace("$PLUGIN_ROOT", str(PLUGIN))), match[2]],
                input=json.dumps(result),
                text=True,
                capture_output=True,
                check=True,
            )
            result = json.loads(merged.stdout)
        if _ == 1:
            assert result == previous

    original_permissions = settings.get("permissions", {})
    for kind in ["allow", "deny"]:
        shipped = json.loads((PLUGIN / f".claude-plugin/permissions-{kind}.json").read_text(encoding="utf-8"))
        expected = set(original_permissions.get(kind, [])) | set(shipped)
        if kind == "allow":
            expected.discard("Bash(gh api repos/*:*)")
        assert result["permissions"][kind] == sorted(expected)
    assert {key: value for key, value in result.items() if key != "permissions"} == {
        key: value for key, value in settings.items() if key != "permissions"
    }


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to execute the shipped dispatcher")
def test_standalone_dispatcher_records_two_lanes_without_allowing_api_write(tmp_path: Path) -> None:
    """Absent optional read lane must produce valid audit evidence and no write auto-allow."""
    home = tmp_path / "home"
    home.mkdir()
    payload = {
        "session_id": "standalone-permission-check",
        "tool_use_id": "api-write-check",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": "gh api repos/example/project/issues -X POST -f title=example"},
    }
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "RIG_AUDIT": "1"}
    run = subprocess.run(
        ["node", str(PLUGIN / "hooks/allow-dispatch.js")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=tmp_path,
        check=True,
    )
    assert run.stdout == ""
    logs = list((home / ".claude/logs/audit").glob("*.jsonl"))
    assert len(logs) == 1
    records = [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    detail = records[0]["action_detail"]
    assert detail["decision"] == "passthrough"
    assert [verdict["lane"] for verdict in detail["verdicts"]] == ["blueprint", "shape"]
    assert all(verdict.get("why") != "module-error" for verdict in detail["verdicts"])
    verified = subprocess.run(
        [sys.executable, str(PLUGIN / "bin/verify_blueprint_audit.py"), "verify", str(logs[0].parent)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_setup_and_readme_explain_foundry_only_cleanup_and_upgrade_rules() -> None:
    """Documentation must disclose preserved upgrade allows and the single deny cleanup owner."""
    skill = (PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    assert "stale gh deny entries from older versions: run /foundry:setup" in skill
    assert "ships no git/gh guard" in readme
    assert "existing settings" in readme
    assert "manually" in readme
    assert "requires `foundry` plugin" in readme
