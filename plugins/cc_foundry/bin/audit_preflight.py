#!/usr/bin/env python
"""audit_preflight.py — parse /foundry:audit arguments and prepare its run state.

Replaces the inline pre-flight block. It does four things:

1. Splits ``--keep "<items>"`` and the mode flags out of the raw argument string,
   leaving the scope tokens.
2. Rejects ``--upgrade`` combined with ``--adversarial`` or ``--efficiency``, and ``--consolidate`` combined
   with any of the three other modes, and ``--measured`` without ``--efficiency``.
3. Probes ``jq``, ``git`` and ``node``, memoising each result for four hours
   under ``.temp/state/preflight/`` so repeat invocations skip the lookup.
4. Resolves the audit ``templates/`` directory and writes the session sentinels
   the later steps read back (``local-mode``, ``measured``, ``audit-tpl``, ``keep-items``),
   then clears any stale skill contract. The scope tokens are printed rather than
   persisted, as in the shell original — nothing re-derives scope across blocks.

Flags are parsed by exact token, not substring, so a scope token containing a
flag name is not mistaken for the flag. The shell original used ``grep``/``sed``
rather than ``[[ =~ ]]`` because zsh leaves ``BASH_REMATCH`` empty on a match;
that trap does not exist here.

stdout is the state summary the skill reads:

    local-mode=false adversarial=false efficiency=true consolidate=false upgrade=false skip-gate=false fast=false
    measured=false
    tools: jq=yes git=yes node=no
    audit-tpl=/path/to/templates
    keep-items=
    scope=plugins
    dispatch=mode:consolidate    (only with --consolidate: the skill runs that mode and no other step)

Usage:
    audit_preflight.py --arguments "<raw $ARGUMENTS>"

Exit codes:
    0   pre-flight complete
    1   mutually exclusive flags, no .claude/ directory (not required by --consolidate), no jq under
        --consolidate, templates unresolvable, or the state directory cannot be secured (unsafe
        session token, symlink, owned by another user)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

from resolve_skill_subdir import resolve as resolve_subdir

#: How long a successful tool probe stays valid, in seconds.
PREFLIGHT_TTL = 4 * 60 * 60

#: Matches a quoted ``--keep "..."`` argument so it can be extracted before flag parsing.
_KEEP = re.compile(r'--keep\s+"([^"]*)"')

#: Flag token → the state key it sets. `--challenge` is an alias of `--adversarial`.
FLAGS: dict[str, str] = {
    "--local": "local-mode",
    "--adversarial": "adversarial",
    "--challenge": "adversarial",
    "--efficiency": "efficiency",
    "--consolidate": "consolidate",
    "--upgrade": "upgrade",
    "--skip-gate": "skip-gate",
    "--fast": "fast",
    "--measured": "measured",
}

#: Names of the boolean audit state flags, in the order they are printed as ``key=value`` pairs.
STATE_KEYS = ("local-mode", "adversarial", "efficiency", "consolidate", "upgrade", "skip-gate", "fast", "measured")

#: Characters a session token may use; it becomes part of a directory name under TMPDIR, so no separator or ``..``.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")


def _temp_base() -> Path:
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    return Path(os.environ.get("TMPDIR") or tmp_fallback)


def _session_token() -> str:
    token = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError(f"unsafe session token {token!r}: CSID must match [A-Za-z0-9_-]+")
    return token


def _private_dir(path: Path) -> Path:
    """Create ``path`` for this user only, refusing a symlink or a directory another user owns.

    ``TMPDIR`` falls back to a shared ``/tmp`` where another user can pre-create the predictable state directory;
    writing into it would leak ``keep-items`` and ``audit-tpl`` or follow a planted link.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise PermissionError(f"{path} is a symlink or owned by another user; remove it and rerun")
    return path


def parse_arguments(raw: str) -> tuple[dict[str, bool], str, str]:
    """Split a raw argument string into (flag state, keep items, scope tokens)."""
    keep_match = _KEEP.search(raw)
    keep_items = keep_match.group(1) if keep_match else ""
    without_keep = _KEEP.sub(" ", raw)

    state = dict.fromkeys(STATE_KEYS, False)
    scope: list[str] = []
    for token in without_keep.split():
        key = FLAGS.get(token)
        if key:
            state[key] = True
        else:
            scope.append(token)
    return state, keep_items, " ".join(scope)


def probe_tool(name: str, *, state_dir: Path) -> bool:
    """Report whether a tool is available, memoising a hit for PREFLIGHT_TTL."""
    marker = state_dir / f"{name}.ok"
    try:
        stamp = int(marker.read_text(encoding="utf-8").strip())
        if time.time() - stamp < PREFLIGHT_TTL:
            return True
    except (OSError, ValueError):
        pass
    if shutil.which(name) is None:
        return False
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        _write_fresh(marker, str(int(time.time())))
    except OSError:
        pass
    return True


#: Run-state paths the audit creates or writes under, relative to the working tree. Each is resolved in full (every link
#: along it followed), so a link at any component, the last included, is seen.
_STATE_PATHS = (".temp/state", ".temp/state/preflight", ".reports/audit")

#: Top-level directories of the tree whose files are instruction sources; the session state directory never lands there.
_SOURCE_TOPS = ("plugins", ".claude", ".codex")


def _state_dir_problem() -> str:
    """Return why the run-state paths cannot be trusted, or an empty string.

    ``.temp``, ``.reports`` and the directories below them may be links to somewhere else, but not to another place
    inside the tree: state files and run directories are created, replaced and removed there by name, and a link into
    the instruction sources would let them reach one.
    """
    root = Path.cwd().resolve()
    for name in _STATE_PATHS:
        target = (root / name).resolve()
        if target.is_relative_to(root) and not any(target.is_relative_to(root / top) for top in (".temp", ".reports")):
            return f"{name} resolves to {target}, inside the tree but outside .temp/ and .reports/"
    try:
        session = (_temp_base() / f"audit-state-{_session_token()}").resolve()
    except ValueError:
        return ""  # an unsafe session token is reported where the state is written
    if any(session.is_relative_to(root / top) for top in _SOURCE_TOPS):
        return f"the TMPDIR state directory resolves to {session}, inside the instruction sources"
    return ""


def _write_fresh(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` by replacing the name, never writing through whatever the name points at.

    A sentinel that is already a symlink (or a hard link) to another file is replaced by a fresh regular file, so the
    file it pointed at keeps its content.
    """
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staging.unlink(missing_ok=True)
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(staging, path)


def write_state(values: dict[str, str]) -> Path:
    """Write the session sentinels the later audit steps read back."""
    state_dir = _private_dir(_temp_base() / f"audit-state-{_session_token()}")
    for name, value in values.items():
        _write_fresh(state_dir / name, f"{value}\n")
    return state_dir


def _bind_arguments(argv: list[str]) -> list[str]:
    """Return ``argv`` with an ``--arguments`` value that starts with a dash bound to its option.

    The skill passes the raw user text as ``--arguments "$ARGUMENTS"``. When that text is only a flag
    (``--consolidate``, ``--efficiency``), argparse reads it as the next option and exits 2, so it is joined as
    ``--arguments=<text>``.
    """
    bound: list[str] = []
    skip = False
    for index, option in enumerate(argv):
        if skip:
            skip = False
        elif option == "--arguments" and index + 1 < len(argv) and argv[index + 1].startswith("-"):
            bound.append(f"--arguments={argv[index + 1]}")
            skip = True
        else:
            bound.append(option)
    return bound


def main() -> int:
    """Parse /foundry:audit arguments and prepare its run state."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arguments", default="")
    parser.add_argument("--claude-dir", default=".claude")
    args = parser.parse_args(_bind_arguments(sys.argv[1:]))

    state, keep_items, scope = parse_arguments(args.arguments)

    if state["upgrade"] and (state["adversarial"] or state["efficiency"]):
        print("! --upgrade is mutually exclusive with --adversarial and --efficiency")
        return 1
    if state["consolidate"] and (state["upgrade"] or state["adversarial"] or state["efficiency"]):
        print("! --consolidate is standalone: mutually exclusive with --upgrade, --adversarial and --efficiency")
        return 1
    unknown = [token for token in scope.split() if token.startswith("-")]
    if state["consolidate"] and unknown:
        # The consolidate dispatch skips the skill's own unknown-flag question, and a stray token would reach the scope.
        print(f"! --consolidate got unknown flag(s): {' '.join(unknown)} — re-invoke with supported flags only")
        return 1
    if state["measured"] and not state["efficiency"]:
        print("! --measured only adds measured evidence to --efficiency; pass both")
        return 1
    if not state["consolidate"] and not Path(args.claude_dir).is_dir():
        print("! BREAKING: .claude/ directory not found — nothing to audit")
        return 1

    if problem := _state_dir_problem():
        print(f"! BLOCKED — {problem}; remove the link and rerun")
        return 1
    preflight_dir = Path(".temp/state/preflight")
    tools = {name: probe_tool(name, state_dir=preflight_dir) for name in ("jq", "git", "node")}
    if state["consolidate"] and not tools["jq"]:
        print("! MISSING: jq — --consolidate merges the curator outputs with jq; install it before the cluster spend")
        return 1
    if not tools["jq"]:
        print("⚠ MISSING: jq not found — Check 4 (permissions-guide drift) will be skipped")
    if not tools["git"]:
        print("⚠ MISSING: git not found — path portability check may miss repo-root references")
    if not tools["node"]:
        print("⚠ MISSING: node not found — the upgrade-mode hook syntax check will be skipped")

    templates = resolve_subdir("audit", "templates", local=state["local-mode"])
    if templates is None:
        print("! BREAKING: audit/templates not found — run /foundry:setup first")
        return 1

    Path(".temp/state/skill-contract.md").unlink(missing_ok=True)
    try:
        write_state(
            {
                "local-mode": str(state["local-mode"]).lower(),
                "measured": str(state["measured"]).lower(),
                "audit-tpl": str(templates),
                "keep-items": keep_items,
            }
        )
    except (OSError, ValueError) as exc:
        print(f"! BLOCKED — cannot prepare audit state: {exc}")
        return 1

    print(" ".join(f"{key}={str(state[key]).lower()}" for key in STATE_KEYS))
    print("tools: " + " ".join(f"{name}={'yes' if ok else 'no'}" for name, ok in tools.items()))
    print(f"audit-tpl={templates}")
    print(f"keep-items={keep_items}")
    print(f"scope={scope}")
    if state["consolidate"]:
        print("dispatch=mode:consolidate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
