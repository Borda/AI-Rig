"""Runtime log isolation (claude / codex / direct).

Proves the logging contract:

- each runtime writes into its own ``<log-root>/<runtime>/`` subtree; Claude and
  Codex never append to the same file;
- ``CODEMAP_LOG_DIR`` overrides the log root but the ``<runtime>/`` component is
  still appended;
- an invalid runtime identity falls back to ``direct`` with a bounded diagnostic
  and never becomes a path component;
- a missing session never collapses writers into one unqualified ``cli.jsonl``;
- logging failure never blocks (covered by the read-only-root case).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import _runtime_log as rl
import pytest


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable runtime logging for every test in this module."""
    # tests/conftest.py disables telemetry globally; the logging suite needs it on.
    monkeypatch.setenv("CODEMAP_LOGGING", "true")


def _lines(path: Path) -> list[dict]:
    """Decode JSON Lines records from a runtime log file."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _logs_root(root: Path) -> Path:
    """Return the repository-local runtime log directory."""
    return root / ".cache" / "codemap" / "logs"


class TestWriteLog:
    """``write_log`` writes each runtime into its own subtree and degrades instead of failing."""

    def test_runtimes_write_separate_files(self, root: Path) -> None:
        """Claude and Codex records with one session id land in distinct per-runtime files.

        Two runtimes sharing a project and session id is the normal delegation case; if they appended to one file their
        records would interleave and be unattributable.
        """
        rl.write_log("claude", {"event": "a"}, session="s1", root=root)
        rl.write_log("codex", {"event": "b"}, session="s1", root=root)

        claude = _logs_root(root) / "claude" / "cli_s1.jsonl"
        codex = _logs_root(root) / "codex" / "cli_s1.jsonl"
        assert claude.is_file()
        assert codex.is_file()
        assert claude != codex
        assert _lines(claude)[0]["runtime"] == "claude"
        assert _lines(codex)[0]["runtime"] == "codex"

    def test_override_keeps_runtime_component(self, tmp_path: Path, root: Path) -> None:
        """An override replaces the log root but the ``<runtime>/`` component is still appended.

        Operators redirect logs to a shared location; the runtime split must survive so two runtimes still never share a
        file there.
        """
        override = tmp_path / "shared_logs"

        rl.write_log("claude", {"event": "x"}, session="s", root=root, override=str(override))

        assert (override / "claude" / "cli_s.jsonl").is_file()
        assert not _logs_root(root).exists()  # the project-anchored root is unused under an override

    def test_invalid_identity_falls_back_to_direct(self, root: Path) -> None:
        """An unknown runtime name is logged under ``direct`` with a diagnostic and never becomes a path component.

        The runtime name comes from the environment; letting an arbitrary value name a directory would let it escape or
        pollute the log tree.
        """
        diag = rl.write_log("kotlin", {"event": "y"}, session="s", root=root)

        assert diag is not None
        assert diag.code == rl.INVALID_RUNTIME
        assert (_logs_root(root) / "direct" / "cli_s.jsonl").is_file()
        assert not (_logs_root(root) / "kotlin").exists()

    def test_other_runtime_write_leaves_existing_file_untouched(self, root: Path) -> None:
        """A Codex write never modifies the Claude file and creates only its own subtree.

        Byte-for-byte equality of the Claude file proves isolation rather than just the existence of two files.
        """
        rl.write_log("claude", {"event": "a"}, session="s", root=root)
        claude = _logs_root(root) / "claude" / "cli_s.jsonl"
        before = claude.read_bytes()

        rl.write_log("codex", {"event": "b"}, session="s", root=root)

        assert claude.read_bytes() == before
        assert sorted(p.name for p in _logs_root(root).iterdir()) == ["claude", "codex"]

    def test_missing_session_uses_invocation_not_bare_cli(self, root: Path) -> None:
        """Without a session id the file name still carries a per-invocation suffix, never a bare ``cli_.jsonl``.

        A fixed fallback name would collapse every session-less writer into one file.
        """
        rl.write_log("direct", {"event": "z"}, root=root)

        files = list((_logs_root(root) / "direct").glob("cli_*.jsonl"))
        assert len(files) == 1
        assert files[0].name != "cli_.jsonl"

    @mock.patch("pathlib.Path.mkdir", autospec=True, side_effect=OSError("disk full"))
    def test_failure_never_raises(self, mock_mkdir: mock.MagicMock, root: Path) -> None:
        """An unwritable log directory degrades silently and returns no diagnostic.

        Logging can never block index build/reuse/query, so the OS error must be swallowed. The root exists before the
        patch because fixtures run first.
        """
        result = rl.write_log("claude", {"event": "a"}, session="s", root=root)

        assert result is None
        mock_mkdir.assert_called()  # the failing directory creation really was attempted


class TestResolveRuntime:
    """``resolve_runtime`` accepts the allowlist and falls back to ``direct`` with a bounded diagnostic."""

    @pytest.mark.parametrize("runtime", sorted(rl.RUNTIME_ALLOWLIST))
    def test_allowlisted_runtime_resolves_to_itself(self, runtime: str) -> None:
        """Every supported runtime resolves to itself without a diagnostic.

        The allowlist is the set of names that may become a directory component, so each member must pass unchanged.
        """
        assert rl.resolve_runtime(runtime) == (runtime, None)

    def test_missing_runtime_falls_back_to_direct(self) -> None:
        """A missing runtime resolves to direct and reports the bounded diagnostic.

        An unset runtime is the standalone-CLI case; it must still log, but visibly as ``direct``.
        """
        runtime, diag = rl.resolve_runtime(None)

        assert runtime == "direct"
        assert diag is not None
        assert diag.code == rl.INVALID_RUNTIME


def test_session_id_is_sanitized(root: Path) -> None:
    """Path separators, spaces and colons in a session id are folded to ``-`` in the file name.

    Session ids come from the host; an unsanitised value could escape the log directory or be an invalid file name.
    """
    path = rl.log_path("claude", "a/b c:d", root=root)

    assert path.name == "cli_a-b-c-d.jsonl"
