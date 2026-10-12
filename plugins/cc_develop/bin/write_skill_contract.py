#!/usr/bin/env python
"""write_skill_contract.py — write the compaction-boundary contract the PreCompact hook appends verbatim.

Only the skeleton is shared. `preserve` and `next` stay bespoke per boundary by design
(compaction.md §Verbatim-file principle) — this script parameterises the mkdir + write
around them, never the prose itself.

Argparse is deliberately not used: `preserve` and `next` routinely begin with a dash or
carry flag-like text, which argparse would read as options. `-h`/`--help` is handled
explicitly instead, so the bin/ help contract still holds.

Usage: python write_skill_contract.py <skill> <phase> <run-dir> <preserve> <next>
       python write_skill_contract.py <skill> <phase> <run-dir> <preserve> <next> <label> <items>
       Either form may end with -- calls_ahead=<N> drop=<text> interactive=false
  Metadata keys are optional, unique, and accepted only after a literal `--`; N is a non-negative integer.
  Callers keep their own sentinel reads: the `${_OUT}`-style values must expand in the
  caller's shell, while `<REFINE_ITER>`-style placeholders are literal fill-in tokens the
  orchestrator substitutes as prose. Both survive as-is through a double-quoted argument.
  The 7-argument form appends one labelled list — `<items>` is a newline-separated blob,
  each line indented under `- <label>:`. It carries the ledgers a `next:` string refers to
  ("skip any candidate marked refuted/ruled-out above"); without it, a post-compaction
  resume is told to consult a list that was never written, and re-probes what it ruled out.
  An empty `<items>` omits the block, so a caller need not branch.
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
    "usage: write_skill_contract.py <skill> <phase> <run-dir> <preserve> <next>\n"
    "       write_skill_contract.py <skill> <phase> <run-dir> <preserve> <next> <label> <items>\n"
    "       Either form: [-- calls_ahead=<non-negative int> drop=<text> interactive=false]"
)


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
