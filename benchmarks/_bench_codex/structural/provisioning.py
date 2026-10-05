"""Disposable Codex homes: authentication, permission profiles, plugin installation, and frozen input snapshots."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from _bench_common.coordination_gate import (
    COORDINATION_NAME as _COORDINATION_NAME,
)
from _bench_common.coordination_gate import (
    assert_safe_path_components as _assert_safe_path_components,
)
from _bench_common.coordination_gate import (
    cleanup_coordination_root as _cleanup_coordination_root,
)
from _bench_common.coordination_gate import (
    prepare_coordination_root,
)
from _bench_common.coordination_gate import (
    validate_coordination_root as _validate_coordination_root,
)
from _bench_common.mutation_isolation import (
    ExecutableAgentWorkspace,
)

from _bench_codex import plugin_registration, runtime
from _bench_codex.structural.arms import _is_known_codex_arm
from _bench_codex.structural.config import (
    _AUTH_MAX_BYTES,
    _BENCHMARK_EVIDENCE_ROOTS_ENV,
    _CODEMAP_PERMISSION_PROFILE,
    _CODEX_BIN,
    _FROZEN_MARKETPLACE_NAME,
    _PLAIN_PERMISSION_PROFILE,
    PACKAGE_DIR,
    PARITY_MANIFEST_PATH,
    REPO_ROOT,
)

if TYPE_CHECKING:  # annotation-only: a real import would close a cycle with ``runner``.
    from _bench_codex.structural.runner import CodexRunner


@dataclass
class ArmHome:
    """Disposable Codex home and environment for one canonical arm."""

    arm: str
    path: Path
    env: dict[str, str]
    codemap_available: bool
    codemap_verified: bool = False
    auth_provisioned: bool = False
    authenticated: bool = False
    permission_profile: str = ""
    coordination_path: Path | None = None
    codemap_launcher_path: Path | None = None
    codemap_launcher_sha256: str = ""
    codemap_plugin_path: Path | None = None
    codemap_plugin_manifest_sha256: str = ""
    codemap_skill_path: Path | None = None
    codemap_skill_sha256: str = ""
    codex_rig_path: Path | None = None
    codex_rig_manifest_sha256: str = ""
    codex_rig_adapter_path: Path | None = None
    codex_rig_adapter_sha256: str = ""
    codemap_context_path: Path | None = None
    codemap_context_sha256: str = ""
    denied_read_paths: tuple[Path, ...] = ()
    evidence_probe_paths: tuple[Path, ...] = ()
    host_plugin_names: tuple[str, ...] = ()

    def cleanup(self) -> None:
        """Remove the disposable home after a run."""
        _remove_private_directory(self.path, description="disposable Codex home")

    def __enter__(self) -> ArmHome:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()


@contextlib.contextmanager
def bind_executable_agent_workspace(adapter: CodexRunner, workspace: ExecutableAgentWorkspace) -> Iterable[None]:
    """Bind one existing frozen adapter to a per-cell editable worktree temporarily."""
    original_repo_path, original_index_path = adapter.repo_path, adapter.index_path
    adapter.repo_path, adapter.index_path = workspace.worktree, workspace.index_path
    try:
        yield
    finally:
        adapter.repo_path, adapter.index_path = original_repo_path, original_index_path


@dataclass(frozen=True)
class _AuthFileIdentity:
    """Stable metadata required for one private credential file."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _auth_identity(metadata: os.stat_result) -> _AuthFileIdentity:
    """Return the immutable metadata tuple used for credential stability checks."""
    return _AuthFileIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_auth_metadata(metadata: os.stat_result, *, description: str) -> None:
    """Reject credentials that cannot safely carry mutable OAuth state."""
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{description} must be a regular file")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError(f"{description} must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError(f"{description} permissions must be exactly 0600")
    if metadata.st_nlink != 1:
        raise ValueError(f"{description} must not be hard-linked")
    if not 0 < metadata.st_size <= _AUTH_MAX_BYTES:
        raise ValueError(f"{description} size is invalid")


def _read_auth_payload(path: Path, *, description: str) -> tuple[bytes, _AuthFileIdentity]:
    """Read one stable JSON-object credential through a no-follow descriptor."""
    path = Path(path)
    try:
        _assert_safe_path_components(path)
    except ValueError:
        raise ValueError(f"{description} path is unsafe") from None
    try:
        before = path.lstat()
    except OSError:
        raise ValueError(f"{description} is unavailable") from None
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"{description} must not be a symlink")
    _validate_auth_metadata(before, description=description)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        _validate_auth_metadata(opened, description=description)
        if _auth_identity(opened) != _auth_identity(before):
            raise ValueError(f"{description} changed while being opened")
        chunks: list[bytes] = []
        remaining = _AUTH_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != opened.st_size or len(payload) > _AUTH_MAX_BYTES:
            raise ValueError(f"{description} changed while being read")
        after_descriptor = os.fstat(descriptor)
    except OSError:
        raise ValueError(f"{description} could not be read securely") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        after = path.lstat()
    except OSError:
        raise ValueError(f"{description} changed while being read") from None
    identity = _auth_identity(before)
    if _auth_identity(after_descriptor) != identity or _auth_identity(after) != identity:
        raise ValueError(f"{description} changed while being read")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{description} must contain a JSON object") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise ValueError(f"{description} must contain a non-empty JSON object")
    return payload, identity


def _fsync_directory(path: Path) -> None:
    """Durably publish a same-directory credential replacement when supported."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError:
        # The payload has already been fsynced; directory fsync is unavailable
        # on some supported filesystems and must not erase valid state.
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _apply_private_mode(path: Path, descriptor: int, mode: int) -> None:
    """Apply a private file mode where the host filesystem exposes POSIX modes."""
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, mode)
    elif os.name != "nt":
        os.chmod(path, mode)


def _atomic_write_auth_payload(destination: Path, payload: bytes, *, description: str) -> None:
    """Atomically replace one validated credential while retaining prior state on failure."""
    destination = Path(destination)
    parent = destination.parent
    try:
        _assert_safe_path_components(parent)
    except ValueError as exc:
        raise ValueError(f"{description} parent path is unsafe") from exc
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise ValueError(f"{description} parent is unavailable") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise ValueError(f"{description} parent must be a private directory")
    if hasattr(os, "getuid") and parent_metadata.st_uid != os.getuid():
        raise ValueError(f"{description} parent must be owned by the current user")
    if not 0 < len(payload) <= _AUTH_MAX_BYTES:
        raise ValueError(f"{description} payload size is invalid")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{description} payload must be a JSON object") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise ValueError(f"{description} payload must be a non-empty JSON object")
    temporary = parent / f".{destination.name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        _apply_private_mode(temporary, descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("credential write returned no bytes")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, destination)
        _fsync_directory(parent)
    except OSError as exc:
        raise ValueError(f"{description} could not be updated securely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    _read_auth_payload(destination, description=description)


def _remove_private_directory(path: Path, *, description: str, require_private: bool = True) -> None:
    """Remove a disposable private directory after validating its safe identity.

    POSIX callers retain exact owner and ``0700`` checks. Windows does not expose equivalent ACL privacy through
    ``stat`` mode bits, so cleanup keeps the symlink, directory-type, containment, and post-removal checks without
    treating its emulated mode as a POSIX security guarantee.
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    try:
        _assert_safe_path_components(path.parent)
    except ValueError as exc:
        raise RuntimeError(f"{description} parent path is unsafe") from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{description} could not be inspected for cleanup") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"{description} is not a private directory")
    if require_private and os.name != "nt":
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise RuntimeError(f"{description} is not owned by the current user")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RuntimeError(f"{description} permissions must be exactly 0700")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise RuntimeError(f"{description} could not be removed") from exc
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"{description} remains after cleanup")


class _RunAuthState:
    """Private sequential OAuth state shared only between disposable cell homes."""

    def __init__(self, source: Path) -> None:
        self.source = Path(source)
        payload, self._source_identity = _read_auth_payload(self.source, description="auth source")
        root = Path(tempfile.gettempdir()).resolve(strict=True)
        _assert_safe_path_components(root)
        self.directory = Path(tempfile.mkdtemp(prefix="codex-benchmark-auth-", dir=root))
        self.directory.chmod(0o700)
        self.path = self.directory / "auth.json"
        try:
            _atomic_write_auth_payload(self.path, payload, description="run auth state")
        except BaseException:
            _remove_private_directory(self.directory, description="run auth state")
            raise
        self._closed = False

    def assert_source_unchanged(self) -> None:
        """Fail before a model call when the approved source metadata has drifted."""
        _payload, identity = _read_auth_payload(self.source, description="auth source")
        if identity != self._source_identity:
            raise ValueError("auth source metadata changed during benchmark run")

    def seed_home(self, home: Path) -> None:
        """Copy the current private credential state into one disposable home."""
        if self._closed:
            raise RuntimeError("run auth state is closed")
        payload, _identity = _read_auth_payload(self.path, description="run auth state")
        _atomic_write_auth_payload(Path(home) / "auth.json", payload, description="cell auth state")

    def refresh_from_home(self, home: Path) -> None:
        """Atomically retain a valid credential refresh produced by one cell."""
        if self._closed:
            raise RuntimeError("run auth state is closed")
        payload, _identity = _read_auth_payload(Path(home) / "auth.json", description="cell auth state")
        _atomic_write_auth_payload(self.path, payload, description="run auth state")

    def close(self) -> None:
        """Remove private run credential state exactly once."""
        if self._closed:
            return
        _remove_private_directory(self.directory, description="run auth state")
        self._closed = True


def _copy_auth_source(auth_source: Path, home: Path) -> None:
    """Copy one validated source credential into a disposable Codex home."""
    payload, _identity = _read_auth_payload(Path(auth_source), description="auth source")
    _atomic_write_auth_payload(Path(home) / "auth.json", payload, description="cell auth state")


def _canonical_index_path(index_path: Path) -> Path:
    """Return one regular, single-link index path with no symlink components."""
    absolute = Path(os.path.abspath(index_path))
    _assert_safe_path_components(absolute)
    try:
        metadata = absolute.lstat()
    except OSError as exc:
        raise ValueError("canonical Codemap index is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("canonical Codemap index must be a regular file")
    if metadata.st_nlink != 1:
        raise ValueError("canonical Codemap index must not be hard-linked")
    return absolute.resolve(strict=True)


def _prepare_coordination_root(index_path: Path, coordination_root: Path | None = None) -> Path:
    """Create a clean rwgate skeleton for the index, relocated when ``coordination_root`` names a directory."""
    return prepare_coordination_root(_canonical_index_path(index_path).parent, coordination_root)


# Declared stage surface: supported replacements for stage-module reach-ins into
# private names. Each delegates rather than aliases, so tests patching the private
# attribute are still observed through the public one.
def cleanup_coordination_root(coordination_root: Path) -> list[str]:
    """Discard an idle rwgate skeleton and report the non-skeleton entries removed with it."""
    return _cleanup_coordination_root(coordination_root)


def _shell_environment(home: ArmHome) -> dict[str, str]:
    """Return the explicit non-secret environment allowed in model commands."""
    allowed = {
        "PATH": home.env.get("PATH", os.defpath),
        "HOME": str(home.path),
        "CODEX_HOME": str(home.path),
    }
    for name in (
        "CODEMAP_BIN",
        "CODEMAP_COORDINATION_DIR",
        "CODEMAP_SKILL_FILE",
        "CODEMAP_PYTHON",
        "SCAN_NO_AUTOBUILD",
        "CODEMAP_LOGGING",
        "CODEX_CODEMAP_AVAILABLE",
    ):
        value = home.env.get(name)
        if value is not None:
            allowed[name] = value
    return allowed


def _untrusted_host_agent_roots(
    home: ArmHome,
    arm: str,
    marketplace_root: Path | None = None,
) -> tuple[Path, ...]:
    """Return host tooling roots that a measured model must not inspect."""
    if not _is_known_codex_arm(arm):
        raise ValueError(f"unknown benchmark arm {arm!r}")
    roots = [Path.home() / name for name in (".agents", ".claude", ".codex")]
    if marketplace_root is not None:
        roots.append(marketplace_root)

    home_root = home.path.resolve()
    denied: list[Path] = []
    for candidate in roots:
        root = candidate.expanduser().resolve()
        if home_root == root or home_root.is_relative_to(root):
            raise ValueError("disposable Codex home must be outside denied host tooling roots")
        if root not in denied:
            denied.append(root)
    return tuple(denied)


def _benchmark_evidence_roots(environment: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Return absolute evaluator roots that measured cells must not read."""
    raw_roots = (os.environ if environment is None else environment).get(_BENCHMARK_EVIDENCE_ROOTS_ENV)
    if raw_roots is None:
        return (REPO_ROOT,)
    try:
        serialized_roots = json.loads(raw_roots)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{_BENCHMARK_EVIDENCE_ROOTS_ENV} must be a JSON array") from exc
    if (
        not isinstance(serialized_roots, list)
        or not serialized_roots
        or not all(isinstance(root, str) and root for root in serialized_roots)
    ):
        raise ValueError(f"{_BENCHMARK_EVIDENCE_ROOTS_ENV} must contain non-empty path strings")

    roots: list[Path] = []
    for raw_root in serialized_roots:
        candidate = Path(raw_root)
        if not candidate.is_absolute():
            raise ValueError(f"{_BENCHMARK_EVIDENCE_ROOTS_ENV} paths must be absolute")
        try:
            root = candidate.resolve(strict=False)
        except OSError as exc:
            raise ValueError(f"benchmark evidence root is unavailable: {candidate}") from exc
        if root.exists() and not root.is_dir():
            raise ValueError(f"benchmark evidence root must be a directory: {root}")
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _evidence_probe_paths(evidence_roots: Iterable[Path]) -> tuple[Path, ...]:
    """Return oracle files."""
    candidates = (
        Path("benchmarks") / "run-codex-structural.py",
        Path("inputs") / "shared" / "run-codex-structural.py",
        Path("inputs") / "input-snapshot.json",
    )
    probes: list[Path] = []
    for root in evidence_roots:
        for relative_path in candidates:
            probe = root / relative_path
            if probe.is_file() and not probe.is_symlink():
                probes.append(probe)
                break
    return tuple(probes)


def _write_permission_config(
    home: ArmHome,
    arm: str,
    index_path: Path | None,
    *,
    marketplace_root: Path | None = None,
    writable_workspace: Path | None = None,
    denied_workspace: Path | None = None,
    evidence_roots: Iterable[Path] = (),
) -> Path:
    """Compose permissions ahead of any preserved Codex plugin registration."""
    if not _is_known_codex_arm(arm):
        raise ValueError(f"unknown benchmark arm {arm!r}")
    profile = _PLAIN_PERMISSION_PROFILE if arm == "A_plain" else _CODEMAP_PERMISSION_PROFILE
    auth_path = (home.path / "auth.json").resolve()
    filesystem_rules = [f'{json.dumps(str(auth_path))} = "deny"']
    denied_read_paths = list(_untrusted_host_agent_roots(home, arm, marketplace_root))
    if denied_workspace is not None:
        denied_workspace = denied_workspace.resolve()
        if denied_workspace not in denied_read_paths:
            denied_read_paths.append(denied_workspace)
    normalized_evidence_roots = tuple(Path(root).resolve(strict=False) for root in evidence_roots)
    for evidence_root in normalized_evidence_roots:
        if home.path.resolve().is_relative_to(evidence_root):
            raise ValueError("disposable Codex home must be outside benchmark evidence roots")
        if evidence_root not in denied_read_paths:
            denied_read_paths.append(evidence_root)
    filesystem_rules.extend(f'{json.dumps(str(path))} = "deny"' for path in denied_read_paths)
    if writable_workspace is not None:
        filesystem_rules.append(f'{json.dumps(str(writable_workspace.resolve()))} = "write"')
    if index_path is None:
        raise ValueError(f"{arm} permission profile requires the locked index")
    canonical_index = _canonical_index_path(index_path)
    coordination_root: Path | None = None
    if arm == "A_plain":
        filesystem_rules.append(f'{json.dumps(str(canonical_index.parent))} = "deny"')
    else:
        coordination_root = home.coordination_path or canonical_index.parent / _COORDINATION_NAME
        if coordination_root.is_symlink():
            raise ValueError("Codemap coordination root must not be a symlink")
        filesystem_rules.append(f'{json.dumps(str(coordination_root))} = "write"')

    explicit_environment = ", ".join(
        f"{name} = {json.dumps(value)}" for name, value in sorted(_shell_environment(home).items())
    )
    config_text = "\n".join(
        [
            f'default_permissions = "{profile}"',
            "",
            "[shell_environment_policy]",
            'inherit = "none"',
            "ignore_default_excludes = false",
            f"set = {{ {explicit_environment} }}",
            "",
            "[permissions]",
            "",
            f"[permissions.{profile}]",
            'description = "Read-only provider parity with isolated Codemap coordination."',
            'extends = ":read-only"',
            "",
            f"[permissions.{profile}.filesystem]",
            *filesystem_rules,
            "",
            f"[permissions.{profile}.network]",
            "enabled = false",
            "",
        ]
    )
    config_path = home.path / "config.toml"
    existing_config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    managed_markers = (
        "default_permissions =",
        "[shell_environment_policy]",
        f"[permissions.{_PLAIN_PERMISSION_PROFILE}]",
        f"[permissions.{_CODEMAP_PERMISSION_PROFILE}]",
    )
    if any(marker in existing_config for marker in managed_markers):
        raise ValueError("disposable Codex home already contains benchmark permission configuration")
    if existing_config.strip():
        config_text = f"{config_text.rstrip()}\n\n{existing_config.lstrip()}"
    config_path.write_text(config_text, encoding="utf-8")
    config_path.chmod(0o600)
    home.permission_profile = profile
    home.coordination_path = coordination_root
    home.denied_read_paths = tuple(denied_read_paths)
    home.evidence_probe_paths = _evidence_probe_paths(normalized_evidence_roots)
    return config_path


def prepare_arm_home(
    arm: str,
    *,
    root: Path | None = None,
    auth_source: Path | None = None,
    codemap_bin: Path | None = None,
    plugin_installer: Callable[[Path], bool | None] | None = None,
) -> ArmHome:
    """Create an isolated ``CODEX_HOME`` implementing A/B/C availability."""
    if not _is_known_codex_arm(arm):
        raise ValueError(f"unknown benchmark arm {arm!r}")
    if root is None:
        try:
            temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        except OSError as exc:
            raise ValueError("default temporary root is unavailable") from exc
    else:
        temp_root = Path(os.path.abspath(root))
        _assert_safe_path_components(temp_root)
    if not temp_root.is_dir():
        raise ValueError("temporary root must be a real directory")
    home = Path(tempfile.mkdtemp(prefix=f"codex-{arm}-", dir=str(temp_root)))
    try:
        home.chmod(0o700)
        config = home / "config.toml"
        config.touch(mode=0o600)
        config.chmod(0o600)
        if auth_source is not None:
            _copy_auth_source(auth_source, home)
        verified = False
        if arm == "B_auto":
            _validated_direct_codemap_launcher(codemap_bin)
            verified = True
        elif arm == "C_strict" and plugin_installer is not None:
            verified = bool(plugin_installer(home))
        env = os.environ.copy()
        # Batch admission values belong to the parent orchestrator, not the
        # Codex process or any measured arm environment.
        for variable in (
            "CODEX_PAID_APPROVAL",
            "CODEX_AUTH_SOURCE",
            "CODEX_RUN_DIR",
            _BENCHMARK_EVIDENCE_ROOTS_ENV,
        ):
            env.pop(variable, None)
        env.pop("CODEMAP_SKILL_FILE", None)
        env["CODEX_HOME"] = str(home)
        env["CODEX_BENCHMARK_ARM"] = arm
        env["CODEX_CODEMAP_AVAILABLE"] = "1" if verified else "0"
        arm_home = ArmHome(
            arm,
            home,
            env,
            verified,
            verified,
            auth_provisioned=auth_source is not None,
        )
        if arm == "B_auto":
            _configure_direct_codemap_launcher(arm_home, codemap_bin)
        return arm_home
    except BaseException:
        _remove_private_directory(home, description="disposable Codex home")
        raise


def probe_arm_home(home: ArmHome | Path, arm: str | None = None) -> dict[str, Any]:
    """Return deterministic isolation evidence, raising on cross-arm mismatch."""
    path = home.path if isinstance(home, ArmHome) else Path(home)
    expected = arm or (home.arm if isinstance(home, ArmHome) else None)
    config = path / "config.toml"
    available = home.codemap_available if isinstance(home, ArmHome) else False
    if expected == "A_plain" and available:
        raise ValueError("A_plain Codex home unexpectedly contains Codemap")
    if expected in {"B_auto", "C_strict"} and not (
        isinstance(home, ArmHome) and home.codemap_available and home.codemap_verified
    ):
        raise ValueError(f"{expected} Codex home requires verified Codemap delivery")
    if isinstance(home, ArmHome):
        skill_file = home.env.get("CODEMAP_SKILL_FILE")
        if expected == "C_strict":
            if home.codemap_skill_path is None or skill_file != str(home.codemap_skill_path.resolve()):
                raise ValueError("C_strict requires the exact installed Skill binding")
        elif skill_file is not None:
            raise ValueError(f"{expected} Codex home unexpectedly exposes CODEMAP_SKILL_FILE")
    return {
        "home": str(path),
        "config": str(config),
        "arm": expected,
        "codemap_available": available,
        "codemap_verified": isinstance(home, ArmHome) and home.codemap_verified,
        "auth_provisioned": isinstance(home, ArmHome) and home.auth_provisioned,
        "authenticated": isinstance(home, ArmHome) and home.authenticated,
        "permission_profile": home.permission_profile if isinstance(home, ArmHome) else "",
        "host_plugins": list(home.host_plugin_names) if isinstance(home, ArmHome) else [],
        "coordination_write_enabled": bool(isinstance(home, ArmHome) and home.coordination_path is not None),
        "codemap_python": (
            home.env.get("CODEMAP_PYTHON") if isinstance(home, ArmHome) and expected in {"B_auto", "C_strict"} else None
        ),
        "codemap_launcher_path": (
            str(home.codemap_launcher_path)
            if isinstance(home, ArmHome) and expected in {"B_auto", "C_strict"} and home.codemap_launcher_path
            else None
        ),
        "codemap_launcher_sha256": (
            home.codemap_launcher_sha256 if isinstance(home, ArmHome) and expected in {"B_auto", "C_strict"} else ""
        ),
        "codemap_context_path": (
            str(home.codemap_context_path)
            if isinstance(home, ArmHome) and expected == "C_strict" and home.codemap_context_path
            else None
        ),
        "codemap_context_sha256": (
            home.codemap_context_sha256 if isinstance(home, ArmHome) and expected == "C_strict" else ""
        ),
        "codemap_skill_path": (
            str(home.codemap_skill_path)
            if isinstance(home, ArmHome) and expected == "C_strict" and home.codemap_skill_path
            else None
        ),
        "codemap_skill_sha256": (
            home.codemap_skill_sha256 if isinstance(home, ArmHome) and expected == "C_strict" else ""
        ),
        "codemap_skill_file": (
            home.env.get("CODEMAP_SKILL_FILE") if isinstance(home, ArmHome) and expected == "C_strict" else None
        ),
        "codex_rig_path": (
            str(home.codex_rig_path)
            if isinstance(home, ArmHome) and expected == "C_strict" and home.codex_rig_path
            else None
        ),
        "codex_rig_manifest_sha256": (
            home.codex_rig_manifest_sha256 if isinstance(home, ArmHome) and expected == "C_strict" else ""
        ),
        "network_access": False,
        "config_mode": stat.S_IMODE(config.stat().st_mode),
    }


def _invoke_plugin_command(
    command: list[str],
    env: Mapping[str, str],
    command_runner: Callable[..., Any] | None = None,
    *,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """Run a no-model Codex plugin command through an injectable seam."""
    runner = command_runner or subprocess.run
    kwargs: dict[str, Any] = {
        "env": dict(env),
        "capture_output": True,
        "text": True,
        "check": False,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    try:
        completed = runner(command, **kwargs)
    except TypeError:
        completed = runner(command, dict(env))
    if isinstance(completed, tuple):
        code, stdout, stderr = (list(completed) + ["", ""])[:3]
        return int(code), str(stdout), str(stderr)
    return (
        int(getattr(completed, "returncode", 1)),
        str(getattr(completed, "stdout", "") or ""),
        str(getattr(completed, "stderr", "") or ""),
    )


def _verify_locked_codemap_python(
    *,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    command_runner: Callable[..., Any] | None = None,
) -> str:
    """Resolve and validate a Python matching the manifest's treatment runtime."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        runtime = manifest["codex_permission_profiles"]["treatment_runtime"]
        required_major_minor = tuple(runtime["required_major_minor"])
        scope = runtime["scope"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity treatment runtime is unavailable or malformed") from exc
    if required_major_minor != (3, 11) or scope != ["B_auto", "C_strict"]:
        raise ValueError("provider-parity treatment runtime contract does not match the active manifest")
    configured = runtime.get("environment", {}).get("CODEMAP_PYTHON")
    candidates = [
        configured,
        shutil.which("python3.11"),
        sys.executable,
        shutil.which("python3"),
        shutil.which("python"),
    ]
    checked: set[Path] = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        path = Path(candidate).resolve()
        if path in checked or not path.is_file() or not os.access(path, os.X_OK):
            continue
        checked.add(path)
        code, stdout, stderr = _invoke_plugin_command(
            [str(path), "--version"],
            {},
            command_runner=command_runner,
        )
        version_match = re.search(r"(\d+)\.(\d+)(?:\.\d+)?", f"{stdout}\n{stderr}")
        found_major_minor = tuple(int(part) for part in version_match.groups()) if version_match else ()
        if code == 0 and found_major_minor == required_major_minor:
            return str(path)
    required = ".".join(str(part) for part in required_major_minor)
    raise ValueError(f"Codemap treatment runtime requires an executable Python {required}")


def _verify_permission_profile(
    home: ArmHome,
    repo_path: Path,
    index_path: Path | None = None,
    command_runner: Callable[..., Any] | None = None,
    *,
    writable_workspace: Path | None = None,
) -> None:
    """Prove the selected profile denies secrets/source and permits only coordination."""
    sandbox_environment = _shell_environment(home)
    code, stdout, stderr = _invoke_plugin_command(
        [_CODEX_BIN, "--version"],
        sandbox_environment,
        command_runner=command_runner,
    )
    if code != 0 or not f"{stdout}\n{stderr}".strip():
        raise ValueError("Codex permission-profile version probe failed")

    profile = home.permission_profile or (
        _PLAIN_PERMISSION_PROFILE if home.arm == "A_plain" else _CODEMAP_PERMISSION_PROFILE
    )
    # An activated project virtualenv may expose a workspace symlink even when
    # the running interpreter itself lives outside the protected source tree.
    probe_python = str(Path(sys.executable).resolve())
    sandbox_command = [
        _CODEX_BIN,
        "sandbox",
        "-P",
        profile,
        "--include-managed-config",
        "-C",
        str(repo_path),
        "--",
    ]
    sandbox_prefix = [
        *sandbox_command,
        probe_python,
        "-c",
    ]
    code, _stdout, error = _invoke_plugin_command(
        [*sandbox_prefix, "pass"],
        sandbox_environment,
        command_runner=command_runner,
    )
    if code != 0:
        raise ValueError(f"Codex permission profile is unsupported or rejected: {error[:200]}")

    if home.arm != "A_plain" and home.codemap_available:
        codemap_bin = home.env.get("CODEMAP_BIN")
        codemap_python = home.env.get("CODEMAP_PYTHON")
        if not codemap_bin or not codemap_python:
            raise ValueError("Codemap permission profile lacks staged runtime paths")
        code, _stdout, error = _invoke_plugin_command(
            [*sandbox_command, codemap_bin, "--help"],
            sandbox_environment,
            command_runner=command_runner,
        )
        if code != 0:
            raise ValueError(f"Codex permission profile denied staged Codemap runtime: {error[:200]}")

    source_probe = repo_path / f".codex-parity-write-{uuid4().hex}"
    write_script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'probe')"
    code, _stdout, _stderr = _invoke_plugin_command(
        [*sandbox_prefix, write_script, str(source_probe)],
        sandbox_environment,
        command_runner=command_runner,
    )
    if writable_workspace is None:
        if code == 0 or source_probe.exists():
            source_probe.unlink(missing_ok=True)
            raise ValueError("Codex permission profile allowed a source-tree write")
    elif code != 0 or not source_probe.is_file():
        raise ValueError("Codex permission profile denied benchmark-workspace writes")
    source_probe.unlink(missing_ok=True)

    read_script = "from pathlib import Path; import sys; Path(sys.argv[1]).read_bytes()"
    auth_path = home.path / "auth.json"
    if auth_path.exists():
        code, probe_stdout, probe_stderr = _invoke_plugin_command(
            [*sandbox_prefix, read_script, str(auth_path)],
            sandbox_environment,
            command_runner=command_runner,
        )
        if code == 0:
            raise ValueError("Codex permission profile allowed credential reads")
        auth_bytes = auth_path.read_bytes()
        combined_output = (probe_stdout + probe_stderr).encode("utf-8", errors="replace")
        if auth_bytes and auth_bytes in combined_output:
            raise ValueError("Codex permission probe disclosed credential material")

    enumerate_script = "from pathlib import Path; import sys; next(Path(sys.argv[1]).iterdir(), None)"
    for denied_root in home.denied_read_paths:
        if not denied_root.exists():
            continue
        code, probe_stdout, _probe_stderr = _invoke_plugin_command(
            [*sandbox_prefix, enumerate_script, str(denied_root)],
            sandbox_environment,
            command_runner=command_runner,
        )
        if code == 0 or probe_stdout:
            raise ValueError("Codex permission profile allowed host tooling discovery")

    for evidence_probe in home.evidence_probe_paths:
        code, probe_stdout, _probe_stderr = _invoke_plugin_command(
            [*sandbox_prefix, read_script, str(evidence_probe)],
            sandbox_environment,
            command_runner=command_runner,
        )
        if code == 0 or probe_stdout:
            raise ValueError("Codex permission profile allowed benchmark evaluator evidence reads")

    if index_path is not None:
        code, _stdout, error = _invoke_plugin_command(
            [*sandbox_prefix, read_script, str(index_path)],
            sandbox_environment,
            command_runner=command_runner,
        )
        if home.arm == "A_plain" and code == 0:
            raise ValueError("A_plain permission profile allowed locked-index reads")
        if home.arm != "A_plain" and code != 0:
            raise ValueError(f"Codemap permission profile denied locked-index reads: {error[:200]}")

    if home.coordination_path is not None:
        coordination_probe = home.coordination_path / f".codex-parity-allow-{uuid4().hex}"
        code, _stdout, error = _invoke_plugin_command(
            [*sandbox_prefix, write_script, str(coordination_probe)],
            sandbox_environment,
            command_runner=command_runner,
        )
        if code != 0 or not coordination_probe.is_file():
            raise ValueError(f"Codex permission profile denied coordination writes: {error[:200]}")
        coordination_probe.unlink()
        _validate_coordination_root(home.coordination_path)


def _verify_authentication(
    home: ArmHome,
    command_runner: Callable[..., Any] | None = None,
) -> None:
    """Prove the disposable home is authenticated without retaining command output."""
    returncode, _stdout, _stderr = _invoke_plugin_command(
        ["codex", "login", "status"],
        home.env,
        command_runner,
    )
    if returncode != 0:
        raise RuntimeError("disposable Codex home is not authenticated")
    home.authenticated = True


_enabled_plugin_names = plugin_registration.enabled_plugin_names
_registered_plugin_tables = plugin_registration.registered_plugin_tables


def _plugin_enabled(plugin_json: str, plugin_name: str) -> bool:
    """Return whether one exact plugin appears enabled in ``codex plugin list --json``."""
    return plugin_name.lower() in _enabled_plugin_names(plugin_json)


def _plugin_listing_evidence(home: ArmHome, code: int, stdout: str, stderr: str) -> str:
    """Summarize one ``codex plugin list`` result for a fail-closed registration error."""
    return plugin_registration.plugin_listing_evidence(home.path / "config.toml", code, stdout, stderr)


def _verify_installed_plugin_pair(
    home: ArmHome,
    *,
    codex_bin: str = _CODEX_BIN,
    command_runner: Callable[..., Any] | None = None,
) -> None:
    """Require the reviewed C plugin pair among this home's own registrations."""
    code, stdout, stderr = _invoke_plugin_command(
        [codex_bin, "plugin", "list", "--json"],
        home.env,
        command_runner,
    )
    admitted, host_plugins = plugin_registration.treatment_admission(home.path / "config.toml", code, stdout)
    if not admitted:
        raise RuntimeError(
            f"final Codex plugin registration is invalid: {_plugin_listing_evidence(home, code, stdout, stderr)}"
        )
    home.host_plugin_names = host_plugins


def _configure_codemap_launcher(home: ArmHome, install_json: str) -> None:
    """Validate and expose the exact launcher reported by Codex plugin install."""
    try:
        payload = json.loads(install_json)
        raw_installed_path = payload["installedPath"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Codemap plugin install did not report installedPath") from exc
    if not isinstance(raw_installed_path, str) or not raw_installed_path:
        raise RuntimeError("Codemap plugin installedPath must be a non-empty string")

    installed_path = Path(os.path.abspath(raw_installed_path))
    _assert_safe_path_components(installed_path)
    try:
        installed_path = installed_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Codemap plugin installedPath is unavailable") from exc
    home_root = home.path.resolve(strict=True)
    if not installed_path.is_relative_to(home_root):
        raise RuntimeError("Codemap plugin installedPath escaped the disposable CODEX_HOME")

    plugin_manifest = installed_path / ".codex-plugin" / "plugin.json"
    launcher = installed_path / "bin" / "codemap-py"
    query_skill = installed_path / "codex-skills" / "query-code" / "SKILL.md"
    _assert_safe_path_components(plugin_manifest)
    _assert_safe_path_components(launcher)
    _assert_safe_path_components(query_skill)
    try:
        manifest_metadata = plugin_manifest.lstat()
        launcher_metadata = launcher.lstat()
        skill_metadata = query_skill.lstat()
        manifest_payload = json.loads(plugin_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codemap plugin launcher or manifest is unavailable") from exc
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or plugin_manifest.is_symlink()
        or manifest_metadata.st_nlink != 1
        or manifest_payload.get("name") != "codemap-py"
    ):
        raise RuntimeError("Codemap plugin manifest identity is invalid")
    if (
        not stat.S_ISREG(launcher_metadata.st_mode)
        or launcher.is_symlink()
        or launcher_metadata.st_nlink != 1
        or not os.access(launcher, os.X_OK)
    ):
        raise RuntimeError("Codemap plugin launcher must be a regular executable")
    if not stat.S_ISREG(skill_metadata.st_mode) or query_skill.is_symlink() or skill_metadata.st_nlink != 1:
        raise RuntimeError("Codemap query skill must be a regular file")

    resolved_launcher = launcher.resolve(strict=True)
    if not resolved_launcher.is_relative_to(installed_path):
        raise RuntimeError("Codemap plugin launcher escaped installedPath")
    home.env["CODEMAP_BIN"] = str(resolved_launcher)
    home.codemap_plugin_path = installed_path
    home.codemap_plugin_manifest_sha256 = hashlib.sha256(plugin_manifest.read_bytes()).hexdigest()
    home.codemap_launcher_path = resolved_launcher
    home.codemap_launcher_sha256 = hashlib.sha256(resolved_launcher.read_bytes()).hexdigest()
    home.codemap_skill_path = query_skill.resolve(strict=True)
    home.codemap_skill_sha256 = hashlib.sha256(home.codemap_skill_path.read_bytes()).hexdigest()
    home.env["CODEMAP_SKILL_FILE"] = str(home.codemap_skill_path)


def _configure_codex_rig_plugin(home: ArmHome, install_json: str) -> None:
    """Lock the exact Codex Rig plugin installed for the skill-required arm."""
    try:
        payload = json.loads(install_json)
        raw_installed_path = payload["installedPath"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Codex Rig plugin install did not report installedPath") from exc
    if not isinstance(raw_installed_path, str) or not raw_installed_path:
        raise RuntimeError("Codex Rig plugin installedPath must be a non-empty string")

    installed_path = Path(os.path.abspath(raw_installed_path))
    _assert_safe_path_components(installed_path)
    try:
        installed_path = installed_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Codex Rig plugin installedPath is unavailable") from exc
    home_root = home.path.resolve(strict=True)
    if not installed_path.is_relative_to(home_root):
        raise RuntimeError("Codex Rig plugin installedPath escaped the disposable CODEX_HOME")

    plugin_manifest = installed_path / ".codex-plugin" / "plugin.json"
    adapter = installed_path / "shared" / "codemap_adapter.py"
    _assert_safe_path_components(plugin_manifest)
    _assert_safe_path_components(adapter)
    try:
        manifest_metadata = plugin_manifest.lstat()
        adapter_metadata = adapter.lstat()
        manifest_payload = json.loads(plugin_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex Rig plugin manifest is unavailable") from exc
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or plugin_manifest.is_symlink()
        or manifest_metadata.st_nlink != 1
        or manifest_payload.get("name") != "codex-rig"
    ):
        raise RuntimeError("Codex Rig plugin manifest identity is invalid")
    if not stat.S_ISREG(adapter_metadata.st_mode) or adapter.is_symlink() or adapter_metadata.st_nlink != 1:
        raise RuntimeError("Codex Rig adapter must be a regular file")
    home.codex_rig_path = installed_path
    home.codex_rig_manifest_sha256 = hashlib.sha256(plugin_manifest.read_bytes()).hexdigest()
    home.codex_rig_adapter_path = adapter.resolve(strict=True)
    home.codex_rig_adapter_sha256 = hashlib.sha256(home.codex_rig_adapter_path.read_bytes()).hexdigest()


class TreatmentArtifactLockError(ValueError):
    """Report stale local treatment bytes without obscuring the safe recovery."""


def _verify_treatment_artifact_locks(home: ArmHome, manifest_path: Path) -> None:
    """Require installed treatment files and versions to match the reviewed manifest locks."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        hashes = manifest["artifact_sha256"]
        codemap_version = str(manifest["codemap_candidate"]["version"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Codex treatment manifest is missing artifact locks") from exc
    expected_launcher = hashes.get("codemap_runtime_cli") if isinstance(hashes, Mapping) else None
    if not isinstance(expected_launcher, str) or home.codemap_launcher_sha256 != expected_launcher:
        raise ValueError("Codemap launcher does not match the locked runtime artifact")
    if home.arm == "B_auto":
        try:
            runtime_lock = manifest["direct_cli_runtime"]
            expected_files = runtime_lock["files"]
            expected_aggregate = runtime_lock["aggregate_sha256"]
            staged_root = home.codemap_launcher_path.parent.parent
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError("direct CLI runtime closure lock is missing") from exc
        observed_files = _runtime_file_hashes(staged_root)
        if not isinstance(expected_files, Mapping) or observed_files != dict(expected_files):
            raise ValueError("staged direct CLI runtime does not match the locked file closure")
        if not isinstance(expected_aggregate, str) or _aggregate_file_hashes(observed_files) != expected_aggregate:
            raise ValueError("staged direct CLI runtime aggregate does not match the manifest")
        return
    if home.codemap_skill_path is None or home.env.get("CODEMAP_SKILL_FILE") != str(home.codemap_skill_path.resolve()):
        raise ValueError("installed Codemap Skill binding does not match the locked path")
    try:
        codex_rig_version = str(manifest["codex_rig_candidate"]["version"])
        expected = {
            "codemap_candidate_manifest": home.codemap_plugin_manifest_sha256,
            "codemap_query_skill": home.codemap_skill_sha256,
            "codex_rig_plugin_manifest": home.codex_rig_manifest_sha256,
            "codex_rig_adapter": home.codex_rig_adapter_sha256,
        }
        codemap_manifest = json.loads(
            (home.codemap_plugin_path / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        rig_manifest = json.loads((home.codex_rig_path / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (AttributeError, OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("installed Codemap/Codex Rig artifact identity is incomplete") from exc
    observed_versions = {
        "codemap-py": str(codemap_manifest.get("version")),
        "codex-rig": str(rig_manifest.get("version")),
    }
    expected_versions = {"codemap-py": codemap_version, "codex-rig": codex_rig_version}
    version_drift = {
        name: (expected_versions[name], observed_versions[name])
        for name in expected_versions
        if expected_versions[name] != observed_versions[name]
    }
    if version_drift:
        raise TreatmentArtifactLockError(_treatment_artifact_version_mismatch_message(version_drift))
    for artifact_name, observed_sha256 in expected.items():
        expected_sha256 = hashes.get(artifact_name) if isinstance(hashes, Mapping) else None
        if not isinstance(expected_sha256, str) or observed_sha256 != expected_sha256:
            raise TreatmentArtifactLockError(_treatment_artifact_lock_mismatch_message(artifact_name))


def _treatment_artifact_lock_mismatch_message(artifact_name: str) -> str:
    """Explain how to refresh a stale local treatment lock without weakening provenance."""
    return (
        f"installed treatment artifact does not match lock: {artifact_name}. "
        "The local treatment bytes changed after `benchmarks/manifests/codex-integration.json` was generated; "
        "no paid model call was started. Refresh the lock with "
        "`uv run python benchmarks/build-codex-integration-manifest.py`, then resolve a new scope for the same study, "
        "repository, model, and task IDs. Do not reuse the previous --paid-approval value. "
        "If Codex Rig edits are still in progress, regenerate only after the intended local bytes are ready."
    )


def _treatment_artifact_version_mismatch_message(version_drift: Mapping[str, tuple[str, str]]) -> str:
    """Explain how to relock reviewed local plugin versions before a paid retry."""
    observed = ", ".join(
        f"{name}: manifest={expected}, installed={installed}"
        for name, (expected, installed) in sorted(version_drift.items())
    )
    return (
        f"installed treatment version differs from the active manifest ({observed}). "
        "No paid model call was started. The local plugin changed after the treatment manifest was generated. "
        "When the intended local plugin bytes are ready, run "
        "`uv run python benchmarks/build-codex-integration-manifest.py`, then resolve a new scope for the same study, "
        "repository, model, and task IDs. Do not reuse the previous --paid-approval value."
    )


def _admit_installed_skill_pair(
    home: ArmHome,
    repo_path: Path,
    index_path: Path,
    *,
    manifest_path: Path,
    command_runner: Callable[..., Any] | None = None,
) -> None:
    """Run the installed Codex Rig adapter once and persist verified C admission context."""
    if home.arm != "C_strict" or home.codex_rig_adapter_path is None or home.codemap_plugin_path is None:
        raise ValueError("installed-skill admission requires a locked C skill home")
    if home.codemap_launcher_path is None or not home.env.get("CODEMAP_PYTHON"):
        raise ValueError("installed-skill admission requires locked Codemap runtime paths")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        admission = manifest["codex_rig_integration_admission"]
        category = admission["probe_category"]
        target = admission["probe_target"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Codex treatment manifest is missing installed-skill admission controls") from exc
    if category != "analysis" or not isinstance(target, str) or not target:
        raise ValueError("Codex treatment manifest has invalid installed-skill admission controls")
    root = repo_path.resolve(strict=True)
    locked_index = _canonical_index_path(index_path)
    context_path = home.path.resolve(strict=True) / "codemap-context.json"
    command = [
        home.env["CODEMAP_PYTHON"],
        str(home.codex_rig_adapter_path),
        "context",
        "--category",
        category,
        "--target",
        target,
        "--root",
        str(root),
        "--out",
        str(context_path),
    ]
    code, _stdout, stderr = _invoke_plugin_command(
        command,
        _shell_environment(home),
        command_runner,
        cwd=root,
    )
    if code != 0:
        raise RuntimeError(f"installed Codex Rig context admission failed: {stderr[:300]}")
    _assert_safe_path_components(context_path)
    try:
        metadata = context_path.lstat()
        payload = json.loads(context_path.read_text(encoding="utf-8"))
        probe = payload["probe"]
        doctor = probe["doctor"]
        queries = payload["queries"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed Codex Rig context admission produced no valid context") from exc
    if not stat.S_ISREG(metadata.st_mode) or context_path.is_symlink() or metadata.st_nlink != 1:
        raise RuntimeError("installed Codex Rig context artifact must be a regular file")
    query_evidence_valid = (
        isinstance(queries, list)
        and bool(queries)
        and all(
            isinstance(query, Mapping)
            and query.get("exit_code") == 0
            and query.get("error") is None
            and query.get("query_complete") is True
            for query in queries
        )
    )
    checks = {
        "protocol": payload.get("protocol_version") == "codemap-py.integration.v1",
        "target": payload.get("target") == target,
        "context_status": payload.get("status") in {"available", "degraded"},
        "probe_status": probe.get("status") == "available",
        "launcher": probe.get("launcher") == str(home.codemap_launcher_path),
        "plugin_root": doctor.get("plugin_root") == str(home.codemap_plugin_path),
        "index_path": doctor.get("index_path") == str(locked_index),
        "queries": query_evidence_valid,
    }
    failed_checks = [name for name, passed in checks.items() if not passed]
    if failed_checks:
        raise RuntimeError("installed Codex Rig context admission failed checks: " + ", ".join(failed_checks))
    home.codemap_context_path = context_path.resolve(strict=True)
    home.codemap_context_sha256 = hashlib.sha256(home.codemap_context_path.read_bytes()).hexdigest()


def _admit_staged_direct_cli(
    home: ArmHome,
    repo_path: Path,
    index_path: Path,
    *,
    manifest_path: Path,
    command_runner: Callable[..., Any] | None = None,
) -> None:
    """Execute one task-shaped compact query through B's staged CLI runtime."""
    if home.arm != "B_auto" or home.codemap_launcher_path is None:
        raise ValueError("direct CLI admission requires a locked B runtime")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        admission = manifest["direct_cli_admission"]
        subcommand = admission["probe_subcommand"]
        target = admission["probe_target"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("direct CLI admission contract is missing") from exc
    if subcommand != "fn-rdeps" or not isinstance(target, str) or "::" not in target:
        raise ValueError("direct CLI admission query is not task-shaped")

    index_sha256 = hashlib.sha256(index_path.read_bytes()).hexdigest()
    profile = home.permission_profile or _CODEMAP_PERMISSION_PROFILE
    command = [
        _CODEX_BIN,
        "sandbox",
        "-P",
        profile,
        "--include-managed-config",
        "-C",
        str(repo_path),
        "--",
        str(home.codemap_launcher_path),
        "query",
        "--compact",
        subcommand,
        target,
    ]
    code, stdout, stderr = _invoke_plugin_command(
        command,
        _shell_environment(home),
        command_runner=command_runner,
    )
    output_item = {"aggregated_output": stdout}
    if code != 0 or not runtime._query_output_complete(output_item):
        detail = stderr.strip() or stdout.strip()
        raise RuntimeError(f"staged direct CLI admission query failed: {detail[:300]}")
    if hashlib.sha256(index_path.read_bytes()).hexdigest() != index_sha256:
        raise RuntimeError("staged direct CLI admission mutated the locked index")


def _validated_direct_codemap_launcher(codemap_bin: Path | None) -> Path:
    """Return a directly supplied regular Codemap launcher without plugin discovery."""
    if codemap_bin is None:
        raise ValueError("B_auto requires --codemap-bin")
    launcher = Path(codemap_bin)
    if not launcher.is_absolute():
        raise ValueError("--codemap-bin must be an absolute path")
    _assert_safe_path_components(launcher)
    try:
        metadata = launcher.lstat()
        resolved_launcher = launcher.resolve(strict=True)
    except OSError as exc:
        raise ValueError("--codemap-bin is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or launcher.is_symlink()
        or metadata.st_nlink != 1
        or not os.access(resolved_launcher, os.X_OK)
    ):
        raise ValueError("--codemap-bin must be a regular executable")
    return resolved_launcher


def _direct_runtime_files(source_root: Path) -> dict[str, Path]:
    """Return the exact source files required by the isolated direct CLI."""
    relative_paths = [
        Path("bin/codemap-py"),
        Path("bin/_exclusions.py"),
        Path("scripts/codemap_py_entry.py"),
        *sorted(path.relative_to(source_root) for path in (source_root / "src" / "codemap_py").rglob("*.py")),
    ]
    files: dict[str, Path] = {}
    resolved_root = source_root.resolve(strict=True)
    for relative_path in relative_paths:
        path = source_root / relative_path
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("--codemap-bin runtime bundle is incomplete") from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not resolved.is_relative_to(resolved_root):
            raise ValueError("--codemap-bin runtime bundle contains an unsafe path")
        files[relative_path.as_posix()] = resolved
    return files


def _runtime_file_hashes(runtime_root: Path) -> dict[str, str]:
    """Hash the exact files present in a staged direct CLI runtime."""
    return {
        path.relative_to(runtime_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(runtime_root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _aggregate_file_hashes(hashes: Mapping[str, str]) -> str:
    """Return a stable aggregate identity for a relative-path hash mapping."""
    payload = "".join(f"{path}\0{sha256}\n" for path, sha256 in sorted(hashes.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _archive_snapshot_file(
    source: Path,
    destination: Path,
    *,
    role: str,
    archive_root: Path,
    source_root: Path | None = None,
    entries: list[dict[str, Any]],
) -> None:
    """Copy one verified non-secret input and append its deterministic identity."""
    _assert_safe_path_components(source)
    metadata = source.lstat()
    resolved = source.resolve(strict=True)
    if source.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(f"snapshot source must be a regular single-link file: {source}")
    if source_root is not None and not resolved.is_relative_to(source_root.resolve(strict=True)):
        raise ValueError(f"snapshot source escaped its locked root: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(source_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise ValueError(f"snapshot source changed while being opened: {source}")
        destination_mode = 0o700 if stat.S_IMODE(opened.st_mode) & 0o111 else 0o600
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, destination_mode)
        _apply_private_mode(destination, destination_fd, destination_mode)
        with os.fdopen(source_fd, "rb") as source_handle:
            source_fd = None
            with os.fdopen(destination_fd, "wb") as destination_handle:
                destination_fd = None
                shutil.copyfileobj(source_handle, destination_handle)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise ValueError(f"snapshot source could not be copied securely: {source}") from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    payload = destination.read_bytes()
    entries.append(
        {
            "role": role,
            "archived_path": destination.relative_to(archive_root).as_posix(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            # Windows does not expose the POSIX mode bits used by the ledger; retain
            # the logical mode selected from the verified source instead.
            "mode": destination_mode,
        }
    )


def _archive_snapshot_tree(
    source_root: Path,
    destination_root: Path,
    *,
    role: str,
    entries: list[dict[str, Any]],
) -> None:
    """Archive runtime files while excluding private evaluator, cache, plan, and test trees."""
    root = source_root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"snapshot package root must be a real directory: {source_root}")
    excluded_parts = {".cache", ".git", ".plans", ".reports", "__pycache__", "test", "tests"}
    for source in sorted(root.rglob("*")):
        if (
            not source.is_file()
            or source.is_symlink()
            or source.suffix == ".pyc"
            or excluded_parts.intersection(source.relative_to(root).parts)
        ):
            continue
        relative = source.relative_to(root)
        _archive_snapshot_file(
            source,
            destination_root / relative,
            role=role,
            archive_root=destination_root.parent.parent,
            source_root=root,
            entries=entries,
        )


def _write_frozen_marketplace(snapshot_root: Path, arm: str, entries: list[dict[str, Any]]) -> Path:
    """Write and ledger the fixed local marketplace for one archived C plugin pair."""
    marketplace_manifest = snapshot_root / arm / ".agents" / "plugins" / "marketplace.json"
    marketplace_manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": _FROZEN_MARKETPLACE_NAME,
        "plugins": [
            {"name": "codemap-py", "source": {"source": "local", "path": "./codemap-py"}},
            {"name": "codex-rig", "source": {"source": "local", "path": "./codex-rig"}},
        ],
    }
    serialized = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    marketplace_manifest.write_bytes(serialized)
    marketplace_manifest.chmod(0o600)
    entries.append(
        {
            "role": f"{arm}:marketplace",
            "archived_path": marketplace_manifest.relative_to(snapshot_root).as_posix(),
            "sha256": hashlib.sha256(serialized).hexdigest(),
            "bytes": len(serialized),
            "mode": 0o600,
        }
    )
    return marketplace_manifest


def _write_input_snapshot(
    snapshot_root: Path,
    *,
    manifest_path: Path,
    tasks_path: Path,
    runner_path: Path,
    invocation_launcher_path: Path | None = None,
    index_path: Path | None,
    auth_source: Path | None,
    arm_archives: Mapping[str, Mapping[str, Path]],
    arm_files: Mapping[str, Mapping[str, Path]] | None = None,
    additional_shared_files: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Write immutable launch inputs without copying credential bytes."""
    if snapshot_root.exists():
        raise FileExistsError(snapshot_root)
    snapshot_root.mkdir(parents=True, mode=0o700)
    entries: list[dict[str, Any]] = []
    shared = snapshot_root / "shared"
    for role, source, relative in (
        ("manifest", manifest_path, Path("manifest.json")),
        ("task_suite", tasks_path, Path(tasks_path.name)),
        ("runner", runner_path, Path(runner_path.name)),
    ):
        _archive_snapshot_file(source, shared / relative, role=role, archive_root=snapshot_root, entries=entries)
    # The ``runner`` row above archives the entrypoint, which is now a re-export shim. Freezing it
    # alone would leave the bundle without the code that actually produced the run, so the
    # implementation package is archived alongside it under a new role. Additive: the existing
    # ``runner`` entry keeps its role, name, and meaning.
    _archive_snapshot_tree(
        PACKAGE_DIR,
        shared / "runner_package",
        role="runner_package",
        entries=entries,
    )
    for relative, source in sorted((additional_shared_files or {}).items()):
        _archive_snapshot_file(
            source,
            shared / relative,
            role=f"shared:{relative}",
            archive_root=snapshot_root,
            entries=entries,
        )
    if invocation_launcher_path is not None and invocation_launcher_path.resolve() != runner_path.resolve():
        _archive_snapshot_file(
            invocation_launcher_path,
            shared / invocation_launcher_path.name,
            role="invocation_launcher",
            archive_root=snapshot_root,
            entries=entries,
        )
    if index_path is not None:
        _archive_snapshot_file(
            index_path,
            shared / "locked-index.json",
            role="locked_index",
            archive_root=snapshot_root,
            entries=entries,
        )
    files_by_arm = arm_files or {}
    for arm in sorted(set(arm_archives) | set(files_by_arm)):
        for relative, source in sorted(files_by_arm.get(arm, {}).items()):
            _archive_snapshot_file(
                source,
                snapshot_root / arm / relative,
                role=f"{arm}:{relative}",
                archive_root=snapshot_root,
                entries=entries,
            )
        for package_role, root in sorted(arm_archives.get(arm, {}).items()):
            _archive_snapshot_tree(
                root, snapshot_root / arm / package_role, role=f"{arm}:{package_role}", entries=entries
            )
        if arm == "C_strict":
            _write_frozen_marketplace(snapshot_root, arm, entries)

    auth_metadata: dict[str, Any] | None = {"supplied": True, "archived": False} if auth_source is not None else None

    entries.sort(key=lambda item: (str(item["role"]), str(item["archived_path"])))
    payload = {
        "schema_version": "codex-structural-input-snapshot-v1",
        "files": entries,
        "auth_source": auth_metadata,
    }
    snapshot_path = snapshot_root / "input-snapshot.json"
    serialized = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    snapshot_path.write_bytes(serialized)
    snapshot_path.chmod(0o600)
    payload["path"] = str(snapshot_path.resolve())
    payload["sha256"] = hashlib.sha256(serialized).hexdigest()
    payload["bytes"] = len(serialized)
    return payload


def _validate_invocation_launcher(path: Path, expected_sha256: str) -> None:
    """Require the executing paid launcher to remain the locked regular file."""
    try:
        metadata = path.lstat()
        observed_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"invocation launcher is unavailable: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_nlink != 1:
        raise ValueError(f"invocation launcher is not a private regular file: {path}")
    if observed_sha256 != expected_sha256:
        raise ValueError(f"invocation launcher changed: expected {expected_sha256}, observed {observed_sha256}")


def _configure_direct_codemap_launcher(home: ArmHome, codemap_bin: Path | None) -> None:
    """Stage the direct CLI runtime inside B's disposable home and expose it."""
    source_launcher = _validated_direct_codemap_launcher(codemap_bin)
    source_root = source_launcher.parent.parent
    if source_launcher.parent.name != "bin" or source_launcher.name != "codemap-py":
        raise ValueError("--codemap-bin must use the Codemap runtime layout")
    source_files = _direct_runtime_files(source_root)

    # Only the CLI closure is staged: no plugin manifest, skill, marketplace,
    # or Codex Rig bytes enter B's model-visible home.
    staged_root = home.path / "direct-cli"
    staged_launcher = staged_root / "bin" / "codemap-py"
    for relative_path, source in source_files.items():
        destination = staged_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    staged_launcher.chmod(source_launcher.stat().st_mode & 0o777)
    source_hashes = {
        relative_path: hashlib.sha256(source.read_bytes()).hexdigest() for relative_path, source in source_files.items()
    }
    if _runtime_file_hashes(staged_root) != source_hashes:
        raise RuntimeError("staged Codemap runtime differs from its locked source closure")
    home.env["CODEMAP_BIN"] = str(staged_launcher)
    home.codemap_launcher_path = staged_launcher
    home.codemap_launcher_sha256 = hashlib.sha256(staged_launcher.read_bytes()).hexdigest()


def _install_codemap_plugin(
    home: ArmHome,
    marketplace_root: Path | None,
    *,
    plugin_sources: Mapping[str, Path] | None = None,
    codex_bin: str = _CODEX_BIN,
    command_runner: Callable[..., Any] | None = None,
) -> bool:
    """Install Codemap and Codex Rig through an admitted local marketplace."""
    if plugin_sources is None:
        if marketplace_root is None:
            return False
        marketplace_root = marketplace_root.resolve()
        marketplace_manifest = marketplace_root / ".agents" / "plugins" / "marketplace.json"
        if not marketplace_manifest.is_file():
            raise RuntimeError(
                "Codemap plugin source must be a marketplace root containing .agents/plugins/marketplace.json"
            )
        setup_commands = [[codex_bin, "plugin", "marketplace", "add", str(marketplace_root)]]
        add_plugin = [codex_bin, "plugin", "add", "codemap-py@borda-ai-rig", "--json"]
        add_codex_rig = [codex_bin, "plugin", "add", "codex-rig@borda-ai-rig", "--json"]
    else:
        expected_sources = {"codemap-py", "codex-rig"}
        if set(plugin_sources) != expected_sources:
            raise ValueError(f"runtime plugin source set must be {sorted(expected_sources)}")
        if marketplace_root is None:
            raise ValueError("runtime plugin sources require a frozen local marketplace")
        marketplace_root = marketplace_root.resolve(strict=True)
        marketplace_manifest = marketplace_root / ".agents" / "plugins" / "marketplace.json"
        try:
            manifest = json.loads(marketplace_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("frozen runtime marketplace is unavailable or malformed") from exc
        expected_plugins = [
            {"name": "codemap-py", "source": {"source": "local", "path": "./codemap-py"}},
            {"name": "codex-rig", "source": {"source": "local", "path": "./codex-rig"}},
        ]
        if not isinstance(manifest, Mapping) or manifest.get("name") != _FROZEN_MARKETPLACE_NAME:
            raise ValueError("frozen runtime marketplace name drifted")
        if manifest.get("plugins") != expected_plugins:
            raise ValueError("frozen runtime marketplace schema drifted")
        for name, source in plugin_sources.items():
            if Path(source).resolve(strict=True) != (marketplace_root / name).resolve(strict=True):
                raise ValueError(f"frozen runtime marketplace source drifted for {name}")
        setup_commands = [[codex_bin, "plugin", "marketplace", "add", str(marketplace_root)]]
        add_plugin = [codex_bin, "plugin", "add", f"codemap-py@{_FROZEN_MARKETPLACE_NAME}", "--json"]
        add_codex_rig = [codex_bin, "plugin", "add", f"codex-rig@{_FROZEN_MARKETPLACE_NAME}", "--json"]
    list_plugins = [codex_bin, "plugin", "list", "--json"]
    codex_rig_install_json = ""
    install_json = ""
    for command in (*setup_commands, add_plugin, add_codex_rig):
        code, stdout, stderr = _invoke_plugin_command(command, home.env, command_runner)
        if code != 0:
            raise RuntimeError(f"Codemap plugin setup failed ({' '.join(command[1:4])}): {stderr[:300]}")
        if command is add_plugin:
            install_json = stdout
        elif command is add_codex_rig:
            codex_rig_install_json = stdout
    _configure_codex_rig_plugin(home, codex_rig_install_json)
    _configure_codemap_launcher(home, install_json)
    code, stdout, stderr = _invoke_plugin_command(list_plugins, home.env, command_runner)
    if code != 0 or not _plugin_enabled(stdout, "codex-rig") or not _plugin_enabled(stdout, "codemap-py"):
        raise RuntimeError(
            f"Codemap plugin verification failed: {_plugin_listing_evidence(home, code, stdout, stderr)}"
        )
    return True


def _verify_plain_plugin_absent(
    home: ArmHome,
    *,
    codex_bin: str = _CODEX_BIN,
    command_runner: Callable[..., Any] | None = None,
) -> None:
    """Prove A has no Codemap plugin or Codemap binary exposed on PATH."""
    code, stdout, stderr = _invoke_plugin_command([codex_bin, "plugin", "list", "--json"], home.env, command_runner)
    if code != 0:
        raise RuntimeError(
            f"A_plain plugin absence probe failed: {_plugin_listing_evidence(home, code, stdout, stderr)}"
        )
    admitted, host_plugins = plugin_registration.control_admission(home.path / "config.toml", code, stdout)
    if not admitted:
        raise RuntimeError(
            f"A_plain Codex home carries a treatment plugin: {_plugin_listing_evidence(home, code, stdout, stderr)}"
        )
    home.host_plugin_names = host_plugins
    path_dirs = home.env.get("PATH", "").split(os.pathsep)
    if any(
        (Path(directory) / candidate).exists() for directory in path_dirs for candidate in ("codemap-py", "scan-query")
    ):
        raise RuntimeError("A_plain Codemap binary is exposed on PATH")
