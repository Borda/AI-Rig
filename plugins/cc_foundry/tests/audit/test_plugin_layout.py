"""Tests for ``bin/plugin_layout.py`` — manifest-declared skills/agents directory resolution."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent.parent.parent / "bin" / "plugin_layout.py"
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
def test_declared_dir(manifest: dict, expected: str) -> None:
    """The declared directory loses its ``./`` prefix and trailing slash, else the default is used."""
    assert pl.declared_dir(manifest, "skills", "skills") == expected


def test_layout_lines_per_manifest(tmp_path: Path) -> None:
    """Each manifest yields one line; an override is honoured and malformed JSON falls back to defaults."""
    _manifest(tmp_path, "cc_alpha", json.dumps({"skills": "./claude-skills/"}))
    _manifest(tmp_path, "cc_beta", "{not json")
    _manifest(tmp_path, "gamma", json.dumps({"agents": "./agent-files/"}), folder=".codex-plugin")
    (tmp_path / "no_manifest").mkdir()
    assert pl.layout_lines(tmp_path) == [
        "PLUGIN_LAYOUT: cc_alpha | skills=claude-skills | agents=agents",
        "PLUGIN_LAYOUT: cc_beta | skills=skills | agents=agents",
        "PLUGIN_LAYOUT: gamma | skills=skills | agents=agent-files",
    ]


def test_main_prints_lines_and_tolerates_missing_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI prints the lines, and a missing plugins directory prints nothing and exits 0."""
    _manifest(tmp_path, "cc_alpha", "{}")
    assert pl.main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == "PLUGIN_LAYOUT: cc_alpha | skills=skills | agents=agents\n"
    assert pl.main([str(tmp_path / "absent")]) == 0
    assert capsys.readouterr().out == ""
