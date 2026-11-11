"""Tests for the audit_preflight bin script.

Covers argument splitting (flags, aliases, --keep, scope tokens), the tool-probe memo, sentinel writing, the CLI's
failure exits and the refusal to write state through links into the instruction sources.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest import mock

import audit_preflight as ap
import pytest
from _hook_env import _symlinks_supported

_skip_no_posix_symlinks = pytest.mark.skipif(ap.os.name != "posix", reason="creating symlinks needs POSIX permissions")


@pytest.fixture
def cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., tuple[int, str]]:
    """Return a runner that calls ``main()`` in a scratch tree and yields ``(exit_code, stdout)``.

    The tree has a ``templates`` directory that resolves, every tool probes present, ``TMPDIR`` is the scratch root and
    the session token is ``sess``, so the sentinels land in ``tmp_path / "audit-state-sess"``.
    """
    templates = tmp_path / "templates"
    templates.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "sess")
    monkeypatch.setattr(ap, "resolve_subdir", mock.create_autospec(ap.resolve_subdir, return_value=templates))
    monkeypatch.setattr(ap, "probe_tool", mock.create_autospec(ap.probe_tool, return_value=True))

    def run(*args: str) -> tuple[int, str]:
        monkeypatch.setattr("sys.argv", ["audit_preflight.py", *args])
        return ap.main(), capsys.readouterr().out

    return run


class TestParseArguments:
    """Splitting the raw argument string into state flags, the --keep text and scope tokens."""

    def test_bare_scope_sets_no_flags(self) -> None:
        """A plain scope token sets no flags and no keep text.

        The default invocation names only a scope; any flag that came out true here would change the audit mode without
        the user asking for it.
        """
        state, keep, scope = ap.parse_arguments("agents")
        assert (scope, keep, any(state.values())) == ("agents", "", False)

    @pytest.mark.parametrize(
        ("arguments", "keys", "scope"),
        [
            pytest.param(
                "plugins --local --efficiency --skip-gate --fast",
                ("local-mode", "efficiency", "skip-gate", "fast"),
                "plugins",
                id="local-efficiency-skip-gate-fast",
            ),
            pytest.param("plugins --efficiency --measured", ("measured",), "plugins", id="measured"),
            pytest.param("rules --consolidate", ("consolidate",), "rules", id="consolidate"),
            pytest.param("--challenge", ("adversarial",), "", id="challenge-is-adversarial-alias"),
        ],
    )
    def test_flag_sets_state_key_and_stays_out_of_scope(
        self, arguments: str, keys: tuple[str, ...], scope: str
    ) -> None:
        """Each supported flag sets its state key and never leaks into the scope tokens.

        ``--challenge`` is an alias that must set the same ``adversarial`` key as ``--adversarial``; a flag read as a
        scope token would widen the audit to a path that does not exist.
        """
        state, _, parsed_scope = ap.parse_arguments(arguments)
        assert (all(state[key] is True for key in keys), parsed_scope) == (True, scope)

    def test_keep_text_is_extracted_and_removed(self) -> None:
        """--keep "..." is captured whole and does not leak into the scope tokens.

        The keep text carries spaces and commas; splitting on whitespace first would cut it into scope tokens.
        """
        state, keep, scope = ap.parse_arguments('agents --keep "run-dir, task ids" --local')
        assert (keep, scope, state["local-mode"]) == ("run-dir, task ids", "agents", True)

    def test_empty_keep_value_is_empty_string(self) -> None:
        """An empty --keep "" yields an empty string, not a missing match, and leaves the scope intact."""
        _, keep, scope = ap.parse_arguments('plugins --keep ""')
        assert (keep, scope) == ("", "plugins")

    def test_flag_name_inside_scope_token_is_not_a_flag(self) -> None:
        """A scope token containing a flag name is not treated as that flag.

        A path such as ``my--local-notes`` is legitimate scope text; matching flags by substring would silently switch
        the run into local mode.
        """
        state, _, scope = ap.parse_arguments("my--local-notes")
        assert (state["local-mode"], scope) == (False, "my--local-notes")

    def test_scope_tokens_keep_their_order(self) -> None:
        """Scope tokens keep their order with flags removed from between them."""
        _, _, scope = ap.parse_arguments("agents --local skills")
        assert scope == "agents skills"

    @pytest.mark.parametrize("scope", ["plugins", "cc_demo", "plugins/cc_demo", "plugins/cc_demo/"])
    def test_plugin_scope_forces_local_mode(self, scope: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plugin scopes select source templates even without the explicit local flag."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plugins" / "cc_demo").mkdir(parents=True)
        state, _, parsed = ap.parse_arguments(scope)
        assert (state["local-mode"], parsed) == (True, scope)

    @pytest.mark.parametrize(
        ("arguments", "expected", "mode"),
        [
            pytest.param("foundry --efficiency", "cc_foundry", "efficiency", id="short-plugin-name"),
            pytest.param("oss research --adversarial", "cc_oss cc_research", "adversarial", id="multiple-plugins"),
            pytest.param("plugins foundry --efficiency", "plugins cc_foundry", "efficiency", id="qualified-plugin"),
        ],
    )
    def test_plugin_aliases_resolve_before_local_inference(
        self, arguments: str, expected: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Short plugin names resolve to source directories without changing flags or quoted keep text."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plugins" / "cc_foundry").mkdir(parents=True)
        (tmp_path / "plugins" / "cc_oss").mkdir()
        (tmp_path / "plugins" / "cc_research").mkdir()
        state, keep, scope = ap.parse_arguments(f'{arguments} --keep "a b"')
        assert (state["local-mode"], state[mode], keep, scope) == (True, True, "a b", expected)

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("bridge", "bridge_cc-codex", id="manifest-name"),
            pytest.param("plugins bridge", "plugins bridge_cc-codex", id="qualified-manifest-name"),
            pytest.param("bridge_cc-codex", "bridge_cc-codex", id="directory-name"),
        ],
    )
    def test_manifest_name_resolves_a_nonstandard_plugin_directory(
        self, arguments: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plugin's manifest name and directory name select the same source root."""
        monkeypatch.chdir(tmp_path)
        manifest = tmp_path / "plugins" / "bridge_cc-codex" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name":"bridge"}', encoding="utf-8")
        state, _, scope = ap.parse_arguments(arguments)
        assert (state["local-mode"], scope) == (True, expected)

    @pytest.mark.parametrize(
        ("direct", "expected"),
        [
            pytest.param("demo", "demo", id="exact-directory"),
            pytest.param("cc_demo", "cc_demo", id="prefixed-directory"),
        ],
    )
    def test_directory_match_precedes_manifest_alias(
        self, direct: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An existing directory wins over another plugin's matching manifest name."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plugins" / direct).mkdir(parents=True)
        (tmp_path / "plugins" / "cc_demo").mkdir(exist_ok=True)
        manifest = tmp_path / "plugins" / "custom-demo" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name":"demo"}', encoding="utf-8")
        state, _, scope = ap.parse_arguments("demo")
        assert (state["local-mode"], scope) == (True, expected)

    @pytest.mark.parametrize("manifest_text", ["not json", "[]", '{"name":"other"}'])
    def test_unmatched_manifest_does_not_infer_local_mode(
        self, manifest_text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed or unrelated manifests leave an unknown scope available for later validation."""
        monkeypatch.chdir(tmp_path)
        manifest = tmp_path / "plugins" / "custom-demo" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(manifest_text, encoding="utf-8")
        state, _, scope = ap.parse_arguments("demo --unknown")
        assert (state["local-mode"], scope) == (False, "demo --unknown")

    def test_reserved_scope_precedes_a_plugin_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A reserved scope cannot turn into a plugin selection with a coinciding name."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plugins" / "cc_agents").mkdir(parents=True)
        state, _, scope = ap.parse_arguments("agents")
        assert (state["local-mode"], scope) == (False, "agents")


class TestProbeTool:
    """The four-hour tool probe memo: PATH lookup, timestamp marker, and the cases that bypass or ignore the marker."""

    @mock.patch("audit_preflight.shutil.which", autospec=True, return_value=None)
    def test_missing_tool_probes_false(self, which: mock.Mock, tmp_path: Path) -> None:
        """A tool not on PATH probes false.

        With no marker on disk the PATH lookup is the only evidence, so its absence must be reported, not assumed.
        """
        assert ap.probe_tool("nope", state_dir=tmp_path) is False
        which.assert_called_once_with("nope")

    @mock.patch("audit_preflight.shutil.which", autospec=True, return_value="/usr/bin/jq")
    def test_present_tool_writes_marker(self, which: mock.Mock, tmp_path: Path) -> None:
        """A tool on PATH probes true and leaves a timestamp marker.

        The marker is what lets later audit runs inside the TTL skip the lookup, so it must exist after a hit.
        """
        assert (ap.probe_tool("jq", state_dir=tmp_path), (tmp_path / "jq.ok").is_file()) == (True, True)
        which.assert_called_once_with("jq")

    @mock.patch("audit_preflight.shutil.which", autospec=True)
    def test_fresh_marker_skips_path_lookup(self, which: mock.Mock, tmp_path: Path) -> None:
        """A recent marker returns true without consulting PATH.

        A marker younger than the TTL is the memo working; consulting PATH anyway would defeat its purpose.
        """
        (tmp_path / "jq.ok").write_text(str(int(ap.time.time())), encoding="utf-8")
        assert ap.probe_tool("jq", state_dir=tmp_path) is True
        which.assert_not_called()

    @pytest.mark.parametrize(
        "marker",
        [
            pytest.param("0", id="expired-timestamp"),
            pytest.param("not-a-number", id="corrupt-content"),
        ],
    )
    @mock.patch("audit_preflight.shutil.which", autospec=True, return_value=None)
    def test_unusable_marker_falls_through_to_path_lookup(self, which: mock.Mock, marker: str, tmp_path: Path) -> None:
        """A marker older than the TTL, or one that is not a timestamp, is ignored and PATH is consulted again.

        The epoch timestamp is far past the TTL on any host. A corrupt marker must degrade to a fresh lookup instead of
        raising and aborting the audit before it starts.
        """
        (tmp_path / "jq.ok").write_text(marker, encoding="utf-8")
        assert ap.probe_tool("jq", state_dir=tmp_path) is False
        which.assert_called_once_with("jq")


class TestWriteState:
    """Sentinel writing: directory naming, session token safety, ownership, links and permissions."""

    def test_writes_session_scoped_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sentinels land in a session-scoped directory under TMPDIR, one newline-terminated file per key.

        The skill's later bash blocks read these files by name, so directory name and file contents are the contract.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess1")
        state_dir = ap.write_state({"local-mode": "true", "audit-tpl": "/x"})
        assert state_dir == tmp_path / "audit-state-sess1"
        assert [(state_dir / key).read_text(encoding="utf-8") for key in ("local-mode", "audit-tpl")] == [
            "true\n",
            "/x\n",
        ]

    def test_session_token_falls_back_to_shared(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no session variables the token is ``shared``, never a pid.

        A pid differs on every call, so two bash blocks of one skill run would look for state in different directories.
        """
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
    def test_unsafe_session_token_is_rejected(
        self, token: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session token that could leave TMPDIR is refused, not written outside it.

        ``CSID=../escape`` would put the sentinels in a sibling of TMPDIR; the skill's bash blocks read the same
        unsanitized ``CSID``, so falling back to another directory would silently split writer and readers.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", token)
        with pytest.raises(ValueError, match="unsafe session token"):
            ap.write_state({"k": "v"})

    @pytest.mark.skipif(ap.os.name != "posix", reason="Windows has no POSIX mode bits to assert")
    def test_new_state_dir_is_private(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A freshly created state directory is readable by its owner only.

        The sentinels name run directories and keep-items text, so group or world access would expose them on a shared
        machine.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess3")
        state_dir = ap.write_state({"k": "v"})
        assert state_dir.stat().st_mode & 0o077 == 0

    def test_directory_owned_by_another_user_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pre-existing state directory owned by someone else is not written into.

        With TMPDIR unset on a shared ``/tmp``, another user pre-creates ``audit-state-shared``; sentinels such as
        ``keep-items`` would land in a directory they control. The foreign owner is simulated by shifting the uid.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess4")
        (tmp_path / "audit-state-sess4").mkdir()
        foreign_uid = (tmp_path / "audit-state-sess4").lstat().st_uid + 1
        monkeypatch.setattr(ap.os, "getuid", lambda: foreign_uid, raising=False)
        with pytest.raises(PermissionError, match="owned by another user"):
            ap.write_state({"k": "v"})

    def test_symlinked_state_dir_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A state directory that is a symlink is not written through.

        The link is simulated on the ``lstat`` mode check so the case runs on hosts that cannot create symlinks.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess5")
        monkeypatch.setattr(ap.stat, "S_ISLNK", lambda _mode: True)
        with pytest.raises(PermissionError, match="symlink"):
            ap.write_state({"k": "v"})

    @_skip_no_posix_symlinks
    def test_sentinel_symlink_is_replaced_not_written_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sentinel pre-planted as a symlink to another file leaves that file untouched.

        The user-owned state directory already holds ``local-mode`` pointing at the repository's AGENTS.md; a plain
        write would replace the instruction file with the sentinel value.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess7")
        target = tmp_path / "AGENTS.md"
        target.write_text("instructions\n", encoding="utf-8")
        state_dir = tmp_path / "audit-state-sess7"
        state_dir.mkdir(mode=0o700)
        (state_dir / "local-mode").symlink_to(target)
        ap.write_state({"local-mode": "true"})
        sentinel = state_dir / "local-mode"
        assert (target.read_text(encoding="utf-8"), sentinel.is_symlink(), sentinel.read_text(encoding="utf-8")) == (
            "instructions\n",
            False,
            "true\n",
        )

    @_skip_no_posix_symlinks
    def test_probe_marker_symlink_is_replaced_not_written_through(self, tmp_path: Path) -> None:
        """A tool-probe marker pre-planted as a symlink does not redirect the timestamp into another file.

        The probe runs real ``shutil.which`` for ``python``, so the marker write is the only thing under test.
        """
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


class TestBindArguments:
    """Keeping a raw argument string that starts with a dash bound to its ``--arguments`` option."""

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


class TestCli:
    """The command-line contract: exit codes, output lines and the sentinels a successful run writes."""

    @pytest.mark.parametrize("other", ["--efficiency", "--adversarial", "--challenge"])
    def test_mutually_exclusive_flags_exit_one(self, other: str, cli: Callable[..., tuple[int, str]]) -> None:
        """Upgrade rejects either compatible audit mode and its alias before preparing state."""
        code, out = cli("--arguments", f"--upgrade {other}")
        assert (code, "mutually exclusive" in out) == (1, True)

    def test_adversarial_and_efficiency_can_combine(self, cli: Callable[..., tuple[int, str]]) -> None:
        """The permitted pair remains runnable while excluded mode pairs are rejected."""
        code, out = cli("--arguments", "plugins --adversarial --efficiency")
        assert code == 0
        assert "adversarial=true efficiency=true" in out

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("foundry --efficiency", "cc_foundry", id="short-plugin-name"),
            pytest.param("plugins foundry --efficiency", "plugins cc_foundry", id="qualified-plugin"),
        ],
    )
    def test_plugin_alias_prints_resolved_scope_without_claude_directory(
        self, arguments: str, expected: str, tmp_path: Path, cli: Callable[..., tuple[int, str]]
    ) -> None:
        """The CLI selects source templates and prints the resolved directory before mode dispatch."""
        (tmp_path / "plugins" / "cc_foundry").mkdir(parents=True)
        code, out = cli("--arguments", arguments)
        assert code == 0
        assert f"scope={expected}" in out.splitlines()
        assert (tmp_path / "audit-state-sess" / "local-mode").read_text(encoding="utf-8") == "true\n"
        ap.resolve_subdir.assert_called_once_with("audit", "templates", local=True)

    @pytest.mark.parametrize("arguments", ["--local agents", "plugins"])
    def test_local_audit_needs_no_claude_directory(
        self, arguments: str, tmp_path: Path, cli: Callable[..., tuple[int, str]]
    ) -> None:
        """Source audits resolve local templates and write local state without an installed tree."""
        code, out = cli("--arguments", arguments, "--claude-dir", str(tmp_path / "absent"))
        assert (code, "local-mode=true" in out) == (0, True)
        assert (tmp_path / "audit-state-sess" / "local-mode").read_text(encoding="utf-8") == "true\n"
        ap.resolve_subdir.assert_called_once_with("audit", "templates", local=True)

    @pytest.mark.parametrize(
        "other",
        [
            pytest.param("--upgrade", id="upgrade"),
            pytest.param("--adversarial", id="adversarial"),
            pytest.param("--efficiency", id="efficiency"),
        ],
    )
    def test_consolidate_is_standalone(self, other: str, cli: Callable[..., tuple[int, str]]) -> None:
        """--consolidate combined with any other mode is rejected before any work happens.

        The mode is advisory and skips the fix gate, so stacking it onto a mode that fixes or shares a run directory
        would blur which findings may be applied.
        """
        code, out = cli("--arguments", f"--consolidate {other}")
        assert (code, "--consolidate is standalone" in out) == (1, True)

    def test_measured_without_efficiency_exits_one(self, cli: Callable[..., tuple[int, str]]) -> None:
        """--measured alone is rejected: it only adds evidence to the efficiency pass.

        A run that silently ignored it would leave the user believing transcripts were measured when nothing ran.
        """
        code, out = cli("--arguments", "plugins --measured")
        assert (code, "pass both" in out) == (1, True)

    def test_measured_sentinel_is_written(self, tmp_path: Path, cli: Callable[..., tuple[int, str]]) -> None:
        """--efficiency --measured writes the ``measured`` sentinel the Phase B block reads back as ``true``.

        The measured phase runs only when that file holds ``true``, so a missing or stale sentinel would either skip the
        requested measurement or run it unasked.
        """
        code, _ = cli("--arguments", "plugins --efficiency --measured", "--claude-dir", str(tmp_path))
        assert (code, (tmp_path / "audit-state-sess" / "measured").read_text(encoding="utf-8")) == (0, "true\n")

    def test_consolidate_prints_dispatch_line_and_needs_no_claude_dir(
        self, tmp_path: Path, cli: Callable[..., tuple[int, str]]
    ) -> None:
        """--consolidate prints the dispatch line that tells the skill to skip every other step.

        The mode reads plugins/, not .claude/, so a checkout without .claude/ must still run it; the dispatch line is
        what makes the skip of Step 1 (pre-commit --all-files rewriting sources) deterministic rather than prose.
        """
        code, out = cli("--arguments", "rules --consolidate", "--claude-dir", str(tmp_path / "absent"))
        assert (code, "dispatch=mode:consolidate" in out) == (0, True)

    def test_plain_audit_prints_no_dispatch_line(self, tmp_path: Path, cli: Callable[..., tuple[int, str]]) -> None:
        """Only --consolidate prints a dispatch line, so the regular audit flow is unchanged."""
        (tmp_path / ".claude").mkdir()
        code, out = cli("--arguments", "plugins", "--claude-dir", str(tmp_path / ".claude"))
        assert (code, "dispatch=" in out) == (0, False)

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param("--consolidate --bogus", id="unknown-flag-after"),
            pytest.param("--typo rules --consolidate", id="unknown-flag-before-scope"),
        ],
    )
    def test_consolidate_rejects_an_unknown_flag(self, arguments: str, cli: Callable[..., tuple[int, str]]) -> None:
        """An unknown ``--flag`` next to --consolidate exits 1 instead of reaching the scope.

        The consolidate dispatch skips the skill's own unknown-flag question; without this check the stray token became
        a scope token and, before scope tokens were validated, widened the run to the whole tree.
        """
        code, out = cli("--arguments", arguments)
        assert (code, "unknown flag" in out) == (1, True)

    def test_consolidate_without_jq_exits_one(self, cli: Callable[..., tuple[int, str]]) -> None:
        """A missing jq stops --consolidate before the cluster spend, since the merge step needs it."""
        ap.probe_tool.side_effect = lambda name, **_kwargs: name != "jq"
        code, out = cli("--arguments", "--consolidate")
        assert (code, "! MISSING: jq" in out) == (1, True)

    def test_missing_claude_dir_exits_one(self, tmp_path: Path, cli: Callable[..., tuple[int, str]]) -> None:
        """No .claude/ directory aborts with the BREAKING line."""
        code, out = cli("--arguments", "agents", "--claude-dir", str(tmp_path / "absent"))
        assert (code, "! BREAKING: .claude/ directory not found" in out) == (1, True)

    def test_success_prints_state_summary_and_writes_sentinels(
        self, tmp_path: Path, cli: Callable[..., tuple[int, str]]
    ) -> None:
        """A successful run prints the state lines and writes the sentinels, but does not persist the scope.

        Scope is printed, not persisted: nothing re-derives it across bash blocks, and the shell original never wrote it
        either.
        """
        (tmp_path / ".claude").mkdir()
        code, out = cli("--arguments", 'plugins --efficiency --keep "a"', "--claude-dir", str(tmp_path / ".claude"))
        state_dir = tmp_path / "audit-state-sess"
        assert code == 0
        assert all(line in out for line in ("efficiency=true", "scope=plugins", "keep-items=a"))
        assert (state_dir / "keep-items").read_text(encoding="utf-8") == "a\n"
        assert not (state_dir / "clean-args").exists()

    def test_unresolvable_templates_exit_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Callable[..., tuple[int, str]]
    ) -> None:
        """An unresolvable templates directory aborts with a plugin reinstall hint."""
        (tmp_path / ".claude").mkdir()
        ap.resolve_subdir.return_value = None
        code, out = cli("--arguments", "plugins", "--claude-dir", str(tmp_path / ".claude"))
        assert (code, "reinstall the foundry plugin" in out) == (1, True)

    def test_unsafe_session_token_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Callable[..., tuple[int, str]]
    ) -> None:
        """A session token that cannot be used safely aborts with a BLOCKED line instead of a traceback."""
        (tmp_path / ".claude").mkdir()
        monkeypatch.setenv("CSID", "../escape")
        code, out = cli("--arguments", "plugins", "--claude-dir", str(tmp_path / ".claude"))
        assert (code, "! BLOCKED — cannot prepare audit state" in out) == (1, True)


class TestStateDirectoryLinks:
    """A ``.temp/state`` that is a link: refused when it points into the instruction sources, allowed elsewhere."""

    @_skip_no_posix_symlinks
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
        """A link whose target lies in the tree outside .temp/ and .reports/ is a problem; one inside them is not.

        Preflight writes probe markers and run directories below ``.temp/state``; through a link into a rules directory
        those writes would land inside instruction files.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / target).mkdir(parents=True)
        (tmp_path / ".temp").mkdir()
        (tmp_path / ".temp" / "state").symlink_to(tmp_path / target)
        assert bool(ap._state_dir_problem()) is problem

    @_skip_no_posix_symlinks
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
    """A link at a state path below ``.temp`` or ``.reports``, not only at ``.temp/state`` itself."""

    def test_regular_reset_sentinels_allow_preflight(self, tmp_path: Path, cli: Callable[..., tuple[int, str]]) -> None:
        """Ordinary stale sentinel files remain valid until the successful preflight's shell reset."""
        state = tmp_path / "audit-state-sess"
        state.mkdir()
        (state / "run-dir").write_text("old run\n", encoding="utf-8")
        (state / "unscanned-plugins").write_text("old coverage\n", encoding="utf-8")
        (state / "precommit-validate.txt").write_text("old diagnostics\n", encoding="utf-8")
        code, out = cli("--arguments", "plugins")
        assert (code, "BLOCKED" in out) == (0, False)
        assert (state / "run-dir").read_text(encoding="utf-8") == "old run\n"
        assert (state / "unscanned-plugins").read_text(encoding="utf-8") == "old coverage\n"
        assert (state / "precommit-validate.txt").read_text(encoding="utf-8") == "old diagnostics\n"

    @pytest.mark.parametrize("name", ["run-dir", "unscanned-plugins", "precommit-validate.txt"])
    @pytest.mark.parametrize(
        "kind",
        [
            pytest.param(
                "symlink",
                marks=pytest.mark.skipif(not _symlinks_supported(), reason="file symlinks unavailable"),
                id="symbolic-link",
            ),
            "hardlink",
        ],
    )
    def test_linked_reset_sentinel_stops_before_state_changes(
        self, name: str, kind: str, tmp_path: Path, cli: Callable[..., tuple[int, str]]
    ) -> None:
        """A shell reset must never truncate a linked target or replace prior evidence on a rejected run."""
        target = tmp_path / "user.md"
        target.write_text("user's uncommitted text\n", encoding="utf-8")
        state = tmp_path / "audit-state-sess"
        state.mkdir()
        prior = state / "keep-items"
        prior.write_text("prior state\n", encoding="utf-8")
        sentinel = state / name
        {"symlink": sentinel.symlink_to, "hardlink": sentinel.hardlink_to}[kind](target)
        code, out = cli("--arguments", "plugins")
        assert (code, "linked sentinel" in out) == (1, True)
        assert target.read_text(encoding="utf-8") == "user's uncommitted text\n"
        assert prior.read_text(encoding="utf-8") == "prior state\n"
        ap.probe_tool.assert_not_called()

    @_skip_no_posix_symlinks
    @pytest.mark.parametrize(
        "link",
        [
            pytest.param(".temp/state/preflight", id="preflight-into-sources"),
            pytest.param(".reports/audit", id="reports-audit-into-sources"),
            pytest.param(".reports", id="reports-into-sources"),
        ],
    )
    def test_link_into_instruction_sources_blocks_preflight(
        self, tmp_path: Path, cli: Callable[..., tuple[int, str]], link: str
    ) -> None:
        """A regular `.temp/state` does not hide a linked child: the probe marker and run dir would land in a rule.

        Each case links one state path to a plugin ``rules`` directory; preflight must stop with BLOCKED and leave that
        directory empty.
        """
        rules = tmp_path / "plugins" / "cc_demo" / "rules"
        rules.mkdir(parents=True)
        (tmp_path / ".claude").mkdir()
        path = tmp_path / link
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(rules)
        code, out = cli("--arguments", "--consolidate")
        assert (code, "BLOCKED" in out, list(rules.iterdir())) == (1, True, [])

    @_skip_no_posix_symlinks
    def test_child_link_into_reports_is_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run area kept under another `.reports` directory is not a problem."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".reports" / "elsewhere").mkdir(parents=True)
        (tmp_path / ".reports" / "audit").symlink_to(tmp_path / ".reports" / "elsewhere")
        assert ap._state_dir_problem() == ""


class TestTempDirectoryIntoSources:
    """A ``TMPDIR`` that places the session state directory inside the tree's instruction sources."""

    @_skip_no_posix_symlinks
    @pytest.mark.parametrize(
        "route",
        [
            pytest.param("direct", id="tmpdir-is-a-rules-directory"),
            pytest.param("linked-ancestor", id="tmpdir-reaches-it-through-a-link"),
        ],
    )
    def test_tmpdir_into_sources_blocks_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Callable[..., tuple[int, str]], route: str
    ) -> None:
        """The state directory is made below ``TMPDIR``; one that resolves into a rule directory is refused first.

        The link route reaches the rules directory through a symlinked ancestor, which a check on the literal path would
        miss.
        """
        rules = tmp_path / "plugins" / "cc_demo" / "rules"
        rules.mkdir(parents=True)
        (tmp_path / ".claude").mkdir()
        scratch_link = tmp_path / "scratch-link"
        scratch_link.symlink_to(rules)
        monkeypatch.setenv("TMPDIR", str({"direct": rules, "linked-ancestor": scratch_link}[route]))
        code, out = cli("--arguments", "--consolidate")
        assert (code, "TMPDIR state directory" in out, list(rules.iterdir())) == (1, True, [])

    def test_tmpdir_outside_the_tree_is_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A temp directory elsewhere on the machine is the normal case and is not touched by the check."""
        tree = tmp_path / "tree"
        tree.mkdir()
        monkeypatch.chdir(tree)
        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        assert ap._state_dir_problem() == ""
