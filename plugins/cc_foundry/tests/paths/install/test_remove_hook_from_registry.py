"""Tests for ``bin/remove_hook_from_registry.py``.

Covers the contract /foundry:manage delete hook relies on: removes only the named hook, leaves unrelated hooks intact,
prunes empty groups/events, preserves non-``hooks`` top-level keys, and produces the documented exit codes for absent
files and invalid JSON.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import remove_hook_from_registry as rhfr

_SAMPLE_REGISTRY: dict = {
    "permissions": {"allow": ["Bash(jq:*)"]},
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [{"command": ".claude/hooks/rtk-rewrite.js"}, {"command": ".claude/hooks/commit-guard.js"}],
            }
        ],
        "PostToolUse": [
            {"matcher": "*", "hooks": [{"command": ".claude/hooks/rtk-rewrite.js"}]},
            {"matcher": "Edit", "hooks": [{"command": ".claude/hooks/statusline.js"}]},
        ],
    },
}

_RTK_REWRITE = re.compile(r"rtk-rewrite\.js", re.IGNORECASE)


def _write_registry(path: Path, registry: dict) -> None:
    """Write one hook-registry fixture as indented JSON.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     path = Path(directory) / "registry.json"
        ...     _write_registry(path, {"hooks": []})
        ...     json.loads(path.read_text())
        {'hooks': []}
    """
    path.write_text(json.dumps(registry, indent=2), encoding="utf-8")


@pytest.fixture(name="sample_registry")
def _sample_registry() -> dict:
    """Return a fresh deep copy of the sample registry so a test can never leak a mutation into another."""
    return copy.deepcopy(_SAMPLE_REGISTRY)


@pytest.fixture(name="settings_file")
def _settings_file(tmp_path: Path, sample_registry: dict) -> Path:
    """Write the sample registry to ``settings.json`` under ``tmp_path`` and return its path."""
    target = tmp_path / "settings.json"
    _write_registry(target, sample_registry)
    return target


class TestFilterHooks:
    """filter_hooks: pure transform — selective removal of named commands."""

    def test_removes_only_named_hook(self, sample_registry: dict) -> None:
        """Drops every command for the targeted basename; keeps unrelated commands.

        Scenario: ``rtk-rewrite.js`` appears in two events. The PreToolUse group survives with ``commit-guard.js`` and
        the PostToolUse group that held only the target is dropped, leaving the ``Edit`` group.
        """
        out = rhfr.filter_hooks(sample_registry, _RTK_REWRITE)

        pre = out["hooks"]["PreToolUse"]
        post = out["hooks"]["PostToolUse"]
        assert len(pre) == 1
        assert pre[0]["hooks"] == [{"command": ".claude/hooks/commit-guard.js"}]
        assert len(post) == 1
        assert post[0]["matcher"] == "Edit"
        assert post[0]["hooks"] == [{"command": ".claude/hooks/statusline.js"}]

    def test_preserves_non_hooks_top_level_keys(self, sample_registry: dict) -> None:
        """Top-level keys other than ``hooks`` (e.g. ``permissions``) survive untouched.

        Scenario: the registry also carries user permissions; removing a hook must not drop or rewrite them.
        """
        out = rhfr.filter_hooks(sample_registry, _RTK_REWRITE)
        assert out["permissions"] == {"allow": ["Bash(jq:*)"]}

    def test_empty_event_dropped_when_all_groups_removed(self) -> None:
        """An event with no surviving groups is dropped from the output.

        Scenario: the only group under ``PreToolUse`` held the targeted command, so the event key itself must go
        rather than linger as an empty list.
        """
        reg = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": ".claude/hooks/foo.js"}]}]}}
        out = rhfr.filter_hooks(reg, re.compile(r"foo\.js", re.IGNORECASE))
        assert out["hooks"] == {}

    def test_no_match_leaves_registry_structurally_equivalent(self, sample_registry: dict) -> None:
        """Pattern that matches nothing → output equals input (modulo dict copy).

        Scenario: the pattern names a hook that is not registered; the registry comes back unchanged.
        """
        out = rhfr.filter_hooks(sample_registry, re.compile(r"nonexistent\.js", re.IGNORECASE))
        assert out == _SAMPLE_REGISTRY


class TestCountMatches:
    """count_matches: post-write verification helper."""

    @pytest.mark.parametrize(
        ("pattern", "expected"),
        [
            pytest.param(_RTK_REWRITE, 2, id="counts-across-events"),
            pytest.param(re.compile(r"missing\.js"), 0, id="pattern-misses"),
        ],
    )
    def test_counts_matching_command_entries(
        self, sample_registry: dict, pattern: re.Pattern[str], expected: int
    ) -> None:
        """Return the total of matching command entries, summed across events; zero when nothing matches.

        Scenario: ``rtk-rewrite.js`` is registered under two events, so the count is 2; a pattern naming an absent hook
        counts 0. The count is what the CLI uses to verify the removal after writing.
        """
        assert rhfr.count_matches(sample_registry, pattern) == expected


class TestRun:
    """Run: atomic file rewrite + exit-code contract."""

    def test_rewrites_file_without_named_hook(self, tmp_path: Path, settings_file: Path) -> None:
        """Target file rewritten with the named hook removed; exit 0; the temp file is cleaned up.

        Scenario: a registry with ``rtk-rewrite.js`` under two events is rewritten in place. Non-hook keys survive and
        no PostToolUse command mentions the removed hook.
        """
        rc = rhfr.run(settings_file, "rtk-rewrite", r"\.claude/hooks/rtk-rewrite\.js")

        rewritten = json.loads(settings_file.read_text(encoding="utf-8"))
        post_commands = [
            cmd["command"] for group in rewritten["hooks"].get("PostToolUse", []) for cmd in group["hooks"]
        ]
        assert rc == 0
        assert not (tmp_path / "settings.json.tmp").exists()
        assert rewritten["permissions"] == {"allow": ["Bash(jq:*)"]}
        assert "rtk-rewrite.js" not in " ".join(post_commands)

    def test_missing_target_returns_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Missing JSON file → exit 1 + stderr message.

        Scenario: the registry path does not exist, as after a mistyped ``--json-file``.
        """
        rc = rhfr.run(tmp_path / "absent.json", "x", r"x\.js")
        assert rc == 1
        assert "target not found" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("file_text", "pattern", "expected_rc", "message"),
        [
            pytest.param("{not json", r"x\.js", 2, "invalid JSON", id="malformed-json"),
            pytest.param(json.dumps(_SAMPLE_REGISTRY), "(unclosed", 3, "invalid --path-pattern", id="malformed-regex"),
        ],
    )
    def test_invalid_input_returns_specific_exit_code(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        file_text: str,
        pattern: str,
        expected_rc: int,
        message: str,
    ) -> None:
        """Malformed JSON exits 2 and a malformed regex exits 3, each naming the offending input on stderr.

        Scenario: the registry file is not valid JSON, or the registry is fine but ``--path-pattern`` is an unclosed
        group. The two failures must stay distinguishable so the caller can tell which input to fix.
        """
        target = tmp_path / "settings.json"
        target.write_text(file_text, encoding="utf-8")

        rc = rhfr.run(target, "x", pattern)

        assert rc == expected_rc
        assert message in capsys.readouterr().err

    def test_idempotent_when_pattern_misses(self, settings_file: Path) -> None:
        """Running against a registry with no matches still succeeds (no-op rewrite).

        Scenario: the named hook is not registered. The run exits 0 and the file keeps the same structure; only
        whitespace, such as the trailing newline, may differ after re-serialising.
        """
        original = settings_file.read_text(encoding="utf-8")

        rc = rhfr.run(settings_file, "missing", r"missing\.js")

        assert rc == 0
        assert json.loads(settings_file.read_text(encoding="utf-8")) == json.loads(original)


class TestMain:
    """Main: CLI surface — flag parsing + exit codes."""

    @pytest.mark.parametrize(
        "match_args",
        [
            pytest.param([], id="path-pattern-only"),
            # An explicit ``--path-pattern`` wins over ``--match`` — the documented escape hatch.
            pytest.param(["--match", "basename"], id="path-pattern-overrides-match"),
        ],
    )
    def test_removes_named_hook_end_to_end(self, settings_file: Path, match_args: list[str]) -> None:
        """Full main() round-trip via argv removes the named hook and keeps the others.

        Scenario: a hook is removed by ``--path-pattern`` alone, and when ``--match`` is also given the explicit
        pattern still decides.
        """
        argv = ["--json-file", str(settings_file), "--hook-name", "rtk-rewrite", *match_args]

        rc = rhfr.main([*argv, "--path-pattern", r"\.claude/hooks/rtk-rewrite\.js"])

        rewritten = json.loads(settings_file.read_text(encoding="utf-8"))
        pre_cmds = [cmd["command"] for cmd in rewritten["hooks"]["PreToolUse"][0]["hooks"]]
        assert rc == 0
        assert pre_cmds == [".claude/hooks/commit-guard.js"]

    def test_missing_required_flag_exits_nonzero(self) -> None:
        """Argparse: missing required flag → SystemExit with a non-zero code.

        Scenario: only ``--json-file`` is given, so the required hook name is absent and the CLI must refuse to run.
        """
        with pytest.raises(SystemExit) as exc_info:
            rhfr.main(["--json-file", "x.json"])
        assert exc_info.value.code != 0

    def test_match_basename_escapes_hook_name_metacharacter(self, tmp_path: Path) -> None:
        """``--match basename`` escapes the hook name — a literal ``.`` never means "any char".

        A raw ``--path-pattern`` built by interpolating the hook name unescaped (the real-world call-site shape this fix
        replaces) would treat ``a.b`` as "a, any char, b" and remove ``axb.js`` too. The safe default must match only
        the literal ``a.b.js``.
        """
        target = tmp_path / "settings.json"
        commands = [{"command": ".claude/hooks/axb.js"}, {"command": ".claude/hooks/a.b.js"}]
        _write_registry(target, {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": commands}]}})

        rc = rhfr.main(["--json-file", str(target), "--hook-name", "a.b", "--match", "basename"])

        rewritten = json.loads(target.read_text(encoding="utf-8"))
        remaining = [cmd["command"] for cmd in rewritten["hooks"]["PreToolUse"][0]["hooks"]]
        assert rc == 0
        assert remaining == [".claude/hooks/axb.js"]
