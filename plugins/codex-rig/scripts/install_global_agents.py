#!/usr/bin/env python3
"""Safely check, migrate, install, update, or remove managed global instructions.

## Purpose

Maintain one integrity-marked Codex Rig section inside a user global instruction file while preserving surrounding user
content. The managed block gives session tooling a known instruction entry point without taking ownership of unrelated
user-authored text.

## Scope

Checks the managed file and two verified legacy skill routes without mutation. Explicit digest-bound migration removes
only the reviewed global prefix before an authenticated block and preserves its custom suffix. Other lifecycle actions
perform local file mutation with backup and atomic-write checks; this script does not manage role shims or remote
services. Its safety contract covers only the marked block and target-file replacement, not arbitrary edits to the rest
of the instruction file.

## Usage

Run the documented CLI only for an approved global-instruction lifecycle operation. Select the action through the
workflow for agent shims so diagnosis and approval occur before this script changes the target.

## Used by

The agent-shims workflow, session setup procedures, and global-instruction installer tests call this installer. These
callers depend on its marker and hash format to distinguish managed content from user content during update and removal.

## Outputs

Reports the target, backup when created, and lifecycle status after an atomic local update or removal. A successful
result identifies the resulting managed-block state so session setup can continue with an auditable local outcome.

## Failure

Malformed managed markers, hash mismatch, unsafe target state, or concurrent replacement raises
``UnsafeGlobalAgentsState`` and leaves user content untouched. The caller must surface that failure for review because
continuing could overwrite instructions that are no longer the approved file.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

#: Token in the packaged template that rendering replaces with the absolute installed Codex Rig plugin root.
PLUGIN_ROOT_PLACEHOLDER = b"{{CODEX_RIG_PLUGIN_ROOT}}"
#: Rendered template line that defines ``PLUGIN_ROOT``, capturing the root written into its code span.
RENDERED_ROOT_PATTERN = re.compile(rb"^`PLUGIN_ROOT` = `([^`\r\n]+)`", re.MULTILINE)
#: Fixed opening text of the begin marker that precedes the managed global-instructions block.
BEGIN_PREFIX = b"<!-- codex-rig:global-agents begin sha256="
#: Pattern for a complete begin marker line, capturing the SHA-256 digest of the managed block body.
BEGIN_PATTERN = re.compile(rb"<!-- codex-rig:global-agents begin sha256=([0-9a-f]{64}) -->\n")
#: Marker line that closes the managed global-instructions block.
END_MARKER = b"<!-- codex-rig:global-agents end -->\n"
#: Top heading of the managed global agent instructions file.
GLOBAL_HEADING = b"# Global Agent Instructions"
#: Frontmatter descriptions of the retired develop and analyse skills, used to spot stale copies under the Codex home.
LEGACY_SKILL_DESCRIPTIONS = {
    "develop": "Minimal codex-native develop loop. Use for implementation tasks with linear plan-build-verify flow and measurable quality gates.",
    "analyse": "Minimal codex-native analysis loop. Use for issue/PR/problem analysis before implementation with measurable gates.",
}


class UnsafeGlobalAgentsState(ValueError):
    """Report target state that cannot be changed without risking user content."""


def sha256(payload: bytes) -> str:
    """Return the lowercase SHA-256 digest for exact bytes."""
    return hashlib.sha256(payload).hexdigest()


def rendered_template(template: bytes, source: Path, plugin_root: str | None = None) -> bytes:
    """Write the absolute Codex Rig plugin root into the template's single placeholder.

    The managed block is loaded by Codex sessions in unrelated projects, so its packaged ``shared/<file>`` pointers
    and ``PLUGIN_ROOT`` helper recipes resolve only when the block itself names the installed plugin root. The root
    defaults to the package that ships ``source`` (``<plugin-root>/assets/AGENTS.md``); sync passes the installed
    cache root explicitly because it reads the template from the marketplace checkout. A root on this host is
    canonicalized like the derived default (symlinks resolved, relative values anchored), so install, sync and a
    later ``--check`` render one block however a symlinked Codex home was spelled; an absolute literal of the other
    path flavour is written verbatim. A template without the placeholder is returned unchanged, which keeps arbitrary
    or older templates installable byte for byte.

    Example:
        >>> template = b"`PLUGIN_ROOT` = `{{CODEX_RIG_PLUGIN_ROOT}}`\\n"
        >>> rendered_template(template, Path("unused"), r"D:\\codex\\codex-rig\\0.1.0")
        b'`PLUGIN_ROOT` = `D:\\\\codex\\\\codex-rig\\\\0.1.0`\\n'
    """
    count = template.count(PLUGIN_ROOT_PLACEHOLDER)
    if count == 0:
        if plugin_root is not None:
            raise UnsafeGlobalAgentsState("template has no plugin-root placeholder; omit --plugin-root")
        return template
    if count > 1:
        raise UnsafeGlobalAgentsState("template repeats the plugin-root placeholder; refusing to render")
    root = plugin_root if plugin_root is not None else str(source.resolve().parent.parent)
    # The derived default is already resolved; resolving an explicit host root too keeps `--plugin-root <link>` and
    # `--source <link>/assets/AGENTS.md` from rendering two different blocks (a false stale-template diagnosis). An
    # absolute literal of the other flavour is kept: the host Path flavour would rewrite its separators.
    other_flavour = not Path(root).is_absolute() and (
        PurePosixPath(root).is_absolute() or PureWindowsPath(root).is_absolute()
    )
    if not other_flavour:
        root = os.path.realpath(root)
    # The root is rendered inside a one-line Markdown code span; a backtick or line break would end it early.
    if any(character in root for character in "`\r\n"):
        raise UnsafeGlobalAgentsState("plugin root contains a backtick or line break; refusing to render")
    try:
        encoded = root.encode("utf-8")
    except UnicodeEncodeError as error:
        raise UnsafeGlobalAgentsState("plugin root is not representable as UTF-8; refusing to render") from error
    return template.replace(PLUGIN_ROOT_PLACEHOLDER, encoded)


def managed_block(template: bytes) -> bytes:
    """Wrap exact template bytes in authenticated ownership markers."""
    if not template:
        raise UnsafeGlobalAgentsState("global instruction template is empty")
    try:
        template.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnsafeGlobalAgentsState("global instruction template is not UTF-8") from error
    if BEGIN_PREFIX in template or END_MARKER.rstrip(b"\n") in template:
        raise UnsafeGlobalAgentsState("global instruction template contains ownership markers")
    body = template if template.endswith(b"\n") else template + b"\n"
    begin = BEGIN_PREFIX + sha256(body).encode("ascii") + b" -->\n"
    return begin + body + END_MARKER


def merged_payload(existing: bytes, block: bytes) -> tuple[bytes, str]:
    """Merge one trusted managed block while preserving all external bytes."""
    try:
        existing.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnsafeGlobalAgentsState("existing AGENTS.md is not UTF-8; refusing write") from error

    begin_count = existing.count(BEGIN_PREFIX)
    end_count = existing.count(END_MARKER.rstrip(b"\n"))
    if begin_count == 0 and end_count == 0:
        separator = b"" if not existing else (b"\n" if existing.endswith(b"\n") else b"\n\n")
        return existing + separator + block, "merged"
    if begin_count != 1 or end_count != 1:
        raise UnsafeGlobalAgentsState("managed markers are malformed or duplicated; refusing write")

    begin_match = BEGIN_PATTERN.search(existing)
    if begin_match is None:
        raise UnsafeGlobalAgentsState("managed begin marker is malformed; refusing write")
    end_index = existing.find(END_MARKER, begin_match.end())
    if end_index < 0:
        raise UnsafeGlobalAgentsState("managed end marker is malformed; refusing write")
    body = existing[begin_match.end() : end_index]
    if sha256(body) != begin_match.group(1).decode("ascii"):
        raise UnsafeGlobalAgentsState("managed block was modified; refusing write")

    block_end = end_index + len(END_MARKER)
    updated = existing[: begin_match.start()] + block + existing[block_end:]
    return updated, "already current" if updated == existing else "updated"


def stripped_payload(existing: bytes) -> tuple[bytes, str]:
    """Remove one authenticated managed block, preserving every external byte."""
    try:
        existing.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnsafeGlobalAgentsState("existing AGENTS.md is not UTF-8; refusing write") from error

    begin_count = existing.count(BEGIN_PREFIX)
    end_count = existing.count(END_MARKER.rstrip(b"\n"))
    if begin_count == 0 and end_count == 0:
        return existing, "absent"
    if begin_count != 1 or end_count != 1:
        raise UnsafeGlobalAgentsState("managed markers are malformed or duplicated; refusing write")

    begin_match = BEGIN_PATTERN.search(existing)
    if begin_match is None:
        raise UnsafeGlobalAgentsState("managed begin marker is malformed; refusing write")
    end_index = existing.find(END_MARKER, begin_match.end())
    if end_index < 0:
        raise UnsafeGlobalAgentsState("managed end marker is malformed; refusing write")
    body = existing[begin_match.end() : end_index]
    if sha256(body) != begin_match.group(1).decode("ascii"):
        raise UnsafeGlobalAgentsState("managed block was modified; refusing write")

    block_end = end_index + len(END_MARKER)
    updated = existing[: begin_match.start()] + existing[block_end:]
    # collapse the single separator install prepended so removal leaves no doubled blank line
    if updated.endswith(b"\n\n") and existing[: begin_match.start()].endswith(b"\n\n"):
        updated = updated[:-1]
    return updated, "removed"


def authenticated_managed_body(existing: bytes) -> bytes | None:
    """Return the body of the single authenticated managed block, without changing anything.

    Read-only diagnostics use it to inspect what installation wrote. ``None`` means there is no such block: absent,
    duplicated, malformed, or edited by hand (``--check`` reports the last three).

    Example:
        >>> managed_block(b"body\\n") == BEGIN_PREFIX + sha256(b"body\\n").encode() + b" -->\\nbody\\n" + END_MARKER
        True
        >>> authenticated_managed_body(b"user notes\\n\\n" + managed_block(b"body\\n"))
        b'body\\n'
        >>> authenticated_managed_body(managed_block(b"body\\n").replace(b"body", b"edit")) is None
        True
    """
    if existing.count(BEGIN_PREFIX) != 1 or existing.count(END_MARKER.rstrip(b"\n")) != 1:
        return None
    begin_match = BEGIN_PATTERN.search(existing)
    if begin_match is None:
        return None
    end_index = existing.find(END_MARKER, begin_match.end())
    if end_index < 0:
        return None
    body = existing[begin_match.end() : end_index]
    return body if sha256(body) == begin_match.group(1).decode("ascii") else None


def rendered_plugin_root(body: bytes) -> str | None:
    """Return the plugin root a managed block body was rendered with, or ``None`` when it names none.

    A body from a template that predates the ``PLUGIN_ROOT`` line names no root; diagnostics compare a returned root
    with the running plugin to spot a block left behind by a plugin update.

    Example:
        >>> rendered_plugin_root(b"`PLUGIN_ROOT` = `/opt/codex-rig/0.1.0`, the installed Codex Rig package\\n")
        '/opt/codex-rig/0.1.0'
        >>> rendered_plugin_root(b"# Global Agent Instructions\\n") is None
        True
    """
    roots = RENDERED_ROOT_PATTERN.findall(body)
    if len(roots) != 1:
        return None
    try:
        return roots[0].decode("utf-8")
    except UnicodeDecodeError:
        return None


def backup_target(target: Path, codex_home: Path, payload: bytes) -> Path:
    """Create and verify a unique backup before changing an existing target."""
    backup_root = codex_home / "backups" / "codex-rig"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_root / f"{timestamp}-{sha256(payload)[:12]}-AGENTS.md"
    shutil.copy2(target, backup, follow_symlinks=False)
    if backup.read_bytes() != payload:
        raise OSError(f"backup verification failed: {backup}")
    return backup


def atomic_write(
    target: Path, payload: bytes, mode: int, expected: bytes | None, expected_identity: os.stat_result | None = None
) -> None:
    """Replace target atomically after rejecting observable concurrent drift."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".AGENTS.md.codex-rig-", delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        if target.is_symlink():
            raise UnsafeGlobalAgentsState(f"target became a symlink; refusing write: {target}")
        if expected is None:
            if target.exists():
                raise UnsafeGlobalAgentsState(f"target appeared during installation; refusing write: {target}")
        elif not target.is_file() or target.read_bytes() != expected:
            raise UnsafeGlobalAgentsState(f"target changed during installation; refusing write: {target}")
        if expected_identity is not None:
            observed = target.stat()
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(observed, field) != getattr(expected_identity, field) for field in fields):
                raise UnsafeGlobalAgentsState("target was replaced during installation; refusing write")
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def check_global_agents(source: Path, codex_home: Path, *, plugin_root: str | None = None) -> list[str]:
    """Diagnose only the managed instruction file and two verified legacy skill routes.

    Missing optional setup is allowed. Diagnostics contain reasons, never private policy bytes; this is not a scan of
    the entire user directory and does not modify any target, backup, or skill. The template is rendered with the same
    plugin root installation would write, so a current block is not misreported as stale.
    """
    if source.is_symlink() or not source.is_file():
        raise UnsafeGlobalAgentsState("template must be an ordinary file")
    template = rendered_template(source.read_bytes(), source, plugin_root)
    block = managed_block(template)
    reasons: list[str] = []
    target = codex_home / "AGENTS.md"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        reasons.append("unsafe-global-target")
    elif target.exists():
        existing = target.read_bytes()
        try:
            # Check the same proposed composition as installation, including exact unmarked adoption.
            template_body = template if template.endswith(b"\n") else template + b"\n"
            if existing in {template, template_body}:
                desired, action = block, "adopted"
            else:
                desired, action = merged_payload(existing, block)
            unmanaged, _ = stripped_payload(desired)
            if GLOBAL_HEADING in unmanaged and GLOBAL_HEADING in block:
                reasons.append("global-agents-overlap")
            if action == "updated":
                reasons.append("stale-managed-template")
            elif action == "merged" and existing.count(GLOBAL_HEADING) > 1:
                reasons.append("duplicate-unmanaged-global-policy")
        except UnsafeGlobalAgentsState:
            reasons.append("invalid-managed-instructions")
    for name, description in LEGACY_SKILL_DESCRIPTIONS.items():
        skill = codex_home / "skills" / name / "SKILL.md"
        if skill.is_symlink() or not skill.is_file():
            continue
        try:
            text = skill.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
        if frontmatter is None:
            continue
        fields = frontmatter.group(1).splitlines()
        if f"name: {name}" in fields and f"description: {description}" in fields:
            reasons.append(f"legacy-skill-route:{name}")
    return reasons


def migrated_prefix_payload(existing: bytes, block: bytes, digest: str) -> bytes:
    """Remove the exact reviewed global prefix before one authenticated managed block.

    The digest selects every byte from the start of the file up to the managed begin marker. All custom suffix bytes
    survive unchanged; an absent, ambiguous, or unauthenticated block cannot authorize removal.
    """
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise UnsafeGlobalAgentsState("legacy prefix digest must be lowercase SHA-256; refusing write")
    desired, _ = merged_payload(existing, block)
    begin = BEGIN_PATTERN.search(existing)
    if begin is None:
        raise UnsafeGlobalAgentsState("legacy migration requires an authenticated managed block; refusing write")
    prefix = existing[: begin.start()]
    if not prefix.startswith((GLOBAL_HEADING + b"\n", GLOBAL_HEADING + b"\r\n")) or prefix.count(GLOBAL_HEADING) != 1:
        raise UnsafeGlobalAgentsState("legacy global prefix is absent or ambiguous; refusing write")
    if sha256(prefix) != digest:
        raise UnsafeGlobalAgentsState("legacy prefix digest changed; refusing write")
    remaining = desired[len(prefix) :]
    unmanaged, _ = stripped_payload(remaining)
    if GLOBAL_HEADING in unmanaged:
        raise UnsafeGlobalAgentsState("additional unmanaged global policy remains; refusing write")
    return remaining


def install_global_agents(
    source: Path, codex_home: Path, legacy_prefix_sha256: str | None = None, *, plugin_root: str | None = None
) -> tuple[str, Path, Path | None]:
    """Install the template or explicitly migrate one digest-selected legacy prefix.

    Ordinary installation refuses unmanaged global-policy overlap. Migration removes only the reviewed prefix before the
    authenticated managed block and keeps all custom suffix bytes; every changed existing target gets a backup. The
    block holds the rendered template, so a new plugin root (a plugin upgrade) updates it like any template change.
    """
    if source.is_symlink() or not source.is_file():
        raise UnsafeGlobalAgentsState(f"template must be an ordinary file: {source}")
    template = rendered_template(source.read_bytes(), source, plugin_root)
    block = managed_block(template)
    target = codex_home / "AGENTS.md"
    if target.is_symlink():
        raise UnsafeGlobalAgentsState(f"target is a symlink; refusing write: {target}")
    if target.exists() and not target.is_file():
        raise UnsafeGlobalAgentsState(f"target is not an ordinary file; refusing write: {target}")

    if not target.exists():
        if legacy_prefix_sha256 is not None:
            raise UnsafeGlobalAgentsState("legacy migration requires an existing target; refusing write")
        codex_home.mkdir(parents=True, exist_ok=True)
        atomic_write(target, block, 0o600, None)
        return "created", target, None

    identity = target.stat()
    existing = target.read_bytes()
    template_body = template if template.endswith(b"\n") else template + b"\n"
    if legacy_prefix_sha256 is not None:
        desired, action = migrated_prefix_payload(existing, block, legacy_prefix_sha256), "migrated legacy prefix"
    elif existing in {template, template_body}:
        desired, action = block, "adopted"
    else:
        desired, action = merged_payload(existing, block)
    unmanaged, _ = stripped_payload(desired)
    if GLOBAL_HEADING in unmanaged and GLOBAL_HEADING in template:
        raise UnsafeGlobalAgentsState(
            "global-agents-overlap: unmanaged global policy overlaps the managed template; refusing write. "
            "Review the explicit digest-bound legacy prefix migration."
        )
    if action == "already current":
        return action, target, None
    mode = stat.S_IMODE(identity.st_mode)
    observed = target.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        target.is_symlink()
        or target.read_bytes() != existing
        or any(getattr(observed, field) != getattr(identity, field) for field in fields)
    ):
        raise UnsafeGlobalAgentsState("target changed before backup; refusing write")
    backup = backup_target(target, codex_home, existing)
    atomic_write(target, desired, mode, existing, identity)
    return action, target, backup


def remove_global_agents(codex_home: Path) -> tuple[str, Path, Path | None]:
    """Strip Codex Rig's managed block from AGENTS.md without touching user content."""
    target = codex_home / "AGENTS.md"
    if not target.exists():
        return "absent", target, None
    if target.is_symlink():
        raise UnsafeGlobalAgentsState(f"target is a symlink; refusing write: {target}")
    if not target.is_file():
        raise UnsafeGlobalAgentsState(f"target is not an ordinary file; refusing write: {target}")

    existing = target.read_bytes()
    updated, action = stripped_payload(existing)
    if action == "absent":
        return "absent", target, None

    backup = backup_target(target, codex_home, existing)
    if updated.strip() == b"":
        target.unlink()  # file held only our block — remove it entirely
        return "removed-file", target, backup
    mode = stat.S_IMODE(target.stat().st_mode)
    atomic_write(target, updated, mode, existing)
    return "removed-block", target, backup


def parse_args() -> argparse.Namespace:
    """Parse explicit source and Codex-home paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="packaged assets/AGENTS.md template (required unless --remove)")
    parser.add_argument("--codex-home", type=Path, required=True, help="target Codex home")
    parser.add_argument(
        "--plugin-root",
        help="absolute installed Codex Rig root written into the managed block (default: package containing --source)",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--remove", action="store_true", help="strip the managed block instead of installing it")
    actions.add_argument(
        "--check", action="store_true", help="read-only check of instructions and two legacy skill routes"
    )
    actions.add_argument(
        "--migrate-legacy-prefix-sha256",
        help="explicitly remove the reviewed legacy global prefix with this exact digest",
    )
    args = parser.parse_args()
    if not args.remove and args.source is None:
        parser.error("--source is required unless --remove is given")
    if args.remove and args.plugin_root is not None:
        parser.error("--plugin-root renders the template and does not apply to --remove")
    return args


def main() -> int:
    """Run one explicit instruction check, migration, installation, or removal."""
    args = parse_args()
    try:
        if args.check:
            reasons = check_global_agents(args.source, args.codex_home, plugin_root=args.plugin_root)
            for reason in reasons:
                print(f"global-agents-check: {reason}", file=sys.stderr)
            if reasons:
                return 4
            print(
                "  [ok] bounded instruction check: AGENTS.md and legacy develop/analyse routes; optional absence allowed"
            )
            return 0
        if args.remove:
            action, target, backup = remove_global_agents(args.codex_home)
        else:
            action, target, backup = install_global_agents(
                args.source, args.codex_home, args.migrate_legacy_prefix_sha256, plugin_root=args.plugin_root
            )
    except UnsafeGlobalAgentsState as error:
        print(f"global-agents-error: {error}", file=sys.stderr)
        return 4
    except OSError as error:
        print(f"global-agents-error: {error}", file=sys.stderr)
        return 2
    print(f"  [ok] global instructions {action}: {target}")
    if backup is not None:
        print(f"    backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
