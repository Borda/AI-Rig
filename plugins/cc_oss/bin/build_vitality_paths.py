#!/usr/bin/env python
"""build_vitality_paths.py — derive the vitality report path and the provenance metadata written into its header.

Emits, for the report-generation step of ``oss:analyse vitality``: the timestamped report path, the installed plugin
version, the current commit, whether the Codex bridge is available, and the frontmatter agents list that matches the
run's actual contributors.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/build_vitality_paths.py" --owner "$GH_OWNER" --repo "$GH_REPO" \
        --quick "$QUICK_MODE"

Sentinels written to ``${TMPDIR:-/tmp}/<name>-${CSID}``:
    analyse-report-file — written before the report exists; gates SKILL.md Step 6a via enforce-analyse-header.js

Exit codes:
    0 — metadata emitted
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

_VERSION_GLOB: Final = ".claude/plugins/cache/borda-ai-rig/oss/*/.claude-plugin/plugin.json"
_VERSION_FALLBACK: Final = "plugins/cc_oss/.claude-plugin/plugin.json"
_QUICK_AGENTS: Final = "  - oss:analyse (orchestrator, --quick: core scoring only)"
_FULL_AGENTS: Final = "  - oss:analyse (orchestrator)\n  - foundry:challenger (adversarial review)"
_CODEX_AGENT: Final = "  - bridge:review (independent repo review + adversarial review)"


def _sentinel_path(name: str) -> Path:
    """Build the session-scoped sentinel path for ``name``.

    Args:
        name: Sentinel base name, without the trailing session token.

    Returns:
        Path of the form ``<tmpdir>/<name>-<csid>``.

    Examples:
        >>> _sentinel_path("analyse-report-file").name.startswith("analyse-report-file-")
        True
    """
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    tmpdir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return Path(tmpdir) / f"{name}-{csid}"


#: Set by ``--dry-run``. Suppresses every sentinel write for the process.
_DRY_RUN = False


def _set_dry_run(enabled: bool) -> None:
    """Enable or disable dry-run mode for this process."""
    global _DRY_RUN  # noqa: PLW0603 — one process-wide switch, set once from argv
    _DRY_RUN = enabled


def _write_sentinel(name: str, value: str) -> None:
    """Write ``value`` plus a trailing newline to the sentinel named ``name``.

    Readers use ``IFS= read -r VAR < file``, which exits non-zero on a file with no final newline and silently falls
    back to its default; ``newline="\\n"`` stops Windows from appending a carriage return inside the value.

    Sentinels are live session state, not scratch output: they are named for the current
    ``CSID`` and are what the skill's later steps and its PreToolUse hooks read. Running this
    script by hand to inspect its output therefore forges state for whatever session is
    running — one observed case wrote an ``analyse-report-file`` pointing at a report that was
    never produced, and the resulting hook denial blocked an unrelated question. Pass
    ``--dry-run`` for any invocation that is not a real skill step.

    Args:
        name: Sentinel base name, without the trailing session token.
        value: Payload to persist.
    """
    if _DRY_RUN:
        print(f"[dry-run] would write {name}={value}")
        return
    _sentinel_path(name).write_text(f"{value}\n", encoding="utf-8", newline="\n")


def _run(cmd: list[str], timeout: int, fallback: str) -> str:
    """Run ``cmd`` and return its stripped stdout, or ``fallback`` on any failure.

    Args:
        cmd: Argument vector to execute.
        timeout: Maximum wait in seconds.
        fallback: Value returned when the command fails, is missing, or times out.

    Returns:
        Stripped stdout on success, otherwise ``fallback``.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return fallback
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else fallback


def build_report_path(owner: str, repo: str, timestamp: str) -> str:
    """Build the vitality report path.

    Args:
        owner: GitHub owner.
        repo: GitHub repository name.
        timestamp: UTC ``%Y-%m-%dT%H-%M-%SZ`` stamp.

    Returns:
        Repository-relative report path.

    Examples:
        >>> build_report_path("owner", "repo", "2026-09-11T10-00-00Z")
        '.reports/analyse/vitality/output-analyse-vitality-owner-repo-2026-09-11T10-00-00Z.md'
    """
    return f".reports/analyse/vitality/output-analyse-vitality-{owner}-{repo}-{timestamp}.md"


def resolve_version_file() -> Path:
    """Locate the plugin manifest that reports the installed skill version.

    The newest installed cache entry wins by plain lexical order of the glob results, matching the ``ls … | sort | tail
    -1`` pipeline this replaces — deliberately not a version-aware sort. Falls back to the source tree when no cache
    entry exists.

    Returns:
        Path to a ``plugin.json``; may not exist when neither location is populated.
    """
    matches = sorted(str(p) for p in Path.home().glob(_VERSION_GLOB))
    return Path(matches[-1]) if matches else Path(_VERSION_FALLBACK)


def read_version(path: Path) -> str:
    """Read the ``version`` field from a plugin manifest.

    Args:
        path: Path to a ``plugin.json``.

    Returns:
        The version string, or ``unknown`` when the file is missing or unparsable.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    version = data.get("version") if isinstance(data, dict) else None
    return str(version) if version else "unknown"


def agents_yaml(quick: bool, codex_available: bool) -> str:
    """Build the frontmatter agents list for the report header.

    Args:
        quick: ``True`` when ``--quick`` skipped the codex and adversarial passes.
        codex_available: ``True`` when the Codex bridge answered ``available``.

    Returns:
        Indented YAML list items, newline-separated.

    Examples:
        >>> agents_yaml(True, True)
        '  - oss:analyse (orchestrator, --quick: core scoring only)'
        >>> print(agents_yaml(False, False))
          - oss:analyse (orchestrator)
          - foundry:challenger (adversarial review)
    """
    if quick:
        return _QUICK_AGENTS
    return f"{_FULL_AGENTS}\n{_CODEX_AGENT}" if codex_available else _FULL_AGENTS


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code 0; argparse exits 2 on an unknown flag.
    """
    parser = argparse.ArgumentParser(
        prog="build_vitality_paths.py",
        description="Derive the vitality report path and its provenance metadata.",
    )
    parser.add_argument("--owner", default="", help="GitHub owner (GH_OWNER).")
    parser.add_argument("--repo", default="", help="GitHub repository name (GH_REPO).")
    parser.add_argument("--quick", default="false", help="QUICK_MODE sentinel value ('true' skips review passes).")
    parser.add_argument("--timeout", type=int, default=10, help="Max subprocess wait in seconds (default: 10).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print, but write no sentinels — use for any run that is not a real skill step.",
    )
    args = parser.parse_args(argv)
    _set_dry_run(args.dry_run)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    report_file = build_report_path(args.owner, args.repo, timestamp)
    _write_sentinel("analyse-report-file", report_file)

    skill_version = read_version(resolve_version_file())
    report_commit = _run(["git", "rev-parse", "--short", "HEAD"], args.timeout, "unknown")
    bridge = Path(__file__).resolve().parent / "check_bridge.py"
    codex_status = _run([sys.executable, str(bridge), "--status"], args.timeout, "absent")
    codex_available = codex_status == "available"

    print(f"REPORT_FILE={report_file}")
    print(f"REPORT_TIMESTAMP={timestamp}")
    print(f"SKILL_VERSION={skill_version}")
    print(f"REPORT_COMMIT={report_commit}")
    print(f"CODEX_AVAILABLE={1 if codex_available else 0}")
    # Fenced because the value is the only multi-line one: an unterminated block of
    # indented lines gives a reader no way to tell where the value ends.
    print("REPORT_AGENTS_YAML<<END")
    print(agents_yaml(args.quick == "true", codex_available))
    print("END")
    return 0


if __name__ == "__main__":
    sys.exit(main())
