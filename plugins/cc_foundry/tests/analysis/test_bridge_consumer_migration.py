"""Regression checks for consumer migration to the bridge plugin.

The fixture below deliberately names retired selectors. Current routing code and guidance must not contain them, while
this test and the root historical changelog may retain them to document and detect the migration.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
CONSUMER_PLUGINS = ("cc_foundry", "cc_oss", "cc_develop", "cc_research")
TARGET_SELECTOR = "bridge@borda-ai-rig"
TARGET_SKILLS = (
    'Skill(skill="bridge:implement"',
    'Skill(skill="bridge:advise"',
    'Skill(skill="bridge:review"',
)

# Fixture exception: these retired production literals are intentionally named
# here so the assertion can prevent their reintroduction outside test files.
RETIRED_LITERALS = (
    "codex@openai-codex",
    "codex:codex-rescue",
    "codex-companion",
    "/codex:",
    "openai/codex-plugin-cc",
    "codex-openai-codex",
    "openai-codex",
    # Detection probes, not selectors. The retired plugin was also recognized by the file
    # it shipped and by its CLI on PATH, so a consumer could keep gating on the old
    # integration without naming a single retired selector — which is how two live gates
    # survived the migration undetected.
    "codex-rescue.md",
    "command -v codex",
    '-name "codex*"',
)
PLACEHOLDER_ONLY_BRIEFS = (
    "args=<self-contained task",
    'args="<self-contained task',
    'args="Apply this fix: <issue description',
)
TEXT_SUFFIXES = {".json", ".js", ".md", ".py", ".toml", ".yaml", ".yml"}


def _production_files(plugin: str) -> list[Path]:
    """Return current shipped routing surfaces, excluding tests and the historical changelog."""
    root = REPOSITORY_ROOT / "plugins" / plugin
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and "tests" not in path.parts
        and path != root / "CHANGELOG.md"
        and path.suffix in TEXT_SUFFIXES
    ]


@functools.cache
def _plugin_texts(plugin: str) -> dict[Path, str]:
    """Read every shipped routing surface of one consumer once per session (the tree is immutable during a run)."""
    return {path: path.read_text(encoding="utf-8") for path in _production_files(plugin)}


def _all_texts() -> dict[Path, str]:
    """Merge the cached texts of every consumer plugin."""
    return {path: text for plugin in CONSUMER_PLUGINS for path, text in _plugin_texts(plugin).items()}


def _files_containing(literal: str) -> list[str]:
    """Return repository-relative names of consumer files that contain ``literal``."""
    return [path.relative_to(REPOSITORY_ROOT).as_posix() for path, text in _all_texts().items() if literal in text]


class TestConsumersUseBridge:
    """Every consumer plugin routes through the installed ``bridge`` plugin and nothing retired."""

    @pytest.mark.parametrize("plugin", CONSUMER_PLUGINS)
    def test_declares_the_installed_selector(self, plugin: str) -> None:
        """Each consumer plugin names the installed bridge selector somewhere in its shipped files.

        A consumer without the selector cannot resolve the bridge, so its delegated work silently has no target.
        """
        assert any(TARGET_SELECTOR in text for text in _plugin_texts(plugin).values())

    @pytest.mark.parametrize("retired", RETIRED_LITERALS)
    def test_omits_retired_literal(self, retired: str) -> None:
        """No consumer ships a retired selector or detection probe of the replaced integration.

        A leftover literal keeps a live gate on the old integration, which is how two gates once survived migration.
        """
        assert _files_containing(retired) == []

    @pytest.mark.parametrize("skill", TARGET_SKILLS)
    def test_uses_canonical_bridge_operation(self, skill: str) -> None:
        """Implementation, advice, and read-only review calls each stay reachable from some consumer."""
        assert _files_containing(skill) != []

    @pytest.mark.parametrize("placeholder", PLACEHOLDER_ONLY_BRIEFS)
    def test_ships_no_placeholder_only_brief(self, placeholder: str) -> None:
        """Executable bridge examples spell out a complete brief instead of a bare placeholder."""
        assert _files_containing(placeholder) == []
