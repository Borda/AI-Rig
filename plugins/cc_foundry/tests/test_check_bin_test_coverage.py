"""Tests for check_bin_test_coverage bin script.

Covers the four R4 failure modes (missing, empty, no test functions, stub-only), the pass case, private-script skipping,
the --local gate, and the MANIFEST-copy exemption (a byte-identical propagate_shared.py copy of a tested canonical is
never re-flagged, while the canonical itself and orphaned copies still are).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import check_bin_test_coverage as cbtc


def _plugin(root: Path, name: str = "cc_demo") -> Path:
    plugin = root / name
    (plugin / "bin").mkdir(parents=True)
    (plugin / "tests").mkdir(parents=True)
    return plugin


class TestTestFunctions:
    """Covers the AST scan of a test module."""

    def test_counts_real_functions(self) -> None:
        """A module with one asserting test reports one total, one real."""
        assert cbtc.test_functions("def test_a():\n    assert True\n") == (1, 1)

    def test_pass_body_is_stub(self) -> None:
        """A `pass`-only body counts as a stub."""
        assert cbtc.test_functions("def test_a():\n    pass\n") == (1, 0)

    def test_ellipsis_body_is_stub(self) -> None:
        """An `...`-only body counts as a stub."""
        assert cbtc.test_functions("def test_a():\n    ...\n") == (1, 0)

    def test_async_test_counted(self) -> None:
        """An async test function is counted like a sync one."""
        assert cbtc.test_functions("async def test_a():\n    assert 1\n") == (1, 1)

    def test_non_test_function_ignored(self) -> None:
        """A helper that is not named test_* does not count."""
        assert cbtc.test_functions("def helper():\n    assert 1\n") == (0, 0)

    def test_nested_test_in_class_counted(self) -> None:
        """A test method inside a class is counted."""
        assert cbtc.test_functions("class TestX:\n    def test_a(self):\n        assert 1\n") == (1, 1)

    def test_syntax_error_treated_as_covered(self) -> None:
        """An unparsable test module is left to the lint hooks, not double-reported."""
        assert cbtc.test_functions("def test_a(:\n") == (1, 1)


class TestCheckScript:
    """Covers the per-script verdict."""

    def test_covered_script_returns_none(self, tmp_path: Path) -> None:
        """A script with a real test file produces no finding."""
        plugin = _plugin(tmp_path)
        script = plugin / "bin" / "thing.py"
        script.write_text("x = 1\n", encoding="utf-8")
        (plugin / "tests" / "test_thing.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
        assert cbtc.check_script(script) is None

    def test_missing_test_file(self, tmp_path: Path) -> None:
        """No test file names both the script and the expected path."""
        plugin = _plugin(tmp_path)
        script = plugin / "bin" / "thing.py"
        script.write_text("x = 1\n", encoding="utf-8")
        finding = cbtc.check_script(script)
        assert finding is not None
        assert "R4-FAIL (no test file)" in finding
        assert "test_thing.py" in finding

    def test_empty_test_file(self, tmp_path: Path) -> None:
        """A zero-byte test file is its own failure mode."""
        plugin = _plugin(tmp_path)
        script = plugin / "bin" / "thing.py"
        script.write_text("x = 1\n", encoding="utf-8")
        (plugin / "tests" / "test_thing.py").write_text("", encoding="utf-8")
        finding = cbtc.check_script(script)
        assert finding is not None
        assert "R4-FAIL (empty test file)" in finding

    def test_no_test_functions(self, tmp_path: Path) -> None:
        """A non-empty file with no test_ functions is reported."""
        plugin = _plugin(tmp_path)
        script = plugin / "bin" / "thing.py"
        script.write_text("x = 1\n", encoding="utf-8")
        (plugin / "tests" / "test_thing.py").write_text("import os\n", encoding="utf-8")
        finding = cbtc.check_script(script)
        assert finding is not None
        assert "no test functions" in finding

    def test_stub_only(self, tmp_path: Path) -> None:
        """All-stub tests are reported with the function count."""
        plugin = _plugin(tmp_path)
        script = plugin / "bin" / "thing.py"
        script.write_text("x = 1\n", encoding="utf-8")
        (plugin / "tests" / "test_thing.py").write_text(
            "def test_a():\n    pass\n\n\ndef test_b():\n    ...\n", encoding="utf-8"
        )
        finding = cbtc.check_script(script)
        assert finding is not None
        assert "stub tests only" in finding
        assert "all 2 test function(s)" in finding


class TestBinScripts:
    """Covers script discovery."""

    def test_private_scripts_skipped(self, tmp_path: Path) -> None:
        """An `_`-prefixed helper is not required to have a test."""
        plugin = _plugin(tmp_path)
        (plugin / "bin" / "_helper.py").write_text("x = 1\n", encoding="utf-8")
        (plugin / "bin" / "public.py").write_text("x = 1\n", encoding="utf-8")
        found = {p.name for p in cbtc.bin_scripts(tmp_path)}
        assert found == {"public.py"}

    def test_expected_test_path(self, tmp_path: Path) -> None:
        """The expected test path sits in the plugin's tests/ directory."""
        plugin = _plugin(tmp_path)
        script = plugin / "bin" / "thing.py"
        assert cbtc.expected_test(script) == plugin / "tests" / "test_thing.py"


class TestCli:
    """Covers the command-line gate and exit codes."""

    def test_without_local_skips(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Without --local the check reports a skip and exits 0."""
        monkeypatch.setattr("sys.argv", ["check_bin_test_coverage.py"])
        assert cbtc.main() == 0
        assert "skipped in non-local mode" in capsys.readouterr().out

    def test_local_clean_tree_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A tree where every script is covered exits 0 with the success line."""
        plugin = _plugin(tmp_path)
        (plugin / "bin" / "thing.py").write_text("x = 1\n", encoding="utf-8")
        (plugin / "tests" / "test_thing.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["check_bin_test_coverage.py", "--local", "--scan-dir", str(tmp_path)])
        assert cbtc.main() == 0
        assert "all bin/ Python scripts have non-empty test files" in capsys.readouterr().out

    def test_local_dirty_tree_reports_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An uncovered script prints its finding and still exits 0 (report-only)."""
        plugin = _plugin(tmp_path)
        (plugin / "bin" / "thing.py").write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["check_bin_test_coverage.py", "--local", "--scan-dir", str(tmp_path)])
        assert cbtc.main() == 0
        assert "R4-FAIL (no test file)" in capsys.readouterr().out


class TestManifestCopies:
    """Covers manifest_copies() — the MANIFEST-driven copy-vs-canonical split (M33)."""

    @staticmethod
    def _write_manifest(root: Path, entries: list[dict[str, object]]) -> None:
        """Write a propagate_shared.py exposing MANIFEST at the path manifest_copies() expects."""
        bin_dir = root / "plugins" / "cc_foundry" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "propagate_shared.py").write_text(f"MANIFEST = {entries!r}\n", encoding="utf-8")

    def test_copies_returned_when_canonical_exists(self, tmp_path: Path) -> None:
        """A copy of an on-disk canonical is included in the exemption set."""
        canonical = tmp_path / "plugins" / "p" / "bin" / "canon.py"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("x = 1\n", encoding="utf-8")
        self._write_manifest(tmp_path, [{"canonical": "plugins/p/bin/canon.py", "copies": ["plugins/q/bin/copy.py"]}])
        assert cbtc.manifest_copies(tmp_path) == {"plugins/q/bin/copy.py"}

    def test_canonical_itself_excluded_from_result(self, tmp_path: Path) -> None:
        """The canonical path is never in the returned set — only its copies are.

        extract-keep-flag.py is exactly this shape in the real repo: a MANIFEST canonical with no test of its own, which
        must keep failing R4 after this fix.
        """
        canonical = tmp_path / "plugins" / "p" / "bin" / "canon.py"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("x = 1\n", encoding="utf-8")
        self._write_manifest(tmp_path, [{"canonical": "plugins/p/bin/canon.py", "copies": ["plugins/q/bin/copy.py"]}])
        assert "plugins/p/bin/canon.py" not in cbtc.manifest_copies(tmp_path)

    def test_copies_skipped_when_canonical_missing(self, tmp_path: Path) -> None:
        """A copy whose canonical no longer exists on disk is not exempted."""
        self._write_manifest(tmp_path, [{"canonical": "plugins/p/bin/gone.py", "copies": ["plugins/q/bin/copy.py"]}])
        assert cbtc.manifest_copies(tmp_path) == set()

    def test_no_propagate_shared_file_returns_empty_set(self, tmp_path: Path) -> None:
        """A tree with no propagate_shared.py degrades to no exemptions, never a crash."""
        assert cbtc.manifest_copies(tmp_path) == set()


class TestManifestCopiesForRepo:
    """Covers _manifest_copies_for_repo() — the git-backed wrapper main() calls."""

    def test_degrades_to_empty_on_git_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A git failure (not a repo, git absent) yields (None, empty set), never a crash.

        Check R4's contract is exit 0 always; losing the MANIFEST exemption is an acceptable degradation, aborting is
        not.
        """

        def _raise() -> Path:
            raise subprocess.CalledProcessError(128, ["git", "rev-parse", "--show-toplevel"])

        monkeypatch.setattr(cbtc, "repo_root", _raise)
        assert cbtc._manifest_copies_for_repo() == (None, set())


class TestMainManifestExemption:
    """Covers the MANIFEST-copy exemption wired into main()'s R4 loop."""

    def test_tested_canonicals_copy_suppressed_but_untested_canonical_still_flagged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A copy of a tested canonical is suppressed; the untested canonical itself still fails.

        Mirrors extract-keep-flag.py: canonicals seed the copies list but are never in it, so an untested canonical must
        keep failing R4 even after copies of it stop being flagged.
        """
        plugin = _plugin(tmp_path, "cc_demo")
        canonical_script = plugin / "bin" / "canon.py"
        canonical_script.write_text("x = 1\n", encoding="utf-8")  # canonical itself has no test
        copy_script = plugin / "bin" / "copy.py"
        copy_script.write_text("x = 1\n", encoding="utf-8")  # copy has no test either — normally R4-FAIL

        rel_copy = copy_script.resolve().relative_to(tmp_path).as_posix()
        monkeypatch.setattr(cbtc, "_manifest_copies_for_repo", lambda: (tmp_path, {rel_copy}))
        monkeypatch.setattr("sys.argv", ["check_bin_test_coverage.py", "--local", "--scan-dir", str(tmp_path)])

        assert cbtc.main() == 0
        out = capsys.readouterr().out
        assert "canon.py" in out and "R4-FAIL" in out
        assert "copy.py" not in out
        assert "(1 MANIFEST copies deferred to their canonical)" in out

    def test_end_to_end_orphan_copy_still_flagged_when_canonical_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Full wiring, manifest_copies() computed for real rather than mocked.

        A copy whose canonical is gone is still flagged; a copy of a real tested canonical is suppressed — exercised
        together against one real propagate_shared.py module.
        """
        plugin = _plugin(tmp_path, "cc_demo")
        tested_canonical = plugin / "bin" / "tested_canon.py"
        tested_canonical.write_text("x = 1\n", encoding="utf-8")
        (plugin / "tests" / "test_tested_canon.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
        exempt_copy = plugin / "bin" / "exempt_copy.py"
        exempt_copy.write_text("x = 1\n", encoding="utf-8")
        orphan_copy = plugin / "bin" / "orphan_copy.py"
        orphan_copy.write_text("x = 1\n", encoding="utf-8")

        entries = [
            {"canonical": "cc_demo/bin/tested_canon.py", "copies": ["cc_demo/bin/exempt_copy.py"]},
            {"canonical": "cc_demo/bin/does_not_exist.py", "copies": ["cc_demo/bin/orphan_copy.py"]},
        ]
        bin_dir = tmp_path / "plugins" / "cc_foundry" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "propagate_shared.py").write_text(f"MANIFEST = {entries!r}\n", encoding="utf-8")

        monkeypatch.setattr(cbtc, "repo_root", lambda: tmp_path)
        monkeypatch.setattr("sys.argv", ["check_bin_test_coverage.py", "--local", "--scan-dir", str(tmp_path)])

        assert cbtc.main() == 0
        out = capsys.readouterr().out
        assert "exempt_copy.py" not in out
        assert "orphan_copy.py" in out and "R4-FAIL" in out
        assert "(1 MANIFEST copies deferred to their canonical)" in out
