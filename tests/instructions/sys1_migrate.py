#!/usr/bin/env python
"""Migrate plugin Markdown bootstrap paths with a reviewable dry-run receipt.

Inventory uses the repository's fenced-block parser. Only unambiguous shell
blocks are rewritten; text, prompts and embedded shell data require a human
decision. The default is read-only. ``--apply`` refuses the entire operation
when any ambiguity remains, and checks source bytes again before writing.

Usage:
    python sys1_migrate.py --root . --receipt /tmp/root-migration.json
    python sys1_migrate.py --root FIXTURE --apply --receipt receipt.json

``--exclude`` accepts one repository-relative path or glob per occurrence and
may be repeated. No file outside the selected plugin document scopes is edited.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "plugins/cc_foundry/bin"))

from extract_code_blocks import CodeBlock, parse_blocks  # noqa: E402

DEFAULT_ROOT = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT:-plugins/([^{}\s]+)\}")
PLAIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"
PROLOGUE = (
    'PLUGIN_ROOT=$(python "${{CLAUDE_PLUGIN_ROOT}}/bin/resolve_plugin_root.py" --plugin-name {plugin})'
    " || exit $?; export PLUGIN_ROOT"
)
SCOPES = ("skills", "claude-skills", "agents", "references", "templates", "modes", "_shared")
SHELL_MARKERS = frozenset({"bash", "sh", "zsh", "shell"})
ROOT_WRITE = re.compile(r"(?<![\w])(?:export\s+)?PLUGIN_ROOT\s*=")
OLD_ASSIGNMENT = re.compile(r'^\s*PLUGIN_ROOT="\$\{CLAUDE_PLUGIN_ROOT:-plugins/([^{}\s]+)\}"\s*(?P<comment>#.*)?$')
HEREDOC = re.compile(r"(?<!<)<<(-)?\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][\w]*))")
FENCE = re.compile(r"^(?:[ \t]*>[ \t]?)*[ \t]*(`{3,}|~{3,})")
METADATA = re.compile(r"^\s*(?:#|<!--|-->)")
COUNTS = (
    "files_scanned",
    "files_affected",
    "files_changed",
    "blocks_affected",
    "blocks_rewritten",
    "prose_replacements",
    "root_assignments_replaced",
)


def _digest(content: bytes) -> str:
    """Return a stable receipt digest of exact file bytes."""
    return hashlib.sha256(content).hexdigest()


def _category(relative: Path) -> str:
    """Classify a document by its delivery context."""
    if relative.name == "SKILL.md":
        return "skill"
    for component, category in (
        ("modes", "mode"),
        ("templates", "template"),
        ("_shared", "shared"),
        ("references", "reference"),
        ("agents", "agent"),
    ):
        if component in relative.parts:
            return category
    return "sidecar"


def _excluded(relative: Path, excludes: list[str]) -> bool:
    """Exclude historical fixtures, tests and explicitly selected paths."""
    if relative.name.upper().startswith("CHANGELOG") or {"tests", "fixtures"} & set(relative.parts):
        return True
    value = relative.as_posix()
    return any(
        value == item.rstrip("/") or value.startswith(item.rstrip("/") + "/") or fnmatch.fnmatchcase(value, item)
        for item in excludes
    )


def _documents(root: Path, excludes: list[str]) -> list[tuple[Path, str, str]]:
    """Inventory independently owned Claude plugin document scopes in stable order."""
    documents: list[tuple[Path, str, str]] = []
    for plugin in sorted((root / "plugins").iterdir()):
        manifest = plugin / ".claude-plugin/plugin.json"
        if not manifest.is_file():
            continue
        name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"invalid plugin identity: {manifest}")
        paths = {path for scope in SCOPES for path in (plugin / scope).rglob("*.md")}
        for path in sorted(paths):
            relative = path.relative_to(root)
            if not _excluded(relative, excludes):
                _validate_document(path, plugin, root)
                documents.append((path, name, _category(relative)))
    return documents


def _validate_document(path: Path, plugin: Path, root: Path) -> None:
    """Keep selected source documents within their resolved owning plugin."""
    if not plugin.resolve().is_relative_to((root / "plugins").resolve()):
        raise ValueError(f"plugin escapes selected collection: {plugin}")
    if not path.resolve().is_relative_to(plugin.resolve()):
        raise ValueError(f"document escapes its plugin: {path}")


def _validate_receipt(receipt: Path, documents: list[tuple[Path, str, str]]) -> None:
    """Reject receipt destinations that alias any selected Markdown input."""
    destination = receipt.resolve()
    for path, _plugin, _category_name in documents:
        if destination == path.resolve() or (receipt.exists() and receipt.samefile(path)):
            raise ValueError(f"receipt aliases input document: {path}")


def _ambiguity(path: str, line: int, reason: str) -> dict:
    """Describe a refusal without placing private data in filenames."""
    return {"file": path, "line": line, "reason": reason}


def _heredoc_lines(lines: list[str]) -> set[int]:
    """Identify heredoc payload lines so shell-like data is never rewritten."""
    payload: set[int] = set()
    pending: list[tuple[str, bool]] = []
    for index, line in enumerate(lines):
        if pending:
            payload.add(index)
            delimiter, strip_tabs = pending[0]
            if (line.lstrip("\t") if strip_tabs else line).rstrip("\r\n") == delimiter:
                pending.pop(0)
            continue
        if line.lstrip().startswith("#"):
            continue
        for match in HEREDOC.finditer(line):
            pending.append((next(value for value in match.groups()[1:] if value is not None), bool(match.group(1))))
    return payload


def _root_conflict(lines: list[str], owner: str, expected: str) -> str | None:
    """Reject unknown root writes while admitting one exact existing bootstrap."""
    prologues = [
        index for index, line in enumerate(lines) if "resolve_plugin_root.py" in line and ROOT_WRITE.search(line)
    ]
    if len(prologues) > 1:
        return "multiple resolver prologues"
    for line in lines:
        if not ROOT_WRITE.search(line):
            continue
        assignment = OLD_ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if line.strip() == expected or (assignment and assignment.group(1) == owner):
            continue
        return "conflicting or unrecognized PLUGIN_ROOT assignment"
    return None


def _block_refusal(block: CodeBlock, owner: str, expected: str) -> str | None:
    """Validate shell ownership and data boundaries before making a local edit."""
    if block.lang_marker.lower() not in SHELL_MARKERS:
        return f"non-shell or prompt/text fence ({block.lang_marker or 'unmarked'})"
    if any(match.group(1) != owner for match in DEFAULT_ROOT.finditer(block.content)):
        return "root token names a different plugin directory"
    lines = block.content.splitlines(keepends=True)
    if any(DEFAULT_ROOT.search(lines[index]) for index in _heredoc_lines(lines)):
        return "root token appears in heredoc data"
    conflict = _root_conflict(lines, owner, expected)
    if conflict:
        return conflict
    return _operand_refusal(lines)


def _operand_refusal(lines: list[str]) -> str | None:
    """Require a path operand or a recognized whole-root assignment."""
    for line in lines:
        if OLD_ASSIGNMENT.fullmatch(line.rstrip("\r\n")):
            continue
        if any(not line[match.end() :].startswith("/") for match in DEFAULT_ROOT.finditer(line)):
            return "root-only token is not an exact standalone root assignment"
    return None


def _first_command(lines: list[str]) -> int:
    """Locate the first command without splitting a multiline command or payload."""
    for index, line in enumerate(lines):
        if line.strip() and not METADATA.match(line):
            return index
    return len(lines)


def _rewrite_body(content: str, owner: str, plugin: str) -> tuple[str, int]:
    """Replace operands and obsolete assignments while preserving every other byte."""
    expected = PROLOGUE.format(plugin=plugin)
    lines = content.splitlines(keepends=True)
    replaced = 0
    rewritten: list[str] = []
    for line in lines:
        assignment = OLD_ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if assignment and assignment.group(1) == owner:
            replaced += 1
            if assignment.group("comment"):
                rewritten.append(assignment.group("comment") + ("\r\n" if line.endswith("\r\n") else "\n"))
            continue
        rewritten.append(DEFAULT_ROOT.sub("$PLUGIN_ROOT", line))
    if not any(line.strip() == expected for line in rewritten):
        newline = "\r\n" if "\r\n" in content else "\n"
        rewritten.insert(_first_command(rewritten), expected + newline)
    return "".join(rewritten), replaced


def _body_prefix(opener: str) -> str:
    """Preserve the exact blockquote prefix on inserted shell lines."""
    return opener[: opener.index("`")] if "`" in opener else opener[: opener.index("~")]


def _rewrite_block(raw: list[str], block: CodeBlock, owner: str, plugin: str) -> tuple[list[str], int]:
    """Rewrite a parsed body while retaining its original fence and quote layout."""
    prefix = _body_prefix(raw[0])
    body_lines = [line[len(prefix) :] if line.startswith(prefix) else line for line in raw[1:-1]]
    rewritten, replaced = _rewrite_body("".join(body_lines), owner, plugin)
    return [raw[0], *(prefix + line for line in rewritten.splitlines(keepends=True)), raw[-1]], replaced


def _layout_refusal(raw: list[str]) -> str | None:
    """Refuse a layout that cannot gain a prologue without reformatting old lines."""
    prefix = _body_prefix(raw[0])
    if any(not line.startswith(prefix) for line in raw[1:-1]):
        return "inconsistent fence body prefix"
    return None


def _normalize_fence_indents(text: str) -> str:
    """Expose list-indented fences to the existing parser without shifting lines."""
    lines: list[str] = []
    prefix = ""
    closing: re.Pattern | None = None
    for line in text.splitlines(keepends=True):
        if closing is not None:
            normalized = line[len(prefix) :] if line.startswith(prefix) else line
            lines.append(normalized)
            if closing.fullmatch(normalized.rstrip("\r\n")):
                closing = None
            continue
        match = FENCE.match(line)
        if match:
            outer_prefix, fence = line[: match.start(1)], match.group(1)
            prefix = outer_prefix if ">" not in outer_prefix else ""
            quote_prefix = outer_prefix if ">" in outer_prefix else ""
            closing = re.compile(re.escape(quote_prefix) + re.escape(fence[0]) + "{" + str(len(fence)) + ",}\\s*")
            line = line[len(prefix) :]
        lines.append(line)
    return "".join(lines)


def _unparsed_fence_lines(lines: list[str], covered: set[int]) -> set[int]:
    """Protect tokens in unsupported or unterminated fences from prose rewriting."""
    inside = False
    protected: set[int] = set()
    for index, line in enumerate(lines):
        if index in covered:
            continue
        if FENCE.match(line):
            inside = not inside
        if inside:
            protected.add(index)
    return protected


def _migrate_text(text: str, relative: str, owner: str, plugin: str) -> tuple[str, Counter, list[dict]]:
    """Plan per-block and prose substitutions for a single document."""
    lines = text.splitlines(keepends=True)
    blocks = parse_blocks(_normalize_fence_indents(text), relative)
    covered = {index for block in blocks for index in range(block.line_start - 1, block.line_end)}
    replacements: dict[int, tuple[int, list[str]]] = {}
    counts: Counter = Counter()
    refusals: list[dict] = []
    for block in blocks:
        if not DEFAULT_ROOT.search(block.content):
            reason = _existing_bootstrap_refusal(block, owner, plugin)
            if reason:
                refusals.append(_ambiguity(relative, block.line_start + 1, reason))
            continue
        counts["blocks_affected"] += 1
        raw = lines[block.line_start - 1 : block.line_end]
        reason = _layout_refusal(raw) or _block_refusal(block, owner, PROLOGUE.format(plugin=plugin))
        if reason:
            refusals.append(_ambiguity(relative, block.line_start + 1, reason))
            continue
        new, removed = _rewrite_block(raw, block, owner, plugin)
        replacements[block.line_start - 1] = (block.line_end, new)
        counts["blocks_rewritten"] += 1
        counts["root_assignments_replaced"] += removed
    protected = _unparsed_fence_lines(lines, covered)
    _rewrite_prose(lines, covered, protected, relative, owner, counts, refusals)
    for start, (end, replacement) in sorted(replacements.items(), reverse=True):
        lines[start:end] = replacement
    return "".join(lines), counts, refusals


def _existing_bootstrap_refusal(block: CodeBlock, owner: str, plugin: str) -> str | None:
    """Validate already migrated shell blocks without changing their bytes."""
    if block.lang_marker.lower() not in SHELL_MARKERS or "resolve_plugin_root.py" not in block.content:
        return None
    return _root_conflict(block.content.splitlines(keepends=True), owner, PROLOGUE.format(plugin=plugin))


def _rewrite_prose(
    lines: list[str],
    covered: set[int],
    protected: set[int],
    relative: str,
    owner: str,
    counts: Counter,
    refusals: list[dict],
) -> None:
    """Normalize prose tokens only outside all recognized or unsupported fences."""
    for index, line in enumerate(lines):
        if index in covered or not DEFAULT_ROOT.search(line):
            continue
        if index in protected:
            refusals.append(_ambiguity(relative, index + 1, "token in unsupported or unterminated fence"))
        elif any(match.group(1) != owner for match in DEFAULT_ROOT.finditer(line)):
            refusals.append(_ambiguity(relative, index + 1, "prose token names a different plugin directory"))
        else:
            lines[index], changed = DEFAULT_ROOT.subn(PLAIN_ROOT, line)
            counts["prose_replacements"] += changed


def _inventory(
    root: Path, documents: list[tuple[Path, str, str]]
) -> tuple[list[dict], dict, list[dict], list[tuple[Path, bytes, bytes]]]:
    """Collect deterministic receipts and pending writes without changing input files."""
    files: list[dict] = []
    plugins: dict = {}
    refusals: list[dict] = []
    writes: list[tuple[Path, bytes, bytes]] = []
    for path, plugin, category in documents:
        before = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        text = before.decode("utf-8")
        after, counts, issues = _migrate_text(text, relative, path.relative_to(root).parts[1], plugin)
        encoded = after.encode("utf-8")
        counts.update(
            files_scanned=1, files_affected=int(bool(DEFAULT_ROOT.search(text))), files_changed=int(encoded != before)
        )
        bucket = plugins.setdefault(plugin, {"totals": Counter(), "categories": {}})
        bucket["totals"].update(counts)
        bucket["categories"].setdefault(category, Counter()).update(counts)
        refusals.extend(issues)
        if DEFAULT_ROOT.search(text):
            files.append(
                {
                    "file": relative,
                    "plugin": plugin,
                    "category": category,
                    "before_sha256": _digest(before),
                    "after_sha256": _digest(encoded),
                    **dict(counts),
                }
            )
        if encoded != before:
            writes.append((path, before, encoded))
    return files, plugins, refusals, writes


def _apply(writes: list[tuple[Path, bytes, bytes]]) -> None:
    """Refuse concurrent edits before applying the already reviewed byte changes."""
    for path, before, _after in writes:
        if path.read_bytes() != before:
            raise ValueError(f"source changed during inventory: {path}")
    for path, before, after in writes:
        if path.read_bytes() != before:
            raise ValueError(f"source changed before write: {path}")
        path.write_bytes(after)


def main(argv: list[str] | None = None) -> int:
    """Write a dry-run receipt or apply an unambiguous document migration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPOSITORY, help="Repository or fixture root containing plugins/.")
    parser.add_argument("--receipt", type=Path, required=True, help="JSON receipt destination.")
    parser.add_argument("--exclude", action="append", default=[], help="Repository-relative path or glob; repeatable.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inventory without writing Markdown (default).")
    mode.add_argument("--apply", action="store_true", help="Apply only when the entire inventory is unambiguous.")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        documents = _documents(root, args.exclude)
        _validate_receipt(args.receipt, documents)
        files, plugins, refusals, writes = _inventory(root, documents)
        totals = Counter({name: 0 for name in COUNTS})
        for bucket in plugins.values():
            totals.update(bucket["totals"])
        totals["ambiguities"] = len(refusals)
        receipt = {
            "mode": "apply" if args.apply else "dry-run",
            "totals": dict(totals),
            "plugins": plugins,
            "files": files,
            "ambiguities": refusals,
            "exclude": args.exclude,
        }
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_bytes(
            (json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        )
        print(json.dumps({"mode": receipt["mode"], "totals": totals, "plugins": plugins}, sort_keys=True))
        if args.apply and not refusals:
            _apply(writes)
        return 2 if refusals else 0
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
