"""Record an operator-approved Claude transfer policy in native Codex configuration.

Purpose: Give Bridge setup a durable, reviewer-visible consent boundary without treating plugin installation as
authorization. Scope: Read or change only the versioned Bridge block in the user's auto_review.extra_policy; preserve
all other configuration. Usage: Run check from a skill; the operator runs grant or revoke in their own terminal after
reading the displayed scope. There is no noninteractive approval flag. Outputs: Check emits sanitized JSON; mutations
show only the selected path and shipped policy, never user configuration or credentials. Failure: Invalid TOML,
unsupported placement, unsafe files, changed input, missing terminal or declined confirmation exits nonzero without
an intended policy update. Used by: Codex Bridge setup and executable skills, install-shaped tests, and operators.
The terminal check is an interaction requirement, not proof against another process controlled by the same user.
Stored policy is not evidence that an active or managed host loaded it; this helper never changes reviewer selection,
sandbox permissions, authentication or plugin installation, and never invokes a provider.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None


#: Exact policy bytes shared with installation-independent setup and root policy-sync preservation.
POLICY_PATH = Path(__file__).resolve().parents[1] / "rules" / "claude-transfer-policy.txt"
#: Marker family used to detect unsupported, edited or duplicate grants rather than silently replacing them.
MARKER = "<!-- bridge:claude-transfer-consent:"
#: Maximum native configuration size accepted without retaining raw contents in diagnostics.
MAX_CONFIG_BYTES = 2 * 1024 * 1024


def _parse(text: str) -> dict[str, object]:
    """Validate native TOML without echoing potentially sensitive parser input."""
    if tomllib is None:
        raise ValueError("TOML validation needs Python 3.11+ or tomli installed for Python 3.10")
    try:
        return tomllib.loads(text)
    except ValueError:
        raise ValueError("invalid native configuration TOML") from None


def _extra(config: dict[str, object]) -> str:
    """Return the additive policy only when its native shape is supported."""
    review = config.get("auto_review", {})
    if not isinstance(review, dict) or not isinstance(review.get("extra_policy", ""), str):
        raise ValueError("auto_review.extra_policy must be a string")
    return review.get("extra_policy", "")


def _present(extra: str, block: str) -> bool:
    """Reject edited or unknown consent before recognizing the exact shipped block."""
    if MARKER not in extra:
        return False
    if extra.count(MARKER) != 2 or extra.count(block) != 1:
        raise ValueError("consent block is edited, duplicated or unsupported; inspect it manually")
    return True


def _replace_extra(text: str, policy: str) -> str:
    """Replace a normal table assignment while preserving unrelated TOML bytes and values."""
    before = _parse(text)
    chunks: list[str] = []
    pending = ""
    for line in text.splitlines(keepends=True):
        pending += line
        try:
            _parse(pending)
        except ValueError:
            continue
        chunks.append(pending)
        pending = ""
    if pending:
        raise ValueError("unsupported TOML statement placement")
    in_review = False
    table_index = None
    assignment_index = None
    insertion_index = len(chunks)
    for index, chunk in enumerate(chunks):
        first = chunk.splitlines()[0]
        if first.lstrip().startswith("["):
            if in_review:
                insertion_index = index
            in_review = (
                re.fullmatch(r'\s*\[\s*(?:auto_review|"auto_review"|\'auto_review\')\s*\]\s*(?:#.*)?', first)
                is not None
            )
            if in_review:
                table_index = index
        elif in_review and re.match(r'\s*(?:extra_policy|"extra_policy"|\'extra_policy\')\s*=', first):
            assignment_index = index
    literal = json.dumps(policy, ensure_ascii=False).replace("\x7f", "\\u007f")
    if assignment_index is not None:
        match = re.fullmatch(
            r'(?P<prefix>\s*(?:extra_policy|"extra_policy"|\'extra_policy\')\s*=\s*)(?P<value>.*)',
            chunks[assignment_index],
            flags=re.DOTALL,
        )
        if match is None:
            raise ValueError("unsupported policy assignment")
        raw_value = match["value"]
        # Let TOML identify the string end so quoted keys, comments and line endings survive.
        for delimiter in re.finditer(r"[\"']", raw_value):
            end = delimiter.end()
            if raw_value.startswith(('"""', "'''")) and end < 6:
                continue
            suffix = raw_value[end:]
            if re.fullmatch(r"[ \t]*(?:#[^\r\n]*)?(?:\r?\n)?", suffix) is None:
                continue
            try:
                value = _parse("value = " + raw_value[:end]).get("value")
            except ValueError:
                continue
            if isinstance(value, str):
                chunks[assignment_index] = match["prefix"] + literal + suffix
                break
        else:
            raise ValueError("extra_policy must be a string")
    elif "auto_review" in before and table_index is None:
        raise ValueError("use a normal [auto_review] table before configuring consent")
    elif table_index is None:
        chunks.append(("\n" if text and not text.endswith("\n") else "") + f"[auto_review]\nextra_policy = {literal}\n")
    else:
        if insertion_index and not chunks[insertion_index - 1].endswith("\n"):
            chunks[insertion_index - 1] += "\n"
        chunks.insert(insertion_index, f"extra_policy = {literal}\n")
    result = "".join(chunks)
    expected = {**before, "auto_review": {**before.get("auto_review", {}), "extra_policy": policy}}
    if _parse(result) != expected:
        raise ValueError("consent update would change unrelated settings")
    return result


def _metadata(path: Path, *, directory: bool = False) -> os.stat_result:
    """Refuse linked, shared or unexpected filesystem objects at the config boundary."""
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if path.is_symlink() or reparse or not expected_type or (not directory and info.st_nlink != 1):
        raise ValueError("native configuration must use ordinary unlinked files and directories")
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("native configuration must be user-owned and not writable by other users")
    return info


def _read(path: Path) -> tuple[bytes, os.stat_result | None]:
    """Read bounded configuration, checking that its opened identity matches the target."""
    try:
        expected = _metadata(path)
    except FileNotFoundError:
        return b"", None
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino) or info.st_size > MAX_CONFIG_BYTES:
            raise ValueError("native configuration changed or exceeds the size limit")
        data = stream.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("native configuration exceeds the size limit")
    return data, info


def _write(path: Path, content: str, original: bytes, identity: os.stat_result | None, home: os.stat_result) -> None:
    """Stage one replacement and recheck the approved input immediately before publishing it."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".bridge-consent-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        current_home = _metadata(path.parent, directory=True)
        observed, current = _read(path)
        original_identity = (identity.st_dev, identity.st_ino, identity.st_mtime_ns) if identity else None
        current_identity = (current.st_dev, current.st_ino, current.st_mtime_ns) if current else None
        if (
            observed != original
            or current_identity != original_identity
            or (current_home.st_dev, current_home.st_ino) != (home.st_dev, home.st_ino)
        ):
            raise ValueError("native configuration changed after the displayed plan; inspect again")
        if os.name != "nt":
            os.chmod(temporary, stat.S_IMODE(identity.st_mode) if identity is not None else 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    """Check consent or let the operator approve one displayed grant or revocation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "grant", "revoke"))
    args = parser.parse_args(argv)
    try:
        value = os.environ.get("CODEX_HOME")
        home = Path(value) if value else Path.home() / ".codex"
        if not home.is_absolute():
            raise ValueError("CODEX_HOME must be absolute")
        if not home.exists():
            if args.action != "check":
                raise ValueError("initialize Codex home before configuring consent")
            print(json.dumps({"consent": "absent", "configured_reviewer": None, "session_effective": "unverified"}))
            return 0
        home_identity = _metadata(home, directory=True)
        path = home / "config.toml"
        original, identity = _read(path)
        text = original.decode("utf-8")
        config = _parse(text)
        extra = _extra(config)
        block = POLICY_PATH.read_text(encoding="utf-8").strip()
        present = _present(extra, block)
        if args.action == "check":
            reviewer = config.get("approvals_reviewer")
            print(
                json.dumps(
                    {
                        "consent": "configured" if present else "absent",
                        "configured_reviewer": reviewer
                        if reviewer in ("auto_review", "user")
                        else "unspecified-or-unsupported",
                        "session_effective": "unverified",
                    }
                )
            )
            return 0
        if present == (args.action == "grant"):
            print("Consent already configured." if present else "Consent already absent.")
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError("grant and revoke require the operator terminal; no piped or agent-supplied approval")
        # Only this exact marked text is added or removed; surrounding user policy survives.
        if args.action == "grant":
            updated = extra + ("\n\n" if extra else "") + block
        elif extra.endswith("\n\n" + block):
            updated = extra.removesuffix("\n\n" + block)
        else:
            updated = extra.replace(block, "", 1)
        replacement = _replace_extra(text, updated)
        print(
            f"Bridge consent {args.action}: {path}\nChanges only auto_review.extra_policy; preserves other settings.\n"
        )
        print(block)
        print(
            "\nThis applies to later authorized tasks in native-confirmed project folders until revoked. It includes ordinary inference quota/cost through your existing Claude configuration; no credential is requested, displayed or changed by this helper. No provider call runs now.\nA fresh Codex session may be needed; project or managed policy can override user defaults. Revoke removes only this block and does not cancel running calls.\nType Approve to apply this exact change, or anything else to leave configuration unchanged:"
        )
        if input() != "Approve":
            print("Consent unchanged.")
            return 2
        _write(path, replacement, original, identity, home_identity)
        print(
            "Consent configured; active-session effectiveness unverified."
            if args.action == "grant"
            else "Consent revoked; start a fresh session to reload host policy."
        )
        return 0
    except (OSError, UnicodeError, EOFError):
        # Do not echo parser or operating-system details that may contain configuration contents.
        print(
            "bridge-consent: check configuration, supported TOML, ordinary user-owned paths and operator terminal; no consent confirmed",
            file=sys.stderr,
        )
        return 2
    except ValueError as error:
        print(f"bridge-consent: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
