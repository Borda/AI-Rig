"""Subprocess tests for ``hooks/rules-check.js``.

The hook fires on every ``SessionStart``. Foundry rules reach the model only through
``<config>/rules/foundry-<name>.md`` links created by ``/foundry:setup``; when setup never ran, every rule is silently
absent and commits go out without the mandated message format or co-author trailers. The hook names each missing rule
file and injects the commit rule. Every case builds a fake plugin root and config dir, passed through
``CLAUDE_PLUGIN_ROOT`` and ``CLAUDE_CONFIG_DIR``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _hook_env import _symlinks_supported

HOOK = Path(__file__).resolve().parents[3] / "hooks" / "rules-check.js"
_requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node executable not available")


_requires_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="file symlinks unavailable on this host")


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    """Plugin root shipping a commit rule, one other rule, and a non-loaded ``_full`` body."""
    root = tmp_path / "plugin"
    (root / "rules" / "_full").mkdir(parents=True)
    (root / "rules" / "git-commit.md").write_text("---\npaths:\n  - '**'\n---\n\nCOMMIT-RULE-BODY\n", encoding="utf-8")
    (root / "rules" / "python-code.md").write_text("PYTHON-RULE-BODY\n", encoding="utf-8")
    (root / "rules" / "_full" / "git-commit.md").write_text("FULL-BODY\n", encoding="utf-8")
    return root


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """Config dir that setup has not populated yet; its ``rules`` links are created per test."""
    return tmp_path / "config"


def _link(config: Path, root: Path, *names: str) -> None:
    """Install ``foundry-<name>`` links the way setup does."""
    rules = config / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    for name in names:
        (rules / f"foundry-{name}").symlink_to(root / "rules" / name)


def _run(config: Path, root: Path, payload: str = '{"hook_event_name":"SessionStart","source":"startup"}') -> str:
    """Run the hook and return its stdout."""
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config), "CLAUDE_PLUGIN_ROOT": str(root)}
    result = subprocess.run(
        ["node", str(HOOK)], input=payload, env=env, capture_output=True, text=True, encoding="utf-8", timeout=15
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@_requires_node
class TestRulesCheck:
    """Inject only when setup's rule links are missing."""

    def test_missing_links_name_every_rule_and_inject_the_commit_rule(self, plugin: Path, config: Path) -> None:
        """A blank config gets every rule path and the commit rule body without its frontmatter.

        Setup never ran, so no rule is loaded: the user must be told where the rules live and the commit rule, the one
        whose absence silently corrupts commits, is injected inline. The ``_full`` body stays out of the injection.
        """
        out = _run(config, plugin)

        assert "FOUNDRY RULES NOT INSTALLED: 2 of 2" in out
        assert "/foundry:setup" in out
        assert str(plugin / "rules" / "git-commit.md") in out
        assert str(plugin / "rules" / "python-code.md") in out
        assert "COMMIT-RULE-BODY" in out
        assert "paths:" not in out
        assert "FULL-BODY" not in out

    @_requires_symlinks
    def test_silent_when_every_rule_is_linked(self, plugin: Path, config: Path) -> None:
        """An installed setup leaves nothing to report.

        Both shipped rules are linked into the config dir, so the session is complete and a warning would be noise.
        """
        _link(config, plugin, "git-commit.md", "python-code.md")

        assert _run(config, plugin) == ""

    @_requires_symlinks
    def test_commit_rule_not_inlined_when_only_another_rule_is_missing(self, plugin: Path, config: Path) -> None:
        """A partial install names the missing rule but injects no commit text that is already loaded.

        The commit rule is linked, so the model already has it; repeating its body would only spend context.
        """
        _link(config, plugin, "git-commit.md")

        out = _run(config, plugin)

        assert "1 of 2" in out
        assert str(plugin / "rules" / "python-code.md") in out
        assert "COMMIT-RULE-BODY" not in out

    def test_other_events_stay_silent(self, plugin: Path, config: Path) -> None:
        """Only SessionStart triggers the check.

        A Stop payload against a blank config would warn if the event gate were missing.
        """
        assert _run(config, plugin, '{"hook_event_name":"Stop"}') == ""
