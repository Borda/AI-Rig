"""Guard portable launchers in operative Codex helper recipes."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_skip_posix_shell_unavailable = pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell unavailable")


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_codex_helper_recipes_use_the_explicit_packaged_launcher() -> None:
    """Prevent inline recipes and installed global policy bypassing the portable launcher."""
    violations = []
    for path in PLUGIN_ROOT.rglob("*.md"):
        relative = path.relative_to(PLUGIN_ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(
                r"(?<![\w/-])python3?\s+(?:(?:PLUGIN_ROOT/|<installed-codex-rig-cache-root>/|plugins/codex-rig/)(?:shared|scripts|skills|runtime|tests)/|shared/)",
                line,
            ):
                violations.append(f"{relative}:{number}")

    assert not violations, "helper recipe bypasses packaged launcher: " + ", ".join(violations)
    skill = (PLUGIN_ROOT / "skills/code-review/SKILL.md").read_text(encoding="utf-8")
    assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/select-git-remote.py --canonical-pr-url" in skill
    assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/find-review-report.py --help" in skill
    policy = (PLUGIN_ROOT / "assets/AGENTS.md").read_text(encoding="utf-8")
    assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/escalation_ledger.py --ledger" in policy


@pytest.mark.packaging
@pytest.mark.integration
@pytest.mark.installed_plugin
@_skip_posix_shell_unavailable
@pytest.mark.parametrize("helper", ["select-git-remote.py", "find-review-report.py"])
def test_inline_review_recipe_runs_from_a_copied_package_with_only_python3(tmp_path: Path, helper: str) -> None:
    """Prevent inline review recipes relying on checkout context or a bare python executable."""
    installed = tmp_path / "installed package"
    shutil.copytree(PLUGIN_ROOT, installed)
    skill = (installed / "skills/code-review/SKILL.md").read_text(encoding="utf-8")
    commands = re.findall(r"`(PLUGIN_ROOT/bin/python [^`\n]+)`", skill)
    command = next(text for text in commands if f"PLUGIN_ROOT/shared/{helper} " in text)
    recipe = command.split()[:2]
    assert recipe == ["PLUGIN_ROOT/bin/python", f"PLUGIN_ROOT/shared/{helper}"]
    argv = [text.replace("PLUGIN_ROOT", str(installed)) for text in recipe] + ["--help"]
    search_path = tmp_path / "runtime path"
    search_path.mkdir()
    runtime = search_path / "python3"
    runtime.write_text(
        f'#!/bin/sh\nexec {shlex.quote(Path(sys.executable).as_posix())} "$@"\n', encoding="utf-8", newline="\n"
    )
    runtime.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(search_path)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [shutil.which("sh"), *argv], cwd=tmp_path, env=env, capture_output=True, text=True, check=False, timeout=30
    )

    assert not (search_path / "python").exists()
    assert not (installed / ".git").exists()
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"usage: {helper}" in result.stdout
    assert "--help" in result.stdout
