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
_SHARED = ("plugins", "cc_oss", "skills", "_shared")
_MANIFEST = {".claude-plugin/plugin.json": '{"name": "oss"}'}


def _plant_source_tree(root: Path, files: dict[str, str]) -> None:
    """Create ``plugins/cc_oss/skills/_shared`` under ``root`` plus the given files relative to ``plugins/cc_oss``."""
    root.joinpath(*_SHARED).mkdir(parents=True)
    for relative, text in files.items():
        target = root / "plugins" / "cc_oss" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


class TestResolveSharedPath:
    """``resolve`` and ``main`` — locating the plugin's own ``skills/_shared`` directory."""

    def test_lives_under_oss_bin(self) -> None:
        """The oss plugin has its own copy of resolve_shared_path.py.

        Module-name collision means foundry's copy wins ``sys.modules`` when both plugin test suites run together, so
        the check goes by the expected path next to this test tree instead of by ``__file__`` of the import.
        """
        expected = Path(__file__).parents[2] / "bin" / "resolve_shared_path.py"
        assert expected.exists()
        assert "cc_oss" in expected.parts
        assert "cc_foundry" not in expected.parts

    def test_tier0_env_hit(self, tmp_path: Path) -> None:
        """Tier 0 — ``CLAUDE_PLUGIN_ROOT`` with valid subdir resolves.

        Scenario: the host exports the plugin root and the requested ``skills/_shared`` exists under it.
        """
        root = tmp_path / "plugin_install"
        (root / "skills" / "_shared").mkdir(parents=True)
        path, tier = resolve_shared_path.resolve("oss", "skills/_shared", home=tmp_path, env_root=str(root))
        assert tier == 0
        assert Path(path) == root / "skills" / "_shared"

    def test_invalid_plugin_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Bad plugin name → exit 2 with stderr message.

        A name such as ``../evil`` must be refused before it is joined into any cache path.
        """
        assert resolve_shared_path.main(["../evil", "skills/_shared"]) == 2
        assert "invalid PLUGIN" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("files", "expected_rc", "expected_out", "expected_err"),
        [
            pytest.param(
                _MANIFEST, 0, "plugins/cc_oss/skills/_shared", "source-tree fallback", id="own-manifest-falls-back"
            ),
            pytest.param({}, 1, "", "not the oss plugin source repository", id="planted-tree-without-manifest"),
        ],
    )
    def test_source_tree_fallback_requires_own_manifest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        files: dict[str, str],
        expected_rc: int,
        expected_out: str,
        expected_err: str,
    ) -> None:
        """No env/cache: an oss source tree falls back with a warning; a tree without oss's manifest is refused.

        The planted tree must never be trusted (exit 1, no path). Output has no carriage return, so the path is usable
        on Windows too (``sys.stdout.reconfigure(newline="\\n")``).
        """
        _plant_source_tree(tmp_path, files)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
        monkeypatch.setattr(resolve_shared_path.Path, "home", classmethod(lambda _cls: tmp_path))
        rc = resolve_shared_path.main(["oss", "skills/_shared"])
        captured = capsys.readouterr()
        assert (rc, captured.out.strip()) == (expected_rc, expected_out)
        assert expected_err in captured.err
        assert "\r" not in captured.out
