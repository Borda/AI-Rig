"""Regression checks for consumer migration to the bridge plugin.

The fixture below deliberately names retired selectors. Production files must not contain them, while this test must
retain them to detect a regression.
"""

from __future__ import annotations

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
    """Return shipped text files for one consumer plugin."""
    root = REPOSITORY_ROOT / "plugins" / plugin
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and "tests" not in path.parts and path.suffix in TEXT_SUFFIXES
    ]


@pytest.mark.parametrize("plugin", CONSUMER_PLUGINS)
def test_consumers_select_the_installed_bridge(plugin: str) -> None:
    """Require every consumer to declare the installed bridge selector."""
    contents = "\n".join(path.read_text(encoding="utf-8") for path in _production_files(plugin))
    assert TARGET_SELECTOR in contents
    for retired in RETIRED_LITERALS:
        assert retired not in contents


def test_consumers_use_canonical_bridge_operations() -> None:
    """Keep implementation, advice, and read-only review calls available."""
    contents = "\n".join(
        path.read_text(encoding="utf-8") for plugin in CONSUMER_PLUGINS for path in _production_files(plugin)
    )
    for skill in TARGET_SKILLS:
        assert skill in contents


def test_bridge_calls_do_not_ship_placeholder_only_briefs() -> None:
    """Require executable bridge examples to spell out a complete brief contract."""
    contents = "\n".join(
        path.read_text(encoding="utf-8") for plugin in CONSUMER_PLUGINS for path in _production_files(plugin)
    )
    for placeholder in PLACEHOLDER_ONLY_BRIEFS:
        assert placeholder not in contents
