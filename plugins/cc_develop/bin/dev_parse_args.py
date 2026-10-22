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

   Skills registered in ``_DERIVED_SENTINELS`` also get values classified from the
   flag-stripped arguments, so no skill has to test raw argument text in shell — for
   example ``dev-review-pr-number``, the PR number ``/develop:review`` was given, or empty.

Flag spec types (each occupies 3 positional tokens after the type keyword):

    --bool FLAG VAR DEFAULT     --FLAG present → VAR=true;  absent → VAR=DEFAULT
    --neg-bool FLAG VAR DEFAULT --FLAG present → VAR=false; absent → VAR=DEFAULT
    --codemap VAR DEFAULT       --codemap/--no-codemap → strict/off/auto with
                                double-condition guard (``--no-codemap`` wins on conflict)
    --int FLAG VAR DEFAULT      --FLAG N or --FLAG=N → VAR=N (integer)
    --str FLAG VAR DEFAULT      --FLAG VAL or --FLAG=VAL → VAR=VAL (string; VAL may be a quoted phrase)

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
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dev_run_dir import make_private_dirs, write_atomic  # sibling bin/ script; link-safe, owner-checked writes


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
#: Maps each spec kind keyword to the number of positional tokens it consumes when spec declarations are parsed.
_TYPE_ARITIES: dict[str, int] = {f"--{kind.value}": (2 if kind == SpecType.CODEMAP else 3) for kind in SpecType}

#: Shape of a spec's VAR token. Spec-driven output is ``VAR=value`` lines fed to ``eval``, so a VAR carrying shell
#: syntax (``X;cmd``, ``$(...)``) would run as code; only a plain ASCII shell identifier may reach that output.
_SHELL_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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
        SystemExit(1): on malformed spec (unknown keyword, wrong token count, or a VAR that is not a plain shell
            identifier — the VAR name is emitted unquoted into ``eval`` input).

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
            flag = ""
        else:
            flag, var, default = rest
        if not _SHELL_VAR_RE.fullmatch(var):
            print(f"dev_parse_args: '{tok}' variable name {var!r} is not a shell identifier", file=sys.stderr)
            sys.exit(1)
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


#: A flag value: a double- or single-quoted phrase (spaces allowed) or one non-whitespace token.
_VALUE = r"""("[^"]*"|'[^']*'|\S+)"""


def _unquote(value: str) -> str:
    """Strip one pair of matching surrounding quotes from a captured flag value.

    Examples:
        >>> _unquote('"a b"'), _unquote("'a'"), _unquote("plain"), _unquote('"')
        ('a b', 'a', 'plain', '"')
    """
    return value[1:-1] if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0] else value


def _extract_value_flag(flag: str, args: str) -> tuple[str | None, str]:
    """Extract ``--flag VALUE`` or ``--flag=VALUE`` from args.

    VALUE is one token, or a quoted phrase whose surrounding quotes are dropped.

    Returns (value_or_None, args_with_flag_and_value_stripped).

    Examples:
        >>> _extract_value_flag("max-depth", "--max-depth 5 target.py")
        ('5', 'target.py')
        >>> _extract_value_flag("plan", "--plan=path/to/file.md fix issue")
        ('path/to/file.md', 'fix issue')
        >>> _extract_value_flag("plan", "fix issue")
        (None, 'fix issue')
        >>> _extract_value_flag("keep", 'src/x.py --keep "a b"')
        ('a b', 'src/x.py')
    """
    # --flag=VALUE form. Require a full flag-token match so --planets does not
    # satisfy ``--plan``.
    eq_pattern = re.compile(r"(?<!\S)--" + re.escape(flag) + "=" + _VALUE)
    m = eq_pattern.search(args)
    if m:
        return _unquote(m.group(1)), eq_pattern.sub("", args).strip()

    # --flag VALUE form (value = next non-flag token or quoted phrase).
    space_pattern = re.compile(r"(?<!\S)--" + re.escape(flag) + r"\s+(?!--)" + _VALUE)
    m = space_pattern.search(args)
    if m:
        full_match = m.group(0)
        return _unquote(m.group(1)), args.replace(full_match, "", 1).strip()

    return None, args


def _token_pattern(flag: str) -> re.Pattern[str]:
    """Build the whole-token matcher for ``--<flag>``, so ``--team`` never matches ``--teamx``."""
    return re.compile(r"(?<!\S)" + re.escape(f"--{flag}") + r"(?![A-Za-z0-9_-])")


def _extract_bool_flag(spec: FlagSpec, clean: str, present_value: str) -> tuple[str, str]:
    """Resolve a presence-only flag to ``present_value`` when it appears, else its default.

    Shared by BOOL and NEG_BOOL, which differ only in what presence means.
    """
    pattern = _token_pattern(spec.flag)
    if not pattern.search(clean):
        return spec.default, clean
    return present_value, pattern.sub("", clean)


def _extract_codemap_flag(spec: FlagSpec, clean: str) -> tuple[str, str]:
    """Resolve the paired ``--codemap`` / ``--no-codemap`` flags to one mode.

    Double-condition guard: ``--no-codemap`` wins; ``--codemap`` only sets strict
    when ``--no-codemap`` absent.
    """
    no_pattern = re.compile(r"(?<!\S)--no-codemap(?![A-Za-z0-9_-])")
    yes_pattern = re.compile(r"(?<!\S)--codemap(?![A-Za-z0-9_-])")
    if no_pattern.search(clean) is not None:
        return "off", yes_pattern.sub("", no_pattern.sub("", clean))
    if yes_pattern.search(clean) is not None:
        return "strict", yes_pattern.sub("", clean)
    return spec.default, clean


def _extract_int_flag(spec: FlagSpec, clean: str) -> tuple[str, str]:
    """Resolve a value flag that must parse as an integer, exiting 2 when it does not."""
    val, clean = _extract_value_flag(spec.flag, clean)
    if val is None:
        return spec.default, clean
    try:
        int(val)
    except ValueError:
        print(f"dev_parse_args: --{spec.flag} expects integer, got '{val}'", file=sys.stderr)
        sys.exit(2)
    return val, clean


def _extract_str_flag(spec: FlagSpec, clean: str) -> tuple[str, str]:
    """Resolve a value flag taking any string."""
    val, clean = _extract_value_flag(spec.flag, clean)
    return val if val is not None else spec.default, clean


#: Dispatch table from flag kind to the function that extracts that flag's value from the argument string.
_FLAG_HANDLERS: dict[SpecType, Callable[[FlagSpec, str], tuple[str, str]]] = {
    SpecType.BOOL: lambda spec, clean: _extract_bool_flag(spec, clean, "true"),
    SpecType.NEG_BOOL: lambda spec, clean: _extract_bool_flag(spec, clean, "false"),
    SpecType.CODEMAP: _extract_codemap_flag,
    SpecType.INT: _extract_int_flag,
    SpecType.STR: _extract_str_flag,
}


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
        handler = _FLAG_HANDLERS.get(spec.kind)
        if handler is None:
            continue
        result[spec.var], clean = handler(spec, clean)

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
#: ``--keep "<items>"`` for the skills that accept it. Listed first in every skill so its quoted text is cut out of the
#: argument string before any other flag is matched; ``--team`` inside the items must not toggle team mode. The value
#: itself is persisted by ``extract-keep-flag.py``; this spec only keeps it out of the clean args.
_KEEP_ENTRY: tuple[FlagSpec, str | None] = (_spec(SpecType.STR, "keep", "KEEP_ITEMS", ""), None)

#: Flag declarations for each skill, paired with the legacy temp-file name its downstream blocks still read.
SKILL_SPECS: dict[str, list[tuple[FlagSpec, str | None]]] = {
    "feature": [
        _KEEP_ENTRY,
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-challenge-enabled"),
        (_spec(SpecType.BOOL, "challenge", "CHALLENGE_FORCED", "false"), "dev-challenge-forced"),
        (_spec(SpecType.BOOL, "team", "TEAM_MODE", "false"), "dev-team-mode"),
        (_spec(SpecType.BOOL, "worktree", "WORKTREE_ENABLED", "false"), None),
        (_spec(SpecType.NEG_BOOL, "no-batch", "BATCH_ENABLED", "true"), None),
        (_spec(SpecType.BOOL, "accept-no-plan", "ACCEPT_NO_PLAN", "false"), "dev-accept-no-plan"),
        (_spec(SpecType.CODEMAP, "", "CODEMAP_RAW", "auto"), "dev-codemap-raw"),
        (_spec(SpecType.STR, "repo", "REPO_NAME", ""), "dev-upstream"),
    ],
    "fix": [
        _KEEP_ENTRY,
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-challenge-enabled"),
        (_spec(SpecType.BOOL, "challenge", "CHALLENGE_FORCED", "false"), "dev-challenge-forced"),
        (_spec(SpecType.BOOL, "accept-no-plan", "ACCEPT_NO_PLAN", "false"), "dev-accept-no-plan"),
        (_spec(SpecType.BOOL, "team", "TEAM_MODE", "false"), "dev-team-mode"),
        (_spec(SpecType.BOOL, "worktree", "WORKTREE_ENABLED", "false"), None),
        (_spec(SpecType.CODEMAP, "", "CODEMAP_RAW", "auto"), "dev-codemap-raw"),
        (_spec(SpecType.STR, "repo", "REPO_NAME", ""), "dev-upstream"),
    ],
    "debug": [
        _KEEP_ENTRY,
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-challenge-enabled"),
        (_spec(SpecType.BOOL, "challenge", "CHALLENGE_FORCED", "false"), "dev-challenge-forced"),
        (_spec(SpecType.BOOL, "team", "TEAM_MODE", "false"), "dev-team-mode"),
        (_spec(SpecType.BOOL, "worktree", "WORKTREE_ENABLED", "false"), None),
        (_spec(SpecType.CODEMAP, "", "CODEMAP_RAW", "auto"), "dev-codemap-raw"),
        (_spec(SpecType.STR, "ci-run", "CI_RUN_ID", ""), "dev-ci-run-id"),
        (_spec(SpecType.STR, "repo", "REPO_NAME", ""), "dev-upstream"),
    ],
    "refactor": [
        _KEEP_ENTRY,
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-challenge-enabled"),
        (_spec(SpecType.BOOL, "challenge", "CHALLENGE_FORCED", "false"), "dev-challenge-forced"),
        (_spec(SpecType.BOOL, "team", "TEAM_MODE", "false"), "dev-team-mode"),
        (_spec(SpecType.BOOL, "worktree", "WORKTREE_ENABLED", "false"), None),
        (_spec(SpecType.NEG_BOOL, "no-batch", "BATCH_ENABLED", "true"), None),
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
        _KEEP_ENTRY,
        (_spec(SpecType.NEG_BOOL, "no-challenge", "CHALLENGE_ENABLED", "true"), "dev-review-challenge-enabled"),
        (_spec(SpecType.BOOL, "challenge", "CHALLENGE_FORCED", "false"), "dev-review-challenge-forced"),
        (_spec(SpecType.BOOL, "worktree", "WORKTREE_ENABLED", "false"), None),
        (_spec(SpecType.BOOL, "full", "FANOUT_FULL", "false"), "dev-review-fanout-full"),
        (_spec(SpecType.CODEMAP, "", "CODEMAP_RAW", "auto"), "dev-review-codemap-enabled"),
    ],
}

#: A bare GitHub PR or issue number: an optional leading ``#``, then digits only (no extension, no other token).
_PR_NUMBER_RE = re.compile(r"#?([0-9]+)")


def review_pr_number(clean_args: str, cwd: Path | None = None) -> str:
    """Return the PR number a ``/develop:review`` target names, or an empty string for a local target.

    The flag-stripped target counts as a PR number only when it is a single ``#``-optional run of digits and nothing
    exists at that exact path, so a file or directory called ``42`` is still reviewed as a path and ``42.py`` never
    matches. Classifying here, on the parsed arguments, replaces a shell ``[[ =~ ]]`` test on the raw argument text: a
    quote or newline in that text broke the whole bash block, and flags such as ``42 --full`` hid the PR number.

    Args:
        clean_args: The flag-stripped argument string, with ``--keep`` and every registered flag already removed.
        cwd: Directory the target path is resolved against; defaults to the process working directory.

    Returns:
        The digits without the ``#`` prefix, or ``""`` when the target is not a bare PR number.

    Examples:
        >>> review_pr_number("#42"), review_pr_number("42.py"), review_pr_number("42 src"), review_pr_number("")
        ('42', '', '', '')
    """
    token = _unquote(clean_args.strip())
    match = _PR_NUMBER_RE.fullmatch(token)
    if match is None:
        return ""
    if ((cwd if cwd is not None else Path.cwd()) / token).exists():
        return ""
    return match.group(1)


#: Values classified from a skill's flag-stripped arguments, each written to ``dev-<skill>-<key>-<CSID>``. Every run
#: writes them, empty when nothing matches, so a value left by an earlier invocation never leaks into this one.
_DERIVED_SENTINELS: dict[str, dict[str, Callable[[str], str]]] = {
    "review": {"pr-number": review_pr_number},
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


def _value_filename(skill: str, spec: FlagSpec) -> str:
    """Compose the temp filename named by the variable a spec holds, suffixed with the session scope.

    A ``--neg-bool`` file named by its flag (``dev-review-no-challenge``) holds the *enabled* value, so the flag-named
    file reads backwards; this variable-named file (``dev-review-challenge-enabled``) says what it stores. The
    flag-named file stays written as an alias for existing readers.

    Examples:
        >>> _value_filename(
        ...     "review", FlagSpec(kind=SpecType.NEG_BOOL, flag="no-challenge", var="CHALLENGE_ENABLED", default="true")
        ... ).startswith("dev-review-challenge-enabled-")
        True
    """
    return f"dev-{skill}-{spec.var.lower().replace('_', '-')}-{_csid()}"


def _write_sentinel(path: Path, value: str) -> None:
    """Write ``value`` plus a newline to ``path`` atomically; a plain ``write_text`` would follow a planted symlink."""
    write_atomic(path, value)


def _tmp_dir() -> Path:
    """Return the directory temp files are written to (mirrors shell ``${TMPDIR:-/tmp}`` — tmpdir-exempt: base dir only,
    not a filename).

    Per-file session suffixing happens in ``_per_skill_filename``/``write_skill_files``, not here.
    """
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    return Path(os.environ.get("TMPDIR") or tmp_fallback)


def write_skill_files(skill: str, arguments: str, tmp_dir: Path | None = None) -> dict[str, str]:
    """Parse arguments using the registered ``skill`` specs and persist values to temp files.

    Writes two files per variable when a legacy path is registered:

    * Per-skill: ``${TMPDIR}/dev-<skill>-<flag>-<CSID>`` — modern convention used by callers
      that read flags back with explicit per-skill paths.
    * Legacy: ``${TMPDIR}/<legacy-name>-<CSID>`` — preserves backward compatibility with
      downstream Bash() blocks that read shared paths like ``dev-team-mode-<CSID>``.

    Also writes the flag-stripped arguments to ``dev-<skill>-clean-args-<CSID>`` and every
    ``_DERIVED_SENTINELS`` value registered for the skill.

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
    make_private_dirs(target_dir)  # any directory created here is 0700; an existing temp dir keeps its mode

    entries = SKILL_SPECS[skill]
    specs = [spec for spec, _ in entries]
    values, _clean = extract_flags(arguments, specs)

    for spec, legacy in entries:
        value = values[spec.var]
        _write_sentinel(target_dir / _per_skill_filename(skill, spec), value)
        if spec.kind == SpecType.NEG_BOOL:
            _write_sentinel(target_dir / _value_filename(skill, spec), value)
        if legacy is not None:
            _write_sentinel(target_dir / f"{legacy}-{_csid()}", value)
    # Write CLEAN_ARGS (flags stripped) to a per-skill file so callers avoid eval
    _write_sentinel(target_dir / f"dev-{skill}-clean-args-{_csid()}", _clean)
    for key, derive in _DERIVED_SENTINELS.get(skill, {}).items():
        _write_sentinel(target_dir / f"dev-{skill}-{key}-{_csid()}", derive(_clean))
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
