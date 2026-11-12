#!/usr/bin/env python
"""Render a document's literal bootstrap token using its validated owning plugin.

Purpose: Bind emitted executable fragments to their owning plugin installation.
Scope: Literal bootstrap-token rendering for Markdown and shell templates.
Only ``${CLAUDE_PLUGIN_ROOT}`` is replaced. Shell quoting is preserved without
evaluating document text; other bytes, including line endings, pass through.
The selected install must own the loaded document, so mixed install versions
fail closed instead of emitting commands for another version.

Usage:
    PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
    python "$PLUGIN_ROOT/bin/render_plugin_root.py" <document.md> [--plugin-name <name>]

Outputs: Document bytes on stdout, with only the bootstrap token rendered.
Failure: Exit 1 for unreadable documents or untrusted roots; no partial output.
Used by: Mode, shared-document and reference loaders, and explicit raw-file loads.
"""  # noqa: W505 - the guarded bootstrap command is intentionally one physical line

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

from resolve_plugin_root import resolve

BOOTSTRAP_ROOT = "${CLAUDE_PLUGIN_ROOT}"
_FENCE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})", re.MULTILINE)


def serialize_root(root: str | Path) -> str:
    """Return an absolute shell-coordinate path with portable separators."""
    raw = str(root)
    if any(char in raw for char in "\x00\r\n"):
        raise ValueError("plugin root contains a control character")
    if PureWindowsPath(raw).is_absolute():
        return PureWindowsPath(raw).as_posix()
    if not PurePosixPath(raw).is_absolute():
        raise ValueError("plugin root must be absolute")
    return PurePosixPath(raw).as_posix()


def escape_root(root: str, quote: str) -> str:
    """Escape a literal path for the enclosing shell quote context."""
    if quote == '"':
        return re.sub(r'([\\"$`])', r"\\\1", root)
    if quote == "'":
        return root.replace("'", "'\\''")
    return shlex.quote(root)


class _ShellContext:
    """Track quote delimiters without expanding or executing shell text."""

    def __init__(self) -> None:
        """Start at an unquoted command boundary."""
        self.quote = ""
        self.escaped = False
        self.comment = False
        self.substitutions: list[tuple[str, int]] = []

    def _parenthesis(self, char: str) -> None:
        """Restore outer quoting when a nested command substitution closes."""
        if not self.substitutions or self.quote or self.substitutions[-1][1] == -1:
            return
        outer, depth = self.substitutions[-1]
        if char == "(":
            self.substitutions[-1] = outer, depth + 1
        elif char == ")":
            if depth == 1:
                self.substitutions.pop()
                self.quote = outer
            else:
                self.substitutions[-1] = outer, depth - 1

    def advance(self, text: str, index: int) -> int:
        """Consume one lexical delimiter and return the next source index."""
        char = text[index]
        if self.comment:
            self.comment = char != "\n"
        elif self.escaped:
            self.escaped = False
        elif char == "\\" and self.quote != "'":
            self.escaped = True
        elif text.startswith("$(", index) and self.quote != "'":
            self.substitutions.append((self.quote, 1))
            self.quote = ""
            return index + 2
        elif char == "`" and self.quote != "'":
            self._backtick()
        elif char == "#" and not self.quote and (index == 0 or text[index - 1] in " \t\r\n;&|()"):
            self.comment = True
        elif char == self.quote:
            self.quote = ""
        elif char in {"'", '"'} and not self.quote:
            self.quote = char
        else:
            self._parenthesis(char)
        return index + 1

    def _backtick(self) -> None:
        """Enter or close a legacy command substitution without inheriting its outer quote."""
        if self.substitutions and self.substitutions[-1][1] == -1:
            self.quote = self.substitutions.pop()[0]
        else:
            self.substitutions.append((self.quote, -1))
            self.quote = ""


def render_document(text: str, root: str | Path) -> str:
    """Replace only bootstrap tokens, quoting the validated path literally."""
    absolute = serialize_root(root)
    context = _ShellContext()
    rendered: list[str] = []
    index = 0
    while index < len(text):
        if (index == 0 or text[index - 1] == "\n") and _FENCE.match(text, index):
            context = _ShellContext()
            next_index = text.find("\n", index) + 1 or len(text)
            rendered.append(text[index:next_index])
            index = next_index
            continue
        if text.startswith(BOOTSTRAP_ROOT, index):
            rendered.append(escape_root(absolute, context.quote))
            index += len(BOOTSTRAP_ROOT)
            continue
        next_index = context.advance(text, index)
        rendered.append(text[index:next_index])
        index = next_index
    return "".join(rendered)


def _owner(path: Path) -> tuple[Path, str]:
    """Find the manifest that owns a document without following an escaped manifest."""
    for parent in path.resolve().parents:
        manifest = parent / ".claude-plugin/plugin.json"
        if not manifest.is_file():
            continue
        if not manifest.resolve().is_relative_to(parent):
            raise ValueError("document's plugin manifest escapes its root")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(name, str):
            raise ValueError("document's plugin identity is missing")
        return parent, name
    raise ValueError("document has no owning plugin manifest")


def render_file(path: Path, *, plugin_name: str | None = None) -> bytes:
    """Read bytes and render a token only when the selected install owns the file."""
    content = path.read_bytes()
    if BOOTSTRAP_ROOT.encode() not in content:
        return content
    owner, name = _owner(path)
    if plugin_name is not None and name != plugin_name:
        raise ValueError("document belongs to another plugin")
    selected = resolve(Path.home(), "borda-ai-rig", name)
    if selected is None or Path(selected).resolve() != owner:
        raise ValueError("document does not belong to the selected plugin install")
    return render_document(content.decode("utf-8"), selected).encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    """Emit one rendered document, failing before any partial output."""
    parser = argparse.ArgumentParser(description="Render a document's validated plugin bootstrap token.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--plugin-name")
    args = parser.parse_args(argv)
    try:
        content = render_file(args.file, plugin_name=args.plugin_name)
    except (OSError, ValueError) as exc:
        print(f"render_plugin_root: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(content)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
