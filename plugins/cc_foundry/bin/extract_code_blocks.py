#!/usr/bin/env python
"""extract_code_blocks.py — extract fenced code blocks from Markdown files.

Walks a directory tree, extracts every fenced code block (``` ... ```) and
classifies each as programming code vs. prose/markdown using a three-tier
heuristic: known code marker → is_code=True; known non-code marker →
is_code=False (with content override); unknown/no marker → content heuristic.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/extract_code_blocks.py" <dir> [options]

Arguments:
    dir                 Root directory to search for .md files.

Options:
    --include PATTERN   Glob pattern for files (default: *.md).
    --all               Include non-code blocks in output (default: code only).
    --min-tokens N      Skip blocks with estimated token count below N.

Output:
    JSONL to stdout — one JSON object per block:
    {"file": "...", "lang_marker": "bash", "lang_detected": "bash",
     "line_start": 42, "line_end": 58, "token_estimate": 87,
     "is_code": true, "content": "..."}

Exit codes:
    0   success (even if no blocks found)
    1   argument error
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Language classification tables
# ---------------------------------------------------------------------------

#: Fence language tags that mark a block as executable or programming code.
CODE_MARKERS: frozenset[str] = frozenset(
    {
        # Shell
        "bash",
        "sh",
        "zsh",
        "fish",
        "shell",
        "powershell",
        "ps1",
        # Python
        "python",
        "python3",
        "py",
        # JavaScript / TypeScript
        "javascript",
        "js",
        "typescript",
        "ts",
        "node",
        "jsx",
        "tsx",
        # Other scripting
        "ruby",
        "rb",
        "perl",
        "pl",
        "php",
        "lua",
        # Compiled
        "go",
        "rust",
        "rs",
        "java",
        "c",
        "cpp",
        "c++",
        "csharp",
        "cs",
        "swift",
        "kotlin",
        "scala",
        "r",
        "julia",
        "haskell",
        "elixir",
        # Data query
        "sql",
        "graphql",
        "gql",
        # Build / deploy
        "dockerfile",
        "makefile",
        "cmake",
        "gradle",
        # Structured data (repeated configs across files = extraction candidate)
        "json",
        "yaml",
        "yml",
        "toml",
        "xml",
        # Text processing
        "awk",
        "sed",
        "diff",
        "patch",
    }
)

#: Fence language tags that mark a block as prose, sample output or logs rather than code.
NON_CODE_MARKERS: frozenset[str] = frozenset(
    {
        "text",
        "plain",
        "plaintext",
        "markdown",
        "md",
        "rst",
        "adoc",
        "output",
        "log",
        "logs",
        "console",
        "terminal",
        "example",
        "sample",
        "raw",
    }
)

#: Maps language tag aliases to one canonical language name, such as ``zsh`` to ``bash``.
LANG_NORMALIZE: dict[str, str] = {
    "sh": "bash",
    "zsh": "bash",
    "fish": "bash",
    "shell": "bash",
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "rb": "ruby",
    "pl": "perl",
    "rs": "rust",
    "cpp": "c++",
    "cs": "csharp",
    "yml": "yaml",
    "gql": "graphql",
    "ps1": "powershell",
}

#: Lines strongly indicating programming code
_CODE_SIGNAL = re.compile(
    r"(?m)"
    r"(?:"
    r"^\s*#!"  # shebang
    r"|^\s*\$\s+\S"  # shell prompt line ($  command)
    r"|\$\{"  # shell variable expansion ${VAR}
    r"|\$\("  # shell subshell $(...)
    r"|&&|\|\|"  # logical operators
    r"|;\s*$"  # semicolon at line end
    r"|\bdef\s+\w"  # function def (Python, Ruby)
    r"|\bimport\s+\w"  # import statement
    r"|\bclass\s+\w"  # class definition
    r"|\bfunction\s*[\w(]"  # function keyword (JS, PHP)
    r"|\bconst\s+\w"  # const declaration
    r"|\blet\s+\w"  # let declaration
    r"|\bvar\s+\w"  # var declaration
    r"|if\s*\["  # bash if [
    r"|\bfor\s*\("  # C-style for loop
    r"|\bwhile\s*\("  # while loop
    r"|=>"  # arrow operator
    r"|[!=<>]="  # comparison operators (==, !=, <=, >=)
    r"|[+\-*/%]="  # compound assignment (+=, -=, *=, /=, %=)
    r"|\breturn\b"  # return statement
    r"|->(?!\s*[A-Z])"  # pointer/method chain (not markdown → Title)
    r")"
)

#: Matches a line ending in sentence punctuation, evidence that an untagged block is prose.
_PROSE_END = re.compile(r"[.,?!]\s*$")
#: Matches an opening backtick or tilde fence, capturing the fence and its language tag.
_FENCE_OPEN = re.compile(r"^(`{3,}|~{3,})([\w.+\-]*)")
#: One blockquote marker: up to three spaces of indent, ``>``, and one optional following space.
_QUOTE_MARKER = re.compile(r"^ {0,3}> ?")

#: Guard against pathological inputs that would exhaust heap memory when read
#: in one shot. 10 MB is well above any realistic Markdown / agent file.
_MAX_FILE_SIZE = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CodeBlock:
    """A single fenced code block extracted from a Markdown file."""

    file: str
    lang_marker: str
    lang_detected: str
    line_start: int  # 1-based, inclusive (opening fence line)
    line_end: int  # 1-based, inclusive (closing fence line)
    token_estimate: int
    is_code: bool
    content: str


# ---------------------------------------------------------------------------
# Pure functions (testable via doctest)
# ---------------------------------------------------------------------------


def normalize_lang(marker: str) -> str:
    """Return canonical language name for a raw fence marker.

    Args:
        marker: Raw language marker from the fence (may be empty).

    Returns:
        Normalised lowercase name, or the original lowercased marker if unknown.

    Examples:
        >>> normalize_lang("py")
        'python'
        >>> normalize_lang("yml")
        'yaml'
        >>> normalize_lang("bash")
        'bash'
        >>> normalize_lang("PYTHON3")
        'python'
        >>> normalize_lang("")
        ''
        >>> normalize_lang("unknown")
        'unknown'
    """
    lower = marker.lower()
    return LANG_NORMALIZE.get(lower, lower)


def estimate_tokens(content: str) -> int:
    """Estimate token count using a 4-chars-per-token heuristic.

    Args:
        content: Raw block content (excluding fence lines).

    Returns:
        Estimated token count (minimum 1 for non-empty content, 0 for empty).

    Examples:
        >>> estimate_tokens("")
        0
        >>> estimate_tokens("echo hello")
        2
        >>> estimate_tokens("a" * 40)
        10
        >>> estimate_tokens("x")
        1
    """
    if not content:
        return 0
    return max(1, len(content) // 4)


def classify_block(lang_marker: str, content: str) -> bool:
    """Determine whether a fenced block contains programming code.

    Three-tier classification:

    1. Known code marker (bash, python, js, yaml, …) → True.
    2. Known non-code marker (text, markdown, output, …) → False unless
       content heuristic signals strong code presence.
    3. Unknown or empty marker → content heuristic decides.

    Content heuristic: counts lines matching ``_CODE_SIGNAL`` vs. lines ending
    with prose punctuation. A block is code when code-signal density ≥ 0.25
    or (≥ 2 signal hits AND prose density < 0.4).

    Args:
        lang_marker: Raw language marker from the fence (case-insensitive).
        content: Block body (excluding fence lines).

    Returns:
        True if the block is likely programming code.

    Examples:
        >>> classify_block("bash", "echo hello")
        True
        >>> classify_block("python", "def f(): pass")
        True
        >>> classify_block("text", "This is a plain sentence.")
        False
        >>> classify_block("", "#!/usr/bin/env bash\\necho hi")
        True
        >>> classify_block("", "This is just text.\\nAnother sentence here.")
        False
        >>> classify_block("output", "$ grep pattern file.txt\\n$ wc -l out.txt")
        True
        >>> classify_block("json", '{"key": "value"}')
        True
    """
    marker = lang_marker.lower().strip()
    if marker in CODE_MARKERS:
        return True

    lines = content.splitlines()
    total = len(lines)
    if total == 0:
        return False

    code_hits = len(_CODE_SIGNAL.findall(content))
    prose_lines = sum(1 for ln in lines if _PROSE_END.search(ln))
    code_density = code_hits / total
    prose_density = prose_lines / total

    return code_density >= 0.25 or (code_hits >= 2 and prose_density < 0.4)


def _quote_depth(line: str) -> tuple[int, str]:
    """Count leading blockquote markers and return the depth with the remaining text.

    Examples:
        >>> _quote_depth("> ```bash")
        (1, '```bash')
        >>> _quote_depth("> > x")
        (2, 'x')
        >>> _quote_depth("```bash")
        (0, '```bash')
    """
    depth = 0
    match = _QUOTE_MARKER.match(line)
    while match is not None:
        depth += 1
        line = line[match.end() :]
        match = _QUOTE_MARKER.match(line)
    return depth, line


def _strip_quote(line: str, depth: int) -> str | None:
    """Strip exactly ``depth`` blockquote markers, or return None when the line carries fewer.

    Examples:
        >>> _strip_quote("> echo hi", 1)
        'echo hi'
        >>> _strip_quote(">", 1)
        ''
        >>> _strip_quote("plain", 1) is None
        True
    """
    for _ in range(depth):
        match = _QUOTE_MARKER.match(line)
        if match is None:
            return None
        line = line[match.end() :]
    return line


def parse_blocks(text: str, filepath: str) -> list[CodeBlock]:
    """Extract all fenced code blocks from Markdown text.

    Handles 3+ backtick or tilde fences. Closing fence must use the same
    character as the opening fence with equal or greater repetition count.
    Empty blocks (whitespace-only content) are skipped.

    Fences inside a blockquote (``> ```bash``) are extracted too: the opener's
    quote depth is stripped from every content and closing line. A line that
    loses the quote prefix ends the blockquote, and the block is skipped like
    any unclosed fence. A ``>`` inside a plain (unquoted) fence is content and
    is never stripped. Line numbers always refer to the original file lines.

    Args:
        text: Full file content.
        filepath: Path string recorded in each returned CodeBlock.

    Returns:
        List of CodeBlock objects in document order.

    Examples:
        >>> blocks = parse_blocks("```bash\\necho hi\\n```\\n", "f.md")
        >>> len(blocks)
        1
        >>> blocks[0].lang_marker
        'bash'
        >>> blocks[0].lang_detected
        'bash'
        >>> blocks[0].content
        'echo hi'
        >>> blocks[0].line_start
        1
        >>> blocks[0].line_end
        3
        >>> parse_blocks("no fences here", "f.md")
        []
        >>> parse_blocks("```\\n```\\n", "f.md")
        []
        >>> blocks2 = parse_blocks("```py\\nx = 1\\n```\\n```sh\\nls\\n```\\n", "f.md")
        >>> len(blocks2)
        2
        >>> blocks2[0].lang_detected
        'python'
        >>> blocks2[1].lang_detected
        'bash'
        >>> quoted = parse_blocks("> ```bash\\n> echo hi\\n> ```\\n", "f.md")
        >>> (quoted[0].content, quoted[0].line_start, quoted[0].line_end)
        ('echo hi', 1, 3)
        >>> parse_blocks("```text\\n> kept\\n```\\n", "f.md")[0].content
        '> kept'
    """
    blocks: list[CodeBlock] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        depth, opener = _quote_depth(lines[i])
        m = _FENCE_OPEN.match(opener)
        if m:
            fence_char = m.group(1)[0]
            fence_len = len(m.group(1))
            lang_marker = m.group(2)
            open_line = i + 1  # 1-based
            close_pattern = re.compile(r"^" + re.escape(fence_char) + r"{" + str(fence_len) + r",}\s*$")
            i += 1
            content_lines: list[str] = []
            closed = False
            while i < len(lines):
                body = _strip_quote(lines[i], depth) if depth else lines[i]
                if body is None:
                    break  # blockquote ended before the fence closed — rescan this line as normal text
                if close_pattern.match(body):
                    close_line = i + 1  # 1-based
                    content = "\n".join(content_lines)
                    if content.strip():
                        lang_detected = normalize_lang(lang_marker)
                        blocks.append(
                            CodeBlock(
                                file=filepath,
                                lang_marker=lang_marker,
                                lang_detected=lang_detected,
                                line_start=open_line,
                                line_end=close_line,
                                token_estimate=estimate_tokens(content),
                                is_code=classify_block(lang_marker, content),
                                content=content,
                            )
                        )
                    i += 1
                    closed = True
                    break
                content_lines.append(body)
                i += 1
            if not closed:
                pass  # unclosed fence — skip without advancing further
        else:
            i += 1
    return blocks


def iter_md_files(root: str, pattern: str = "*.md") -> list[str]:
    """Walk ``root`` and return a sorted list of files matching ``pattern``.

    Args:
        root: Directory to walk (absolute or relative).
        pattern: Glob pattern applied to filenames only (default ``*.md``).

    Returns:
        Sorted list of file paths (same form as os.walk returns them).

    Examples:
        >>> import os, tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     _ = open(os.path.join(d, "a.md"), "w").write("x")
        ...     _ = open(os.path.join(d, "b.txt"), "w").write("x")
        ...     result = iter_md_files(d)
        ...     [os.path.basename(p) for p in result]
        ['a.md']
    """
    matches: list[str] = []
    for dirpath, _dirs, filenames in os.walk(root):
        for fn in filenames:
            if fnmatch.fnmatch(fn, pattern):
                matches.append(os.path.join(dirpath, fn))
    return sorted(matches)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Extract matching code blocks from the requested inputs."""
    parser = argparse.ArgumentParser(
        prog="extract_code_blocks",
        description="Extract fenced code blocks from Markdown files (JSONL to stdout).",
    )
    parser.add_argument("dir", help="Root directory to search.")
    parser.add_argument("--include", default="*.md", metavar="PATTERN", help="Filename glob (default: *.md).")
    parser.add_argument(
        "--all", action="store_true", dest="include_all", help="Include non-code blocks (default: code only)."
    )
    parser.add_argument(
        "--min-tokens", type=int, default=0, metavar="N", help="Skip blocks with token estimate below N."
    )
    args = parser.parse_args(argv)

    if not os.path.isdir(args.dir):
        print(f"error: {args.dir!r} is not a directory", file=sys.stderr)
        return 1

    for filepath in iter_md_files(args.dir, args.include):
        try:
            if Path(filepath).stat().st_size > _MAX_FILE_SIZE:
                print(
                    f"warning: skipping oversized file (> {_MAX_FILE_SIZE} bytes): {filepath}",
                    file=sys.stderr,
                )
                continue
            text = Path(filepath).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"warning: could not read {filepath}: {exc}", file=sys.stderr)
            continue
        for block in parse_blocks(text, filepath):
            if not args.include_all and not block.is_code:
                continue
            if block.token_estimate < args.min_tokens:
                continue
            print(json.dumps(asdict(block), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
