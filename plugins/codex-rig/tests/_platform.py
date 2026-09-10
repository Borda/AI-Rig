"""Platform capability probes shared by Codex Rig tests."""

from __future__ import annotations

import ctypes
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


def _symlinks_available(*, target_is_directory: bool, root: Path | None = None) -> bool:
    """Return whether the current host permits one requested symlink type.

    A caller may supply a disposable root to exercise the unavailable result without altering host APIs. File and
    directory targets remain distinct because Windows can grant one capability without granting the other.
    """
    if root is None:
        with tempfile.TemporaryDirectory() as scratch:
            return _symlinks_available(target_is_directory=target_is_directory, root=Path(scratch))
    source = root / "source"
    target = root / "target"
    if target_is_directory:
        source.mkdir()
    else:
        source.write_text("fixture\n", encoding="utf-8")
    try:
        target.symlink_to(source, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        return False
    return target.is_symlink()


FILE_SYMLINKS_AVAILABLE = _symlinks_available(target_is_directory=False)
DIRECTORY_SYMLINKS_AVAILABLE = _symlinks_available(target_is_directory=True)


def mode_is_retainable(mode: int) -> bool:
    """Return whether the temporary filesystem preserves one requested directory mode."""
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "mode-probe"
        path.mkdir(mode=0o700)
        try:
            path.chmod(mode)
        except OSError:
            return False
        return stat.S_IMODE(path.stat().st_mode) == mode


def _posix_file_modes_available() -> bool:
    """Return whether regular and directory POSIX permission modes round-trip exactly."""
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        directory = root / "directory"
        regular_file = root / "file"
        directory.mkdir(mode=0o700)
        regular_file.write_bytes(b"fixture\n")
        try:
            directory.chmod(0o755)
            regular_file.chmod(0o600)
        except OSError:
            return False
        return stat.S_IMODE(directory.stat().st_mode) == 0o755 and stat.S_IMODE(regular_file.stat().st_mode) == 0o600


POSIX_FILE_MODES_AVAILABLE = _posix_file_modes_available()


def _posix_descriptor_primitives_available() -> bool:
    """Return whether descriptor-relative no-follow directory reads are available."""
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required):
        return False
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        child = root / "child"
        child.mkdir()
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            return False
        try:
            child_fd = os.open(
                child.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
        except OSError:
            return False
        finally:
            os.close(root_fd)
        try:
            return stat.S_ISDIR(os.fstat(child_fd).st_mode)
        finally:
            os.close(child_fd)


POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE = _posix_descriptor_primitives_available()


def _posix_observer_primitives_available() -> bool:
    """Return whether the observer's ownership and nonblocking descriptor contract is supported."""
    return (
        POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE
        and hasattr(os, "geteuid")
        and hasattr(os, "O_NONBLOCK")
        and os.geteuid() >= 0
    )


POSIX_OBSERVER_PRIMITIVES_AVAILABLE = _posix_observer_primitives_available()


def _hard_links_available() -> bool:
    """Return whether the temporary filesystem permits hard links between regular files."""
    if not hasattr(os, "link"):
        return False
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        source = root / "source"
        target = root / "target"
        source.write_bytes(b"fixture\n")
        try:
            os.link(source, target)
        except OSError:
            return False
        return source.stat().st_ino == target.stat().st_ino and source.stat().st_nlink == 2


HARD_LINKS_AVAILABLE = _hard_links_available()


def _posix_executable_scripts_available() -> bool:
    """Return whether a chmod-marked POSIX shell script can be executed directly."""
    if shutil.which("sh") is None:
        return False
    with tempfile.TemporaryDirectory() as scratch:
        script = Path(scratch) / "executable-probe"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        try:
            script.chmod(0o700)
            completed = subprocess.run([str(script)], check=False, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0


POSIX_EXECUTABLE_SCRIPTS_AVAILABLE = _posix_executable_scripts_available()


def _bash_candidates() -> list[str]:
    """Return Bash candidates, preferring Git for Windows over WSL."""
    if os.name != "nt":
        located = shutil.which("bash")
        return [located] if located else []

    candidates: list[str] = []
    configured = os.environ.get("GIT_BASH")
    if configured:
        candidates.append(configured)
    git_executable = shutil.which("git")
    if git_executable:
        git_directory = Path(git_executable).resolve().parent
        candidates.extend((str(git_directory / "bash.exe"), str(git_directory.parent / "bin" / "bash.exe")))
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            suffix = "Programs/Git/bin/bash.exe" if variable == "LOCALAPPDATA" else "Git/bin/bash.exe"
            candidates.append(str(Path(root) / suffix))
    return candidates


def _find_posix_bash() -> str | None:
    """Return a Bash executable that actually evaluates a POSIX command."""
    for candidate in _bash_candidates():
        try:
            result = subprocess.run(
                [candidate, "-c", "printf ok"], capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip() == "ok":
            return candidate
    return None


POSIX_BASH = _find_posix_bash()


def _shebang_safe_path(path: str | None) -> str | None:
    """Return path usable on a script's ``#!`` line, which has no quoting mechanism.

    The interpreter directive splits on its first whitespace, so a space in the path — as in
    Windows' default ``C:\\Program Files\\Git\\bin\\bash.exe`` — truncates to the text before it
    and fails with "bad interpreter". Windows also exposes every long path through a space-free
    8.3 short name, so resolving to that name keeps the shebang usable without touching the value
    used for direct process invocation, which needs no such escaping.
    """
    if path is None or os.name != "nt":
        return path
    buffer = ctypes.create_unicode_buffer(260)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))  # type: ignore[attr-defined]
    return buffer.value if 0 < length <= len(buffer) else path


POSIX_BASH_SHEBANG = _shebang_safe_path(POSIX_BASH)
