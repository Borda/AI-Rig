#!/usr/bin/env python3
"""dev_parse_args.py — parse develop-skill flags from $ARGUMENTS.

Two calling conventions:

1. **Spec-driven (legacy)** — caller declares flag specs explicitly; script prints
   shell-eval-safe ``KEY=VALUE`` lines on stdout, intended for ``eval "$(...)"``.

   Example::

       eval "$(python dev_parse_args.py "$ARGUMENTS" \\
           --neg-bool no-challenge CHALLENGE_ENABLED true \\
           --bool team TEAM_MODE false \\
           --codemap CODEMAP_RAW auto)"

2. **Skill-driven with file writes** — caller passes the ``--skill <name>`` and
   ``--write-files <arguments>`` options; the script looks up the per-skill flag set
   internally and writes each resulting value to a per-flag temp file
   under ``${TMPDIR:-/tmp}/`` with a ``-<CSID>`` session-scoping suffix on
   every filename (no ``eval`` required by the caller).

   Example::

       python dev_parse_args.py --skill feature --write-files "$ARGUMENTS"
       # Now read: $(cat ${TMPDIR:-/tmp}/dev-feature-codemap-${CSID})

   Two files are written for every variable: a *per-skill* path
   (``dev-<skill>-<flag>``) for the modern convention and a *legacy*
   path (e.g. ``dev-team-mode``) so existing downstream blocks that
   read the legacy names keep working without a flag-day rename.

Flag spec types (each occupies 3 positional tokens after the type keyword):

    --bool FLAG VAR DEFAULT     --FLAG present → VAR=true;  absent → VAR=DEFAULT
    --neg-bool FLAG VAR DEFAULT --FLAG present → VAR=false; absent → VAR=DEFAULT
    --codemap VAR DEFAULT       --codemap/--no-codemap → strict/off/auto with
                                double-condition guard (``--no-codemap`` wins on conflict)
    --int FLAG VAR DEFAULT      --FLAG N or --FLAG=N → VAR=N (integer)
    --str FLAG VAR DEFAULT      --FLAG VAL or --FLAG=VAL → VAR=VAL (string)

The ``$ARGUMENTS`` blob and the trailing spec tokens both carry ``--``-shaped tokens that
are consumed by this script's own spec loop and flag extractor — never by argparse's own
matcher. argparse is present only to supply ``-h/--help``; the blob and specs are dispatched
by inspecting ``argv`` directly.

Usage:
    dev_parse_args.py ARGUMENTS [SPEC...]
    dev_parse_args.py --skill <name> --write-files ARGUMENTS

Exit codes:
    0 — success
    1 — malformed spec, unknown skill, or missing ``--write-files`` argument
    2 — --int flag received a non-integer value (also argparse's bad-argument exit)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


def _csid() -> str:
    """Return this session's file-naming scope suffix, resolved fresh on every call.

    Prefers an explicit ``CSID`` (exported by the calling bash block), falls back to
    ``CLAUDE_CODE_SESSION_ID`` (set by Claude Code itself), and finally ``"shared"``
    when neither is present. Resolved as a function rather than a module constant so
    callers (and tests) can change the environment between calls and see it take effect.

    Examples:
        >>> isinstance(_csid(), str) and len(_csid()) > 0
        True
    """
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


class SpecType(str, Enum):
    """Kind of flag a :class:`FlagSpec` declares — the closed set of spec keywords.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``.
    The mixin keeps ``SpecType.BOOL == "bool"`` true, so values that arrive as plain
    strings from the CLI boundary still compare equal.

    Examples:
        >>> SpecType("neg-bool").value
        'neg-bool'
        >>> SpecType.CODEMAP == "codemap"
        True
    """

    BOOL = "bool"
    NEG_BOOL = "neg-bool"
    CODEMAP = "codemap"
    INT = "int"
    STR = "str"


# Spec keyword → count of positional tokens it consumes. Codemap takes 2 (VAR DEFAULT,
# no FLAG — it is always the ``--codemap``/``--no-codemap`` pair); every other kind takes 3.
_TYPE_ARITIES: dict[str, int] = {f"--{kind.value}": (2 if kind == SpecType.CODEMAP else 3) for kind in SpecType}


@dataclass
class FlagSpec:
    """Parsed declaration of a single flag (or flag pair for codemap)."""

    kind: SpecType
    flag: str  # e.g. "no-challenge"; empty string for codemap kind
    var: str  # shell variable name to emit
    default: str  # string representation of default value


def parse_specs(tokens: list[str]) -> list[FlagSpec]:
    """Parse the flag-spec token list into FlagSpec objects.

    Args:
        tokens: everything after the ARGUMENTS string on the command line.

    Returns:
        List of FlagSpec instances in declaration order.

    Raises:
        SystemExit(1): on malformed spec (wrong token count).

    Examples:
        >>> parse_specs(['--bool', 'team', 'TEAM_MODE', 'false']) == [
        ...     FlagSpec(kind=SpecType.BOOL, flag='team', var='TEAM_MODE', default='false')
        ... ]
        True
        >>> parse_specs(['--codemap', 'CODEMAP_RAW', 'auto']) == [
        ...     FlagSpec(kind=SpecType.CODEMAP, flag='', var='CODEMAP_RAW', default='auto')
        ... ]
        True
    """
    specs: list[FlagSpec] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok not in _TYPE_ARITIES:
            print(f"dev_parse_args: unknown spec keyword '{tok}'", file=sys.stderr)
            sys.exit(1)
        arity = _TYPE_ARITIES[tok]
        rest = tokens[idx + 1 : idx + 1 + arity]
        if len(rest) < arity:
            print(
                f"dev_parse_args: '{tok}' needs {arity} tokens, got {len(rest)}",
                file=sys.stderr,
            )
            sys.exit(1)
        kind = SpecType(tok.lstrip("-"))
        if kind == SpecType.CODEMAP:
            var, default = rest
            specs.append(FlagSpec(kind=kind, flag="", var=var, default=default))
        else:
            flag, var, default = rest
            specs.append(FlagSpec(kind=kind, flag=flag, var=var, default=default))
        idx += 1 + arity
    return specs


# ---------------------------------------------------------------------------
# Flag extraction
# ---------------------------------------------------------------------------


def _shell_quote(value: str) -> str:
    """Return a shell-safe single-quoted representation of value.

    Examples:
        >>> _shell_quote("hello world")
        "'hello world'"
        >>> _shell_quote("it's")
        "'it'\\\\''s'"
        >>> _shell_quote("")
        "''"
    """
    return "'" + value.replace("'", "'\\''") + "'"


def _extract_value_flag(flag: str, args: str) -> tuple[str | None, str]:
    """Extract ``--flag VALUE`` or ``--flag=VALUE`` from args.

    Returns (value_or_None, args_with_flag_and_value_stripped).

    Examples:
        >>> _extract_value_flag("max-depth", "--max-depth 5 target.py")
        ('5', 'target.py')
        >>> _extract_value_flag("plan", "--plan=path/to/file.md fix issue")
        ('path/to/file.md', 'fix issue')
        >>> _extract_value_flag("plan", "fix issue")
        (None, 'fix issue')
    """
    # --flag=VALUE form. Require a full flag-token match so --planets does not
    # satisfy ``--plan``, and preserve the historical non-whitespace value contract.
    eq_pattern = re.compile(r"(?<!\S)--" + re.escape(flag) + r"=(\S+)")
    m = eq_pattern.search(args)
    if m:
        return m.group(1), eq_pattern.sub("", args).strip()

    # --flag VALUE form (value = next non-flag, non-whitespace token).
    space_pattern = re.compile(r"(?<!\S)--" + re.escape(flag) + r"\s+(?!--)(\S+)")
    m = space_pattern.search(args)
    if m:
        full_match = m.group(0)
        return m.group(1), args.replace(full_match, "", 1).strip()

    return None, args


def extract_flags(arguments: str, specs: list[FlagSpec]) -> tuple[dict[str, str], str]:
    """Extract all declared flags from arguments string.

    Args:
        arguments: raw $ARGUMENTS string.
        specs: parsed flag specs.

    Returns:
        Tuple of (var_to_value dict, clean_args string).

    Examples:
        >>> specs = parse_specs(['--bool', 'team', 'TEAM_MODE', 'false'])
        >>> extract_flags('--team do the thing', specs)
        ({'TEAM_MODE': 'true'}, 'do the thing')
        >>> extract_flags('do the thing', specs)
        ({'TEAM_MODE': 'false'}, 'do the thing')
    """
    result: dict[str, str] = {}
    clean = arguments

    for spec in specs:
        if spec.kind == SpecType.BOOL:
            token = f"--{spec.flag}"
            token_pattern = re.compile(r"(?<!\S)" + re.escape(token) + r"(?![A-Za-z0-9_-])")
            if token_pattern.search(clean):
                result[spec.var] = "true"
                clean = token_pattern.sub("", clean)
            else:
                result[spec.var] = spec.default

        elif spec.kind == SpecType.NEG_BOOL:
            token = f"--{spec.flag}"
            token_pattern = re.compile(r"(?<!\S)" + re.escape(token) + r"(?![A-Za-z0-9_-])")
            if token_pattern.search(clean):
                result[spec.var] = "false"
                clean = token_pattern.sub("", clean)
            else:
                result[spec.var] = spec.default

        elif spec.kind == SpecType.CODEMAP:
            # Double-condition guard: ``--no-codemap`` wins; ``--codemap`` only sets strict
            # when ``--no-codemap`` absent.
            no_pattern = re.compile(r"(?<!\S)--no-codemap(?![A-Za-z0-9_-])")
            yes_pattern = re.compile(r"(?<!\S)--codemap(?![A-Za-z0-9_-])")
            has_no = no_pattern.search(clean) is not None
            has_yes = yes_pattern.search(clean) is not None
            if has_no:
                result[spec.var] = "off"
                clean = no_pattern.sub("", clean)
                clean = yes_pattern.sub("", clean)
            elif has_yes:
                result[spec.var] = "strict"
                clean = yes_pattern.sub("", clean)
            else:
                result[spec.var] = spec.default

        elif spec.kind == SpecType.INT:
            val, clean = _extract_value_flag(spec.flag, clean)
            if val is not None:
                try:
                    int(val)
                except ValueError:
                    print(
                        f"dev_parse_args: --{spec.flag} expects integer, got '{val}'",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                result[spec.var] = val
            else:
                result[spec.var] = spec.default

        elif spec.kind == SpecType.STR:
            val, clean = _extract_value_flag(spec.flag, clean)
            result[spec.var] = val if val is not None else spec.default

    # Normalise whitespace in clean args
    clean = " ".join(clean.split())
    return result, clean


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(arguments: str, spec_tokens: list[str]) -> str:
    """Parse flags and return the eval-able shell assignment block.

    Args:
        arguments: raw $ARGUMENTS string.
        spec_tokens: everything after the arguments string on the CLI.

    Returns:
        Multi-line string of shell KEY=VALUE assignments ending with CLEAN_ARGS.

    Examples:
        >>> out = run('--team fix auth.py', ['--bool', 'team', 'TEAM_MODE', 'false'])
        >>> "TEAM_MODE='true'" in out
        True
        >>> "CLEAN_ARGS='fix auth.py'" in out
        True
    """
    specs = parse_specs(spec_tokens)
    values, clean_args = extract_flags(arguments, specs)
    lines = [f"{var}={_shell_quote(val)}" for var, val in values.items()]
    lines.append(f"CLEAN_ARGS={_shell_quote(clean_args)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Skill registry (used by ``--skill <name> --write-files``)
# ---------------------------------------------------------------------------


def _spec(kind: SpecType, flag: str, var: str, default: str) -> FlagSpec:
    """Tiny helper so the registry table below stays compact and readable."""
    return FlagSpec(kind=kind, flag=flag, var=var, default=default)


# Per-skill flag declarations. Keep in sync with each skill's SKILL.md.
# Each entry: (FlagSpec, legacy_filename_or_None).
#
# ``legacy_filename`` preserves the original temp-file paths the existing
# downstream Bash() blocks read (e.g. ``dev-team-mode``, ``dev-upstream``),
# so the surgical replacement of the eval block in each SKILL.md does not
# need to touch any later block.  ``None`` means no legacy path was written
# before (e.g. a newly registered flag).
SKILL_SPECS: dict[str, list[tuple[FlagSpec, str | None]]] = {
    "feature": [
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-challenge-enabled"),
        (_spec(SpecType.BOOL, "challenge", "CHALLENGE_FORCED", "false"), "dev-challenge-forced"),
        (_spec(SpecType.BOOL, "team", "TEAM_MODE", "false"), "dev-team-mode"),
        (_spec(SpecType.BOOL, "worktree", "WORKTREE_ENABLED", "false"), None),
        (_spec(SpecType.BOOL, "accept-no-plan", "ACCEPT_NO_PLAN", "false"), "dev-accept-no-plan"),
        (_spec(SpecType.CODEMAP, "", "CODEMAP_RAW", "auto"), "dev-codemap-raw"),
        (_spec(SpecType.STR, "repo", "REPO_NAME", ""), "dev-upstream"),
    ],
    "fix": [
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-challenge-enabled"),
        (_spec(SpecType.BOOL, "challenge", "CHALLENGE_FORCED", "false"), "dev-challenge-forced"),
        (_spec(SpecType.BOOL, "accept-no-plan", "ACCEPT_NO_PLAN", "false"), "dev-accept-no-plan"),
        (_spec(SpecType.BOOL, "team", "TEAM_MODE", "false"), "dev-team-mode"),
        (_spec(SpecType.BOOL, "worktree", "WORKTREE_ENABLED", "false"), None),
        (_spec(SpecType.CODEMAP, "", "CODEMAP_RAW", "auto"), "dev-codemap-raw"),
        (_spec(SpecType.STR, "repo", "REPO_NAME", ""), "dev-upstream"),
    ],
    "debug": [
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-challenge-enabled"),
        (_spec(SpecType.BOOL, "challenge", "CHALLENGE_FORCED", "false"), "dev-challenge-forced"),
        (_spec(SpecType.BOOL, "team", "TEAM_MODE", "false"), "dev-team-mode"),
        (_spec(SpecType.BOOL, "worktree", "WORKTREE_ENABLED", "false"), None),
        (_spec(SpecType.CODEMAP, "", "CODEMAP_RAW", "auto"), "dev-codemap-raw"),
        (_spec(SpecType.STR, "ci-run", "CI_RUN_ID", ""), "dev-ci-run-id"),
        (_spec(SpecType.STR, "repo", "REPO_NAME", ""), "dev-upstream"),
    ],
    "refactor": [
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-challenge-enabled"),
        (_spec(SpecType.BOOL, "challenge", "CHALLENGE_FORCED", "false"), "dev-challenge-forced"),
        (_spec(SpecType.BOOL, "team", "TEAM_MODE", "false"), "dev-team-mode"),
        (_spec(SpecType.BOOL, "worktree", "WORKTREE_ENABLED", "false"), None),
        (_spec(SpecType.BOOL, "accept-no-plan", "ACCEPT_NO_PLAN", "false"), "dev-accept-no-plan"),
        (_spec(SpecType.CODEMAP, "", "CODEMAP_RAW", "auto"), "dev-codemap-raw"),
        (_spec(SpecType.STR, "repo", "REPO_NAME", ""), "dev-upstream"),
    ],
    "plan": [
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-challenge-enabled"),
        (_spec(SpecType.CODEMAP, "", "CODEMAP_RAW", "auto"), "dev-codemap-raw"),
        (_spec(SpecType.INT, "max-depth", "MAX_DEPTH", "3"), None),
    ],
    "review": [
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-review-challenge-enabled"),
        (_spec(SpecType.BOOL, "challenge", "CHALLENGE_FORCED", "false"), "dev-review-challenge-forced"),
        (_spec(SpecType.BOOL, "worktree", "WORKTREE_ENABLED", "false"), None),
        (_spec(SpecType.BOOL, "full", "FANOUT_FULL", "false"), "dev-review-fanout-full"),
        (_spec(SpecType.CODEMAP, "", "CODEMAP_RAW", "auto"), "dev-review-codemap-enabled"),
    ],
}


def _per_skill_filename(skill: str, spec: FlagSpec) -> str:
    """Compose the per-skill temp filename for a given spec, suffixed with the session scope.

    For codemap (no ``flag`` token) the key is the literal string ``codemap``.

    Examples:
        >>> _per_skill_filename(
        ...     "feature", FlagSpec(kind=SpecType.BOOL, flag="team", var="TEAM_MODE", default="false")
        ... ).startswith("dev-feature-team-")
        True
        >>> _per_skill_filename(
        ...     "debug", FlagSpec(kind=SpecType.CODEMAP, flag="", var="CODEMAP_RAW", default="auto")
        ... ).startswith("dev-debug-codemap-")
        True
    """
    key = spec.flag or (SpecType.CODEMAP.value if spec.kind == SpecType.CODEMAP else spec.var.lower())
    return f"dev-{skill}-{key}-{_csid()}"


def _tmp_dir() -> Path:
    """Return the directory temp files are written to (mirrors shell ``${TMPDIR:-/tmp}`` — tmpdir-exempt: base dir only,
    not a filename).

    Per-file session suffixing happens in ``_per_skill_filename``/``write_skill_files``, not here.
    """
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def write_skill_files(skill: str, arguments: str, tmp_dir: Path | None = None) -> dict[str, str]:
    """Parse arguments using the registered ``skill`` specs and persist values to temp files.

    Writes two files per variable when a legacy path is registered:

    * Per-skill: ``${TMPDIR}/dev-<skill>-<flag>-<CSID>`` — modern convention used by callers
      that read flags back with explicit per-skill paths.
    * Legacy: ``${TMPDIR}/<legacy-name>-<CSID>`` — preserves backward compatibility with
      downstream Bash() blocks that read shared paths like ``dev-team-mode-<CSID>``.

    Args:
        skill: registered skill name (key of ``SKILL_SPECS``).
        arguments: raw ``$ARGUMENTS`` string.
        tmp_dir: override for the temp directory (defaults to ``${TMPDIR:-/tmp}`` — tmpdir-exempt: base directory
            only, not a filename).

    Returns:
        Dict mapping each declared shell variable name to its resolved string value.

    Raises:
        SystemExit(1): if ``skill`` is not a registered key.

    Examples:
        >>> import tempfile
        >>> from pathlib import Path
        >>> with tempfile.TemporaryDirectory() as d:
        ...     vals = write_skill_files("feature", "--team fix auth.py", tmp_dir=Path(d))
        ...     vals["TEAM_MODE"]
        ...     any(p.name.startswith("dev-feature-team-") for p in Path(d).iterdir())
        ...     any(p.name.startswith("dev-team-mode-") for p in Path(d).iterdir())
        'true'
        True
        True
    """
    if skill not in SKILL_SPECS:
        known = ", ".join(sorted(SKILL_SPECS))
        print(
            f"dev_parse_args: unknown skill '{skill}' (registered: {known})",
            file=sys.stderr,
        )
        sys.exit(1)

    target_dir = tmp_dir if tmp_dir is not None else _tmp_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    entries = SKILL_SPECS[skill]
    specs = [spec for spec, _ in entries]
    values, _clean = extract_flags(arguments, specs)

    for spec, legacy in entries:
        value = values[spec.var]
        (target_dir / _per_skill_filename(skill, spec)).write_text(f"{value}\n")
        if legacy is not None:
            (target_dir / f"{legacy}-{_csid()}").write_text(f"{value}\n")
    # Write CLEAN_ARGS (flags stripped) to a per-skill file so callers avoid eval
    (target_dir / f"dev-{skill}-clean-args-{_csid()}").write_text(f"{_clean}\n")
    return values


def _run_skill_mode(argv: list[str]) -> int:
    """Dispatch the ``--skill <name> --write-files ARGUMENTS`` invocation.

    Args:
        argv: The full argument list (``sys.argv[1:]``), containing ``--skill``.

    Returns:
        ``0`` on success; ``1`` on a missing name, missing ``--write-files``, or missing
        ARGUMENTS string.

    No doctest — writes temp files via :func:`write_skill_files`; covered by pytest.
    """
    try:
        skill_idx = argv.index("--skill")
        skill_name = argv[skill_idx + 1]
    except (ValueError, IndexError):
        print("dev_parse_args: --skill requires a name argument", file=sys.stderr)
        return 1
    if "--write-files" not in argv:
        print(
            "dev_parse_args: --skill requires --write-files (only mode supported)",
            file=sys.stderr,
        )
        return 1
    # Remaining positional after stripping both flag pairs is the ARGUMENTS string
    remaining = [tok for i, tok in enumerate(argv) if i not in {skill_idx, skill_idx + 1} and tok != "--write-files"]
    if not remaining:
        print("dev_parse_args: --skill mode needs the ARGUMENTS string", file=sys.stderr)
        return 1
    write_skill_files(skill_name, remaining[0])
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Two invocation forms:

    * ``dev_parse_args.py ARGUMENTS [SPEC...]`` — print eval-able shell block on stdout
      (legacy form used by ``eval "$(...)"`` callers).
    * ``dev_parse_args.py --skill <name> --write-files ARGUMENTS`` — parse using the
      registered skill specs and write each value under ``${TMPDIR:-/tmp}`` (tmpdir-exempt: base directory only, not a
      filename). Each written file gets its own ``-<CSID>`` suffix, see ``_per_skill_filename``.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success; ``1`` on empty argv or a malformed ``--skill`` invocation.
        Internal spec/skill errors exit ``1`` and ``--int`` errors exit ``2`` via SystemExit.

    No doctest — dispatches to stdout-printing / temp-file-writing paths; covered by pytest.
    """
    args = list(sys.argv[1:] if argv is None else argv)

    # argparse only supplies ``-h/--help``; the blob and spec tokens are dispatched directly
    # because they carry ``--``-shaped tokens argparse must never try to match.
    if args and args[0] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(
            prog="dev_parse_args.py",
            description="Parse develop-skill flags from an $ARGUMENTS blob.",
        )
        parser.add_argument("arguments", nargs="?", help="Raw $ARGUMENTS blob (parsed internally).")
        parser.add_argument("--skill", help="Registered skill name for the write-files mode.")
        parser.add_argument("--write-files", action="store_true", help="Write parsed values to temp files.")
        parser.parse_args(args)  # exits 0 after printing help

    if not args:
        print(
            "usage:\n"
            "  dev_parse_args.py ARGUMENTS [SPEC...]\n"
            "  dev_parse_args.py --skill <name> --write-files ARGUMENTS",
            file=sys.stderr,
        )
        return 1

    if "--skill" in args:
        return _run_skill_mode(args)

    # Legacy spec-driven invocation
    print(run(args[0], args[1:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
