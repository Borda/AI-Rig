"""Tests for audit_preflight bin script.

Covers argument splitting (flags, aliases, --keep, scope tokens), the tool-probe memo, sentinel writing, and the CLI's
two failure exits.
"""

from __future__ import annotations

from pathlib import Path

import audit_preflight as ap
import pytest


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
        assert state["local-mode"]
        assert state["efficiency"]
        assert state["skip-gate"]
        assert state["fast"]
        assert scope == "plugins"

    def test_measured_is_a_flag_not_scope(self) -> None:
        """--measured sets its own state key and never leaks into the scope tokens."""
        state, _, scope = ap.parse_arguments("plugins --efficiency --measured")
        assert state["measured"]
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

    @pytest.mark.parametrize(
        "token",
        [
            pytest.param("../escape", id="parent-traversal"),
            pytest.param("a/b", id="path-separator"),
            pytest.param("sess 1", id="space"),
        ],
    )
    def test_unsafe_session_token_rejected(self, token: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A session token that could leave TMPDIR is refused, not written outside it.

        Scenario: ``CSID=../escape`` would put the sentinels in a sibling of TMPDIR; the skill's bash blocks read the
        same unsanitized ``CSID``, so falling back to another directory would silently split writer and readers.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", token)
        with pytest.raises(ValueError, match="unsafe session token"):
            ap.write_state({"k": "v"})

    @pytest.mark.skipif(ap.os.name != "posix", reason="Windows has no POSIX mode bits to assert")
    def test_new_state_dir_is_private(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A freshly created state directory is readable by its owner only."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess3")
        state_dir = ap.write_state({"k": "v"})
        assert state_dir.stat().st_mode & 0o077 == 0

    def test_directory_owned_by_another_user_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pre-existing state directory owned by someone else is not written into.

        Scenario: with TMPDIR unset on a shared ``/tmp``, another user pre-creates ``audit-state-shared``; sentinels
        such as ``keep-items`` would land in a directory they control. The foreign owner is simulated by shifting the
        uid.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess4")
        (tmp_path / "audit-state-sess4").mkdir()
        foreign_uid = (tmp_path / "audit-state-sess4").lstat().st_uid + 1
        monkeypatch.setattr(ap.os, "getuid", lambda: foreign_uid, raising=False)
        with pytest.raises(PermissionError, match="owned by another user"):
            ap.write_state({"k": "v"})

    def test_symlinked_state_dir_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A state directory that is a symlink is not written through.

        The link is simulated on the ``lstat`` mode check so the case runs on hosts that cannot create symlinks.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess5")
        monkeypatch.setattr(ap.stat, "S_ISLNK", lambda _mode: True)
        with pytest.raises(PermissionError, match="symlink"):
            ap.write_state({"k": "v"})

    @pytest.mark.skipif(ap.os.name != "posix", reason="creating symlinks needs POSIX permissions")
    def test_sentinel_that_is_a_symlink_is_replaced_not_written_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sentinel pre-planted as a symlink to another file leaves that file untouched.

        Scenario: the user-owned state directory already holds ``local-mode`` pointing at the repository's AGENTS.md; a
        plain write would replace the instruction file with the sentinel value.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess7")
        target = tmp_path / "AGENTS.md"
        target.write_text("instructions\n", encoding="utf-8")
        state_dir = tmp_path / "audit-state-sess7"
        state_dir.mkdir(mode=0o700)
        (state_dir / "local-mode").symlink_to(target)
        ap.write_state({"local-mode": "true"})
        assert (
            target.read_text(encoding="utf-8"),
            (state_dir / "local-mode").is_symlink(),
            (state_dir / "local-mode").read_text(encoding="utf-8"),
        ) == ("instructions\n", False, "true\n")

    @pytest.mark.skipif(ap.os.name != "posix", reason="creating symlinks needs POSIX permissions")
    def test_probe_marker_that_is_a_symlink_is_replaced_not_written_through(self, tmp_path: Path) -> None:
        """A tool-probe marker pre-planted as a symlink does not redirect the timestamp into another file."""
        target = tmp_path / "AGENTS.md"
        target.write_text("instructions\n", encoding="utf-8")
        marker_dir = tmp_path / "preflight"
        marker_dir.mkdir()
        (marker_dir / "python.ok").symlink_to(target)
        assert ap.probe_tool("python", state_dir=marker_dir) is True
        assert (target.read_text(encoding="utf-8"), (marker_dir / "python.ok").is_symlink()) == (
            "instructions\n",
            False,
        )


class TestCli:
    """Covers the command-line contract."""

    def test_mutually_exclusive_flags_exit_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--upgrade with --efficiency is rejected before any work happens."""
        monkeypatch.setattr("sys.argv", ["audit_preflight.py", "--arguments", "--upgrade --efficiency"])
        assert ap.main() == 1
        assert "mutually exclusive" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "other",
        [
            pytest.param("--upgrade", id="upgrade"),
            pytest.param("--adversarial", id="adversarial"),
            pytest.param("--efficiency", id="efficiency"),
        ],
    )
    def test_consolidate_is_standalone(
        self, other: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--consolidate combined with any other mode is rejected before any work happens.

        The mode is advisory and skips the fix gate, so stacking it onto a mode that fixes or shares a run
        directory would blur which findings may be applied.
        """
        monkeypatch.setattr("sys.argv", ["audit_preflight.py", "--arguments", f"--consolidate {other}"])
        assert ap.main() == 1
        assert "--consolidate is standalone" in capsys.readouterr().out

    def test_measured_without_efficiency_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--measured alone is rejected: it only adds evidence to the efficiency pass.

        A run that silently ignored it would leave the user believing transcripts were measured when nothing ran.
        """
        monkeypatch.setattr("sys.argv", ["audit_preflight.py", "--arguments", "plugins --measured"])
        assert ap.main() == 1
        assert "pass both" in capsys.readouterr().out

    def test_measured_sentinel_is_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--efficiency --measured writes the ``measured`` sentinel the Phase B block reads back as ``true``.

        The measured phase runs only when that file holds ``true``, so a missing or stale sentinel would either skip the
        requested measurement or run it unasked.
        """
        templates = tmp_path / "templates"
        templates.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess6")
        monkeypatch.setattr(ap, "resolve_subdir", lambda *_a, **_k: templates)
        monkeypatch.setattr(ap, "probe_tool", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "sys.argv",
            ["audit_preflight.py", "--arguments", "plugins --efficiency --measured", "--claude-dir", str(tmp_path)],
        )
        assert ap.main() == 0
        assert (tmp_path / "audit-state-sess6" / "measured").read_text(encoding="utf-8") == "true\n"

    def test_consolidate_flag_is_recognised(self) -> None:
        """--consolidate sets its own state key and leaves the scope tokens alone."""
        state, _, scope = ap.parse_arguments("rules --consolidate")
        assert state["consolidate"]
        assert scope == "rules"

    def test_consolidate_prints_dispatch_line_and_needs_no_claude_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--consolidate prints the dispatch line that tells the skill to skip every other step.

        The mode reads plugins/, not .claude/, so a checkout without .claude/ must still run it; the dispatch line is
        what makes the skip of Step 1 (pre-commit --all-files rewriting sources) deterministic rather than prose.
        """
        templates = tmp_path / "templates"
        templates.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess3")
        monkeypatch.setattr(ap, "resolve_subdir", lambda *_a, **_k: templates)
        monkeypatch.setattr(ap, "probe_tool", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "sys.argv",
            ["audit_preflight.py", "--arguments", "rules --consolidate", "--claude-dir", str(tmp_path / "absent")],
        )
        assert ap.main() == 0
        assert "dispatch=mode:consolidate" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param(["--arguments", "--consolidate"], ["--arguments=--consolidate"], id="bare-flag"),
            pytest.param(
                ["--arguments", "rules --consolidate"], ["--arguments", "rules --consolidate"], id="scope-and-flag"
            ),
            pytest.param(
                ["--arguments", "", "--claude-dir", "x"], ["--arguments", "", "--claude-dir", "x"], id="empty"
            ),
        ],
    )
    def test_bare_flag_argument_is_bound_to_its_option(self, argv: list[str], expected: list[str]) -> None:
        """A raw argument string that is only a flag stays the value of --arguments instead of being read as an option.

        The skill passes the user's text as ``--arguments "$ARGUMENTS"``; for ``/audit --consolidate`` that text starts
        with a dash and argparse would otherwise exit 2 before any state is prepared.
        """
        assert ap._bind_arguments(argv) == expected

    def test_plain_audit_prints_no_dispatch_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Only --consolidate prints a dispatch line, so the regular audit flow is unchanged."""
        claude = tmp_path / ".claude"
        claude.mkdir()
        templates = tmp_path / "templates"
        templates.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess4")
        monkeypatch.setattr(ap, "resolve_subdir", lambda *_a, **_k: templates)
        monkeypatch.setattr(ap, "probe_tool", lambda *_a, **_k: True)
        monkeypatch.setattr("sys.argv", ["audit_preflight.py", "--arguments", "plugins", "--claude-dir", str(claude)])
        assert ap.main() == 0
        assert "dispatch=" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param("--consolidate --bogus", id="unknown-flag-after"),
            pytest.param("--typo rules --consolidate", id="unknown-flag-before-scope"),
        ],
    )
    def test_consolidate_rejects_an_unknown_flag(
        self, arguments: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unknown ``--flag`` next to --consolidate exits 1 instead of reaching the scope.

        The consolidate dispatch skips the skill's own unknown-flag question; without this check the stray token became
        a scope token and, before scope tokens were validated, widened the run to the whole tree.
        """
        monkeypatch.setattr("sys.argv", ["audit_preflight.py", "--arguments", arguments])
        assert ap.main() == 1
        assert "unknown flag" in capsys.readouterr().out

    def test_consolidate_without_jq_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing jq stops --consolidate before the cluster spend, since the merge step needs it."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(ap, "probe_tool", lambda name, **_k: name != "jq")
        monkeypatch.setattr("sys.argv", ["audit_preflight.py", "--arguments", "--consolidate"])
        assert ap.main() == 1
        assert "! MISSING: jq" in capsys.readouterr().out

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

    def test_unsafe_session_token_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A session token that cannot be used safely aborts with a BLOCKED line instead of a traceback."""
        claude = tmp_path / ".claude"
        claude.mkdir()
        templates = tmp_path / "templates"
        templates.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "../escape")
        monkeypatch.setattr(ap, "resolve_subdir", lambda *_a, **_k: templates)
        monkeypatch.setattr("sys.argv", ["audit_preflight.py", "--arguments", "plugins", "--claude-dir", str(claude)])
        assert ap.main() == 1
        assert "! BLOCKED — cannot prepare audit state" in capsys.readouterr().out


class TestStateDirectoryLinks:
    """Covers a `.temp/state` that is a link into the instruction sources."""

    @pytest.mark.skipif(ap.os.name != "posix", reason="creating symlinks needs POSIX permissions")
    @pytest.mark.parametrize(
        ("target", "problem"),
        [
            pytest.param("plugins/cc_demo/rules", True, id="into-the-instruction-sources"),
            pytest.param(".reports/state", False, id="into-reports"),
        ],
    )
    def test_state_link_target_decides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, problem: bool
    ) -> None:
        """A link whose target lies in the tree outside .temp/ and .reports/ is a problem; one inside them is not."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / target).mkdir(parents=True)
        (tmp_path / ".temp").mkdir()
        (tmp_path / ".temp" / "state").symlink_to(tmp_path / target)
        assert bool(ap._state_dir_problem()) is problem

    @pytest.mark.skipif(ap.os.name != "posix", reason="creating symlinks needs POSIX permissions")
    def test_link_outside_the_tree_is_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A state directory kept on another disk, outside the tree, is not touched by the check."""
        tree = tmp_path / "tree"
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (tree / ".temp").mkdir(parents=True)
        (tree / ".temp" / "state").symlink_to(elsewhere)
        monkeypatch.chdir(tree)
        assert ap._state_dir_problem() == ""


class TestStateChildLinks:
    """Covers a link at a state path below `.temp` or `.reports`, not only at `.temp/state` itself."""

    @pytest.mark.skipif(ap.os.name != "posix", reason="creating symlinks needs POSIX permissions")
    @pytest.mark.parametrize(
        ("link", "problem"),
        [
            pytest.param(".temp/state/preflight", True, id="preflight-into-sources"),
            pytest.param(".reports/audit", True, id="reports-audit-into-sources"),
            pytest.param(".reports", True, id="reports-into-sources"),
        ],
    )
    def test_link_into_instruction_sources_blocks_preflight(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        link: str,
        problem: bool,
    ) -> None:
        """A regular `.temp/state` does not hide a linked child: the probe marker and run dir would land in a rule."""
        monkeypatch.chdir(tmp_path)
        rules = tmp_path / "plugins" / "cc_demo" / "rules"
        rules.mkdir(parents=True)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".temp" / "state").mkdir(parents=True)
        (tmp_path / ".reports").mkdir(exist_ok=True)
        path = tmp_path / link
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        path.symlink_to(rules)
        monkeypatch.setattr(ap.sys, "argv", ["audit_preflight.py", "--arguments", "--consolidate"])

        assert ap.main() == 1
        assert "BLOCKED" in capsys.readouterr().out
        assert list(rules.iterdir()) == []

    @pytest.mark.skipif(ap.os.name != "posix", reason="creating symlinks needs POSIX permissions")
    def test_child_link_into_reports_is_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run area kept under another `.reports` directory is not a problem."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".reports" / "elsewhere").mkdir(parents=True)
        (tmp_path / ".reports" / "audit").symlink_to(tmp_path / ".reports" / "elsewhere")
        assert ap._state_dir_problem() == ""


class TestTempDirectoryIntoSources:
    """Covers a ``TMPDIR`` that places the session state directory inside the tree's instruction sources."""

    @pytest.mark.skipif(ap.os.name != "posix", reason="creating symlinks needs POSIX permissions")
    @pytest.mark.parametrize(
        "route",
        [
            pytest.param("direct", id="tmpdir-is-a-rules-directory"),
            pytest.param("linked-ancestor", id="tmpdir-reaches-it-through-a-link"),
        ],
    )
    def test_tmpdir_into_sources_blocks_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], route: str
    ) -> None:
        """The state directory is made below ``TMPDIR``; one that resolves into a rule directory is refused first."""
        monkeypatch.chdir(tmp_path)
        rules = tmp_path / "plugins" / "cc_demo" / "rules"
        rules.mkdir(parents=True)
        (tmp_path / ".claude").mkdir()
        tmpdir = rules
        if route == "linked-ancestor":
            tmpdir = tmp_path / "scratch-link"
            tmpdir.symlink_to(rules)
        monkeypatch.setenv("TMPDIR", str(tmpdir))
        monkeypatch.setattr(ap.sys, "argv", ["audit_preflight.py", "--arguments", "--consolidate"])

        assert ap.main() == 1
        assert "TMPDIR state directory" in capsys.readouterr().out
        assert list(rules.iterdir()) == []

    def test_tmpdir_outside_the_tree_is_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A temp directory elsewhere on the machine is the normal case and is not touched by the check."""
        tree = tmp_path / "tree"
        tree.mkdir()
        monkeypatch.chdir(tree)
        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        assert ap._state_dir_problem() == ""
