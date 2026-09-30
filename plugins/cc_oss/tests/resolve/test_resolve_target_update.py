"""Run the Step 5 merge block from conflict-resolution.md against real repositories.

The block must refresh the target branch — both `origin/<base>` and the local `<base>` branch — before merging it into
the PR branch, and must never let a stale or diverged local target leak into the merge.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

_MODE = Path(__file__).resolve().parents[2] / "skills/resolve/modes/conflict-resolution.md"
_GIT = shutil.which("git")


def _find_bash(git: str | None, path_bash: str | None, *, windows: bool) -> str | None:
    """Find an executable shell suitable for running the merge block."""
    if not windows:
        return path_bash
    if git is None:
        return None

    # Windows PATH may resolve bash.exe to WSL, while git.exe may be a shim outside Git's install.
    git_dir = PureWindowsPath(git).parent
    git_root = git_dir.parent
    if git_dir.name.lower() == "bin" and git_root.name.lower() in {"mingw64", "usr"}:
        git_root = git_root.parent
    candidates = [str(git_root / "bin" / "bash.exe"), str(git_root / "usr" / "bin" / "bash.exe")]
    if path_bash is not None and path_bash not in candidates:
        candidates.append(path_bash)
    for candidate in candidates:
        if not Path(candidate).is_file():
            continue
        try:
            probe = subprocess.run([candidate, "-c", "uname -s"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0 and re.match(rb"^(?:MINGW\d+|MSYS|UCRT\d+|CLANGARM\d+)_NT-", probe.stdout):
            return candidate
    return None


_BASH = _find_bash(_GIT, shutil.which("bash"), windows=sys.platform == "win32")
_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

requires_git_bash = pytest.mark.skipif(_GIT is None or _BASH is None, reason="needs git and usable Git Bash on Windows")


def _git(cwd: Path, *args: str) -> str:
    """Run one git command and return its stripped stdout."""
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    """Add one file and commit it."""
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


def _merge_block() -> str:
    """Return the Case B fenced bash block that updates both branches and merges."""
    text = _MODE.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
    return next(block for block in blocks if "# 2. update target branch" in block)


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """Build origin + clone, then advance origin's target past the clone's local target."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "base.txt")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "checkout", "-q", "-b", "feature")
    _commit(clone, "feature.txt")
    _git(seed, "remote", "add", "origin", str(origin))
    _commit(seed, "target-new.txt")
    _git(seed, "push", "-q", "origin", "main")
    return clone, seed


def _run_block(clone: Path) -> subprocess.CompletedProcess[str]:
    """Execute the merge block on the PR branch with the skill's variables bound."""
    assert _BASH is not None
    env = {**_ENV, "BASE_REF": "main", "HEAD_REF": "feature", "FORK_REMOTE": "origin"}
    return subprocess.run([_BASH, "-c", _merge_block()], cwd=clone, env=env, capture_output=True, text=True)


@pytest.mark.parametrize(
    "git",
    [
        pytest.param(r"C:\Program Files\Git\cmd\git.exe", id="git-cmd"),
        pytest.param(r"C:\Program Files\Git\mingw64\bin\git.exe", id="git-mingw-bin"),
    ],
)
def test_windows_bash_selection_rejects_wsl_shim(monkeypatch: pytest.MonkeyPatch, git: str) -> None:
    """Choose Git Bash when the PATH bash is a nonfunctional WSL launcher."""
    git_bash = r"C:\Program Files\Git\bin\bash.exe"
    wsl_bash = r"C:\Windows\System32\bash.exe"
    attempted: list[str] = []

    monkeypatch.setattr(Path, "is_file", lambda path: PureWindowsPath(path) == PureWindowsPath(git_bash))

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Model the WSL launcher failure and Git Bash's probe response."""
        attempted.append(command[0])
        output = b"MINGW64_NT-10.0" if command[0] == git_bash else b"Install a WSL distribution"
        return subprocess.CompletedProcess(command, 0 if command[0] == git_bash else 1, output, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _find_bash(git, wsl_bash, windows=True) == git_bash
    assert attempted == [git_bash]
    monkeypatch.setattr(Path, "is_file", lambda _path: False)
    assert _find_bash(git, wsl_bash, windows=True) is None
    assert attempted == [git_bash]


def test_windows_bash_selection_uses_path_git_bash_with_git_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Git shim does not hide a working Git Bash on PATH."""
    git = r"C:\Shims\git.exe"
    git_bash = r"C:\Program Files\Git\bin\bash.exe"
    attempted: list[list[str]] = []
    monkeypatch.setattr(Path, "is_file", lambda path: PureWindowsPath(path) == PureWindowsPath(git_bash))

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Report the native shell identity for the PATH Git Bash."""
        attempted.append(command)
        return subprocess.CompletedProcess(command, 0, b"MINGW64_NT-10.0", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _find_bash(git, git_bash, windows=True) == git_bash
    assert attempted == [[git_bash, "-c", "uname -s"]]


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        pytest.param(1, b"Install a WSL distribution", id="wsl-uninitialized"),
        pytest.param(0, b"Linux", id="wsl-installed"),
    ],
)
def test_windows_bash_selection_rejects_path_wsl(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: bytes
) -> None:
    """A WSL launcher on PATH cannot satisfy the native Git Bash requirement."""
    git = r"C:\Shims\git.exe"
    wsl_bash = r"C:\Windows\System32\bash.exe"
    attempted: list[list[str]] = []
    monkeypatch.setattr(Path, "is_file", lambda path: PureWindowsPath(path) == PureWindowsPath(wsl_bash))

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Report the selected WSL launcher's response."""
        attempted.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _find_bash(git, wsl_bash, windows=True) is None
    assert attempted == [[wsl_bash, "-c", "uname -s"]]


@pytest.mark.integration
@requires_git_bash
def test_merge_block_updates_local_target_and_merges_fresh_target(tmp_path: Path) -> None:
    """A stale local target is fast-forwarded, and the merge brings in the newest target commit."""
    clone, seed = _setup(tmp_path)
    fresh = _git(seed, "rev-parse", "main")
    assert _git(clone, "rev-parse", "main") != fresh

    result = _run_block(clone)

    assert result.returncode == 0, result.stderr
    assert _git(clone, "rev-parse", "main") == fresh
    assert _git(clone, "rev-parse", "MERGE_HEAD") == fresh
    assert (clone / "target-new.txt").exists()


@pytest.mark.integration
@requires_git_bash
def test_merge_block_keeps_diverged_local_target_and_still_merges_remote(tmp_path: Path) -> None:
    """Local target commits are never rewritten; the merge still uses the fetched remote target."""
    clone, seed = _setup(tmp_path)
    _git(clone, "checkout", "-q", "main")
    _commit(clone, "local-only.txt")
    local = _git(clone, "rev-parse", "main")
    _git(clone, "checkout", "-q", "feature")

    result = _run_block(clone)

    assert result.returncode == 0, result.stderr
    assert "local main not updated" in result.stdout
    assert _git(clone, "rev-parse", "main") == local
    assert _git(clone, "rev-parse", "MERGE_HEAD") == _git(seed, "rev-parse", "main")
    assert not (clone / "local-only.txt").exists()
