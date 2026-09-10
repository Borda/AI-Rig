#!/usr/bin/env python3
"""audit_hook_coverage.py — measure how often the auto-allow hooks fire in real sessions.

Two decision modules can grant a Bash call a permission bypass: ``blueprint-allow.js``
(exact normalized-text match against a plugin's committed ``blueprint-manifest.json``)
and ``sentinel-read-allow.js`` (shape match on read-only compounds). Neither is
registered as a hook of its own any more — each plugin registers ``allow-dispatch.js``,
which calls both as libraries in rank order and emits the first allow. Both modules keep
their standalone entry points, which is what lets this tool subprocess one of them
directly. Both were validated against *committed text* — the share of fenced blueprint
blocks each one covers. That is not the same population as the commands sessions
actually execute, and nothing guaranteed the first predicts the second.

This tool measures the second directly. It replays every Bash command recorded in the
local session transcripts through the installed hooks and reports what fraction would
be auto-allowed, split by mechanism.

Two properties matter for the number to mean anything:

1. **Every installed manifest counts.** All installed plugins run their own dispatcher,
   each consulting its own manifest, and any one allow is enough for the call to run
   without a prompt — so the effective coverage set is the UNION of the manifests.
   Probing a single plugin's copy under-reports: a block owned by ``foundry`` is passed
   through by ``oss``, ``develop`` and ``research`` alike.
2. **Sessions predating a hook must be excluded.** A transcript recorded before a hook
   shipped contains commands generated when there was nothing to match, against skill
   text that has since changed. Including them measures history, not behaviour — pass
   ``--since`` with the hook's ship date.

Blueprint verdicts are resolved in-process (normalize + sha256 against the manifests,
importing the generator so normalization cannot drift); only the shape hook needs a
subprocess, and only once per distinct command.

Interpreting the result: the denominator is EVERY Bash call, not only those derived
from a blueprint block. Skill sessions also run ad-hoc greps, test invocations and
agent-side calls that were never blueprint text. A command that came from a blueprint
block but was adapted before running is indistinguishable from one that never was —
a hash miss looks identical either way. The reported rate is therefore a floor on the
mechanism's value, not a verdict on it.

Usage:
    python plugins/cc_foundry/bin/audit_hook_coverage.py
    python plugins/cc_foundry/bin/audit_hook_coverage.py --since 2026-08-17 --skills-only
    python plugins/cc_foundry/bin/audit_hook_coverage.py --project rf-detr --json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_blueprint_manifest import normalize, sha256_text  # noqa: E402

PLUGIN_CACHE = Path.home() / ".claude" / "plugins" / "cache" / "borda-ai-rig"
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
SKILL_CALL = re.compile(r"<command-name>/([a-z-]+):([a-z-]+)")
NO_MATCH = "none"


def parse_since(text: str) -> float:
    """Convert an ISO date to a POSIX timestamp for transcript filtering.

    Args:
        text: Date in ``YYYY-MM-DD`` form.

    Returns:
        Seconds since the epoch at local midnight on that date.

    Raises:
        ValueError: If the text is not an ISO date.

    Examples:
        >>> parse_since("2026-08-17") == dt.datetime(2026, 8, 17).timestamp()
        True
    """
    return dt.datetime.fromisoformat(text).timestamp()


def verdict_of(owner: str | None, shape_allowed: bool) -> str:
    """Label a command by which mechanism allows it.

    Blueprint wins when both would allow: it is the more specific claim (this exact
    reviewed text) and the runtime hook order puts it alongside the shape hook with
    first-allow-wins semantics.

    Args:
        owner: Plugin owning the manifest entry, or None when no manifest matched.
        shape_allowed: Whether the shape hook allows the command.

    Returns:
        ``blueprint:<plugin>``, ``shape``, or ``none``.

    Examples:
        >>> verdict_of("oss", False)
        'blueprint:oss'
        >>> verdict_of(None, True)
        'shape'
        >>> verdict_of(None, False)
        'none'
    """
    if owner:
        return f"blueprint:{owner}"
    return "shape" if shape_allowed else NO_MATCH


def rate(covered: int, total: int) -> str:
    """Format a coverage share, tolerating an empty denominator.

    Args:
        covered: Number of auto-allowed calls.
        total: Number of calls examined.

    Returns:
        Percentage with one decimal, or ``n/a`` when nothing was examined.

    Examples:
        >>> rate(72, 1140)
        '6.3%'
        >>> rate(0, 0)
        'n/a'
    """
    return f"{covered / total:.1%}" if total else "n/a"


def version_key(version: str) -> tuple[int, ...]:
    """Sortable key for a cache version directory name.

    Lexicographic sorting puts ``0.10.0`` below ``0.9.0``, which silently selects a
    stale manifest once a plugin passes its ninth minor.

    Args:
        version: Directory name, normally dotted numerics.

    Returns:
        Numeric components; non-numeric parts contribute 0.

    Examples:
        >>> version_key("0.10.0") > version_key("0.9.0")
        True
        >>> version_key("1.2.3")
        (1, 2, 3)
    """
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


def newest_per_plugin(relative: str) -> list[Path]:
    """Newest installed copy of a per-plugin file, one entry per plugin.

    Args:
        relative: Glob suffix under each plugin's version directory,
            e.g. ``blueprint-manifest.json`` or ``hooks/sentinel-read-allow.js``.

    Returns:
        One path per plugin that ships the file, sorted by plugin name.
    """
    found: list[Path] = []
    for plugin_dir in sorted(PLUGIN_CACHE.glob("*/")):
        # Version dir is the component directly under the plugin, whatever `relative` nests.
        versions = sorted(
            plugin_dir.glob(f"*/{relative}"),
            key=lambda path: version_key(path.relative_to(plugin_dir).parts[0]),
        )
        if versions:
            found.append(versions[-1])
    return found


def load_digests() -> dict[str, str]:
    """Map every manifest digest to its owning plugin, union over installed plugins.

    Returns:
        Digest to plugin name. Earlier plugins win a collision, which cannot change a
        verdict — only the attribution label.
    """
    digests: dict[str, str] = {}
    for manifest in newest_per_plugin("blueprint-manifest.json"):
        plugin = manifest.parent.parent.name
        entries = json.loads(manifest.read_text(encoding="utf-8")).get("entries", {})
        for digest in entries:
            digests.setdefault(digest, plugin)
    return digests


def read_transcript(path: Path) -> tuple[list[str], set[str]]:
    """Extract Bash commands and invoked skill names from one transcript.

    Args:
        path: Session transcript in JSONL form.

    Returns:
        Commands in execution order, and the set of ``plugin:skill`` names invoked.
    """
    commands: list[str] = []
    skills: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        for match in SKILL_CALL.finditer(line):
            skills.add(f"{match.group(1)}:{match.group(2)}")
        try:
            record = json.loads(line)
        except ValueError:
            continue
        content = (record.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and item.get("name") == "Bash":
                command = (item.get("input") or {}).get("command")
                if command:
                    commands.append(command)
    return commands, skills


class Classifier:
    """Assign each distinct command the mechanism that would auto-allow it."""

    def __init__(self, digests: dict[str, str], shape_hook: Path | None) -> None:
        """Store the manifest union and the shape hook to probe.

        Args:
            digests: Digest to owning plugin, from :func:`load_digests`.
            shape_hook: Installed ``sentinel-read-allow.js``; None disables shape probing.
        """
        self._digests = digests
        self._shape_hook = shape_hook
        self._cache: dict[str, str] = {}

    def _shape_allows(self, command: str) -> bool:
        """Run the shape hook against one command."""
        if self._shape_hook is None:
            return False
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        result = subprocess.run(
            [self._resolve_node(), str(self._shape_hook)],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        return '"allow"' in result.stdout

    @staticmethod
    def _resolve_node() -> str:
        """Node executable name; kept a seam so tests can stub the runtime."""
        return "node"

    def verdict(self, command: str) -> str:
        """Classify a command, memoized on its exact text.

        Args:
            command: Raw ``tool_input.command`` string.

        Returns:
            ``blueprint:<plugin>``, ``shape``, or ``none``.
        """
        cached = self._cache.get(command)
        if cached is not None:
            return cached
        normalized = normalize(command)
        owner = self._digests.get(sha256_text(normalized)) if normalized else None
        result = verdict_of(owner, False if owner else self._shape_allows(command))
        self._cache[command] = result
        return result


def collect(args: argparse.Namespace, classifier: Classifier) -> tuple[Counter, list[dict[str, object]]]:
    """Classify every qualifying transcript.

    Args:
        args: Parsed CLI options.
        classifier: Verdict assigner.

    Returns:
        Verdict tally over call instances, and one record per session.
    """
    since = parse_since(args.since) if args.since else None
    tally: Counter = Counter()
    sessions: list[dict[str, object]] = []
    for transcript in sorted(TRANSCRIPT_ROOT.glob("*/*.jsonl")):
        if args.project and args.project not in transcript.parent.name:
            continue
        try:
            # Live sessions rotate transcripts; one can vanish between glob and read.
            if since is not None and transcript.stat().st_mtime < since:
                continue
            commands, skills = read_transcript(transcript)
        except OSError:
            continue
        if not commands or (args.skills_only and not skills):
            continue
        covered = 0
        for command in commands:
            result = classifier.verdict(command)
            tally[result] += 1
            covered += result != NO_MATCH
        sessions.append(
            {
                "project": transcript.parent.name,
                "session": transcript.stem[:8],
                "calls": len(commands),
                "covered": covered,
                "skills": sorted(skills),
            }
        )
    return tally, sessions


def report(tally: Counter, sessions: list[dict[str, object]], limit: int) -> None:
    """Print the human-readable summary.

    Args:
        tally: Verdict counts over call instances.
        sessions: Per-session records from :func:`collect`.
        limit: Maximum session rows to list.
    """
    total = sum(tally.values())
    covered = total - tally[NO_MATCH]
    print(f"sessions: {len(sessions)}   Bash calls: {total}   auto-allowed: {covered} ({rate(covered, total)})\n")
    print("=== by mechanism (call instances) ===")
    for verdict, count in tally.most_common():
        print(f"  {count:>6}  {verdict}")
    ranked = sorted(sessions, key=lambda row: -int(row["covered"]))[:limit]
    if ranked:
        print("\n=== sessions, most-covered first ===")
        for row in ranked:
            share = rate(int(row["covered"]), int(row["calls"]))
            skills = ",".join(row["skills"]) or "-"
            print(
                f"{row['covered']:>5}/{row['calls']:<6} {share:>6}  {row['project'][-34:]:<34} {row['session']:<9} {skills[:60]}"
            )
    with_skill = [row for row in sessions if row["skills"]]
    if with_skill:
        calls = sum(int(row["calls"]) for row in with_skill)
        hits = sum(int(row["covered"]) for row in with_skill)
        print(
            f"\nsessions invoking a skill: {len(with_skill)}   calls {calls}   auto-allowed {hits} ({rate(hits, calls)})"
        )


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--since", help="Ignore transcripts last modified before this ISO date (YYYY-MM-DD).")
    parser.add_argument("--project", help="Only transcripts whose project directory name contains this substring.")
    parser.add_argument("--skills-only", action="store_true", help="Only sessions that invoked a plugin skill.")
    parser.add_argument("--limit", type=int, default=25, help="Session rows to list (default: 25).")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of the table.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code. 0 always — this is a measurement, not a gate.
    """
    args = build_parser().parse_args(argv)
    digests = load_digests()
    hooks = newest_per_plugin("hooks/sentinel-read-allow.js")
    if not digests:
        print("no installed blueprint manifests found — is the plugin cache populated?", file=sys.stderr)
    classifier = Classifier(digests, hooks[-1] if hooks else None)
    tally, sessions = collect(args, classifier)
    if not sum(tally.values()):
        print("no transcripts matched the filters", file=sys.stderr)
        return 0
    if args.json:
        total = sum(tally.values())
        print(json.dumps({"totals": dict(tally), "calls": total, "sessions": sessions}, indent=2, sort_keys=True))
    else:
        if hooks:
            probe = hooks[-1]
            print(
                f"manifest digests: {len(digests)}   shape hook: {probe.parent.parent.parent.name}/{probe.parent.parent.name}"
            )
        report(tally, sessions, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
