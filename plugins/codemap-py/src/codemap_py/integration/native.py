"""Observe the host CLIs and installed plugin state without mutating anything."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .managed_block import _CODEX_RIG_AGENTS_BEGIN_RE, _CODEX_RIG_AGENTS_END
from .types import MARKETPLACE_NAME, IntegrationError, Runtime
from .util import _MAX_JSON_BYTES, _sha256_bytes

#: Timeout in seconds for each native plugin CLI subprocess call.
_NATIVE_TIMEOUT_S = 30


#: Maximum number of files hashed when fingerprinting the provider plugin's identity.
_MAX_PROVIDER_IDENTITY_FILES = 2_048


#: Maximum total bytes hashed when fingerprinting the provider plugin's identity.
_MAX_PROVIDER_IDENTITY_BYTES = 8 * _MAX_JSON_BYTES


#: Chunk size in bytes used when streaming files into the identity hash.
_IDENTITY_READ_CHUNK_BYTES = 64 * 1_024


#: Plugin subdirectories whose files contribute to the provider identity fingerprint.
_PROVIDER_IDENTITY_DIRS = (
    ".claude-plugin",
    ".codex-plugin",
    "bin",
    "scripts",
    "src",
    "claude-skills",
    "codex-skills",
    "shared",
    "hooks",
)


#: Top-level plugin documents that also contribute to the provider identity fingerprint.
_PROVIDER_IDENTITY_DOCS = ("README.md", "LICENSE", "NOTICE", "CHANGELOG.md")


#: Path components that exclude a file from the provider identity fingerprint (caches, tests, local state).
_PROVIDER_IDENTITY_EXCLUDED_PARTS = frozenset(
    {"__pycache__", ".cache", ".reports", ".temp", ".pytest_cache", ".claude", "tests"}
)


#: Characters that make an argument unsafe to pass through a Windows .bat or .cmd launcher.
_WINDOWS_BATCH_METACHARACTERS = frozenset('&|<>^()%!"')


def _unsafe_windows_batch_argv(executable: str, arguments: Sequence[str]) -> bool:
    """Return True when *arguments* could smuggle shell syntax into a Windows ``.bat``/``.cmd``."""
    if any(character in '\r\n"%!' for character in executable):
        return True
    return any(
        not argument or any(ch.isspace() or ch in _WINDOWS_BATCH_METACHARACTERS for ch in argument)
        for argument in arguments
    )


def _resolve_native_command(command: Sequence[str], *, windows: bool) -> tuple[list[str] | str, bool]:
    """Resolve one argv to its executable; return a batch-safe shell line on Windows when needed.

    Examples:
        >>> _resolve_native_command(["definitely-not-a-real-binary-xyz"], windows=False)
        Traceback (most recent call last):
            ...
        codemap_py.integration.types.IntegrationError: command unavailable: definitely-not-a-real-binary-xyz
    """
    executable = shutil.which(command[0])
    if executable is None:
        raise IntegrationError(
            "native_command_missing", f"command unavailable: {command[0]}", detail={"argv": list(command)}
        )
    resolved = [executable, *command[1:]]
    if windows and Path(executable).suffix.lower() in {".bat", ".cmd"}:
        if _unsafe_windows_batch_argv(executable, command[1:]):
            raise IntegrationError(
                "unsafe_windows_argv", "argv is unsafe for a Windows batch launcher", detail={"argv": list(command)}
            )
        line = f'"{executable}"'
        if command[1:]:
            line = f"{line} {' '.join(command[1:])}"
        return line, True
    return resolved, False


def _run_native(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one resolved native command (argv-only; Windows batch launchers quoted safely)."""
    resolved, shell = _resolve_native_command(argv, windows=os.name == "nt")
    try:
        return subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
            resolved,
            shell=shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_NATIVE_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrationError(
            "native_command_failed", f"{argv[0]} failed to start: {exc}", detail={"argv": list(argv)}
        ) from exc


def _run_native_required(argv: Sequence[str]) -> None:
    """Run one native command; raise a bounded :class:`IntegrationError` on non-zero exit."""
    completed = _run_native(argv)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "command failed").strip()[:512]
        raise IntegrationError(
            "native_command_failed",
            f"{argv[0]} failed ({completed.returncode}): {detail}",
            detail={"argv": list(argv)},
        )


def _native_json_probe(argv: Sequence[str]) -> object | None:
    """Best-effort JSON probe: ``None`` on any failure — absence is a valid, non-fatal state."""
    try:
        resolved, shell = _resolve_native_command(argv, windows=os.name == "nt")
    except IntegrationError:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
            resolved,
            shell=shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_NATIVE_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    if len(completed.stdout.encode("utf-8")) > _MAX_JSON_BYTES:
        return None
    try:
        return json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError):
        return None


def _claude_installed_version(consumer: str, installed: object) -> str | None:
    """Return *consumer*'s enabled version from a ``claude plugin list --json`` payload."""
    if not isinstance(installed, list):
        return None
    prefix = f"{consumer}@"
    matches = [
        item["version"]
        for item in installed
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item["id"].startswith(prefix)
        and item.get("enabled") is True
        and isinstance(item.get("version"), str)
    ]
    return matches[0] if len(matches) == 1 else None


def _codex_installed_version(consumer: str, payload: object) -> str | None:
    """Return *consumer*'s enabled version from a ``codex plugin list --json`` payload."""
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, list):
        return None
    matches = [
        item["version"]
        for item in installed
        if isinstance(item, dict)
        and item.get("name") == consumer
        and item.get("enabled") is True
        and isinstance(item.get("version"), str)
    ]
    return matches[0] if len(matches) == 1 else None


def _native_plugin_record(runtime: Runtime, payload: object, consumer: str) -> dict:
    """Return one normalized native plugin record without retaining the host payload."""
    not_observed = {
        "state": "not_observed",
        "name": consumer,
        "version": None,
        "enabled": None,
        "source_path": None,
    }
    installed = (
        payload if runtime == Runtime.CLAUDE else payload.get("installed") if isinstance(payload, dict) else None
    )
    if not isinstance(installed, list):
        return not_observed
    if runtime == Runtime.CLAUDE:
        matches = [
            item
            for item in installed
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].startswith(f"{consumer}@")
            and item.get("enabled") is True
        ]
        path = matches[0].get("installPath") if len(matches) == 1 else None
    else:
        matches = [
            item
            for item in installed
            if isinstance(item, dict) and item.get("name") == consumer and item.get("enabled") is True
        ]
        source = matches[0].get("source") if len(matches) == 1 else None
        path = source.get("path") if isinstance(source, dict) else None
    if len(matches) != 1:
        return not_observed
    item = matches[0]
    return {
        "state": "observed",
        "name": consumer,
        "version": item.get("version") if isinstance(item.get("version"), str) else None,
        "enabled": True,
        "source_path": path if isinstance(path, str) else None,
    }


def _provider_content_identity(path: Path, *, unreadable_reason: str) -> dict:
    """Hash one explicit shipped-payload surface within fixed limits, without exposing content."""
    if path.is_symlink() or not path.is_dir():
        return {"state": "unknown", "reason": unreadable_reason}
    try:
        files = [candidate for name in _PROVIDER_IDENTITY_DOCS if (candidate := path / name).is_file()]
        for directory in _PROVIDER_IDENTITY_DIRS:
            root = path / directory
            if not root.is_dir() or root.is_symlink():
                continue
            files.extend(
                candidate
                for candidate in root.rglob("*")
                if candidate.is_file()
                and not candidate.is_symlink()
                and not _PROVIDER_IDENTITY_EXCLUDED_PARTS.intersection(candidate.relative_to(path).parts)
                and candidate.name != ".DS_Store"
            )
        files.sort(key=lambda candidate: candidate.relative_to(path).as_posix())
    except OSError:
        return {"state": "unknown", "reason": unreadable_reason}
    if len(files) > _MAX_PROVIDER_IDENTITY_FILES:
        return {"state": "unknown", "reason": "provider_content_file_limit_exceeded"}

    digest = hashlib.sha256()
    bytes_hashed = 0
    try:
        for candidate in files:
            relative = candidate.relative_to(path).as_posix()
            file_digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                while chunk := handle.read(_IDENTITY_READ_CHUNK_BYTES):
                    bytes_hashed += len(chunk)
                    if bytes_hashed > _MAX_PROVIDER_IDENTITY_BYTES:
                        return {"state": "unknown", "reason": "provider_content_byte_limit_exceeded"}
                    file_digest.update(chunk)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file_digest.digest())
            digest.update(b"\0")
    except (OSError, UnicodeError, ValueError):
        return {"state": "unknown", "reason": unreadable_reason}
    return {
        "state": "observed",
        "schema_version": 1,
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "bytes_hashed": bytes_hashed,
    }


def _installed_version_lookup(runtime: Runtime | str) -> Callable[[str, object], str | None]:
    return _claude_installed_version if runtime == Runtime.CLAUDE else _codex_installed_version


def _marketplace_entry(runtime: Runtime | str) -> dict | None:
    """Return the configured ``borda-ai-rig`` marketplace entry for *runtime*, or ``None``."""
    if runtime == Runtime.CLAUDE:
        payload = _native_json_probe(["claude", "plugin", "marketplace", "list", "--json"])
        entries = payload if isinstance(payload, list) else None
    else:
        payload = _native_json_probe(["codex", "plugin", "marketplace", "list", "--json"])
        entries = payload.get("marketplaces") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None
    matches = [e for e in entries if isinstance(e, dict) and e.get("name") == MARKETPLACE_NAME]
    return matches[0] if len(matches) == 1 else None


def _codex_home(environ: Mapping[str, str]) -> Path:
    configured = environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def codex_rig_global_status(environ: Mapping[str, str] | None = None) -> str:
    """Return ``absent`` | ``present`` | ``authenticated`` for ``${CODEX_HOME}/AGENTS.md``.

    Read-only inspection of Codex Rig's own authenticated-marker byte format; never invokes
    ``install_global_agents.py`` and never writes the file. ``stale`` is reportable only
    through a versioned, Codex-Rig-owned read-only status contract — which does not exist yet
    — so staleness is always ``unavailable`` to callers of this function, never guessed from
    these bytes alone.

    Args:
        environ: Environment mapping to resolve ``CODEX_HOME`` from (defaults to
            :data:`os.environ`).

    Examples:
        >>> codex_rig_global_status({"CODEX_HOME": "/nonexistent-codemap-py-doctest-home"})
        'absent'
    """
    target = _codex_home(environ if environ is not None else os.environ) / "AGENTS.md"
    if not target.is_file() or target.is_symlink():
        return "absent"
    try:
        content = target.read_bytes()
    except OSError:
        return "absent"
    match = _CODEX_RIG_AGENTS_BEGIN_RE.search(content)
    if match is None:
        return "absent"
    end_index = content.find(_CODEX_RIG_AGENTS_END, match.end())
    if end_index == -1:
        return "present"
    body = content[match.end() : end_index]
    return "authenticated" if _sha256_bytes(body) == match.group(1).decode("ascii") else "present"
