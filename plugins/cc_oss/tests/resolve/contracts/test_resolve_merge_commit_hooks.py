"""Every resolve merge commit refreshes the hook approval first, so a merge never stays pending on a stale digest.

The structural test finds every fenced block that runs ``--commit-merge``; the end-to-end test runs each one under bash
against a real repository whose base merge changes ``package.json``, with the real helper.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[3]
_MODES = _PLUGIN / "skills/resolve/modes"
_BASH = shutil.which("bash")
_GIT = shutil.which("git")
# A shell function, not a symlink: a venv launcher resolves its ``pyvenv.cfg`` from where it is invoked, so a symlink to
# it fails on Windows (``failed to locate pyvenv.cfg``).
_PYTHON_FUNCTION = f'python() {{ "{Path(sys.executable).as_posix()}" "$@"; }}\n'
_HOOK = "#!/bin/sh\necho ran > ran-hook\nexit 0\n"
_ROOT_STUB = "import os\nprint(os.environ['REAL_PLUGIN_ROOT'])\n"


def _bash_blocks(path: Path) -> list[list[str]]:
    """Return the lines of every fenced bash block in ``path``."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if current is None:
            if line.strip() == "```bash":
                current = []
        elif line.strip() == "```":
            blocks.append(current)
            current = None
        else:
            current.append(line)
    return blocks


def _merge_commit_blocks() -> list[tuple[str, list[str]]]:
    """Return ``(file name, block)`` for every resolve block that finishes a merge through the helper."""
    found = []
    for path in sorted(_MODES.glob("*.md")):
        for block in _bash_blocks(path):
            if any("resolve_commit_hooks.py" in line and "--commit-merge" in line for line in block):
                found.append((path.name, block))
    return found


_BLOCKS = _merge_commit_blocks()


def test_merge_commit_blocks_exist() -> None:
    """Step 5 and Step 7b each finish the merge through one fixed block."""
    assert len(_BLOCKS) >= 2


@pytest.mark.parametrize(("name", "block"), [pytest.param(n, b, id=f"{n}-{i}") for i, (n, b) in enumerate(_BLOCKS)])
def test_each_merge_commit_refreshes_the_approval_before_reading_it(name: str, block: list[str]) -> None:
    """The refresh runs first, a failed refresh empties the approval, and the commit reads the refreshed file."""
    commit = next(i for i, line in enumerate(block) if "--commit-merge" in line)
    refresh = [i for i, line in enumerate(block[:commit]) if "--refresh-approval" in line]
    assert len(refresh) == 1, name
    line = block[refresh[0]]
    for part in (
        '--base "origin/$BASE_REF"',
        '--head "$PR_HEAD_OID"',
        '--approved-file "$IMPL_DIR/approved-hooks.txt"',
        '--decision-file "$IMPL_DIR/hooks-decision.txt"',
        ': > "$IMPL_DIR/approved-hooks.txt"',
        "printf 'stale\\n' > \"$IMPL_DIR/hooks-decision.txt\"",
    ):
        assert part in line, (name, part)
    reads = [i for i, text in enumerate(block) if "read -r RESOLVE_APPROVED_HOOKS" in text]
    assert reads, name
    assert all(refresh[0] < i < commit for i in reads), name


@pytest.mark.parametrize(("name", "block"), [pytest.param(n, b, id=f"{n}-{i}") for i, (n, b) in enumerate(_BLOCKS)])
def test_each_merge_commit_fence_outlasts_the_commit_budget(name: str, block: list[str]) -> None:
    """The Bash call finishing a merge is never killed before the helper's own budget stops a slow hook run.

    The harness caps one Bash call at 600 s, so the helper's worst case must stay under both that cap and the fence.
    """
    from resolve_commit_hooks import MERGE_COMMIT_WORST_CASE

    [commit] = [line for line in block if "--commit-merge" in line and "resolve_commit_hooks.py" in line]
    fence_ms = int(commit.rsplit("# timeout:", 1)[1].split()[0])
    assert MERGE_COMMIT_WORST_CASE * 1000 < min(fence_ms, 600_000), name


def _git(repo: Path, *args: str, env: dict[str, str]) -> str:
    """Run one git command in ``repo`` with the isolated environment."""
    assert _GIT is not None
    done = subprocess.run([_GIT, "-C", str(repo), *args], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def merge_world(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """Return a repository with a pending base merge that bumps ``package.json``, its impl dir and environment."""
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    env.pop("RESOLVE_APPROVED_HOOKS", None)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", env=env)
    (repo / "package.json").write_text('{"version": "1"}\n', encoding="utf-8", newline="\n")
    _git(repo, "add", "package.json", env=env)
    _git(repo, "commit", "-q", "-m", "base", env=env)
    _git(repo, "checkout", "-q", "-b", "pr", env=env)
    (repo / "feature.txt").write_text("pr work\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "feature.txt", env=env)
    _git(repo, "commit", "-q", "-m", "feature", env=env)
    pr_head = _git(repo, "rev-parse", "HEAD", env=env)
    _git(repo, "checkout", "-q", "main", env=env)
    (repo / "package.json").write_text('{"version": "2"}\n', encoding="utf-8", newline="\n")
    _git(repo, "commit", "-q", "-am", "bump", env=env)
    _git(repo, "update-ref", "refs/remotes/origin/main", "main", env=env)
    _git(repo, "checkout", "-q", "pr", env=env)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(_HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    impl = tmp_path / "impl"
    impl.mkdir()
    stub = tmp_path / "stub-plugin" / "bin"
    stub.mkdir(parents=True)
    (stub / "resolve_plugin_root.py").write_text(_ROOT_STUB, encoding="utf-8", newline="\n")
    sentinels = tmp_path / "tmp"
    sentinels.mkdir()
    for key, value in (("impl-dir", str(impl)), ("base-ref", "main"), ("pr-head-oid", pr_head)):
        (sentinels / f"resolve-{key}-t1").write_text(value + "\n", encoding="utf-8", newline="\n")
    env.update(
        {
            "CLAUDE_PLUGIN_ROOT": str(stub.parent),
            "REAL_PLUGIN_ROOT": str(_PLUGIN),
            "TMPDIR": str(sentinels),
            "CLAUDE_CODE_SESSION_ID": "t1",
        }
    )
    return repo, impl, env


def _record(repo: Path, impl: Path, env: dict[str, str], *, helper_made: bool) -> None:
    """Record the approval the run would hold before the merge: the helper's own ``run`` or a user's answer."""
    helper = str(_PLUGIN / "bin" / "resolve_commit_hooks.py")
    if helper_made:
        cmd = [
            sys.executable,
            helper,
            "--decide",
            "--base",
            "origin/main",
            "--approved-file",
            str(impl / "approved-hooks.txt"),
        ]
        done = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=True)
        (impl / "hooks-decision.txt").write_text(
            json.loads(done.stdout)["decision"] + "\n", encoding="utf-8", newline="\n"
        )
        return
    done = subprocess.run(
        [sys.executable, helper], cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=True
    )
    (impl / "approved-hooks.txt").write_text(json.loads(done.stdout)["sha256"], encoding="utf-8", newline="\n")
    (impl / "hooks-decision.txt").write_text("ask\n", encoding="utf-8", newline="\n")


@pytest.mark.integration
@pytest.mark.skipif(_BASH is None or _GIT is None, reason="needs bash and git")
@pytest.mark.parametrize(
    ("helper_made", "outcome", "hook_ran", "decision"),
    [
        pytest.param(True, "hooks-refresh=refreshed", True, "run\n", id="helper-made-run-follows-the-merge"),
        pytest.param(False, "hooks-refresh=stale", False, "stale\n", id="user-approval-set-aside"),
    ],
)
@pytest.mark.parametrize(("name", "block"), [pytest.param(n, b, id=f"{n}-{i}") for i, (n, b) in enumerate(_BLOCKS)])
def test_each_merge_commit_block_commits_a_merge_that_changed_package_json(
    merge_world: tuple[Path, Path, dict[str, str]],
    name: str,
    block: list[str],
    helper_made: bool,
    outcome: str,
    hook_ran: bool,
    decision: str,
) -> None:
    """A base-side ``package.json`` bump never leaves the merge pending, and never runs hooks nobody approved."""
    repo, impl, env = merge_world
    _record(repo, impl, env, helper_made=helper_made)
    _git(repo, "merge", "--no-commit", "--no-ff", "origin/main", env=env)
    assert _BASH is not None
    done = subprocess.run(
        [_BASH, "-c", _PYTHON_FUNCTION + "\n".join(block)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, (name, done.stdout, done.stderr)
    assert outcome in done.stdout
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD", env=env).split()) == 3
    assert (repo / "ran-hook").exists() is hook_ran
    assert (impl / "hooks-decision.txt").read_text(encoding="utf-8") == decision
