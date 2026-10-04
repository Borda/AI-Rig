#!/usr/bin/env python3
"""Synchronize the repository's normal-session defaults into Codex home.

Updates ``model``, ``review_model``, and optional ``approvals_reviewer`` and ``auto_review.extra_policy`` settings in
``CODEX_HOME/config.toml``. The main automatic-review policy remains user-owned.

Maintains one authenticated personal-policy block in ``CODEX_HOME/AGENTS.md``. All unrelated configuration and
instructions remain user-owned.
"""

from __future__ import annotations

import argparse
import hashlib
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


BEGIN_PREFIX = "<!-- borda-local:session-model-policy begin sha256="
END_MARKER = "<!-- borda-local:session-model-policy end -->\n"
BEGIN_PATTERN = re.compile(r"<!-- borda-local:session-model-policy begin sha256=([0-9a-f]{64}) -->\n")
MODEL_PATTERN = re.compile(
    r"^(?P<indent>\s*)(?P<spelling>model|review_model|approvals_reviewer|"
    '"model"|"review_model"|"approvals_reviewer"|'
    "'model'|'review_model'|'approvals_reviewer')"
    r"(?P<separator>\s*=\s*)(?P<value>"
    r'"(?:[^"\\]|\\.)*"'
    r"|'[^']*')(?P<suffix>\s*(?:#.*)?)$"
)
MODEL_ASSIGNMENT_PATTERN = re.compile(
    r"^\s*(?:model|review_model|approvals_reviewer|"
    '"model"|"review_model"|"approvals_reviewer"|'
    "'model'|'review_model'|'approvals_reviewer')"
    r"\s*="
)
TABLE_PATTERN = re.compile(r"^\s*\[")
MANAGED_KEYS = frozenset({"model", "review_model"})
AUTO_REVIEW_TABLE = re.compile(r"^\s*\[\s*(?:auto_review|\"auto_review\"|'auto_review')\s*\]\s*(?:#.*)?$")
EXTRA_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:extra_policy|\"extra_policy\"|'extra_policy')\s*=\s*)(?P<value>.*)$", re.DOTALL
)


class SyncError(ValueError):
    """Report a configuration state unsafe to update automatically."""


def _sha256(payload: str) -> str:
    """Return the SHA-256 digest for UTF-8 text."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_text(path: Path, label: str) -> str:
    """Read one ordinary UTF-8 file with a bounded ownership check."""
    if path.is_symlink() or not path.is_file():
        raise SyncError(f"{label} must be an ordinary file: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise SyncError(f"{label} must be UTF-8: {path}") from error


def _parse_config(text: str) -> dict[str, object]:
    """Validate TOML before editing, including duplicate keys and tables."""
    if tomllib is None:
        raise SyncError("TOML validation requires tomli on Python 3.10")
    try:
        return tomllib.loads(text)
    except ValueError as error:
        raise SyncError(f"invalid config TOML: {error}") from error


def _logical_lines(text: str) -> list[str]:
    """Group complete TOML statements so nested string content cannot become config syntax."""
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    index = 0
    while index < len(lines):
        chunk = lines[index]
        index += 1
        while True:
            try:
                _parse_config(chunk)
                break
            except SyncError:
                if index == len(lines):
                    raise
                chunk += lines[index]
                index += 1
        result.append(chunk)
    return result


def _source_models(path: Path) -> dict[str, str]:
    """Extract required model defaults and the optional root approval reviewer."""
    models: dict[str, str] = {}
    in_table = False
    source = _read_text(path, "source config")
    parsed = _parse_config(source)
    for chunk in _logical_lines(source):
        line = chunk.rstrip("\r\n")
        if TABLE_PATTERN.match(line):
            in_table = True
        if in_table:
            continue
        match = MODEL_PATTERN.fullmatch(line)
        if match is None:
            if MODEL_ASSIGNMENT_PATTERN.match(line):
                raise SyncError("source config has an unsupported model string assignment")
            continue
        key = match.group("spelling").strip("\"'")
        value = match.group("value")
        if key in models:
            raise SyncError(f"source config has duplicate {key} assignments")
        models[key] = value
    if not MANAGED_KEYS <= models.keys():
        raise SyncError("source config must define exactly one model and review_model assignment")
    if "approvals_reviewer" in parsed and not isinstance(parsed["approvals_reviewer"], str):
        raise SyncError("source approvals_reviewer must be a string")
    return models


def _replace_models(existing: str, models: dict[str, str]) -> str:
    """Replace managed root settings while preserving table settings and comments.

    Values in ``models`` must already be quoted TOML string literals. Insert
    missing managed keys before the first table, or at the end of a table-free
    document. Raise ``SyncError`` for duplicate or unsupported root assignments.
    Return new text without reading or writing any files.

    Examples:
        >>> models = {"model": '"primary"', "review_model": '"reviewer"'}
        >>> print(_replace_models("[profile]\\nmodel = 'custom'\\n", models))
        model = "primary"
        review_model = "reviewer"
        [profile]
        model = 'custom'
        <BLANKLINE>
    """
    expected = _parse_config(existing)
    expected.update(_parse_config("\n".join(f"{key} = {value}" for key, value in models.items())))
    seen: set[str] = set()
    lines: list[str] = []
    insertion_index: int | None = None
    in_table = False
    for line in _logical_lines(existing):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        if TABLE_PATTERN.match(body):
            in_table = True
            if insertion_index is None:
                insertion_index = len(lines)
        if in_table:
            lines.append(line)
            continue
        match = MODEL_PATTERN.fullmatch(body)
        if match is None:
            if MODEL_ASSIGNMENT_PATTERN.match(body):
                raise SyncError("target config has an unsupported model string assignment")
            lines.append(line)
            continue
        key = match.group("spelling").strip("\"'")
        if key not in models:
            lines.append(line)
            continue
        if key in seen:
            raise SyncError(f"target config has duplicate {key} assignments")
        seen.add(key)
        line_ending = ending or "\n"
        lines.append(
            f"{match.group('indent')}{match.group('spelling')}{match.group('separator')}"
            f"{models[key]}{match.group('suffix')}{line_ending}"
        )
    missing = [f"{key} = {models[key]}\n" for key in sorted(models.keys() - seen)]
    if missing:
        index = insertion_index if insertion_index is not None else len(lines)
        if index and not lines[index - 1].endswith("\n"):
            lines[index - 1] += "\n"
        lines[index:index] = missing
    result = "".join(lines)
    if _parse_config(result) != expected:
        raise SyncError("root defaults update changed unrelated config settings")
    return result


def _replace_extra_policy(existing: str, policy: str) -> str:
    """Replace only the explicit automatic-review extra policy, preserving primary policy bytes."""
    parsed = _parse_config(existing)
    current = parsed.get("auto_review", {})
    if not isinstance(current, dict):
        raise SyncError("target auto_review must be a table")
    lines = _logical_lines(existing)
    table_index: int | None = None
    insertion_index = len(lines)
    assignment_index: int | None = None
    in_auto_review = False
    for index, chunk in enumerate(lines):
        first = chunk.splitlines()[0]
        if TABLE_PATTERN.match(first):
            if in_auto_review:
                insertion_index = index
            in_auto_review = AUTO_REVIEW_TABLE.fullmatch(first) is not None
            if in_auto_review:
                table_index = index
            continue
        if in_auto_review and EXTRA_ASSIGNMENT.match(chunk):
            assignment_index = index
    literal = json.dumps(policy, ensure_ascii=False).replace("\x7f", "\\u007f")
    if assignment_index is not None:
        chunk = lines[assignment_index]
        match = EXTRA_ASSIGNMENT.fullmatch(chunk)
        assert match is not None
        raw_value = match["value"]
        suffix = ""
        # The TOML parser identifies the shortest complete string literal; the
        # remaining comment and whitespace belong to the user's original line.
        for delimiter in re.finditer(r"[\"']", raw_value):
            end = delimiter.end()
            if raw_value.startswith(('"""', "'''")) and end < 6:
                continue
            if re.fullmatch(r"[ \t]*(?:#[^\r\n]*)?(?:\r?\n)?", raw_value[end:]) is None:
                continue
            try:
                value = _parse_config("value = " + raw_value[:end]).get("value")
            except SyncError:
                continue
            if isinstance(value, str):
                suffix = raw_value[end:]
                break
        else:
            raise SyncError("target extra_policy must be a string")
        lines[assignment_index] = match["prefix"] + literal + suffix
    elif "extra_policy" in current or (current and table_index is None):
        raise SyncError("target auto_review uses an unsupported inline or dotted table assignment")
    elif table_index is None:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        lines.append(f"{separator}[auto_review]\nextra_policy = {literal}\n")
    else:
        if insertion_index and not lines[insertion_index - 1].endswith("\n"):
            lines[insertion_index - 1] += "\n"
        lines.insert(insertion_index, f"extra_policy = {literal}\n")
    result = "".join(lines)
    after = _parse_config(result)
    expected = dict(parsed)
    expected["auto_review"] = {**current, "extra_policy": policy}
    if after != expected:
        raise SyncError("extra policy update changed unrelated config settings")
    return result


def _policy_block(policy: str) -> str:
    """Wrap policy text in checksum-bearing ownership markers.

    Ensure a trailing newline before computing the UTF-8 digest. Reject nested
    ownership markers with ``SyncError``; the checksum detects changed content
    but does not authenticate its author.

    Examples:
        >>> _policy_block("Example policy") == _policy_block("Example policy\\n")
        True
        >>> _policy_block("Example policy").endswith(END_MARKER)
        True
    """
    body = policy if policy.endswith("\n") else f"{policy}\n"
    if BEGIN_PREFIX in body or END_MARKER.rstrip("\n") in body:
        raise SyncError("source policy must not contain ownership markers")
    return f"{BEGIN_PREFIX}{_sha256(body)} -->\n{body}{END_MARKER}"


def _replace_policy(existing: str, policy: str, block: str) -> str:
    """Merge one verified personal policy block without touching other instructions."""
    begins = existing.count(BEGIN_PREFIX)
    ends = existing.count(END_MARKER.rstrip("\n"))
    if begins == 0 and ends == 0:
        if existing.endswith(policy):
            return f"{existing[: -len(policy)]}{block}"
        separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        return f"{existing}{separator}{block}"
    if begins != 1 or ends != 1:
        raise SyncError("personal policy markers are malformed or duplicated")
    match = BEGIN_PATTERN.search(existing)
    if match is None:
        raise SyncError("personal policy marker is malformed")
    end = existing.find(END_MARKER, match.end())
    if end < 0:
        raise SyncError("personal policy end marker is missing")
    body = existing[match.end() : end]
    if _sha256(body) != match.group(1):
        raise SyncError("personal policy block was modified; refusing overwrite")
    return f"{existing[: match.start()]}{block}{existing[end + len(END_MARKER) :]}"


def _atomic_write(path: Path, content: str, existing: str | None) -> None:
    """Atomically replace a user file after checking it did not change."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.borda-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
        if existing is None:
            if path.exists():
                raise SyncError(f"target appeared during update: {path}")
        elif path.is_symlink() or not path.is_file() or _read_text(path, "target") != existing:
            raise SyncError(f"target changed during update: {path}")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def sync(source_config: Path, source_policy: Path, codex_home: Path, *, install_policy: bool = True) -> None:
    """Apply model and optional approval defaults while preserving unrelated settings."""
    models = _source_models(source_config)
    source = _parse_config(_read_text(source_config, "source config"))
    automatic_review = source.get("auto_review", {})
    if not isinstance(automatic_review, dict):
        raise SyncError("source auto_review must be a table")
    extra_policy = automatic_review.get("extra_policy")
    if "extra_policy" in automatic_review and not isinstance(extra_policy, str):
        raise SyncError("source auto_review.extra_policy must be a string")
    codex_home.mkdir(parents=True, exist_ok=True)

    config = codex_home / "config.toml"
    existing_config = _read_text(config, "target config") if config.exists() else ""
    _parse_config(existing_config)
    desired_config = _replace_models(existing_config, models)
    if extra_policy is not None:
        desired_config = _replace_extra_policy(desired_config, extra_policy)

    if not install_policy:
        _atomic_write(config, desired_config, existing_config if config.exists() else None)
        return

    policy = _read_text(source_policy, "source policy")
    policy = policy if policy.endswith("\n") else f"{policy}\n"
    agents = codex_home / "AGENTS.md"
    existing_agents = _read_text(agents, "target instructions") if agents.exists() else ""

    # Validate both targets before modifying either one so a malformed policy
    # cannot leave a partially synchronized configuration behind.
    desired_agents = _replace_policy(existing_agents, policy, _policy_block(policy))
    _atomic_write(config, desired_config, existing_config if config.exists() else None)
    _atomic_write(agents, desired_agents, existing_agents if agents.exists() else None)


def parse_args() -> argparse.Namespace:
    """Parse the explicitly scoped source and Codex-home paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--source-policy", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--skip-policy", action="store_true", help="update session defaults without changing AGENTS.md")
    return parser.parse_args()


def main() -> int:
    """Synchronize the requested Codex-home session defaults."""
    args = parse_args()
    try:
        sync(args.source_config, args.source_policy, args.codex_home, install_policy=not args.skip_policy)
    except (OSError, SyncError) as error:
        print(f"codex-home-sync-error: {error}", file=sys.stderr)
        return 2
    print(f"[ok] synced normal-session defaults to {args.codex_home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
