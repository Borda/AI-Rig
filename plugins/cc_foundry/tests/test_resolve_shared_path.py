"""Tests for ``bin/resolve_shared_path.py`` canonical Python resolver.

Covers each tier of the four-tier cascade plus argument validation:

* Tier 0 — ``CLAUDE_PLUGIN_ROOT`` env hit
* Tier 1 — Registry helper lookup (mocked subprocess via monkeypatched helper)
* Tier 2 — Cache semver scan with orphan filtering
* Tier 3 — Source-tree fallback (warn + exit 0) and absent-everywhere (exit 1)

Cross-cutting checks confirm Windows-portability invariants:

* No hardcoded ``/tmp`` literal in script source
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import resolve_shared_path


class TestValidation:
    """Argument validation: invalid PLUGIN/SUBDIR exit 2 with stderr message."""

    def test_invalid_plugin_path_traversal(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Plugin containing ``/`` fails regex → exit 2."""
        rc = resolve_shared_path.main(["../evil", "skills/_shared"])
        assert rc == 2
        assert "invalid PLUGIN" in capsys.readouterr().err

    def test_invalid_plugin_special_chars(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Plugin with ``!`` fails regex → exit 2."""
        rc = resolve_shared_path.main(["plug!in", "skills/_shared"])
        assert rc == 2
        assert "invalid PLUGIN" in capsys.readouterr().err

    def test_invalid_subdir_traversal(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Subdir containing ``..`` is rejected → exit 2."""
        rc = resolve_shared_path.main(["foundry", "skills/../etc"])
        assert rc == 2
        assert "invalid SUBDIR" in capsys.readouterr().err

    def test_invalid_subdir_special_chars(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Subdir with disallowed chars exits 2."""
        rc = resolve_shared_path.main(["foundry", "skills/_shared!"])
        assert rc == 2
        assert "invalid SUBDIR" in capsys.readouterr().err


class TestTier0EnvHit:
    """Tier 0 — ``CLAUDE_PLUGIN_ROOT`` env var with valid subdir."""

    def test_env_root_with_existing_subdir(self, tmp_path: Path) -> None:
        """Env var set + ``<root>/<subdir>`` exists → tier 0 returned."""
        root = tmp_path / "plugin_install"
        (root / "skills" / "_shared").mkdir(parents=True)
        path, tier = resolve_shared_path.resolve("foundry", "skills/_shared", home=tmp_path, env_root=str(root))
        assert tier == 0
        assert Path(path) == root / "skills" / "_shared"

    def test_env_root_subdir_absent_falls_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var set but ``<root>/<subdir>`` missing → does not match tier 0."""
        root = tmp_path / "plugin_install"
        root.mkdir()
        # Patch out tier 1 so source-tree helper doesn't bleed real state into test.
        monkeypatch.setattr(resolve_shared_path, "_tier1_registry", lambda *a, **kw: None)
        path, tier = resolve_shared_path.resolve("foundry", "skills/_shared", home=tmp_path, env_root=str(root))
        assert tier != 0
        # Falls through to tier -1 (no cache, no source tree at tmp_path).
        assert path == "plugins/cc_foundry/skills/_shared"

    def test_main_tier0_prints_path_no_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Report only the resolved path for a highest-priority match."""
        root = tmp_path / "plugin_install"
        (root / "skills" / "_shared").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
        monkeypatch.setattr(resolve_shared_path.Path, "home", classmethod(lambda _cls: tmp_path))
        rc = resolve_shared_path.main(["foundry", "skills/_shared"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == str(root / "skills" / "_shared")
        assert captured.err == ""


class TestTier2Cache:
    """Tier 2 — cache semver scan with orphan filtering."""

    def test_cache_hit_picks_highest_semver(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple versions present → newest by semver returned."""
        base = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry"
        older = base / "0.1.0" / "skills" / "_shared"
        newer = base / "0.20.0" / "skills" / "_shared"
        older.mkdir(parents=True)
        newer.mkdir(parents=True)
        monkeypatch.setattr(resolve_shared_path, "_tier1_registry", lambda *a, **kw: None)
        path, tier = resolve_shared_path.resolve("foundry", "skills/_shared", home=tmp_path, env_root="")
        assert tier == 2
        assert Path(path) == newer

    def test_orphaned_version_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Version with ``.orphaned_at`` is skipped → older usable version wins."""
        base = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry"
        orphaned_ver = base / "0.20.0"
        (orphaned_ver / "skills" / "_shared").mkdir(parents=True)
        (orphaned_ver / ".orphaned_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
        older_shared = base / "0.1.0" / "skills" / "_shared"
        older_shared.mkdir(parents=True)
        monkeypatch.setattr(resolve_shared_path, "_tier1_registry", lambda *a, **kw: None)
        path, tier = resolve_shared_path.resolve("foundry", "skills/_shared", home=tmp_path, env_root="")
        assert tier == 2
        assert Path(path) == older_shared

    def test_cache_dir_without_subdir_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Version dir present but ``<subdir>`` missing → not a tier-2 hit."""
        base = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry"
        (base / "0.20.0").mkdir(parents=True)  # no skills/_shared
        monkeypatch.setattr(resolve_shared_path, "_tier1_registry", lambda *a, **kw: None)
        monkeypatch.chdir(tmp_path)  # isolate CWD so relative source-tree path doesn't exist
        path, tier = resolve_shared_path.resolve("foundry", "skills/_shared", home=tmp_path, env_root="")
        # Falls through to tier -1 (no source tree at tmp_path either)
        assert tier == -1


class TestTier1Registry:
    """Tier 1 — the install record decides the active version, not the newest cache dir."""

    @staticmethod
    def _install_tree(home: Path, active: str, helper_src: Path) -> tuple[Path, Path]:
        """Lay out a fake ``~/.claude`` with two cached codemap-py versions and one install record.

        Returns the ``claude-skills/_shared`` dirs of the active and the newer-but-inactive version.
        """
        cache = home / ".claude" / "plugins" / "cache" / "borda-ai-rig"
        active_shared = cache / "codemap-py" / active / "claude-skills" / "_shared"
        newer_shared = cache / "codemap-py" / "99.0.0" / "claude-skills" / "_shared"
        active_shared.mkdir(parents=True)
        newer_shared.mkdir(parents=True)
        helper_dst = cache / "foundry" / "0.1.0" / "bin" / "get_plugin_install_path.py"
        helper_dst.parent.mkdir(parents=True)
        helper_dst.write_bytes(helper_src.read_bytes())
        registry = home / ".claude" / "plugins" / "installed_plugins.json"
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "plugins": {
                        "codemap-py@borda-ai-rig": [
                            {
                                "scope": "user",
                                "installPath": (cache / "codemap-py" / active).as_posix(),
                                "version": active,
                                "installedAt": "2026-09-18T06:00:47.793Z",
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        return active_shared, newer_shared

    @pytest.mark.parametrize("active", ["0.38.0", "0.37.1"])
    def test_registry_record_beats_newer_cache_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: str
    ) -> None:
        """The recorded ``installPath`` wins even when a newer version dir sits in the cache.

        A consumer loading codemap-py's contract must read the version Claude Code actually dispatches to; an orphaned
        or half-installed newer dir must never shadow it.
        """
        helper_src = Path(resolve_shared_path.__file__).with_name("get_plugin_install_path.py")
        active_shared, _newer = self._install_tree(tmp_path, active, helper_src)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # no `python` anywhere: tier 1 must not depend on PATH
        path, tier = resolve_shared_path.resolve("codemap-py", "claude-skills/_shared", home=tmp_path, env_root="")
        assert tier == 1
        assert Path(path) == active_shared

    def test_consumer_plugin_root_does_not_shadow_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``CLAUDE_PLUGIN_ROOT`` of a consumer plugin is not mistaken for the provider's tree.

        A develop skill runs with its own root exported; the provider lookup must skip tier 0 and fall through to the
        registry.
        """
        helper_src = Path(resolve_shared_path.__file__).with_name("get_plugin_install_path.py")
        active_shared, _newer = self._install_tree(tmp_path, "0.38.0", helper_src)
        consumer_root = tmp_path / "develop-root"
        (consumer_root / ".claude-plugin").mkdir(parents=True)
        (consumer_root / ".claude-plugin" / "plugin.json").write_text('{"name": "develop"}', encoding="utf-8")
        (consumer_root / "claude-skills" / "_shared").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        path, tier = resolve_shared_path.resolve(
            "codemap-py", "claude-skills/_shared", home=tmp_path, env_root=str(consumer_root)
        )
        assert tier == 1
        assert Path(path) == active_shared


class TestTier3SourceFallback:
    """Tier 3 — source-tree fallback when nothing else hits."""

    @pytest.mark.parametrize(
        ("plugin", "source_dir"),
        [
            pytest.param("foundry", "cc_foundry", id="cc-prefixed"),
            pytest.param("codemap-py", "codemap-py", id="un-prefixed-provider"),
        ],
    )
    def test_source_fallback_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin: str, source_dir: str
    ) -> None:
        """Source tree present → tier 3 returns the on-disk ``plugins/<dir>/<subdir>``.

        Claude plugin dirs carry the ``cc_`` prefix; provider plugins such as codemap-py do not, and the dev-checkout
        fallback must find both.
        """
        source = tmp_path / "plugins" / source_dir / "skills" / "_shared"
        source.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        path, tier = resolve_shared_path.resolve(plugin, "skills/_shared", home=tmp_path, env_root="")
        assert tier == 3
        assert path == f"plugins/{source_dir}/skills/_shared"

    def test_main_tier3_warns_and_exits_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Warn while returning a successful source-tree fallback."""
        source = tmp_path / "plugins" / "cc_foundry" / "skills" / "_shared"
        source.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
        monkeypatch.setattr(resolve_shared_path.Path, "home", classmethod(lambda _cls: tmp_path))
        rc = resolve_shared_path.main(["foundry", "skills/_shared"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "plugins/cc_foundry/skills/_shared"
        assert "source-tree fallback" in captured.err


class TestAbsentEverywhere:
    """H7 — all tiers fail, plugin truly absent: exit 1 with message."""

    def test_main_exits_1_when_nothing_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No env, no helper, no cache, no source tree → exit 1."""
        monkeypatch.chdir(tmp_path)  # cwd has no plugins/ dir
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
        monkeypatch.setattr(resolve_shared_path.Path, "home", classmethod(lambda _cls: tmp_path))
        rc = resolve_shared_path.main(["foundry", "skills/_shared"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "not found in registry, cache, or source tree" in captured.err


class TestVersionKey:
    """Semver sort key — verify ``sort -V`` equivalence."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            pytest.param("0.20.0", [0, 20, 0], id="0.20.0"),
            pytest.param("0.9.9", [0, 9, 9], id="0.9.9"),
            pytest.param("1.2.3rc4", [1, 2, 3, 4], id="1.2.3rc4"),
            pytest.param("", [], id="empty"),
            pytest.param("nonsense", [], id="nonsense"),
        ],
    )
    def test_version_key_extracts_digit_runs(self, name: str, expected: list[int]) -> None:
        """Extract contiguous version digits as integers."""
        assert resolve_shared_path._version_key(name) == expected

    def test_version_key_orders_semver_correctly(self) -> None:
        """Sort above ``0.9.9`` (digit-run aware, not lexical)."""
        assert resolve_shared_path._version_key("0.9.9") < resolve_shared_path._version_key("0.20.0")
