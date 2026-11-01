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
from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_skip_posix_shell_unavailable = pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell unavailable")
#: A bare ``python`` call on a packaged helper path, which skips the portable ``bin/python`` launcher.
_LAUNCHER_BYPASS = re.compile(
    r"(?<![\w/-])python3?\s+"
    r"(?:(?:PLUGIN_ROOT/|<installed-codex-rig-cache-root>/|plugins/codex-rig/)(?:shared|scripts|skills|runtime|tests)/|shared/)"
)


def _launcher_bypass_locations() -> list[str]:
    """Return ``path:line`` for each Markdown line that invokes a packaged helper without the launcher."""
    return [
        f"{path.relative_to(PLUGIN_ROOT).as_posix()}:{number}"
        for path in PLUGIN_ROOT.rglob("*.md")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _LAUNCHER_BYPASS.search(line)
    ]


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_no_markdown_recipe_bypasses_the_packaged_launcher() -> None:
    """No operative Markdown recipe runs a packaged helper through a bare ``python`` call.

    Inline recipes and the installed global policy must go through ``PLUGIN_ROOT/bin/python``, because a bare
    interpreter name does not exist on every supported host.
    """
    violations = _launcher_bypass_locations()

    assert not violations, "helper recipe bypasses packaged launcher: " + ", ".join(violations)


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_documented_recipes_name_the_explicit_packaged_launcher() -> None:
    """The code-review skill and the global policy spell their helper recipes with the packaged launcher.

    The absence check above would pass if a recipe vanished; this pins the three recipes that must exist.
    """
    skill = read_with_references(PLUGIN_ROOT / "skills/code-review/SKILL.md", "source-and-scope.md")
    policy = (PLUGIN_ROOT / "assets/AGENTS.md").read_text(encoding="utf-8")

    assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/select-git-remote.py --canonical-pr-url" in skill
    assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/find-review-report.py --help" in skill
    assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/escalation_ledger.py --ledger" in policy


@pytest.mark.packaging
@pytest.mark.integration
@pytest.mark.installed_plugin
@_skip_posix_shell_unavailable
@pytest.mark.parametrize("helper", ["select-git-remote.py", "find-review-report.py"])
def test_inline_review_recipe_runs_from_copied_package_with_only_python3(tmp_path: Path, helper: str) -> None:
    """A documented review recipe runs from a copied package when only ``python3`` is on PATH.

    The package is copied outside any checkout, the recipe is taken verbatim from the skill, and PATH holds a single
    ``python3`` shim; success proves the recipe needs neither checkout context nor a bare ``python`` executable.
    """
    installed = tmp_path / "installed package"
    shutil.copytree(PLUGIN_ROOT, installed)
    skill = read_with_references(installed / "skills/code-review/SKILL.md", "source-and-scope.md")
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
