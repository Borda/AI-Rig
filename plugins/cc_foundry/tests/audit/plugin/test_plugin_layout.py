"""Tests for ``bin/plugin_layout.py`` — manifest-declared skills/agents directory resolution."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[3] / "bin" / "plugin_layout.py"
_spec = importlib.util.spec_from_file_location("plugin_layout", _MOD_PATH)
assert _spec
assert _spec.loader
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)


def _manifest(root: Path, plugin: str, body: str, *, folder: str = ".claude-plugin") -> None:
    """Write a raw manifest body for ``plugin`` under ``root``."""
    target = root / plugin / folder
    target.mkdir(parents=True)
    (target / "plugin.json").write_text(body, encoding="utf-8")


class TestDeclaredDir:
    """declared_dir: a manifest key's directory with ``./`` and trailing slash removed, else the default."""

    @pytest.mark.parametrize(
        ("manifest", "expected"),
        [
            pytest.param({}, "skills", id="absent-defaults"),
            pytest.param({"skills": "./claude-skills/"}, "claude-skills", id="override"),
            pytest.param({"skills": "skills"}, "skills", id="bare-name"),
            pytest.param({"skills": ""}, "skills", id="empty-falls-back"),
            pytest.param({"skills": ["a", "b"]}, "skills", id="non-string-falls-back"),
        ],
    )
    def test_normalizes_declared_value_or_falls_back(self, manifest: dict, expected: str) -> None:
        """The declared directory loses its ``./`` prefix and trailing slash, else the default is used.

        A manifest may omit the key, spell the path in several ways, or hold a value of the wrong type. Every spelling
        of a real override resolves to one bare name, and anything unusable resolves to the default instead of raising.
        """
        assert pl.declared_dir(manifest, "skills", "skills") == expected


class TestLayoutLines:
    """layout_lines: one ``PLUGIN_LAYOUT`` line per plugin directory that carries a manifest."""

    def test_one_line_per_manifest_with_overrides_and_fallbacks(self, tmp_path: Path) -> None:
        """Each manifest yields one line; an override is honoured and malformed JSON falls back to defaults.

        Three plugins share a root: one overrides the skills directory, one ships unparsable JSON, and one is a Codex
        plugin overriding agents. A fourth directory has no manifest and must not appear.
        """
        _manifest(tmp_path, "cc_alpha", json.dumps({"skills": "./claude-skills/"}))
        _manifest(tmp_path, "cc_beta", "{not json")
        _manifest(tmp_path, "gamma", json.dumps({"agents": "./agent-files/"}), folder=".codex-plugin")
        (tmp_path / "no_manifest").mkdir()
        assert pl.layout_lines(tmp_path) == [
            "PLUGIN_LAYOUT: cc_alpha | skills=claude-skills | agents=agents",
            "PLUGIN_LAYOUT: cc_beta | skills=skills | agents=agents",
            "PLUGIN_LAYOUT: gamma | skills=skills | agents=agent-files",
        ]


class TestMain:
    """Main: the CLI prints the layout lines and exits 0 whether or not the plugins directory exists."""

    def test_prints_layout_lines(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The CLI prints one line per manifest-bearing plugin and exits 0.

        A plugins directory holding one default-layout plugin is passed on the command line.
        """
        _manifest(tmp_path, "cc_alpha", "{}")
        assert pl.main([str(tmp_path)]) == 0
        assert capsys.readouterr().out == "PLUGIN_LAYOUT: cc_alpha | skills=skills | agents=agents\n"

    def test_missing_directory_prints_nothing(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A plugins directory that does not exist prints nothing and still exits 0.

        Callers run the helper unconditionally; an absent directory is an empty layout, not a failure.
        """
        assert pl.main([str(tmp_path / "absent")]) == 0
        assert capsys.readouterr().out == ""
