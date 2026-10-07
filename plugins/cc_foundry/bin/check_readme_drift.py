#!/usr/bin/env python3
"""check_readme_drift.py — detect README facts that have drifted from disk.

Deterministic, low-false-positive drift checks for a plugin's ``README.md``.
Two independent classes of fact are verified against ground truth on disk, each
selectable on its own via ``--check``:

1. ``version`` — **version marker**: a literal ``Current version: `X.Y.Z``` line must
   match the ``version`` field in the plugin's ``.claude-plugin/plugin.json``. Only this
   explicit marker is checked; arbitrary version-shaped strings (release
   examples like ``v2.1.0``, historical benchmark tags like ``v0.13.2``) are
   ignored to avoid false positives.
2. ``bin-refs`` — **bin/ script references**: any backtick token ending in ``.py`` or
   ``.sh`` that appears either on a line mentioning ``bin/`` or as an explicit
   ``plugins/<plugin>/bin/<name>`` path must exist in the plugin's ``bin/``
   directory. Catches the sh→py migration drift where README kept the old names.

Both are facts about the source tree, so either alone drives exit code 1. A bare
invocation runs both and behaves exactly as it did before the split.

Usage:
    check_readme_drift.py plugins/cc_research [plugins/cc_oss ...]
    check_readme_drift.py --scan-dir plugins
    check_readme_drift.py --scan-dir plugins --check version

Exit code 0 when clean, 1 when any drift is found, 2 on usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Matches the README ``Current version: `X.Y.Z` `` line, capturing the version.
VERSION_MARKER = re.compile(r"Current version:\s*`(\d+\.\d+\.\d+)`")
#: Matches a backticked ``.py`` or ``.sh`` file name in README prose, capturing the name.
BACKTICK_SCRIPT = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh))`")


class FindingKind(str, Enum):
    """Which drift subcheck produced a finding — the ``--check`` selector token.

    Subclasses ``str`` rather than ``enum.StrEnum`` because ``requires-python`` is
    ``>=3.10`` and ``StrEnum`` landed in 3.11. The ``str`` mixin keeps
    ``FindingKind.VERSION == "version"`` true for comparison and f-strings.

    Examples:
        >>> FindingKind.VERSION == "version"
        True
        >>> FindingKind("bin-refs") is FindingKind.BIN_REFS
        True
    """

    VERSION = "version"
    BIN_REFS = "bin-refs"


#: Subchecks a caller may name in ``--check``, in default run order.
SELECTABLE_KINDS: tuple[FindingKind, ...] = (FindingKind.VERSION, FindingKind.BIN_REFS)


@dataclass
class Finding:
    """One README drift finding, tagged with the subcheck that found it.

    Attributes:
        kind: The subcheck responsible — used to filter by ``--check``.
        message: Human-readable drift text, README path already prepended.
    """

    kind: FindingKind
    message: str


def parse_kinds(spec: str) -> set[FindingKind]:
    """Parse a comma-separated ``--check`` spec into selectable kinds.

    Args:
        spec: Comma-separated subcheck tokens, e.g. ``"version,bin-refs"``.

    Returns:
        Set of selected FindingKind members.

    Raises:
        ValueError: When any token is not a selectable subcheck.

    Examples:
        >>> sorted(k.value for k in parse_kinds("bin-refs,version"))
        ['bin-refs', 'version']
        >>> parse_kinds("nope")
        Traceback (most recent call last):
        ...
        ValueError: unknown check mode(s): nope
    """
    selectable = {k.value: k for k in SELECTABLE_KINDS}
    tokens = [t.strip().lower() for t in spec.split(",") if t.strip()]
    unknown = sorted({t for t in tokens if t not in selectable})
    if unknown:
        raise ValueError(f"unknown check mode(s): {', '.join(unknown)}")
    return {selectable[t] for t in tokens}


def _plugin_version(plugin_dir: Path) -> str | None:
    """Return the ``version`` string from a plugin's manifest, or None if absent.

    Args:
        plugin_dir: Path to a plugin directory (containing ``.claude-plugin/``).

    Returns:
        The version string, or None if the manifest is missing or malformed.

    Examples:
        >>> _plugin_version(Path("/nonexistent")) is None
        True
    """
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (json.JSONDecodeError, OSError):
        return None


def _referenced_scripts(readme_text: str, plugin_name: str) -> set[str]:
    """Extract bin/ script basenames a README refers to.

    A token is treated as a bin/ reference when it ends in ``.py``/``.sh`` and
    either appears on a line mentioning ``bin/`` or is written as an explicit
    ``plugins/<plugin>/bin/<name>`` path.

    Args:
        readme_text: Full README contents.
        plugin_name: The plugin directory name (for explicit-path matching).

    Returns:
        Set of referenced script basenames (e.g. ``{"resolve_shared.py"}``).

    Examples:
        >>> _referenced_scripts("see `foo.py` in bin/ here", "research")
        {'foo.py'}
        >>> _referenced_scripts("`plugins/cc_oss/bin/bar.sh`", "oss")
        {'bar.sh'}
        >>> _referenced_scripts("unrelated `train.py` example", "research")
        set()
    """
    found: set[str] = set()
    explicit_path = f"plugins/{plugin_name}/bin/"
    for line in readme_text.splitlines():
        line_flags_bin = "bin/" in line
        for token in BACKTICK_SCRIPT.findall(line):
            if explicit_path in line or line_flags_bin:
                found.add(Path(token).name)
    return found


def check_plugin(plugin_dir: Path, kinds: set[FindingKind] | None = None) -> list[Finding]:
    """Check one plugin's README for version and bin/ reference drift.

    Args:
        plugin_dir: Path to the plugin directory.
        kinds: Subchecks to run; ``None`` (the default) runs all of them. Findings keep
            their historical order — version marker first, then bin/ references.

    Returns:
        List of Finding objects (empty when clean).

    Examples:
        >>> check_plugin(Path("/nonexistent"))
        []
    """
    active = set(SELECTABLE_KINDS) if kinds is None else kinds
    readme = plugin_dir / "README.md"
    if not readme.is_file():
        return []
    text = readme.read_text(encoding="utf-8")
    findings: list[Finding] = []

    if FindingKind.VERSION in active:
        version = _plugin_version(plugin_dir)
        marker = VERSION_MARKER.search(text)
        if marker and version and marker.group(1) != version:
            findings.append(
                Finding(
                    FindingKind.VERSION,
                    f"{readme}: 'Current version: `{marker.group(1)}`' != plugin.json `{version}`",
                )
            )

    if FindingKind.BIN_REFS in active:
        # Existence is checked plugin-wide (not just bin/): a referenced script may
        # legitimately live in tests/ or a skill dir. Drift = referenced nowhere.
        existing = {p.name for p in plugin_dir.rglob("*.py") if "__pycache__" not in p.parts}
        existing |= {p.name for p in plugin_dir.rglob("*.sh")}
        for script in sorted(_referenced_scripts(text, plugin_dir.name)):
            if script not in existing:
                findings.append(
                    Finding(
                        FindingKind.BIN_REFS,
                        f"{readme}: references script `{script}` — not found anywhere in {plugin_dir}",
                    )
                )
    return findings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list (defaults to sys.argv[1:] when None).

    Returns:
        Process exit code: 0 clean, 1 drift found.
    """
    parser = argparse.ArgumentParser(description="Detect README drift from disk (version + bin/ refs)")
    parser.add_argument("plugins", nargs="*", help="plugin directories to check")
    parser.add_argument("--scan-dir", metavar="DIR", help="check every immediate subdir of DIR that has a README.md")
    parser.add_argument(
        "--check",
        default=",".join(k.value for k in SELECTABLE_KINDS),
        metavar="KINDS",
        help="Comma-separated subchecks to run: version, bin-refs (default: both).",
    )
    args = parser.parse_args(argv)

    try:
        active = parse_kinds(args.check)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    targets: list[Path] = [Path(p) for p in args.plugins]
    if args.scan_dir:
        targets += [d for d in sorted(Path(args.scan_dir).iterdir()) if (d / "README.md").is_file()]
    if not targets:
        parser.error("no plugin directories given (pass paths or --scan-dir)")

    all_findings: list[Finding] = []
    for plugin_dir in targets:
        all_findings.extend(check_plugin(plugin_dir, active))

    if all_findings:
        print("\n".join(f"README-DRIFT: {f.message}" for f in all_findings))
        return 1
    if active == set(SELECTABLE_KINDS):
        print("✓: README drift — version markers and bin/ references match disk")
    else:
        print(f"✓: README drift [{','.join(sorted(k.value for k in active))}] — no drift found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
