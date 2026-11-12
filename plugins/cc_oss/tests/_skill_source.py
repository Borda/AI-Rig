"""Read trigger-loaded skill sections in their original execution order."""

from pathlib import Path

_RELOCATED = {
    "resolve": (
        "run-coordination",
        "dispatch-coordination",
        "preflight",
        "handoff-detect",
        "report-merge",
        "item-selection",
        "checkout",
        "dispatch-closeout",
        "push",
        "final-report",
    ),
    "review": ("preflight", "scope-context", "agent-launch"),
    "release": ("delegation-strategy", "gather-changes", "scope-validation", "changelog-contributors", "notes-draft"),
}


def read_skill_source(path: Path) -> str:
    """Expand relocated bodies at their load blocks while retaining the caller's section headings."""
    text = path.read_text(encoding="utf-8")
    if path.name != "SKILL.md":
        return text
    variable = f"_OSS_{path.parent.name.upper()}"
    for name in _RELOCATED.get(path.parent.name, ()):
        marker = f'python "$PLUGIN_ROOT/bin/render_plugin_root.py" "${variable}/modes/{name}.md"'
        position = text.index(marker)
        begin = text.rindex("```bash\n", 0, position)
        end = text.index("```", position) + len("```")
        mode = (path.parent / "modes" / f"{name}.md").read_text(encoding="utf-8")
        text = text[:begin] + mode.split("\n\n", 4)[4] + text[end:]
    return text
