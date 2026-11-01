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

_MODE = Path(__file__).resolve().parents[3] / "skills/resolve/modes/conflict-resolution.md"
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

_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
_WSL_BASH = r"C:\Windows\System32\bash.exe"
_MINGW_PROBE = (0, b"MINGW64_NT-10.0")
_WSL_UNINITIALIZED_PROBE = (1, b"Install a WSL distribution")


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
    chunks = _MODE.read_text(encoding="utf-8").split("```bash\n")[1:]
    return next(
        block for block in (chunk.split("```", 1)[0] for chunk in chunks) if "# 2. update target branch" in block
    )


def _run_block(clone: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Execute the merge block on the PR branch; by default the skill's ref variables are bound in the environment."""
    assert _BASH is not None
    env = env or {**_ENV, "BASE_REF": "main", "HEAD_REF": "feature", "FORK_REMOTE": "origin"}
    return subprocess.run([_BASH, "-c", _merge_block()], cwd=clone, env=env, capture_output=True, text=True)


def _fresh_shell_env(tmp_path: Path, refs: dict[str, str]) -> dict[str, str]:
    """Build a fresh-shell environment: no bound ref variables, Step 4 sentinels written under TMPDIR."""
    session = "target-update-fresh-shell"
    for name, value in refs.items():
        (tmp_path / f"resolve-{name}-{session}").write_text(f"{value}\n", encoding="utf-8", newline="\n")
    unbound = {key: value for key, value in _ENV.items() if key not in {"BASE_REF", "HEAD_REF", "FORK_REMOTE"}}
    return {**unbound, "CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)}


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """Build origin + clone on a ``feature`` branch, then advance origin's target past the clone's local target.

    Returns the clone and the seed repository whose ``main`` is the fresh target.
    """
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


class TestWindowsBashSelection:
    """Pin Windows Bash selection to the native Git Bash."""

    @pytest.mark.parametrize(
        ("git", "path_bash", "existing", "probes", "expected", "attempted"),
        [
            pytest.param(
                r"C:\Program Files\Git\cmd\git.exe",
                _WSL_BASH,
                [_GIT_BASH],
                {_GIT_BASH: _MINGW_PROBE},
                _GIT_BASH,
                [_GIT_BASH],
                id="git-cmd-skips-wsl-shim",
            ),
            pytest.param(
                r"C:\Program Files\Git\mingw64\bin\git.exe",
                _WSL_BASH,
                [_GIT_BASH],
                {_GIT_BASH: _MINGW_PROBE},
                _GIT_BASH,
                [_GIT_BASH],
                id="git-mingw-bin-skips-wsl-shim",
            ),
            pytest.param(
                r"C:\Shims\git.exe",
                _GIT_BASH,
                [_GIT_BASH],
                {_GIT_BASH: _MINGW_PROBE},
                _GIT_BASH,
                [_GIT_BASH],
                id="git-shim-uses-path-git-bash",
            ),
            pytest.param(
                r"C:\Program Files\Git\cmd\git.exe", _WSL_BASH, [], {}, None, [], id="no-candidate-exists-selects-none"
            ),
            pytest.param(
                r"C:\Shims\git.exe",
                _WSL_BASH,
                [_WSL_BASH],
                {_WSL_BASH: _WSL_UNINITIALIZED_PROBE},
                None,
                [_WSL_BASH],
                id="path-wsl-uninitialized-rejected",
            ),
            pytest.param(
                r"C:\Shims\git.exe",
                _WSL_BASH,
                [_WSL_BASH],
                {_WSL_BASH: (0, b"Linux")},
                None,
                [_WSL_BASH],
                id="path-wsl-installed-rejected",
            ),
        ],
    )
    def test_selects_only_native_git_bash(
        self,
        monkeypatch: pytest.MonkeyPatch,
        git: str,
        path_bash: str,
        existing: list[str],
        probes: dict[str, tuple[int, bytes]],
        expected: str | None,
        attempted: list[str],
    ) -> None:
        """Only a native Git Bash is chosen; a WSL launcher on PATH never satisfies the requirement.

        Windows may resolve ``bash.exe`` to a WSL launcher and ``git.exe`` to a shim outside Git's install. The
        selection derives candidates from git's location, probes each existing one with ``uname -s`` and accepts only a
        MINGW-style answer, so a non-functional or non-native shell is skipped without being run at all when the file is
        absent.
        """
        present = {PureWindowsPath(path) for path in existing}
        monkeypatch.setattr(Path, "is_file", lambda path: PureWindowsPath(path) in present)
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            """Answer each probed shell with its scripted response and record the command."""
            commands.append(command)
            returncode, output = probes[command[0]]
            return subprocess.CompletedProcess(command, returncode, output, b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _find_bash(git, path_bash, windows=True) == expected
        assert commands == [[shell, "-c", "uname -s"] for shell in attempted]


@pytest.mark.integration
@requires_git_bash
class TestMergeBlock:
    """Pin the target merge block for stale, diverged, and unresolved refs."""

    def test_fast_forwards_stale_local_target_and_merges_fresh_remote(self, repos: tuple[Path, Path]) -> None:
        """A stale local target is fast-forwarded, and the merge brings in the newest target commit."""
        clone, seed = repos
        fresh = _git(seed, "rev-parse", "main")
        assert _git(clone, "rev-parse", "main") != fresh
        result = _run_block(clone)
        assert result.returncode == 0, result.stderr
        assert _git(clone, "rev-parse", "main") == fresh
        assert _git(clone, "rev-parse", "MERGE_HEAD") == fresh
        assert (clone / "target-new.txt").exists()

    def test_reloads_refs_in_fresh_shell(self, tmp_path: Path, repos: tuple[Path, Path]) -> None:
        """Run unedited from a later step (Step 9.0 re-sync), the block reloads its refs and merges the newest target.

        Each Bash call is a new process, so the variables Step 4 computed are gone; without the sentinel reload the
        block would fetch an empty ref and the drift re-sync would never happen.
        """
        clone, seed = repos
        env = _fresh_shell_env(tmp_path, {"base-ref": "main", "head-ref": "feature", "fork-remote": "origin"})
        result = _run_block(clone, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _git(clone, "rev-parse", "MERGE_HEAD") == _git(seed, "rev-parse", "main")

    def test_refuses_unresolved_refs(self, tmp_path: Path, repos: tuple[Path, Path]) -> None:
        """Empty Step 4 sentinels stop the block before any fetch or merge touches the tree."""
        clone, _ = repos
        result = _run_block(clone, _fresh_shell_env(tmp_path, {"base-ref": "", "head-ref": ""}))
        assert result.returncode == 1
        assert "BASE_REF/HEAD_REF unresolved" in result.stdout
        assert not (clone / ".git" / "MERGE_HEAD").exists()

    def test_keeps_diverged_local_target_and_merges_remote(self, repos: tuple[Path, Path]) -> None:
        """Local target commits are never rewritten; the merge still uses the fetched remote target.

        The clone's local ``main`` carries a commit origin lacks, so a fast-forward is impossible; the block must report
        that it left the local branch alone and still merge ``origin/main`` into the PR branch.
        """
        clone, seed = repos
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
