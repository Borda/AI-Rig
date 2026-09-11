"""Tests for check_bin_test_coverage bin script.

Covers the four R4 failure modes (missing, empty, no test functions, stub-only), the pass case, private-script skipping,
and the --local gate.
"""

from __future__ import annotations

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
