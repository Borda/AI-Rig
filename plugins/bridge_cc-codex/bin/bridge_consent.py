"""Record an operator-approved Claude transfer policy in native Codex configuration.

Purpose: Give Bridge setup a durable, reviewer-visible consent boundary without treating plugin installation as
authorization. Scope: Read or change only the versioned Bridge block in the user's auto_review.extra_policy; preserve
all other configuration. Usage: Run check from a skill; grant or revoke asks for operator-terminal confirmation. ``grant
--approve`` skips the typed question but still needs operator proof: the operator terminal itself, or a one-time token
that ``issue-token`` printed to that terminal and the caller passes as ``BRIDGE_OPERATOR_TOKEN``. With neither it exits
without a policy update. Outputs: Check emits sanitized JSON; mutations show only the selected path and shipped policy,
never user configuration or credentials; ``issue-token`` prints the token to the terminal only. Failure: Invalid TOML,
unsupported placement, unsafe files, changed input, missing operator proof or declined confirmation exits nonzero
without a policy update. Used by: Codex Bridge setup, dispatch skills, package tests, and operators. The terminal check
is an interaction requirement, not proof against another process controlled by the same user. The token file keeps only
a hash and expiry in the Codex home, and a token is consumed by the grant that uses it. Stored policy is not evidence
that an active or managed host loaded it; this helper never changes reviewer selection, sandbox permissions,
authentication or plugin installation, and never invokes a provider.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import time
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
#: Environment variable that carries the operator-minted one-time token into a non-terminal ``grant --approve``.
TOKEN_ENV = "BRIDGE_OPERATOR_TOKEN"  # noqa: S105 - variable name, not a secret
#: File in the Codex home that holds the hash and expiry of the one outstanding token.
TOKEN_FILE = ".bridge-consent-operator-token"  # noqa: S105 - file name, not a secret
#: Seconds an issued token stays valid.
TOKEN_TTL_SECONDS = 300


def _operator_terminal() -> bool:
    """Report whether stdin and stdout are the operator's terminal rather than pipes a model could drive."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _issue_token(home: Path) -> str:
    """Mint a one-time token and keep only its hash and expiry, owner-only, in the Codex home."""
    path = home / TOKEN_FILE
    try:
        _metadata(path)
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(24)
    record = {
        "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "expires_at": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record) + "\n")
    return token


def _verify_token(home: Path, token: str) -> None:
    """Raise ``ValueError`` unless ``token`` matches the unexpired outstanding record."""
    path = home / TOKEN_FILE
    try:
        _metadata(path)
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(
            "operator token not issued or already used; run issue-token in the operator terminal"
        ) from None
    expires_at = record.get("expires_at") if isinstance(record, dict) else None
    digest = record.get("sha256") if isinstance(record, dict) else None
    if type(expires_at) is not int or not isinstance(digest, str):
        raise ValueError("operator token record is malformed; run issue-token again")
    if expires_at < int(time.time()):
        raise ValueError("operator token expired; run issue-token again")
    if not hmac.compare_digest(hashlib.sha256(token.encode("utf-8")).hexdigest(), digest):
        raise ValueError("operator token does not match; run issue-token again")


def _burn_token(home: Path) -> None:
    """Consume the outstanding token; a concurrent grant that already did so makes this one fail."""
    try:
        (home / TOKEN_FILE).unlink()
    except FileNotFoundError:
        raise ValueError("operator token was already used; run issue-token again") from None


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


#: One run of consecutive quote characters of a single kind, matched in place to size TOML string delimiters.
_QUOTE_RUN = re.compile(r"\"+|'+")


def _scan_line(line: str, multiline: str | None, depth: int) -> tuple[str | None, int]:
    """Carry an open multi-line string delimiter and bracket depth across one line of valid TOML.

    Quotes, escapes and comments are tracked only so that brackets inside strings or comments never count. A run of
    three or more quotes inside a multi-line string closes it; TOML allows up to two extra quotes as content there.

    Examples:
        >>> _scan_line('big = [ # "]"\\n', None, 0)
        (None, 1)
        >>> _scan_line('text = \"\"\"open [\\n', None, 0)
        ('\"\"\"', 0)
        >>> _scan_line('still open\"\"\"\"\\n', '\"\"\"', 0)
        (None, 0)
    """
    index = 0
    while index < len(line):
        char = line[index]
        if multiline is not None:
            if multiline == '"""' and char == "\\":
                index += 2
            elif char == multiline[0]:
                run = _QUOTE_RUN.match(line, index).end() - index  # type: ignore[union-attr]
                index += run
                multiline = None if run >= 3 else multiline
            else:
                index += 1
        elif char == "#":
            break
        elif char in "\"'":
            run = _QUOTE_RUN.match(line, index).end() - index  # type: ignore[union-attr]
            if run >= 3:
                multiline, index = char * 3, index + 3
            elif run == 2:
                index += 2
            else:
                index += 1
                while index < len(line) and line[index] != char:
                    index += 2 if char == '"' and line[index] == "\\" else 1
                index += 1
        else:
            depth += (char in "[{") - (char in "]}")
            index += 1
    return multiline, depth


def _statements(text: str) -> list[str]:
    """Split valid TOML text into its top-level statements, each with its exact original bytes.

    The parser is asked only at lines where the scanner sees no open multi-line string or bracket, so each statement is
    parsed about once; re-parsing a growing prefix after every line made a long multi-line array or string quadratic. A
    candidate the parser rejects keeps accumulating as before, and text left over at the end still fails closed.
    """
    chunks: list[str] = []
    pending = ""
    multiline: str | None = None
    depth = 0
    for line in text.splitlines(keepends=True):
        pending += line
        multiline, depth = _scan_line(line, multiline, depth)
        if multiline is not None or depth > 0:
            continue
        try:
            _parse(pending)
        except ValueError:
            continue
        chunks.append(pending)
        pending = ""
    if pending:
        raise ValueError("unsupported TOML statement placement")
    return chunks


def _replace_extra(text: str, policy: str) -> str:
    """Replace a normal table assignment while preserving unrelated TOML bytes and values."""
    before = _parse(text)
    chunks = _statements(text)
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
    """Check consent or apply a grant or revocation with explicit approval."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "grant", "revoke", "issue-token"))
    parser.add_argument(
        "--approve",
        action="store_true",
        help=(
            "approve persistent Claude repository-context transfer and ordinary inference consent without the typed "
            f"question (grant only); needs the operator terminal or a one-time {TOKEN_ENV} from issue-token"
        ),
    )
    args = parser.parse_args(argv)
    if args.approve and args.action != "grant":
        parser.error("--approve is supported only with grant")
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
        if args.action == "issue-token":
            if not _operator_terminal():
                raise ValueError(
                    "issue-token requires the operator terminal; run it yourself, outside model-controlled tools"
                )
            token = _issue_token(home)
            print(f"One-time operator token, valid {TOKEN_TTL_SECONDS // 60} minutes, consumed by one grant:\n{token}")
            print(
                f"Export it as {TOKEN_ENV} for a single `bridge_consent.py grant --approve` run; it grants nothing by itself."
            )
            return 0
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
        terminal = _operator_terminal()
        token = os.environ.get(TOKEN_ENV, "") if args.approve else ""
        if token:
            _verify_token(home, token)
        elif not terminal:
            raise ValueError(
                "grant and revoke require the operator terminal; piped input is not approval. "
                f"Non-interactive grant --approve needs a one-time {TOKEN_ENV} minted by issue-token in that terminal"
            )
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
            "\nThis applies to later authorized tasks in native-confirmed project folders until revoked. It includes ordinary inference quota/cost through your existing Claude configuration; no credential is requested, displayed or changed by this helper. No provider call runs now.\nA fresh Codex session may be needed; project or managed policy can override user defaults. Revoke removes only this block and does not cancel running calls."
        )
        if args.approve:
            print(
                "Explicit approval supplied by --approve"
                + (" with a one-time operator token." if token else " in the operator terminal.")
            )
        else:
            print("Type Approve to apply this exact change, or anything else to leave configuration unchanged:")
            if input() != "Approve":
                print("Consent unchanged.")
                return 2
        if token:
            _burn_token(home)
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
