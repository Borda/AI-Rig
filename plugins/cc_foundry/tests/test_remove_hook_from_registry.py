"""Tests for ``bin/remove_hook_from_registry.py``.

Covers the contract /foundry:manage delete hook relies on: removes only the named hook, leaves unrelated hooks intact,
prunes empty groups/events, preserves non-``hooks`` top-level keys, and produces the documented exit codes for absent
files and invalid JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import remove_hook_from_registry as rhfr  # noqa: E402


_SAMPLE_REGISTRY: dict = {
    "permissions": {"allow": ["Bash(jq:*)"]},
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [{"command": ".claude/hooks/rtk-rewrite.js"}, {"command": ".claude/hooks/commit-guard.js"}],
            },
        ],
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [{"command": ".claude/hooks/rtk-rewrite.js"}],
            },
            {
                "matcher": "Edit",
                "hooks": [{"command": ".claude/hooks/statusline.js"}],
            },
        ],
    },
}


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


class TestFilterHooks:
    """filter_hooks: pure transform — selective removal of named commands."""

    def test_removes_only_named_hook(self) -> None:
        """Drops every command for the targeted basename; keeps unrelated commands."""
        import re

        out = rhfr.filter_hooks(_SAMPLE_REGISTRY, re.compile(r"rtk-rewrite\.js", re.IGNORECASE))
        # PreToolUse group survives with commit-guard.js
        pre = out["hooks"]["PreToolUse"]
        assert len(pre) == 1
        assert pre[0]["hooks"] == [{"command": ".claude/hooks/commit-guard.js"}]
        # PostToolUse: rtk-only group dropped; statusline group remains
        post = out["hooks"]["PostToolUse"]
        assert len(post) == 1
        assert post[0]["matcher"] == "Edit"
        assert post[0]["hooks"] == [{"command": ".claude/hooks/statusline.js"}]

    def test_preserves_non_hooks_top_level_keys(self) -> None:
        """Top-level keys other than ``hooks`` (e.g. ``permissions``) survive untouched."""
        import re

        out = rhfr.filter_hooks(_SAMPLE_REGISTRY, re.compile(r"rtk-rewrite\.js", re.IGNORECASE))
        assert out["permissions"] == {"allow": ["Bash(jq:*)"]}

    def test_empty_event_dropped_when_all_groups_removed(self) -> None:
        """An event with no surviving groups is dropped from the output."""
        import re

        reg = {
            "hooks": {
                "PreToolUse": [{"matcher": "*", "hooks": [{"command": ".claude/hooks/foo.js"}]}],
            },
        }
        out = rhfr.filter_hooks(reg, re.compile(r"foo\.js", re.IGNORECASE))
        assert out["hooks"] == {}

    def test_no_match_leaves_registry_structurally_equivalent(self) -> None:
        """Pattern that matches nothing → output equals input (modulo dict copy)."""
        import re

        out = rhfr.filter_hooks(_SAMPLE_REGISTRY, re.compile(r"nonexistent\.js", re.IGNORECASE))
        assert out == _SAMPLE_REGISTRY


class TestCountMatches:
    """count_matches: post-write verification helper."""

    def test_counts_all_occurrences(self) -> None:
        """Return total command-entries matching, summed across events."""
        import re

        n = rhfr.count_matches(_SAMPLE_REGISTRY, re.compile(r"rtk-rewrite\.js", re.IGNORECASE))
        assert n == 2

    def test_zero_when_pattern_misses(self) -> None:
        """Pattern matching nothing → 0."""
        import re

        assert rhfr.count_matches(_SAMPLE_REGISTRY, re.compile(r"missing\.js")) == 0


class TestRun:
    """Run: atomic file rewrite + exit-code contract."""

    def test_happy_path_rewrites_file(self, tmp_path: Path) -> None:
        """Target file rewritten with named hook removed; exit 0; tmp cleaned."""
        target = tmp_path / "settings.json"
        _write_registry(target, _SAMPLE_REGISTRY)

        rc = rhfr.run(target, "rtk-rewrite", r"\.claude/hooks/rtk-rewrite\.js")

        assert rc == 0
        assert not (tmp_path / "settings.json.tmp").exists()
        rewritten = json.loads(target.read_text(encoding="utf-8"))
        assert rewritten["permissions"] == {"allow": ["Bash(jq:*)"]}
        # No PostToolUse rtk-rewrite group left
        post_commands = [
            cmd["command"] for group in rewritten["hooks"].get("PostToolUse", []) for cmd in group["hooks"]
        ]
        assert "rtk-rewrite.js" not in " ".join(post_commands)

    def test_missing_target_returns_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Missing JSON file → exit 1 + stderr message."""
        rc = rhfr.run(tmp_path / "absent.json", "x", r"x\.js")
        assert rc == 1
        assert "target not found" in capsys.readouterr().err

    def test_invalid_json_returns_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Malformed JSON → exit 2."""
        target = tmp_path / "settings.json"
        target.write_text("{not json", encoding="utf-8")
        rc = rhfr.run(target, "x", r"x\.js")
        assert rc == 2
        assert "invalid JSON" in capsys.readouterr().err

    def test_invalid_regex_returns_3(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Malformed regex → exit 3."""
        target = tmp_path / "settings.json"
        _write_registry(target, _SAMPLE_REGISTRY)
        rc = rhfr.run(target, "x", "(unclosed")
        assert rc == 3
        assert "invalid --path-pattern" in capsys.readouterr().err

    def test_idempotent_when_pattern_misses(self, tmp_path: Path) -> None:
        """Running against a registry with no matches still succeeds (no-op rewrite)."""
        target = tmp_path / "settings.json"
        _write_registry(target, _SAMPLE_REGISTRY)
        original = target.read_text(encoding="utf-8")

        rc = rhfr.run(target, "missing", r"missing\.js")

        assert rc == 0
        # Structural equality preserved (JSON re-serialised may differ in trailing newline only)
        assert json.loads(target.read_text(encoding="utf-8")) == json.loads(original)


class TestMain:
    """Main: CLI surface — flag parsing + exit codes."""

    def test_happy_path_end_to_end(self, tmp_path: Path) -> None:
        """Full main() round-trip via argv."""
        target = tmp_path / "settings.json"
        _write_registry(target, _SAMPLE_REGISTRY)

        rc = rhfr.main(
            [
                "--json-file",
                str(target),
                "--hook-name",
                "rtk-rewrite",
                "--path-pattern",
                r"\.claude/hooks/rtk-rewrite\.js",
            ],
        )

        assert rc == 0
        rewritten = json.loads(target.read_text(encoding="utf-8"))
        # Verify rtk-rewrite gone, commit-guard kept
        pre_cmds = [cmd["command"] for cmd in rewritten["hooks"]["PreToolUse"][0]["hooks"]]
        assert pre_cmds == [".claude/hooks/commit-guard.js"]

    def test_missing_required_flag_exits_nonzero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Argparse: missing required flag → SystemExit(2)."""
        with pytest.raises(SystemExit) as exc_info:
            rhfr.main(["--json-file", "x.json"])
        assert exc_info.value.code != 0
