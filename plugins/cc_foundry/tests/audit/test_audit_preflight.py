"""Tests for audit_preflight bin script.

Covers argument splitting (flags, aliases, --keep, scope tokens), the tool-probe memo, sentinel writing, and the CLI's
two failure exits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import audit_preflight as ap


class TestParseArguments:
    """Covers splitting the raw argument string."""

    def test_bare_scope(self) -> None:
        """A plain scope token sets no flags."""
        state, keep, scope = ap.parse_arguments("plugins")
        assert scope == "plugins"
        assert keep == ""
        assert not any(state.values())

    def test_flags_recognised(self) -> None:
        """Each supported flag sets its state key and leaves the scope clean."""
        state, _, scope = ap.parse_arguments("plugins --local --efficiency --skip-gate --fast")
        assert state["local-mode"] and state["efficiency"] and state["skip-gate"] and state["fast"]
        assert scope == "plugins"

    def test_challenge_is_adversarial_alias(self) -> None:
        """--challenge sets the same state as --adversarial."""
        state, _, _ = ap.parse_arguments("--challenge")
        assert state["adversarial"] is True

    def test_keep_extracted_and_removed(self) -> None:
        """--keep "..." is captured and does not leak into the scope tokens."""
        state, keep, scope = ap.parse_arguments('agents --keep "run-dir, task ids" --local')
        assert keep == "run-dir, task ids"
        assert scope == "agents"
        assert state["local-mode"] is True

    def test_empty_keep_value(self) -> None:
        """An empty --keep "" yields an empty string, not a missing match."""
        _, keep, scope = ap.parse_arguments('plugins --keep ""')
        assert keep == ""
        assert scope == "plugins"

    def test_flag_substring_in_scope_not_matched(self) -> None:
        """A scope token containing a flag name is not treated as that flag."""
        state, _, scope = ap.parse_arguments("my--local-notes")
        assert state["local-mode"] is False
        assert scope == "my--local-notes"

    def test_multiple_scope_tokens_preserved_in_order(self) -> None:
        """Scope tokens keep their order with flags removed from between them."""
        _, _, scope = ap.parse_arguments("agents --local skills")
        assert scope == "agents skills"


class TestProbeTool:
    """Covers the four-hour tool probe memo."""

    def test_missing_tool_reports_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tool not on PATH probes false."""
        monkeypatch.setattr(ap.shutil, "which", lambda _name: None)
        assert ap.probe_tool("nope", state_dir=tmp_path) is False

    def test_present_tool_writes_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tool on PATH probes true and leaves a timestamp marker."""
        monkeypatch.setattr(ap.shutil, "which", lambda _name: "/usr/bin/jq")
        assert ap.probe_tool("jq", state_dir=tmp_path) is True
        assert (tmp_path / "jq.ok").is_file()

    def test_fresh_marker_short_circuits_lookup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A recent marker returns true without consulting PATH."""
        (tmp_path / "jq.ok").write_text(str(int(ap.time.time())), encoding="utf-8")

        def _boom(_name: str) -> str:
            raise AssertionError("PATH lookup should have been skipped")

        monkeypatch.setattr(ap.shutil, "which", _boom)
        assert ap.probe_tool("jq", state_dir=tmp_path) is True

    def test_expired_marker_falls_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A marker older than the TTL is ignored and PATH is consulted again."""
        stale = int(ap.time.time()) - ap.PREFLIGHT_TTL - 10
        (tmp_path / "jq.ok").write_text(str(stale), encoding="utf-8")
        monkeypatch.setattr(ap.shutil, "which", lambda _name: None)
        assert ap.probe_tool("jq", state_dir=tmp_path) is False

    def test_corrupt_marker_falls_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A marker that is not a timestamp is ignored rather than raising."""
        (tmp_path / "jq.ok").write_text("not-a-number", encoding="utf-8")
        monkeypatch.setattr(ap.shutil, "which", lambda _name: None)
        assert ap.probe_tool("jq", state_dir=tmp_path) is False


class TestWriteState:
    """Covers sentinel writing."""

    def test_writes_session_scoped_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sentinels land in a session-scoped directory under TMPDIR."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess1")
        state_dir = ap.write_state({"local-mode": "true", "audit-tpl": "/x"})
        assert state_dir == tmp_path / "audit-state-sess1"
        assert (state_dir / "local-mode").read_text(encoding="utf-8") == "true\n"
        assert (state_dir / "audit-tpl").read_text(encoding="utf-8") == "/x\n"

    def test_session_token_falls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no session variables the token is `shared`, never a pid."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.delenv("CSID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        assert ap.write_state({"k": "v"}) == tmp_path / "audit-state-shared"


class TestCli:
    """Covers the command-line contract."""

    def test_mutually_exclusive_flags_exit_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--upgrade with --efficiency is rejected before any work happens."""
        monkeypatch.setattr("sys.argv", ["audit_preflight.py", "--arguments", "--upgrade --efficiency"])
        assert ap.main() == 1
        assert "mutually exclusive" in capsys.readouterr().out

    def test_missing_claude_dir_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No .claude/ directory aborts with the BREAKING line."""
        monkeypatch.setattr(
            "sys.argv",
            ["audit_preflight.py", "--arguments", "plugins", "--claude-dir", str(tmp_path / "absent")],
        )
        assert ap.main() == 1
        assert "! BREAKING: .claude/ directory not found" in capsys.readouterr().out

    def test_success_prints_state_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A successful run prints the state lines and writes the sentinels."""
        claude = tmp_path / ".claude"
        claude.mkdir()
        templates = tmp_path / "templates"
        templates.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess2")
        monkeypatch.setattr(ap, "resolve_subdir", lambda *_a, **_k: templates)
        monkeypatch.setattr(
            "sys.argv",
            ["audit_preflight.py", "--arguments", 'plugins --efficiency --keep "a"', "--claude-dir", str(claude)],
        )
        assert ap.main() == 0
        out = capsys.readouterr().out
        assert "efficiency=true" in out
        assert "scope=plugins" in out
        assert "keep-items=a" in out
        assert (tmp_path / "audit-state-sess2" / "keep-items").read_text(encoding="utf-8") == "a\n"
        # Scope is printed, not persisted: nothing re-derives it across bash blocks,
        # and the shell original never wrote it either.
        assert not (tmp_path / "audit-state-sess2" / "clean-args").exists()

    def test_unresolvable_templates_exit_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unresolvable templates directory aborts with the setup hint."""
        claude = tmp_path / ".claude"
        claude.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(ap, "resolve_subdir", lambda *_a, **_k: None)
        monkeypatch.setattr("sys.argv", ["audit_preflight.py", "--arguments", "plugins", "--claude-dir", str(claude)])
        assert ap.main() == 1
        assert "run /foundry:setup first" in capsys.readouterr().out
