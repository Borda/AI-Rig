#!/usr/bin/env python
"""write_skill_contract.py — write the compaction-boundary contract the PreCompact hook appends verbatim.

Only the skeleton is shared. `preserve` and `next` stay bespoke per boundary by design
(compaction.md §Verbatim-file principle) — this script parameterises the mkdir + write
around them, never the prose itself.

Argparse is deliberately not used: `preserve` and `next` routinely begin with a dash or
carry flag-like text, which argparse would read as options. `-h`/`--help` is handled
explicitly instead, so the bin/ help contract still holds.

Usage: python write_skill_contract.py [KEEP] <skill> <phase> <run-dir> <preserve> <next>
       python write_skill_contract.py [KEEP] <skill> <phase> <run-dir> <preserve> <next> <label> <items>
       KEEP is `--keep-sentinel <name>` or `--keep-file <path>`, never both.
       Either form may end with -- calls_ahead=<N> drop=<text> interactive=false
  Metadata keys are optional, unique, and accepted only after a literal `--`; N is a non-negative integer.
  KEEP is accepted only as the leading option pair, so a `preserve` or `next` value that happens to look
  like a flag is never taken for one. Either option reads the first line of one keep file and, when that
  line is non-empty, appends `; user-keep: <line>` to `preserve` — the same text callers used to assemble in bash with
  `IFS= read -r _KEEP < …; [ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"`. A missing
  or unreadable file appends nothing, matching the `|| _KEEP=""` default of that idiom.
  `--keep-sentinel <name>` names the flat session sentinel `${TMPDIR}/<name>-${CSID}` (TMPDIR falls back
  to the platform temp dir, CSID to CLAUDE_CODE_SESSION_ID, then "shared"); `<name>` is a bare file-name
  stem (letters, digits, `.`, `_`, `-`), never a path.
  `--keep-file <path>` names the file itself, for keep items that live inside a per-session state
  directory (`${TMPDIR}/<skill>-state-${CSID}/keep-items`) a flat name cannot address; the caller's
  shell expands the path, a relative one resolves against the working directory.
  Callers keep their own sentinel reads: the `${_OUT}`-style values must expand in the
  caller's shell, while `<REFINE_ITER>`-style placeholders are literal fill-in tokens the
  orchestrator substitutes as prose. Both survive as-is through a double-quoted argument.
  The 7-argument form appends one labelled list — `<items>` is a newline-separated blob,
  each line indented under `- <label>:`. It carries the ledgers a `next:` string refers to
  ("skip any candidate marked refuted/ruled-out above"); without it, a post-compaction
  resume is told to consult a list that was never written, and re-probes what it ruled out.
  An empty `<items>` omits the block, so a caller need not branch.
  Every other positional value must be a single line: a line break inside `preserve` or `next` could
  forge a top-level `- next:` / `- run-dir:` line in the owner-stamped contract, so it exits 2.
Exit codes: 0 = written · 1 = contract file unwritable · 2 = invalid arguments or metadata
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

#: CWD-relative, exactly as the shell original: the contract belongs to the project the
#: skill is running in, which is the caller's working directory, not this script's location.
_CONTRACT = Path(".temp/state/skill-contract.md")
_OWNER = _CONTRACT.with_suffix(".owner")

#: Argument synopsis printed for -h or --help: the five-argument form and the seven-argument form with label and items.
_USAGE = (
    "usage: write_skill_contract.py [--keep-sentinel <name> | --keep-file <path>] <skill> <phase> <run-dir> <preserve> <next>\n"
    "       write_skill_contract.py [--keep-sentinel <name> | --keep-file <path>] <skill> <phase> <run-dir> <preserve> <next> <label> <items>\n"
    "       Either form: [-- calls_ahead=<non-negative int> drop=<text> interactive=false]"
)

#: Leading option naming the flat session sentinel that holds the user's `--keep` items.
_KEEP_SENTINEL_FLAG = "--keep-sentinel"

#: Leading option naming the file that holds the user's `--keep` items, wherever it lives.
_KEEP_FILE_FLAG = "--keep-file"
_KEEP_FLAGS = (_KEEP_SENTINEL_FLAG, _KEEP_FILE_FLAG)

#: A sentinel name is a bare file-name stem; anything path-like could read outside the temp dir.
_SENTINEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _sentinel_path(name: str) -> Path:
    """Return the session-scoped sentinel file ``${TMPDIR}/<name>-${CSID}``.

    Callers export CSID before invoking this script, so the child process sees the same token the shell used to
    write the sentinel.

    Examples:
        >>> _sentinel_path("fix-keep-items").name.startswith("fix-keep-items-")
        True
    """
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    tmp_root = os.environ.get("TMPDIR") or tmp_fallback
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    return Path(tmp_root) / f"{name}-{csid}"


def _split_keep_option(args: list[str]) -> tuple[list[str], Path | None]:
    """Take a leading ``--keep-sentinel <name>`` or ``--keep-file <path>`` pair off the argument list.

    Only the very first position is inspected: ``<skill>`` never starts with a dash, while
    ``preserve`` and ``next`` legitimately may, so scanning further would misread them. For the same
    reason a second keep option right after the first is an error, never a skill name.

    Examples:
        >>> rest, keep_path = _split_keep_option(["--keep-file", "state/keep-items", "develop:fix"])
        >>> rest, keep_path.name
        (['develop:fix'], 'keep-items')
        >>> _split_keep_option(["develop:fix", "--keep-file", "x"])
        (['develop:fix', '--keep-file', 'x'], None)
    """
    if not args or args[0] not in _KEEP_FLAGS:
        return args, None
    flag = args[0]
    if len(args) < 2:
        raise ValueError(f"{flag} requires a value")
    if flag == _KEEP_SENTINEL_FLAG:
        if _SENTINEL_NAME.fullmatch(args[1]) is None:
            raise ValueError(f"{flag} requires a bare sentinel name (letters, digits, '.', '_', '-')")
        keep_path = _sentinel_path(args[1])
    else:
        if not args[1]:
            raise ValueError(f"{flag} requires a non-empty path")
        keep_path = Path(args[1])
    rest = args[2:]
    if rest and rest[0] in _KEEP_FLAGS:
        raise ValueError(f"{_KEEP_SENTINEL_FLAG} and {_KEEP_FILE_FLAG} are alternatives; pass one")
    return rest, keep_path


def _read_keep_items(path: Path) -> str:
    """Return the first line of the keep file, or an empty string when it is absent.

    Mirrors the shell read-back ``IFS= read -r _KEEP < "<file>" || _KEEP=""``: the first newline-terminated
    line, surrounding whitespace kept. A missing, unreadable or newline-less file is no keep items, as there
    (``read`` exits non-zero at end of file without a newline and the idiom's default empties the value);
    ``extract-keep-flag.py`` always writes the trailing newline. Two deliberate differences, both for the
    contract rather than for fidelity:

    - One trailing CR is dropped, so a file written with CRLF line endings on Windows does not carry a stray
      carriage return into the contract.
    - The line is cut at its first ``str.splitlines()`` separator (bare CR, VT, FF, FS, GS, RS, NEL, U+2028,
      U+2029), where ``read -r`` would keep them. ``_multiline_field`` treats each of those as a line break, and
      this value joins ``preserve`` after that check, so an uncut separator could forge a top-level ``- next:``
      line in the owner-stamped contract.
    """
    try:
        # bytes, not read_text: text mode translates a bare CR to a newline and would hide the no-newline case
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""
    line, newline, _ = text.partition("\n")
    if not newline:
        return ""
    lines = line.removesuffix("\r").splitlines()
    return lines[0] if lines else ""


def _multiline_field(fields: dict[str, str]) -> str | None:
    """Name the first field holding a line break, or ``None`` when every field is a single line.

    Any ``str.splitlines()`` separator counts (incl. U+2028, U+0085), as for metadata values: a break inside
    ``preserve`` or ``next`` could forge a top-level ``- next:`` or ``- run-dir:`` line in the owner-stamped,
    authoritative contract.

    Examples:
        >>> _multiline_field({"next": "go\\n- run-dir: /elsewhere"})
        'next'
        >>> _multiline_field({"next": "go", "phase": ""}) is None
        True
    """
    return next((name for name, value in fields.items() if "".join(value.splitlines()) != value), None)


def _parse_metadata(args: list[str]) -> tuple[list[str], str]:
    """Separate positional arguments from a strictly validated optional metadata tail.

    Examples:
        >>> _parse_metadata(["skill", "--", "calls_ahead=0", "drop=notes"])
        (['skill'], '- gate-metadata:\\n    - calls_ahead: 0\\n    - drop: notes\\n')
    """
    if "--" not in args:
        return args, ""
    boundary = args.index("--")
    tail = args[boundary + 1 :]
    if not tail:
        raise ValueError("-- requires metadata")
    metadata: dict[str, str] = {}
    for arg in tail:
        key, separator, value = arg.partition("=")
        if not separator or key not in ("calls_ahead", "drop", "interactive") or key in metadata:
            raise ValueError("unknown or duplicate metadata key")
        if "".join(value.splitlines()) != value:
            # Any str.splitlines() separator (incl. U+2028, U+0085) could forge another metadata line for the gate.
            raise ValueError("metadata values must be single-line")
        if key == "calls_ahead":
            # Six digits bound every real phase estimate and keep the gate's arithmetic and rendering finite.
            if re.fullmatch(r"[0-9]{1,6}", value) is None:
                raise ValueError("calls_ahead must be a non-negative integer of at most six digits")
            value = str(int(value))
        if key == "interactive" and value != "false":
            raise ValueError("interactive must be false")
        metadata[key] = value
    return args[:boundary], "- gate-metadata:\n" + "".join(f"    - {key}: {value}\n" for key, value in metadata.items())


def _atomic_write(path: Path, raw: bytes) -> None:
    """Replace one artifact with complete bytes and remove temporary files on failure."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _write_contract(contract: str) -> int:
    """Atomically write LF bytes, then stamp the intended bytes for this session."""
    try:
        _CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        raw = contract.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        _atomic_write(_CONTRACT, raw)
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if session_id:
            owner = {
                "session_id": session_id,
                "sha256": digest,
                "written_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_write(_OWNER, (json.dumps(owner) + "\n").encode("utf-8"))
    except (OSError, UnicodeError) as exc:
        # A failed stamp must not leave valid-looking ownership on a failed boundary write.
        with suppress(OSError):
            _OWNER.unlink(missing_ok=True)
        print(f"write_skill_contract: cannot write {_CONTRACT}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Invalidate prior ownership on every invocation, then validate arguments and write the contract."""
    try:
        _OWNER.unlink(missing_ok=True)
    except OSError as exc:
        print(f"write_skill_contract: cannot write {_CONTRACT}: {exc}", file=sys.stderr)
        return 1
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        print(_USAGE)
        print()
        print("Write .temp/state/skill-contract.md for the current compaction boundary.")
        sys.exit(0)

    try:
        args, keep_path = _split_keep_option(args)
        args, metadata = _parse_metadata(args)
        if any(line.strip() == "- gate-metadata:" for arg in args for line in arg.splitlines()):
            raise ValueError("gate-metadata is a reserved line")
    except ValueError as exc:
        print(f"write_skill_contract: {exc}\n{_USAGE}", file=sys.stderr)
        return 2

    if len(args) not in (5, 7):
        print(
            f"write_skill_contract: expected 5 args (skill phase run-dir preserve next) "
            f"or 7 (… label items), got {len(args)}",
            file=sys.stderr,
        )
        return 2

    skill, phase, run_dir, preserve, next_step = args[:5]
    label, items = args[5:] if len(args) == 7 else ("", "")
    if not skill:
        print("write_skill_contract: <skill> must not be empty", file=sys.stderr)
        return 2
    # <items> is the one multi-line argument; each of its lines is rendered indented under `- <label>:`.
    offender = _multiline_field(
        {"skill": skill, "phase": phase, "run-dir": run_dir, "preserve": preserve, "next": next_step, "label": label}
    )
    if offender:
        print(f"write_skill_contract: <{offender}> must be a single line", file=sys.stderr)
        return 2
    keep_items = _read_keep_items(keep_path) if keep_path else ""
    if keep_items:
        preserve = f"{preserve}; user-keep: {keep_items}"

    contract = (
        "## Active Skill Contract\n"
        f"- skill: {skill} · phase: {phase}\n"
        f"- run-dir: {run_dir}\n"
        f"- preserve: {preserve}\n"
        f"- next: {next_step}\n"
    )
    listed = [line for line in items.splitlines() if line.strip()]
    if listed:
        contract += f"- {label}:\n" + "".join(f"    - {line.strip()}\n" for line in listed)
    if any(line.strip() == "- gate-metadata:" for line in contract.splitlines()):
        print("write_skill_contract: gate-metadata is a reserved line", file=sys.stderr)
        return 2
    return _write_contract(contract + metadata)


if __name__ == "__main__":
    sys.exit(main())
