"""Exercise setup's explicit approval recipes against conflicting user rule files."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _develop_text import bash_blocks

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/setup/SKILL.md"
_BASH = shutil.which("bash")


def test_setup_requires_explicit_invocation() -> None:
    """Conflict replacement must not be reachable by a model's automatic skill selection."""
    frontmatter = _SKILL.read_text(encoding="utf-8").split("---", 2)[1].splitlines()
    assert "disable-model-invocation: true" in frontmatter


@pytest.mark.integration
@pytest.mark.skipif(_BASH is None, reason="Bash is required to execute setup recipes")
@pytest.mark.parametrize("approved", [False, True])
def test_apply_recipe_preserves_or_replaces_conflict(approved: bool, tmp_path: Path) -> None:
    """The ordinary recipe preserves a real file; only explicit replacement creates a backed-up plugin link."""
    skill = _SKILL.read_text(encoding="utf-8")
    apply = skill.split("## Step 3: Apply", 1)[1].split("## Step 4: Conflicts", 1)[0]
    blocks = bash_blocks(apply)
    assert len(blocks) == 2, "Setup needs separate ordinary and explicitly approved recipes"
    conflicts = skill.split("## Step 4: Conflicts", 1)[1].split("## Step 5:", 1)[0]
    block = bash_blocks(conflicts)[0] if approved else blocks[0]
    home = tmp_path / "home"
    rules = home / ".claude/rules"
    rules.mkdir(parents=True)
    destination = rules / "develop-quality-gates.md"
    destination.write_text("User rule to preserve\n", encoding="utf-8")
    env = {
        **os.environ,
        "HOME": home.as_posix(),
        "USERPROFILE": home.as_posix(),
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run([_BASH, "-c", block], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert destination.is_symlink() is approved
    assert destination.read_text(encoding="utf-8") == (
        (_PLUGIN / "rules/quality-gates.md").read_text(encoding="utf-8") if approved else "User rule to preserve\n"
    )
    assert ("replaced (explicit approval):" if approved else "conflict, kept as-is:") in result.stdout
    assert [path.read_text(encoding="utf-8") for path in rules.glob(f"{destination.name}.bak-*")] == (
        ["User rule to preserve\n"] if approved else []
    )


@pytest.mark.integration
@pytest.mark.skipif(_BASH is None, reason="Bash is required to execute setup recipes")
def test_approve_recipe_keeps_real_user_files(tmp_path: Path) -> None:
    """Headless approval alone leaves a user's real file intact without producing a replacement backup."""
    skill = _SKILL.read_text(encoding="utf-8")
    apply = skill.split("## Step 3: Apply", 1)[1].split("## Step 4: Conflicts", 1)[0]
    block = bash_blocks(apply)[1]
    home = tmp_path / "home"
    rules = home / ".claude/rules"
    rules.mkdir(parents=True)
    destination = rules / "develop-quality-gates.md"
    destination.write_text("User rule to preserve\n", encoding="utf-8")
    env = {
        **os.environ,
        "HOME": home.as_posix(),
        "USERPROFILE": home.as_posix(),
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run([_BASH, "-c", block], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == "User rule to preserve\n"
    assert "conflict, kept as-is:" in result.stdout
    assert not list(rules.glob(f"{destination.name}.bak-*"))
