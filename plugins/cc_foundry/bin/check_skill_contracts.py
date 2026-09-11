#!/usr/bin/env python
"""check_skill_contracts.py — structural contract checks over skill Markdown for /foundry:audit.

Replaces two inline check blocks:

``--check 23b``  a ``# timeout: N`` comment with no shell enforcement beside it,
                 a ``subprocess.*`` call with no ``timeout=``, and a ``bin/``
                 script that calls subprocess without exposing ``--timeout``.
                 Lines invoking ``python`` are exempt from the first scan: the
                 script's own ``--timeout`` default enforces internally.
``--check 32f``  a ``modes/<name>.md`` that is both referenced from its
                 ``SKILL.md`` and duplicated inline in it — the "extraction done
                 but inline twin survived" topology. Check 32a covers the
                 inverse, an unreferenced mode file.

Both keep the wording the audit severity table maps onto. 23b findings still get
a model pass afterwards to drop lines inside illustrative example blocks.

Usage:
    check_skill_contracts.py --check {23b,32f} [--root <dir>] [--local]

Exit codes:
    0   always — findings are reported on stdout with their severity markers,
        matching the inline blocks this replaced. The audit reads the text, not
        the status, and a non-zero status would read as "the check failed to
        run" rather than "the check found something".
"""

from __future__ import annotations

import argparse
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

#: A mode file shorter than this, in substantive lines, is too small to judge.
MIN_MODE_LINES = 20

#: Overlapping substantive lines before an inline twin is reported.
MIN_OVERLAP = 20

_TIMEOUT_COMMENT = re.compile(r"#\s*timeout:\s*\d")
_SHELL_TIMEOUT = re.compile(r"\btimeout \d+ ")
_SUBPROCESS_CALL = re.compile(r"subprocess\.(check_output|run|call|Popen)")
# `[^)]` already spans newlines, so a wrapped `add_argument(\n    "--timeout",` matches.
_TIMEOUT_ARGUMENT = re.compile(r"add_argument\([^)]*--timeout")

#: Matched against the whole POSIX path, the way ``find -path`` matched it: ``*``
#: crosses ``/``, so one pattern set covers both the source tree
#: (``plugins/cc_x/agents/a.md``) and the installed layout (``.claude/agents/a.md``),
#: and ``*/rules/*.md`` still reaches nested ``rules/_full/``. A depth-fixed
#: ``Path.glob`` would match neither the installed layout nor the nested rules.
CONFIG_GLOBS = ("*/skills/*/SKILL.md", "*/agents/*.md", "*/rules/*.md")


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _config_files(root: Path) -> list[Path]:
    found = {
        path
        for path in root.rglob("*.md")
        if path.is_file() and any(fnmatch(path.as_posix(), pattern) for pattern in CONFIG_GLOBS)
    }
    return sorted(found)


def unenforced_timeouts(path: Path, lines: list[str]) -> list[str]:
    """Return findings for `# timeout:` comments with no shell timeout beside them."""
    findings = []
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not _TIMEOUT_COMMENT.search(line) or stripped.startswith("#"):
            continue
        if _SHELL_TIMEOUT.search(line) or "python " in line:
            continue
        findings.append(f"{path.as_posix()}:{number}:{line}")
    return findings


def _call_text(lines: list[str], start: int) -> str:
    """Return the full text of a call starting on line index `start`, parens balanced.

    The shell original matched line by line, so every multi-line ``subprocess.run(`` was reported even when ``timeout=``
    sat two lines below. Balancing the parentheses removes that whole false-positive class.
    """
    depth = 0
    chunk: list[str] = []
    for line in lines[start : start + 40]:
        chunk.append(line)
        depth += line.count("(") - line.count(")")
        if depth <= 0 and chunk:
            break
    return "\n".join(chunk)


def untimed_subprocess(path: Path, lines: list[str]) -> list[str]:
    """Return findings for subprocess calls with no timeout= argument."""
    findings = []
    for index, line in enumerate(lines):
        if not _SUBPROCESS_CALL.search(line) or line.strip().startswith("#"):
            continue
        if "timeout=" in _call_text(lines, index):
            continue
        findings.append(f"{path.as_posix()}:{index + 1}:{line}")
    return findings


def missing_timeout_flag(path: Path, text: str) -> str | None:
    """Return a finding when a subprocess-using script exposes no --timeout flag.

    The exemption matches an ``add_argument(... "--timeout" ...)`` call, not a bare ``--timeout`` anywhere in the file.
    Every script here documents ``[--timeout SECS]`` in its module docstring, so a substring test would exempt the whole
    population against exactly the code this polices.
    """
    if "subprocess." not in text or _TIMEOUT_ARGUMENT.search(text):
        return None
    return (
        f"  {path.as_posix()}: --timeout argparse argument absent; "
        "add with default= matching call site # timeout: N ÷ 1000"
    )


def check_timeouts(root: Path) -> None:
    """Check 23b — timeout enforcement across config files and bin/ scripts."""
    print("=== Check 23b: # timeout: comment without shell enforcement ===")
    findings = 0
    for path in _config_files(root):
        for line in unenforced_timeouts(path, _read_lines(path)):
            print(line)
            findings += 1
    if findings:
        print("  hint: prepend 'timeout S' (S = ms ÷ 1000) — e.g. 'timeout 5 $(command 2>/dev/null || echo fallback)'")

    print("=== Check 23b: Python subprocess missing timeout= ===")
    scripts = sorted(p for p in root.glob("*/bin/*.py") if p.is_file())
    sub_findings = 0
    for path in scripts:
        for line in untimed_subprocess(path, _read_lines(path)):
            print(line)
            sub_findings += 1
    if sub_findings:
        print(
            "  hint: add timeout=args.timeout to every subprocess call; "
            "--timeout default must equal call site # timeout: N ÷ 1000"
        )

    print("=== Check 23b: Python --timeout default compliance ===")
    flag_findings = 0
    for path in scripts:
        finding = missing_timeout_flag(path, "\n".join(_read_lines(path)))
        if finding:
            print(finding)
            flag_findings += 1

    print("✓: Check 23b scan complete")


def _substantive(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def shadowed_mode(skill_md: Path, mode_file: Path) -> str | None:
    """Return a finding when a referenced mode file's body is also inline in SKILL.md."""
    skill_text = skill_md.read_text(encoding="utf-8", errors="replace")
    if mode_file.name not in skill_text:
        return None
    mode_lines = _substantive(_read_lines(mode_file))
    if len(mode_lines) < MIN_MODE_LINES:
        return None
    # Count lines of SKILL.md that match a mode line, matching the `grep -Fxf`
    # original: a mode line duplicated twice inline counts twice.
    wanted = set(mode_lines)
    overlap = sum(1 for line in skill_text.splitlines() if line in wanted)
    if overlap < MIN_OVERLAP:
        return None
    return (
        f"⚠ 32f [medium] {skill_md.as_posix()} — body of {mode_file.name} shadowed inline "
        f"({overlap} overlapping lines); delete inline twin"
    )


def check_mode_shadows(root: Path, *, local: bool) -> None:
    """Check 32f — a mode file both referenced and duplicated inline."""
    print("=== Check 32f: mode-file body shadowed in SKILL.md ===")
    if not local:
        print("✓: Check 32f skipped in non-local mode (no plugin source tree)")
        return
    findings = 0
    for skill_md in sorted(root.glob("*/skills/*/SKILL.md")):
        modes_dir = skill_md.parent / "modes"
        if not modes_dir.is_dir():
            continue
        for mode_file in sorted(modes_dir.glob("*.md")):
            finding = shadowed_mode(skill_md, mode_file)
            if finding:
                print(finding)
                findings += 1
    if not findings:
        print("✓: Check 32f — no mode-body shadows found")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=("23b", "32f"), required=True)
    parser.add_argument("--root", default="plugins", help="scope root for the scan")
    parser.add_argument("--local", action="store_true", help="a plugin source tree is present")
    args = parser.parse_args()

    root = Path(args.root)
    if args.check == "23b":
        check_timeouts(root)
    else:
        check_mode_shadows(root, local=args.local)
    return 0


if __name__ == "__main__":
    sys.exit(main())
