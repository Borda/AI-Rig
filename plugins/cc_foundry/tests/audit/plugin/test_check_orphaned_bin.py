"""Tests for check_orphaned_bin — orphaned bin/ script detector (Check 32d)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from check_orphaned_bin import OrphanFinding, find_orphans, is_referenced, iter_bin_scripts, main


def _write_files(base: Path, files: dict[str, str]) -> None:
    """Write each ``relative path -> text`` entry under ``base``, creating parent directories.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _write_files(Path(directory), {"demo/bin/tool.py": "# script"})
        ...     (Path(directory) / "demo" / "bin" / "tool.py").read_text()
        '# script'
    """
    for rel, text in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class TestIterBinScripts:
    """Covers discovery of public ``bin/`` scripts across plugins."""

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            pytest.param(
                ["myplugin/bin/foo.py", "myplugin/bin/bar.sh"],
                [("myplugin", "bar.sh"), ("myplugin", "foo.py")],
                id="py-and-sh-sorted",
            ),
            pytest.param(
                ["myplugin/bin/_private.py", "myplugin/bin/public.py"],
                [("myplugin", "public.py")],
                id="underscore-prefix-skipped",
            ),
            pytest.param(
                ["myplugin/bin/script.py", "myplugin/bin/readme.md", "myplugin/bin/data.json"],
                [("myplugin", "script.py")],
                id="non-script-extensions-skipped",
            ),
            pytest.param(
                ["zebra/bin/z.py", "alpha/bin/a.py"], [("alpha", "a.py"), ("zebra", "z.py")], id="plugins-sorted"
            ),
            pytest.param(["myplugin/skills/SKILL.md"], [], id="plugin-without-bin-dir"),
        ],
    )
    def test_lists_public_scripts_sorted(
        self, tmp_path: Path, files: list[str], expected: list[tuple[str, str]]
    ) -> None:
        """Only public ``.py``/``.sh`` scripts are listed, ordered by plugin then name.

        Private ``_``-prefixed helpers and non-script files are skipped, and a plugin without a ``bin/`` directory
        contributes nothing.
        """
        _write_files(tmp_path, dict.fromkeys(files, "# script"))

        assert [(plugin, script) for plugin, script, _path in iter_bin_scripts(tmp_path)] == expected

    def test_full_path_in_result(self, tmp_path: Path) -> None:
        """Each result carries the script's path ending in ``<plugin>/bin/<script>``."""
        _write_files(tmp_path, {"myplugin/bin/foo.py": "# script"})

        assert iter_bin_scripts(tmp_path)[0][2].endswith("myplugin/bin/foo.py")


class TestIsReferenced:
    """Covers the search for a script's basename in plugin Markdown."""

    @pytest.mark.parametrize(
        ("rel", "text", "expected"),
        [
            pytest.param("skills/modes/efficiency.md", "calls foo.py", True, id="nested-subdir-markdown"),
            pytest.param("notes.txt", "references foo.py", False, id="non-markdown-ignored"),
            pytest.param("skills/SKILL.md", "python bin/foo.py", True, id="python-bin-foo.py"),
            pytest.param(
                "skills/SKILL.md", 'python "$PLUGIN_ROOT/bin/foo.py"', True, id="resolved-plugin-root-reference"
            ),
            pytest.param(
                "skills/SKILL.md",
                'python "C:/Users/user/.claude/plugins/cache/borda-ai-rig/demo/1.0.0/bin/foo.py"',
                True,
                id="rendered-windows-reference",
            ),
            pytest.param(
                "skills/SKILL.md",
                'python "${CLAUDE_PLUGIN_ROOT}/bin/foo.py"',
                True,
                id="python-claude_plugin_root-bin-foo.py",
            ),
            pytest.param("skills/SKILL.md", "python bin/foo.py --help", True, id="python-bin-foo.py---help"),
            pytest.param("skills/SKILL.md", "python bin/foo.py.bak", False, id="python-bin-foo.py.bak"),
            pytest.param("skills/SKILL.md", "python bin/myfoo.py", False, id="python-bin-myfoo.py"),
            pytest.param("skills/SKILL.md", "python bin/foo.py.disabled", False, id="python-bin-foo.py.disabled"),
            pytest.param("skills/SKILL.md", "run bin/foo.py here", True, id="prose-run-bin-foo.py"),
            pytest.param(
                "skills/SKILL.md",
                'python3 "${CLAUDE_PLUGIN_ROOT}/bin/foo.py"',
                True,
                id="python3-claude_plugin_root-bin-foo.py",
            ),
            pytest.param("skills/SKILL.md", "nothing relevant", False, id="no-reference"),
        ],
    )
    def test_basename_boundary_cases(self, tmp_path: Path, rel: str, text: str, expected: bool) -> None:
        """References match the script basename in Markdown files only, not broader substrings.

        The full caller pattern ``${CLAUDE_PLUGIN_ROOT}/bin/foo.py`` contains the basename and counts, as does a prose
        mention anywhere below the plugin; text naming no script, a longer name that merely contains it, or a non-
        Markdown file does not.
        """
        _write_files(tmp_path, {rel: text})

        assert is_referenced("foo.py", tmp_path) is expected


class TestFindOrphans:
    """Covers classification of scripts that no Markdown calls."""

    @pytest.mark.parametrize(
        "files",
        [
            pytest.param(
                {"myplugin/bin/foo.py": "# script", "myplugin/skills/SKILL.md": "calls foo.py"}, id="referenced-script"
            ),
            pytest.param({"myplugin/bin/_helper.py": "# script"}, id="private-module"),
            # A script in plugin A called from plugin B's SKILL.md is still wired.
            pytest.param(
                {
                    "foundry/bin/find-polluter.py": "# script",
                    "develop/skills/SKILL.md": 'python "$_FOUNDRY_BIN/find-polluter.py" <test>',
                },
                id="cross-plugin-caller",
            ),
        ],
    )
    def test_wired_or_private_script_is_not_orphan(self, tmp_path: Path, files: dict[str, str]) -> None:
        """A script called from any plugin's Markdown, or a private helper, is never reported."""
        _write_files(tmp_path, files)

        assert find_orphans(tmp_path) == []

    @pytest.mark.parametrize(
        ("files", "script"),
        [
            pytest.param(
                {"myplugin/bin/orphan.py": "# script", "myplugin/skills/SKILL.md": "nothing here"},
                "orphan.py",
                id="skill-without-reference",
            ),
            pytest.param({"myplugin/bin/mycheck.py": "# script"}, "mycheck.py", id="no-markdown-at-all"),
        ],
    )
    def test_unreferenced_script_is_orphan(self, tmp_path: Path, files: dict[str, str], script: str) -> None:
        """A public script no Markdown mentions is reported once, with its plugin, name and path."""
        _write_files(tmp_path, files)

        orphans = find_orphans(tmp_path)

        assert len(orphans) == 1
        assert isinstance(orphans[0], OrphanFinding)
        assert (orphans[0].plugin, orphans[0].script) == ("myplugin", script)
        assert script in orphans[0].script_path


class TestMain:
    """Covers the command-line contract and exit codes."""

    @pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
    def test_orphan_diagnostic_survives_legacy_stdout(self, tmp_path: Path, encoding: str) -> None:
        """Orphans retain a finding and hint instead of crashing while printing a warning.

        The checker runs as a real subprocess with a legacy stdout encoding, because the contract is that printing the
        warning never raises ``UnicodeEncodeError`` on such a console; mocking the process would hide that.
        """
        _write_files(tmp_path, {"myplugin/bin/orphan.py": "# script"})
        script = Path(__file__).resolve().parents[3] / "bin" / "check_orphaned_bin.py"

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

    def test_exit_0_when_all_referenced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every script referenced exits 0 with the OK line."""
        monkeypatch.chdir(tmp_path)
        _write_files(tmp_path, {"myplugin/bin/foo.py": "# script", "myplugin/skills/SKILL.md": "calls foo.py"})

        rc = main(["--plugins-dir", str(tmp_path)])

        assert rc == 0
        assert "OK: Check 32d" in capsys.readouterr().out

    def test_exit_1_when_orphans_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unreferenced script exits 1 with the warning, the script name and a wiring hint."""
        monkeypatch.chdir(tmp_path)
        _write_files(tmp_path, {"myplugin/bin/orphan.py": "# script"})

        rc = main(["--plugins-dir", str(tmp_path)])

        assert rc == 1
        out = capsys.readouterr().out
        assert "WARN 32d" in out
        assert "orphan.py" in out
        assert "hint" in out

    def test_exit_2_for_missing_plugins_dir(self, tmp_path: Path) -> None:
        """An explicit ``--plugins-dir`` that does not exist is an argument error."""
        assert main(["--plugins-dir", str(tmp_path / "missing")]) == 2

    def test_exit_2_when_default_dir_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``--plugins-dir`` the default ``plugins/`` is resolved from cwd; its absence is an argument error."""
        monkeypatch.chdir(tmp_path)

        assert main([]) == 2
