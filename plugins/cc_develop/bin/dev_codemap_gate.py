#!/usr/bin/env python3
"""dev_codemap_gate.py — CODEMAP_ENABLED normalization shared by all six develop skills.

Consolidates the six near-identical gate blocks: read the skill's raw codemap flag,
normalize it through bin/codemap_resolve.py, abort when strict mode cannot be satisfied,
and persist the resolved true/false to the skill's own sentinel.

Usage: python dev_codemap_gate.py <debug|feature|fix|plan|refactor|review>

Output: resolved "true"/"false" on stdout (also persisted to the skill's sentinel).

Single failure policy across all six skills is behaviour-preserving, not a
unification of differing semantics: codemap_resolve.py exits non-zero *only* when
MODE=strict and codemap-py/index is missing (see its header contract). The
per-skill `CODEMAP_ENABLED=false` fallbacks that previously followed a non-zero
exit were therefore unreachable.

`plan` stores its flags in a run-namespace directory rather than TMPDIR sentinels,
so its paths are resolved from dev-plan-ns-current instead of being composed here.

CSID is inherited from the caller's exported environment and never re-derived from
the parent process id: inside a script that id is the invoking shell's, which changes on
every Bash tool call, so a locally derived CSID would name a different sentinel each time.

Exit codes:
  0 — resolved; value written to the skill's sentinel
  1 — unknown skill, missing plan namespace, or strict mode with codemap unavailable
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_KNOWN_SKILLS = "debug|feature|fix|plan|refactor|review"

# Currency sentinel basename read back by skills/_shared/codemap-gates.md. Owned here
# because codemap_resolve.py is byte-identical across plugins and cannot name one.
CURRENCY_PREFIX = "dev-codemap-currency"


def _tmp_dir() -> Path:
    """Session temp dir; never a hardcoded /tmp, which is absent on native Windows Python."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _csid() -> str:
    """Caller-exported session token, mirroring `${CSID:-${CLAUDE_CODE_SESSION_ID:-shared}}`."""
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def _first_line(path: Path) -> str:
    """First line of *path* minus its trailing newline; empty string when unreadable.

    Mirrors `[ -f f ] && IFS= read -r X < f`: an unterminated final line still yields its value there (read assigns
    before returning non-zero), unlike gate-on-sentinel's explicit `|| VALUE=""` wipe.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.readline().rstrip("\n")
    except OSError:
        return ""


def _resolve_paths(skill: str, tmp: Path, csid: str) -> tuple[Path, Path]:
    """Map *skill* to its (raw-flag input, resolved-value output) sentinel pair."""
    if skill in ("debug", "feature", "fix", "refactor"):
        # All four read the per-skill sentinel dev_parse_args.py writes from its CODEMAP spec.
        # refactor used to name a `-raw` variant that only an inline shell block in its
        # SKILL.md ever wrote, so the skill had to re-parse the flag the parser had already
        # parsed. Reading the same file as its siblings removes that second parse.
        return tmp / f"dev-{skill}-codemap-{csid}", tmp / f"dev-{skill}-codemap-enabled-{csid}"
    if skill == "review":
        # review round-trips one file: raw flag in, resolved value out
        round_trip = tmp / f"dev-review-codemap-enabled-{csid}"
        return round_trip, round_trip
    if skill == "plan":
        plan_ns = _first_line(tmp / f"dev-plan-ns-current-{csid}")
        if not plan_ns:
            print(
                "! PLAN_NS empty — dev-plan-ns-current not found; re-run /develop:plan",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return Path(plan_ns) / "codemap-raw", Path(plan_ns) / "codemap-enabled"
    print(
        f"dev_codemap_gate.py: unknown skill '{skill}' (expected {_KNOWN_SKILLS})",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _run_codemap_resolve(bin_dir: Path, raw: str) -> tuple[str, int]:
    """Run bin/codemap_resolve.py, returning its (stripped stdout, exit code).

    stderr is inherited, not captured — the shell original never redirected it.

    ``--currency-prefix`` is supplied here because ``codemap_resolve.py`` is byte-identical across consuming plugins
    (propagate_shared.py MANIFEST); develop's sentinel name is develop's own concern and must never be hard-coded in
    that shared file.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(bin_dir / "codemap_resolve.py"), raw, "--currency-prefix", CURRENCY_PREFIX],
            stdout=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:  # mirrors the shell's 127 "no such file" path
        print(f"dev_codemap_gate.py: cannot run codemap_resolve.py: {exc}", file=sys.stderr)
        return "", 127
    return proc.stdout.rstrip("\n"), proc.returncode


def _write_line(path: Path, value: str) -> None:
    """Persist `value` + newline, warning instead of aborting when the target dir is missing.

    A failed `> file` redirect under `set -u` (no `-e`) left the shell original still printing its stdout line and
    exiting 0; the warning keeps that contract visible.
    """
    try:
        path.write_text(value + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"dev_codemap_gate.py: cannot write {path}: {exc}", file=sys.stderr)


def main(argv: list[str]) -> int:
    skill = argv[1] if len(argv) > 1 else ""
    bin_dir = Path(__file__).resolve().parent
    in_file, out_file = _resolve_paths(skill, _tmp_dir(), _csid())

    codemap_raw = _first_line(in_file) or "auto"
    enabled, resolve_exit = _run_codemap_resolve(bin_dir, codemap_raw)
    if resolve_exit != 0:
        print(
            "! BLOCKED — codemap unavailable but --codemap (strict) passed; "
            "run /codemap-py:scan-codebase or install codemap plugin",
            file=sys.stderr,
        )
        return 1

    _write_line(out_file, enabled)
    print(enabled)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
