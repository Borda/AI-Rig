"""Verify telemetry logs remain anchored to the project root rather than the process CWD.

The hook layer, the skill layer and the CLI layer each append shards keyed on one session id, and ``debrief-coding``
joins them by reading a single directory. Each layer previously resolved the ``CODEMAP_LOG_DIR`` default against the
*process* CWD, so a session whose hooks fired at the repository root while a query ran from a subdirectory wrote the two
halves into two directories and the join silently returned nothing. ``query.py`` was the worst of the four because its
``_LOG_DIR`` was a module constant frozen at IMPORT time.

Every case here therefore runs from a **subdirectory** of a real git repo and asserts the shard landed at the repo root.
A test run from the repo root itself cannot fail.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import _runtime_log as rl
import pytest
from codemap_py import query, telemetry

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
_TOOL_HOOK = _HOOKS_DIR / "log-tool-use.py"
_SKILL_HOOK = _HOOKS_DIR / "log-skill-start.py"

_LOGS_REL = Path(".cache") / "codemap" / "logs"


def _load_hookutil():
    """Load ``hooks/_hookutil.py`` by path (it is not an importable package member)."""
    spec = importlib.util.spec_from_file_location("codemap_hookutil_anchoring", _HOOKS_DIR / "_hookutil.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="repo_with_subdir")
def _repo_with_subdir(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(repo_root, nested_subdir)`` for a real, initialised git repository.

    A real repo (not a bare ``.git`` directory) so the package layer's ``git rev-parse --show-toplevel`` and the hook
    layer's ``.git`` walk both resolve — the two anchoring mechanisms must agree, and only a real repo proves it.
    """
    root = tmp_path / "proj"
    (root / "src" / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True)
    # tmp_path is a symlink farm on macOS (/var -> /private/var); the resolvers collapse
    # it, so the expected path must be collapsed too or every comparison fails on the prefix.
    return root.resolve(), (root / "src" / "pkg").resolve()


@pytest.fixture
def logging_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Enable telemetry and clear every variable that would pick a log directory or runtime other than the default."""
    monkeypatch.setenv("CODEMAP_LOGGING", "true")
    for name in ("CODEMAP_LOG_DIR", "CODEMAP_RUNTIME", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CSID"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _run_hook(hook: Path, payload: dict, cwd: Path) -> subprocess.CompletedProcess:
    """Feed one event through *hook* as a subprocess rooted at *cwd*."""
    env = {**os.environ}
    env.pop("CODEMAP_LOG_DIR", None)  # inherited override would mask the anchoring
    env["CODEMAP_LOGGING"] = "true"  # conftest's autouse gate disables it suite-wide
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd),
        env=env,
        check=False,
    )


class TestHookLayerAnchoring:
    """Hooks launched inside a subdirectory still write to the repo-root log directory."""

    @pytest.mark.parametrize(
        ("hook", "payload", "shard_glob"),
        [
            pytest.param(
                _TOOL_HOOK, {"tool_name": "Grep", "tool_input": {"pattern": "x"}}, "tools*.jsonl", id="tool-use"
            ),
            pytest.param(
                _SKILL_HOOK,
                {"tool_name": "Skill", "tool_input": {"skill": "codemap-py:query-code"}},
                "skills*.jsonl",
                id="skill-start",
            ),
        ],
    )
    def test_writes_at_the_repo_root(
        self, repo_with_subdir: tuple[Path, Path], hook: Path, payload: dict, shard_glob: str
    ) -> None:
        """Tool-use and skill-start hooks launched in a subdirectory write their shard under the repo-root log dir.

        The hook runs from a nested directory of a real git repo, so only repo-root anchoring can put the shard in the
        ``claude`` log directory; a shard under the subdirectory would split the join across two directories.
        """
        root, subdir = repo_with_subdir

        result = _run_hook(hook, payload, subdir)

        assert result.returncode == 0, result.stderr
        assert sorted(p.name for p in (root / _LOGS_REL / "claude").glob(shard_glob)) != []
        assert not (subdir / _LOGS_REL).exists()


class TestCliLayerAnchoring:
    """The CLI telemetry layer resolves the same repo-root directory from a subdirectory."""

    def test_writes_at_the_repo_root(
        self, repo_with_subdir: tuple[Path, Path], logging_env: pytest.MonkeyPatch
    ) -> None:
        """Command-line telemetry called from a subdirectory lands under the repo-root ``direct`` log dir.

        With no runtime or session variables set the CLI layer logs as ``direct``; it must still resolve the repository
        root, not the process working directory.
        """
        root, subdir = repo_with_subdir
        logging_env.chdir(subdir)

        telemetry.log_cli("rdeps", ["rdeps", "m"], {"ok": True}, 0.0)

        assert sorted(p.name for p in (root / _LOGS_REL / "direct").glob("cli*.jsonl")) != []
        assert not (subdir / _LOGS_REL).exists()

    def test_codex_uses_thread_id_without_reading_the_claude_marker(
        self, repo_with_subdir: tuple[Path, Path], logging_env: pytest.MonkeyPatch
    ) -> None:
        """Codex telemetry uses ``CODEX_THREAD_ID`` in ``logs/codex`` and never a Claude marker session.

        A stale Claude session marker sits in the temp dir; the Codex shard must still be keyed by the thread id, or
        Codex and Claude sessions would be joined under one id.
        """
        root, subdir = repo_with_subdir
        marker_dir = root.parent / "markers"
        marker_dir.mkdir(parents=True)
        (marker_dir / f"codemap-{root.name}-session").write_text("claude-session")
        logging_env.setenv("CODEX_THREAD_ID", "codex-thread")
        logging_env.setenv("TMPDIR", str(marker_dir))
        logging_env.chdir(subdir)

        telemetry.log_cli("rdeps", ["rdeps", "pkg.auth"], {"ok": True}, 0.0)

        shard = root / _LOGS_REL / "codex" / "cli_codex-thread.jsonl"
        record = json.loads(shard.read_text().strip())
        assert record["runtime"] == "codex"
        assert record["session"] == "codex-thread"

    def test_query_engine_has_no_import_time_log_dir(self) -> None:
        """The query engine binds no log directory at import time.

        The regression this pins is specifically the module *constant*: any value bound at import cannot follow a later
        ``chdir`` or a ``CODEMAP_LOG_DIR`` exported after the process started, which is exactly how the engine's shards
        ended up split from the hooks'.
        """
        assert not hasattr(query, "_LOG_DIR")


class TestLayersAgree:
    """The hook layer and the CLI layer resolve one identical directory (the join contract)."""

    def test_hook_and_cli_shards_share_one_log_root(
        self, repo_with_subdir: tuple[Path, Path], logging_env: pytest.MonkeyPatch
    ) -> None:
        """A hook shard and a CLI shard written from the same subdirectory share one ``logs`` tree.

        ``debrief-coding`` joins by reading a single directory, so exactly one shard per layer must appear under the
        repo-root log directory and none anywhere else.
        """
        root, subdir = repo_with_subdir
        hook = _run_hook(_TOOL_HOOK, {"tool_name": "Glob", "tool_input": {"pattern": "*"}}, subdir)
        logging_env.chdir(subdir)

        telemetry.log_cli("central", ["central"], {"ok": True}, 0.0)

        assert hook.returncode == 0, hook.stderr

        shards = {p.parent for p in (root / _LOGS_REL).rglob("*.jsonl")}
        assert shards == {root / _LOGS_REL / "claude", root / _LOGS_REL / "direct"}
        assert len(list((root / _LOGS_REL).rglob("*.jsonl"))) == 2

    def test_hookutil_and_runtime_log_resolve_the_same_path(
        self, repo_with_subdir: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hook resolver and the package resolver return byte-identical paths from a subdirectory.

        The hook layer cannot import the package, so it carries its own resolver; the two must agree or the layers write
        to different directories.
        """
        root, subdir = repo_with_subdir
        monkeypatch.delenv("CODEMAP_LOG_DIR", raising=False)
        monkeypatch.chdir(subdir)

        assert _load_hookutil().log_dir() == rl.log_dir_for("claude") == root / _LOGS_REL / "claude"


class TestOverrideAnchoring:
    """``CODEMAP_LOG_DIR`` keeps its absolute meaning and a relative value anchors to the repo root."""

    def test_absolute_override_is_used_verbatim(
        self, repo_with_subdir: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An absolute ``CODEMAP_LOG_DIR`` is used as given by both the package and the hook resolver.

        An absolute path is already unambiguous, so no anchoring may rewrite it even when run from a subdirectory.
        """
        _root, subdir = repo_with_subdir
        elsewhere = (tmp_path / "shared_logs").resolve()
        monkeypatch.setenv("CODEMAP_LOG_DIR", str(elsewhere))
        monkeypatch.chdir(subdir)

        assert rl.log_root() == elsewhere
        assert _load_hookutil().log_dir() == elsewhere / "claude"

    def test_relative_override_anchors_to_the_repo_root(
        self, repo_with_subdir: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative ``CODEMAP_LOG_DIR`` resolves against the repo root, not the process working directory.

        Resolving it against the working directory splits the hook and CLI shards exactly like the old default did.
        """
        root, subdir = repo_with_subdir
        monkeypatch.setenv("CODEMAP_LOG_DIR", "build/logs")
        monkeypatch.chdir(subdir)

        assert rl.log_root() == root / "build" / "logs"
        assert _load_hookutil().log_dir() == root / "build" / "logs" / "claude"

    def test_runtime_directory_sits_beneath_the_anchored_root(self, repo_with_subdir: tuple[Path, Path]) -> None:
        """The runtime name is appended beneath the anchored log root.

        With no override, the Codex log dir is ``<repo>/.cache/codemap/logs/codex``.
        """
        root, _subdir = repo_with_subdir

        assert rl.log_dir_for("codex", root=root, override="") == root / _LOGS_REL / "codex"
