"""Exercise develop's shipped permission merges and standalone dispatcher contract."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _develop_text import flat

PLUGIN = Path(__file__).resolve().parents[2]
TRIMMED_ALLOWS = {"Bash(gh api repos/*)", "Bash(gh api graphql:*)", "Bash(git branch:*)", "Bash(git tag:*)"}

RETIRED_SHELL_ALLOWS = {"Bash(find:*)", "Bash(node:*)", "Bash(claude:*)", "Bash(sed:*)", "Bash(eval:*)"}


def _shipped_merge_args(skill: str) -> list[str]:
    """Return the arguments of setup's merge call, with ``$PLUGIN_ROOT`` pointing at this plugin."""
    lines = [line for line in skill.splitlines() if 'merge_settings_permissions.py" ' in line]
    assert len(lines) == 1, "setup must carry exactly one merge_settings_permissions.py call"
    call = lines[0].split('merge_settings_permissions.py" ', 1)[1].split(" || {", 1)[0]
    return shlex.split(call.replace("$PLUGIN_ROOT", PLUGIN.as_posix()))


def _shipped(kind: str) -> list[str]:
    """Return the plugin's shipped ``allow`` or ``deny`` permission rules."""
    return json.loads((PLUGIN / f".claude-plugin/permissions-{kind}.json").read_text(encoding="utf-8"))


def _merge(args: list[str], config: Path) -> dict[str, object]:
    """Run the shipped merge script once with the setup call's arguments and return the settings it wrote."""
    subprocess.run(
        [sys.executable, str(PLUGIN / "bin/merge_settings_permissions.py"), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(config.read_text(encoding="utf-8"))


def test_shipped_permissions_omit_broad_api_branch_and_tag_allows() -> None:
    """New installs must not auto-allow writes through the four broad rules.

    Besides the four trimmed rules and the retired shell grants, the fixed read-only rules must still be present.
    """
    allow = _shipped("allow")
    assert not (TRIMMED_ALLOWS | RETIRED_SHELL_ALLOWS).intersection(allow)
    assert {"Bash(python:*)", "Bash(node --check:*)", "Bash(claude plugin list)"} <= set(allow)
    assert "Bash(gh api search/*)" in allow
    assert "Bash(git status:*)" in allow


@pytest.mark.integration
@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({}, id="fresh-install"),
        pytest.param(
            {
                "permissions": {
                    "allow": [
                        *sorted(TRIMMED_ALLOWS | RETIRED_SHELL_ALLOWS),
                        "Bash(gh api repos/*:*)",
                        "Read(private/**)",
                    ],
                    "deny": ["Bash(gh pr merge:*)", "Bash(git branch -D:*)", "Write(private/**)"],
                },
                "theme": "dark",
            },
            id="upgrade-preserves-user-and-foundry-rules",
        ),
    ],
)
def test_shipped_merges_preserve_existing_rules_and_are_idempotent(
    settings: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execute setup's real merge call while retiring shell grants and preserving shared allows and stale denies.

    The real script runs twice against a sandbox home: the second run must change nothing, and the result keeps user
    order (obsolete rules dropped) with shipped rules appended once each in shipped-file order.
    """
    skill = (PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    args = _shipped_merge_args(skill)
    # The script refuses any settings file outside ~/.claude, so the sandbox home carries the target.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = tmp_path / ".claude/settings.json"
    config.parent.mkdir()
    config.write_text(json.dumps(settings), encoding="utf-8")

    first = _merge(args, config)
    result = _merge(args, config)

    original = settings.get("permissions", {})
    obsolete = RETIRED_SHELL_ALLOWS | {"Bash(gh api repos/*:*)"}
    kept_allow = [rule for rule in original.get("allow", []) if rule not in obsolete]
    expected = {
        "allow": list(dict.fromkeys(kept_allow + _shipped("allow"))),
        "deny": list(dict.fromkeys(original.get("deny", []) + _shipped("deny"))),
    }
    unrelated = {key: value for key, value in settings.items() if key != "permissions"}
    assert result == first
    assert result["permissions"] == expected
    assert {key: value for key, value in result.items() if key != "permissions"} == unrelated


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to execute the shipped dispatcher")
def test_standalone_dispatcher_records_two_lanes_without_allowing_api_write(tmp_path: Path) -> None:
    """Absent optional read lane must produce valid audit evidence and no write auto-allow.

    The shipped dispatcher runs under real Node with a ``gh api`` write payload: it must pass through, write one audit
    record naming the blueprint and shape lanes, and that record must verify with the shipped audit verifier.
    """
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
    logs = list((home / ".claude/logs/audit").glob("*.jsonl"))
    records = [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]
    detail = records[0]["action_detail"]
    verified = subprocess.run(
        [sys.executable, str(PLUGIN / "bin/verify_blueprint_audit.py"), "verify", str(logs[0].parent)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert run.stdout == ""
    assert (len(logs), len(records)) == (1, 1)
    assert detail["decision"] == "passthrough"
    assert [verdict["lane"] for verdict in detail["verdicts"]] == ["blueprint", "shape"]
    assert all(verdict.get("why") != "module-error" for verdict in detail["verdicts"])
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_setup_and_readme_explain_foundry_only_cleanup_and_upgrade_rules() -> None:
    """Documentation must disclose preserved upgrade allows and the single deny cleanup owner.

    The setup skill points users at ``/foundry:setup`` for stale deny entries, and the README states the same split.
    """
    skill = flat((PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8"))
    readme = flat((PLUGIN / "README.md").read_text(encoding="utf-8"))
    assert "stale gh deny entries from older versions: run /foundry:setup" in skill
    assert "ships no git/gh guard" in readme
    assert "existing settings" in readme
    assert "manually" in readme
    assert "requires `foundry` plugin" in readme
