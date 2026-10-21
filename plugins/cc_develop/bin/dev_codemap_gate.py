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
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from dev_run_dir import _owned_by_caller, write_atomic  # sibling bin/ script; link-safe, owner-checked writes

#: Skill names this gate accepts, as a pipe-separated list that is also shown in the unknown-skill error message.
_KNOWN_SKILLS = "debug|feature|fix|plan|refactor|review"

#: Seconds the codemap resolver may run; it probes the index and codemap-py, so a hung child must not stall the skill.
_RESOLVE_TIMEOUT_S = 60

#: Basename prefix of the per-invocation plan namespace dir that plan/SKILL.md creates under the temp dir.
_PLAN_NS_PREFIX = "dev-plan-"

# Currency sentinel basename read back by skills/_shared/codemap-gates.md. Owned here
# because codemap_resolve.py is byte-identical across plugins and cannot name one.
#: Basename prefix of the codemap currency sentinel file that the shared codemap gate documentation reads back.
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


def _is_own_plan_namespace(ns_dir: Path, tmp: Path) -> bool:
    """Tell whether ``ns_dir`` is a plan namespace this user may write into, directly under the temp dir.

    plan/SKILL.md creates the namespace as ``${TMPDIR:-/tmp}/dev-plan-<pid>-<CSID>``, so that is the only shape
    accepted. The path is resolved as a whole, not only its parent, so a symlink at the namespace name that points
    elsewhere is caught; a symlink is refused outright either way. An existing entry must be a directory this user owns;
    a missing one passes, since the later write then simply fails with a warning and creates nothing.
    """
    if not ns_dir.name.startswith(_PLAN_NS_PREFIX) or ns_dir.is_symlink():
        return False
    if ns_dir.resolve().parent != tmp.resolve():
        return False
    try:
        status = os.lstat(ns_dir)
    except FileNotFoundError:
        return True
    return stat.S_ISDIR(status.st_mode) and _owned_by_caller(status)


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
        # The sentinel value becomes a write directory, so a planted or stale value must not point outside the temp dir.
        ns_dir = Path(plan_ns)
        if not _is_own_plan_namespace(ns_dir, tmp):
            print(
                f"! PLAN_NS {plan_ns!r} is not a {_PLAN_NS_PREFIX}* dir of yours under the temp dir; re-run /develop:plan",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return ns_dir / "codemap-raw", ns_dir / "codemap-enabled"
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
        proc = subprocess.run(  # noqa: S603 - argv list, no shell; interpreter plus a sibling script path
            [sys.executable, str(bin_dir / "codemap_resolve.py"), raw, "--currency-prefix", CURRENCY_PREFIX],
            stdout=subprocess.PIPE,
            text=True,
            check=False,
            timeout=_RESOLVE_TIMEOUT_S,
        )
    except OSError as exc:  # mirrors the shell's 127 "no such file" path
        print(f"dev_codemap_gate.py: cannot run codemap_resolve.py: {exc}", file=sys.stderr)
        return "", 127
    except subprocess.TimeoutExpired:  # fail closed: an unanswered resolver cannot confirm codemap is usable
        print(f"dev_codemap_gate.py: codemap_resolve.py exceeded {_RESOLVE_TIMEOUT_S}s", file=sys.stderr)
        return "", 124
    return proc.stdout.rstrip("\n"), proc.returncode


def _write_line(path: Path, value: str) -> None:
    """Persist `value` + newline, warning instead of aborting when the target dir is missing.

    A failed `> file` redirect under `set -u` (no `-e`) left the shell original still printing its stdout line and
    exiting 0; the warning keeps that contract visible.
    """
    try:
        write_atomic(path, value)
    except OSError as exc:
        print(f"dev_codemap_gate.py: cannot write {path}: {exc}", file=sys.stderr)


def main(argv: list[str]) -> int:
    """Normalize CODEMAP_ENABLED for one of the six develop skills."""
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
