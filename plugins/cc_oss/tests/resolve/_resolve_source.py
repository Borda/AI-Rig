"""Read resolve instruction sections in their original order across trigger-loaded files."""

from pathlib import Path

_RELOCATED = ("handoff-detect", "report-merge", "item-selection", "push", "final-report")


def read_resolve_source(path: Path) -> str:
    """Read a source file, expanding the five relocated sections when reading SKILL.md.

    Existing contract slices span adjacent step headings, so insert each mode body at its load block rather than
    appending mode files after the workflow. Other sources are read unchanged.
    """
    text = path.read_text(encoding="utf-8")
    if path.name != "SKILL.md":
        return text
    for name in _RELOCATED:
        marker = f'cat "$_OSS_RESOLVE/modes/{name}.md"'
        position = text.index(marker)
        begin = text.rindex("```bash\n", 0, position)
        end = text.index("```", position) + len("```")
        mode = (path.parent / "modes" / f"{name}.md").read_text(encoding="utf-8")
        body = mode.split("\n\n", 4)[4]
        text = text[:begin] + body + text[end:]
    return text
