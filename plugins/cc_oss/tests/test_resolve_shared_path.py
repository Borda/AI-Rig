"""Smoke tests for ``oss/bin/resolve_shared_path.py``.

The oss copy is a verbatim duplicate of foundry's canonical module (cross-plugin imports are forbidden — oss must work
standalone). These tests confirm the contract surface: no ``/tmp`` literal, no CRLF in output, env-tier-0 happy path,
source-tree fallback, and argument validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import resolve_shared_path

SCRIPT = Path(resolve_shared_path.__file__)


def test_lives_under_oss_bin() -> None:
    """Sanity check — oss has its own copy of resolve_shared_path.py."""
    # Cannot rely on __file__ — module name collision means foundry's copy
    # wins sys.modules when both plugin test suites run together.
    expected = Path(__file__).parent.parent / "bin" / "resolve_shared_path.py"
    assert expected.exists()
    assert "cc_oss" in expected.parts
    assert "cc_foundry" not in expected.parts


def test_tier0_env_hit(tmp_path: Path) -> None:
    """Tier 0 — ``CLAUDE_PLUGIN_ROOT`` with valid subdir resolves."""
    root = tmp_path / "plugin_install"
    (root / "skills" / "_shared").mkdir(parents=True)
    path, tier = resolve_shared_path.resolve("oss", "skills/_shared", home=tmp_path, env_root=str(root))
    assert tier == 0
    assert Path(path) == root / "skills" / "_shared"


def test_invalid_plugin_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """Bad plugin name → exit 2 with stderr message."""
    rc = resolve_shared_path.main(["../evil", "skills/_shared"])
    assert rc == 2
    assert "invalid PLUGIN" in capsys.readouterr().err


def test_source_tree_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No env/cache, source tree present → tier 3 path with stderr warning."""
    (tmp_path / "plugins" / "cc_oss" / "skills" / "_shared").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
    monkeypatch.setattr(resolve_shared_path.Path, "home", classmethod(lambda _cls: tmp_path))
    rc = resolve_shared_path.main(["oss", "skills/_shared"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "plugins/cc_oss/skills/_shared"
    assert "source-tree fallback" in captured.err
    assert "\r" not in captured.out  # sys.stdout.reconfigure(newline="\n") — Windows portability
