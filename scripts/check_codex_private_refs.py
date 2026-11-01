#!/usr/bin/env python3
"""Flag private planning material in the published Codex Rig payload and in its parallel-execution documents.

Two scopes, both about documentation hygiene:

* Every UTF-8 file of the release payload (everything below ``plugins/codex-rig`` except build artefacts) must be
  free of retired planning phrases, plan file names, work-item and rollout identifiers, and absolute personal paths.
  Binary files are skipped. The payload scope mirrors ``EXCLUDED_PARTS`` and ``EXCLUDED_FILES`` of the plugin's
  ``scripts/build_package.py``; update both together.
* The documents that publish the bounded parallel-execution flow (architecture, README, the code-review, implement and
  manage skills with their reference files, and ``shared/portable-parallel-read.md``) must not link ``.plans/`` or the
  private multi-agent plan.

This is a hook rather than a test because a leaked reference changes no behaviour; it only embarrasses the release.
It lives outside ``plugins/codex-rig`` so its own pattern literals are never part of the scanned payload.

Usage: ``check_codex_private_refs.py [--root DIR]``. Always scans the whole payload. Exit 1 on any finding.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from pathlib import Path

PLUGIN = Path("plugins") / "codex-rig"
EXCLUDED_PARTS = frozenset({"__pycache__", ".pytest_cache", ".reports"})
EXCLUDED_FILES = frozenset({".coverage", "package-manifest.json"})

PRIVATE_WORK_ITEM = re.compile(r"\b(?:W\d+|H\d{2}|M\d{2})\b")
PRIVATE_ROLLOUT_PHASE = re.compile(r"P[345](?:[ab])?", flags=re.IGNORECASE)
PRIVATE_PLAN_REFERENCE = re.compile(r"(?:^|[/\s`'\"])(plan_[A-Za-z0-9_.-]+\.md)")
PERSONAL_ABSOLUTE_PATH = re.compile(
    r"(?:/(?:Users|home)/[^/\\\s'\"]+|(?i:[A-Z]:[\\/]+Users[\\/]+[^/\\\s'\"]+))"
    r"(?:[\\/][^\s'\"]+)?",
)
#: Retired planning phrases that must not reach the published payload.
FORBIDDEN_PHRASES = (
    "plugin-package.json",
    "(planned)",
    "GA candidate",
    "exact-candidate native",
    "fresh native",
    "repeat independent QA/challenge",
    "currently released/default",
)
#: ``(pattern, capture group)`` pairs; only the first match per pattern is reported, as the group's text.
_SEARCHES = (
    (PRIVATE_PLAN_REFERENCE, 1),
    (PRIVATE_WORK_ITEM, 0),
    (PRIVATE_ROLLOUT_PHASE, 0),
    (PERSONAL_ABSOLUTE_PATH, 0),
)
#: Substrings the parallel-execution documents must not contain.
FORBIDDEN_IN_DOCUMENTS = (".plans/", "plan_multi-agent-parallelization")
#: Skills whose SKILL.md and ``references/*.md`` publish the bounded parallel-execution flow.
DOCUMENT_SKILLS = ("code-review", "implement", "manage")


def private_references(text: str) -> list[str]:
    """Return every private planning identifier or personal path found in one payload file.

    Examples:
        >>> private_references("see plan_foo.md and W12")
        ['plan_foo.md', 'W12']
        >>> private_references("clean text")
        []
    """
    found = [phrase for phrase in FORBIDDEN_PHRASES if phrase in text]
    matches = [(pattern.search(text), group) for pattern, group in _SEARCHES]
    return [*found, *(match.group(group) for match, group in matches if match)]


def payload_files(root: Path) -> Iterator[Path]:
    """Yield every regular payload file below the plugin directory in canonical order."""
    base = root / PLUGIN
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if path.name in EXCLUDED_FILES or any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.is_file() and not path.is_symlink():
            yield path


def parallel_documents(root: Path) -> list[Path]:
    """List the published documents of the bounded parallel-execution flow that exist on disk."""
    base = root / PLUGIN
    documents = [base / "ARCHITECTURE.md", base / "README.md", base / "shared" / "portable-parallel-read.md"]
    for skill in DOCUMENT_SKILLS:
        skill_dir = base / "skills" / skill
        documents.extend([skill_dir / "SKILL.md", *sorted((skill_dir / "references").glob("*.md"))])
    return documents


def payload_findings(root: Path) -> list[str]:
    """Return one finding per private reference found in a UTF-8 payload file."""
    findings = []
    for path in payload_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        name = path.relative_to(root).as_posix()
        findings.extend(
            f"{name}: private reference '{reference}' in the release payload; remove it or rewrite it self-contained"
            for reference in private_references(text)
        )
    return findings


def document_findings(root: Path) -> list[str]:
    """Return one finding per private plan link in the parallel-execution documents."""
    findings = []
    for path in parallel_documents(root):
        if not path.is_file():
            findings.append(f"{path.relative_to(root).as_posix()}: published parallel-execution document is missing")
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            findings.extend(
                f"{path.relative_to(root).as_posix()}:{number}: private plan reference '{forbidden}' in a published "
                "parallel-execution document; link the canonical flow instead"
                for forbidden in FORBIDDEN_IN_DOCUMENTS
                if forbidden in line
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    """Scan the payload and documents under ``--root`` and print findings; exit 1 when any exist."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    root = parser.parse_args(argv).root.resolve()
    findings = [*payload_findings(root), *document_findings(root)]
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
