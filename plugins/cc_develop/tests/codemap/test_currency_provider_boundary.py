"""Resolve executable currency probes from the active provider installation."""

import json
from pathlib import Path

import pytest
from _develop_text import load_module

resolver = load_module(Path(__file__).resolve().parents[2] / "bin/codemap_resolve.py", "provider_currency_resolver")


def _checker(path: Path, status: str, marker: Path) -> None:
    """Create a real checker that leaves evidence when Python executes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        f"print({json.dumps({'status': status, 'reason': 'provider verdict'})!r})\n",
        encoding="utf-8",
    )


def test_path_checker_is_never_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATH entry cannot supply Python code when no provider is installed."""
    config = tmp_path / "config"
    checker = tmp_path / "path/check-index-currency"
    marker = tmp_path / "executed"
    _checker(checker, "stale", marker)
    checker.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("PATH", str(checker.parent))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "provider")
    resolver._record_currency("currency", tmp_path / "index.json")
    assert not marker.exists()
    assert (tmp_path / "currency-provider").read_text() == "current\n"


def test_registered_provider_checker_is_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the active install supplies the currency probe even with a different checker on PATH."""
    config = tmp_path / "config"
    provider = tmp_path / "installed"
    marker = tmp_path / "registered-executed"
    planted = tmp_path / "path-executed"
    _checker(provider / "bin/check-index-currency", "stale", marker)
    _checker(tmp_path / "path/check-index-currency", "current", planted)
    registry = config / "plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"plugins": {"codemap-py@borda-ai-rig": [{"installPath": str(provider)}]}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("PATH", str(tmp_path / "path"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "provider")
    resolver._record_currency("currency", tmp_path / "index.json")
    assert marker.exists()
    assert not planted.exists()
    assert (tmp_path / "currency-provider").read_text() == "stale\n"


def test_relative_install_record_cannot_execute_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed relative install path cannot turn the current repository into the executable provider."""
    config = tmp_path / "config"
    marker = tmp_path / "executed"
    _checker(tmp_path / "installed/bin/check-index-currency", "stale", marker)
    registry = config / "plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"plugins": {"codemap-py@borda-ai-rig": [{"installPath": "installed"}]}}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "relative")
    resolver._record_currency("currency", tmp_path / "index.json")
    assert not marker.exists()
    assert (tmp_path / "currency-relative").read_text() == "current\n"
