"""Tests for check_orphaned_bin — orphaned bin/ script detector (Check 32d)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from check_orphaned_bin import OrphanFinding, find_orphans, is_referenced, iter_bin_scripts, main


# ---------------------------------------------------------------------------
# iter_bin_scripts
# ---------------------------------------------------------------------------


def _make_plugin(base: Path, plugin: str, scripts: list[str]) -> Path:
    """Create a plugin bin-tree fixture with the requested script names.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     (_make_plugin(Path(directory), "demo", ["tool.py"]) / "bin" / "tool.py").is_file()
        True
    """
    plugin_dir = base / plugin / "bin"
    plugin_dir.mkdir(parents=True)
    for name in scripts:
        (plugin_dir / name).write_text("# script")
    return base / plugin


class TestIterBinScripts:
    def test_returns_py_and_sh(self, tmp_path: Path) -> None:
        _make_plugin(tmp_path, "myplugin", ["foo.py", "bar.sh"])
        result = iter_bin_scripts(tmp_path)
        names = [(r[0], r[1]) for r in result]
        assert names == [("myplugin", "bar.sh"), ("myplugin", "foo.py")]

    def test_skips_underscore_prefix(self, tmp_path: Path) -> None:
        _make_plugin(tmp_path, "myplugin", ["_private.py", "public.py"])
        result = iter_bin_scripts(tmp_path)
        names = [r[1] for r in result]
        assert "_private.py" not in names
        assert "public.py" in names

    def test_skips_non_script_extensions(self, tmp_path: Path) -> None:
        _make_plugin(tmp_path, "myplugin", ["script.py", "readme.md", "data.json"])
        result = iter_bin_scripts(tmp_path)
        names = [r[1] for r in result]
        assert names == ["script.py"]

    def test_multiple_plugins_sorted(self, tmp_path: Path) -> None:
        _make_plugin(tmp_path, "zebra", ["z.py"])
        _make_plugin(tmp_path, "alpha", ["a.py"])
        result = iter_bin_scripts(tmp_path)
        plugins = [r[0] for r in result]
        assert plugins == ["alpha", "zebra"]

    def test_no_bin_dir_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "myplugin").mkdir()  # no bin/ subdir
        result = iter_bin_scripts(tmp_path)
        assert result == []

    def test_full_path_in_result(self, tmp_path: Path) -> None:
        _make_plugin(tmp_path, "myplugin", ["foo.py"])
        result = iter_bin_scripts(tmp_path)
        assert result[0][2].endswith("myplugin/bin/foo.py")


# ---------------------------------------------------------------------------
# is_referenced
# ---------------------------------------------------------------------------


class TestIsReferenced:
    def test_found_in_skill_md(self, tmp_path: Path) -> None:
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "SKILL.md").write_text("run bin/foo.py here")
        assert is_referenced("foo.py", tmp_path) is True

    def test_not_found(self, tmp_path: Path) -> None:
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "SKILL.md").write_text("nothing relevant")
        assert is_referenced("foo.py", tmp_path) is False

    def test_found_nested_subdir(self, tmp_path: Path) -> None:
        (tmp_path / "skills" / "modes").mkdir(parents=True)
        (tmp_path / "skills" / "modes" / "efficiency.md").write_text("calls check_orphaned_bin.py")
        assert is_referenced("check_orphaned_bin.py", tmp_path) is True

    def test_non_md_files_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("references foo.py")
        assert is_referenced("foo.py", tmp_path) is False

    def test_substring_match(self, tmp_path: Path) -> None:
        """Full caller pattern ${CLAUDE_PLUGIN_ROOT}/bin/foo.py contains basename."""
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "SKILL.md").write_text('python3 "${CLAUDE_PLUGIN_ROOT}/bin/foo.py"')
        assert is_referenced("foo.py", tmp_path) is True

    @pytest.mark.parametrize(
        ("reference_text", "expected"),
        [
            pytest.param("python bin/foo.py", True, id="python-bin-foo.py"),
            pytest.param('python "${CLAUDE_PLUGIN_ROOT}/bin/foo.py"', True, id="python-claude_plugin_root-bin-foo.py"),
            pytest.param("python bin/foo.py --help", True, id="python-bin-foo.py---help"),
            pytest.param("python bin/foo.py.bak", False, id="python-bin-foo.py.bak"),
            pytest.param("python bin/myfoo.py", False, id="python-bin-myfoo.py"),
            pytest.param("python bin/foo.py.disabled", False, id="python-bin-foo.py.disabled"),
        ],
    )
    def test_basename_boundary_cases(self, reference_text: str, expected: bool, tmp_path: Path) -> None:
        """References match the script basename, not broader substrings."""
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "SKILL.md").write_text(reference_text)
        assert is_referenced("foo.py", tmp_path) is expected


# ---------------------------------------------------------------------------
# find_orphans
# ---------------------------------------------------------------------------


class TestFindOrphans:
    def test_referenced_script_not_orphan(self, tmp_path: Path) -> None:
        _make_plugin(tmp_path, "myplugin", ["foo.py"])
        (tmp_path / "myplugin" / "skills").mkdir()
        (tmp_path / "myplugin" / "skills" / "SKILL.md").write_text("calls foo.py")
        assert find_orphans(tmp_path) == []

    def test_unreferenced_script_is_orphan(self, tmp_path: Path) -> None:
        _make_plugin(tmp_path, "myplugin", ["orphan.py"])
        (tmp_path / "myplugin" / "skills").mkdir()
        (tmp_path / "myplugin" / "skills" / "SKILL.md").write_text("nothing here")
        orphans = find_orphans(tmp_path)
        assert len(orphans) == 1
        assert orphans[0].script == "orphan.py"
        assert orphans[0].plugin == "myplugin"

    def test_private_module_not_orphan(self, tmp_path: Path) -> None:
        _make_plugin(tmp_path, "myplugin", ["_helper.py"])
        orphans = find_orphans(tmp_path)
        assert orphans == []

    def test_orphan_finding_fields(self, tmp_path: Path) -> None:
        _make_plugin(tmp_path, "myplugin", ["mycheck.py"])
        orphans = find_orphans(tmp_path)
        o = orphans[0]
        assert isinstance(o, OrphanFinding)
        assert o.plugin == "myplugin"
        assert o.script == "mycheck.py"
        assert "mycheck.py" in o.script_path

    def test_cross_plugin_caller_not_orphan(self, tmp_path: Path) -> None:
        """Script in plugin A referenced by plugin B's SKILL.md is not an orphan."""
        _make_plugin(tmp_path, "foundry", ["find-polluter.py"])
        (tmp_path / "develop" / "skills").mkdir(parents=True)
        (tmp_path / "develop" / "skills" / "SKILL.md").write_text('python "$_FOUNDRY_BIN/find-polluter.py" <test>')
        assert find_orphans(tmp_path) == []


# ---------------------------------------------------------------------------
# main (CLI)
# ---------------------------------------------------------------------------


class TestMain:
    @pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
    def test_orphan_diagnostic_survives_legacy_stdout(self, tmp_path: Path, encoding: str) -> None:
        """Orphans retain a finding and hint instead of crashing while printing a warning."""
        _make_plugin(tmp_path, "myplugin", ["orphan.py"])
        script = Path(__file__).resolve().parents[1] / "bin" / "check_orphaned_bin.py"
        proc = subprocess.run(
            [sys.executable, str(script), "--plugins-dir", "."],
            cwd=tmp_path,
            env={**os.environ, "PYTHONIOENCODING": encoding},
            capture_output=True,
            encoding=encoding,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 1
        assert proc.stderr == ""
        assert "WARN 32d:" in proc.stdout
        assert "myplugin/bin/orphan.py" in proc.stdout
        assert "hint: wire to SKILL.md caller pattern" in proc.stdout

    def test_exit_0_all_referenced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_plugin(tmp_path, "myplugin", ["foo.py"])
        (tmp_path / "myplugin" / "skills").mkdir()
        (tmp_path / "myplugin" / "skills" / "SKILL.md").write_text("calls foo.py")
        rc = main(["--plugins-dir", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "OK: Check 32d" in out

    def test_exit_1_orphans_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_plugin(tmp_path, "myplugin", ["orphan.py"])
        rc = main(["--plugins-dir", str(tmp_path)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "WARN 32d" in out
        assert "orphan.py" in out

    def test_exit_1_output_includes_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_plugin(tmp_path, "myplugin", ["orphan.py"])
        main(["--plugins-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert "hint" in out

    def test_exit_2_bad_dir(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["--plugins-dir", "/nonexistent/path/does/not/exist"])
        assert rc == 2

    def test_default_plugins_dir_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        rc = main([])
        assert rc == 2
