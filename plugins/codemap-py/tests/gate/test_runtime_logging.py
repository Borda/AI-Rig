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

import pytest

import _runtime_log as rl


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


def test_runtime_isolation(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    rl.write_log("claude", {"event": "a"}, session="s1", root=root)
    rl.write_log("codex", {"event": "b"}, session="s1", root=root)
    claude = _logs_root(root) / "claude" / "cli_s1.jsonl"
    codex = _logs_root(root) / "codex" / "cli_s1.jsonl"
    assert claude.is_file() and codex.is_file()
    assert claude != codex
    assert _lines(claude)[0]["runtime"] == "claude"
    assert _lines(codex)[0]["runtime"] == "codex"


def test_override_keeps_runtime_component(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    override = tmp_path / "shared_logs"
    rl.write_log("claude", {"event": "x"}, session="s", root=root, override=str(override))
    assert (override / "claude" / "cli_s.jsonl").is_file()
    # override replaces the log root but not the runtime split; project-anchored root unused.
    assert not _logs_root(root).exists()


def test_invalid_identity_falls_back_to_direct(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    diag = rl.write_log("kotlin", {"event": "y"}, session="s", root=root)
    assert diag is not None and diag.code == rl.INVALID_RUNTIME
    assert (_logs_root(root) / "direct" / "cli_s.jsonl").is_file()
    # the invalid value never became a path component.
    assert not (_logs_root(root) / "kotlin").exists()


def test_no_cross_runtime_writes(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    rl.write_log("claude", {"event": "a"}, session="s", root=root)
    claude = _logs_root(root) / "claude" / "cli_s.jsonl"
    before = claude.read_bytes()
    rl.write_log("codex", {"event": "b"}, session="s", root=root)
    assert claude.read_bytes() == before  # codex write did not touch the claude file
    assert sorted(p.name for p in _logs_root(root).iterdir()) == ["claude", "codex"]


def test_missing_session_uses_invocation_not_bare_cli(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    rl.write_log("direct", {"event": "z"}, root=root)
    direct = _logs_root(root) / "direct"
    files = list(direct.glob("cli_*.jsonl"))
    assert len(files) == 1
    assert files[0].name != "cli_.jsonl"
    assert files[0].name.startswith("cli_")


@pytest.mark.parametrize("runtime", sorted(rl.RUNTIME_ALLOWLIST))
def test_resolve_runtime_allowlist(runtime: str) -> None:
    """Every supported runtime resolves to itself without a diagnostic."""
    assert rl.resolve_runtime(runtime) == (runtime, None)


def test_resolve_runtime_missing_falls_back_to_direct() -> None:
    """A missing runtime resolves to direct and reports the bounded diagnostic."""
    runtime, diag = rl.resolve_runtime(None)
    assert runtime == "direct"
    assert diag is not None and diag.code == rl.INVALID_RUNTIME


def test_session_id_is_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    path = rl.log_path("claude", "a/b c:d", root=root)
    assert path.name == "cli_a-b-c-d.jsonl"


def test_logging_failure_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An unwritable override must degrade silently, never propagate — logging can
    # never block index build/reuse/query.
    root = tmp_path / "proj"
    root.mkdir()

    def _boom(*_a: object, **_k: object) -> None:
        """Raise the expected OS error to model an unavailable log directory."""
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", _boom)
    assert rl.write_log("claude", {"event": "a"}, session="s", root=root) is None
