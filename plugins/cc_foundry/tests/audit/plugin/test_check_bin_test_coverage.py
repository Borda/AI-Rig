"""Tests for check_bin_test_coverage bin script.

Covers the four R4 failure modes (missing, empty, no test functions, stub-only), the pass case, private-script skipping,
the --local gate, and the MANIFEST-copy exemption (a byte-identical propagate_shared.py copy of a tested canonical is
never re-flagged, while the canonical itself and orphaned copies still are).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import check_bin_test_coverage as cbtc
import pytest

_MISSING_TEST = "R4-FAIL (no test file)"
_PASSING_TEST = "def test_x():\n    assert 1\n"


def _plugin(root: Path, name: str = "cc_demo") -> Path:
    """Create an empty plugin with ``bin/`` and ``tests/`` directories and return its path."""
    plugin = root / name
    (plugin / "bin").mkdir(parents=True)
    (plugin / "tests").mkdir(parents=True)
    return plugin


def _write_bin_scripts(plugin: Path, *names: str) -> None:
    """Write a trivial public ``bin/<name>.py`` for each of ``names``."""
    for name in names:
        (plugin / "bin" / f"{name}.py").write_text("x = 1\n", encoding="utf-8")


def _write_propagate_shared(root: Path, source: str) -> None:
    """Write raw propagate_shared.py source at the path manifest_copies() reads."""
    bin_dir = root / "plugins" / "cc_foundry" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "propagate_shared.py").write_text(source, encoding="utf-8")


def _write_manifest(root: Path, entries: list[dict[str, object]]) -> None:
    """Write a propagate_shared.py exposing the literal MANIFEST ``entries``."""
    _write_propagate_shared(root, f"MANIFEST = {entries!r}\n")


def _main_local(monkeypatch: pytest.MonkeyPatch, scan_dir: Path) -> int:
    """Run ``main()`` as ``check_bin_test_coverage.py --local --scan-dir <scan_dir>`` and return its exit code."""
    monkeypatch.setattr("sys.argv", ["check_bin_test_coverage.py", "--local", "--scan-dir", str(scan_dir)])
    return cbtc.main()


@pytest.fixture
def script(tmp_path: Path) -> Path:
    """Plugin ``cc_demo`` holding a public ``bin/thing.py`` that has no test file yet; returns the script path."""
    path = _plugin(tmp_path) / "bin" / "thing.py"
    path.write_text("x = 1\n", encoding="utf-8")
    return path


@pytest.fixture
def canonical_tree(tmp_path: Path) -> Path:
    """Tree whose MANIFEST canonical ``plugins/p/bin/canon.py`` exists on disk; returns the tree root."""
    canonical = tmp_path / "plugins" / "p" / "bin" / "canon.py"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("x = 1\n", encoding="utf-8")
    return tmp_path


class TestTestFunctions:
    """Covers the AST scan of a test module."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            pytest.param("def test_a():\n    assert True\n", (1, 1), id="real-function"),
            pytest.param("def test_a():\n    pass\n", (1, 0), id="pass-body-is-stub"),
            pytest.param("def test_a():\n    ...\n", (1, 0), id="ellipsis-body-is-stub"),
            pytest.param("async def test_a():\n    assert 1\n", (1, 1), id="async-test-counted"),
            pytest.param("def helper():\n    assert 1\n", (0, 0), id="non-test-function-ignored"),
            pytest.param(
                "class TestX:\n    def test_a(self):\n        assert 1\n", (1, 1), id="nested-in-class-counted"
            ),
            pytest.param("def test_a(:\n", (1, 1), id="syntax-error-treated-as-covered"),
        ],
    )
    def test_counts_total_and_real_functions(self, source: str, expected: tuple[int, int]) -> None:
        """A test module is scanned into (total, real) counts of test_ functions.

        Covers an asserting test, ``pass``- and ``...``-only stubs, an async test, a non-test helper, a method inside a
        class, and an unparsable module, which is left to the lint hooks rather than double-reported.
        """
        assert cbtc.test_functions(source) == expected


class TestCheckScript:
    """Covers the per-script verdict."""

    def test_covered_script_has_no_finding(self, script: Path) -> None:
        """A script with a real, same-named test file produces no finding.

        The baseline R4 pass: ``bin/thing.py`` next to ``tests/test_thing.py`` holding one asserting test.
        """
        (script.parent.parent / "tests" / "test_thing.py").write_text(_PASSING_TEST, encoding="utf-8")

        assert cbtc.check_script(script) is None

    @pytest.mark.parametrize(
        "reference",
        [
            "import thing",
            "from thing import main",
            'SCRIPT = "thing.py"',
            'SCRIPT = "bin/thing.py"',
            'module = load_script("thing")',
        ],
    )
    def test_indirect_test_covers_script(self, script: Path, reference: str) -> None:
        """A differently named, deeply nested test can cover a module by import or filename.

        Tests in this repository are often grouped by feature, so the check accepts any real test file under ``tests/``
        that imports the module or names its file.
        """
        tests = script.parent.parent / "tests" / "unit" / "runtime"
        tests.mkdir(parents=True)
        (tests / "test_behavior.py").write_text(
            reference + "\ndef test_behavior():\n    assert True\n", encoding="utf-8"
        )

        assert cbtc.check_script(script) is None

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("import other_thing\ndef test_behavior():\n    assert True\n", id="similar-import"),
            pytest.param('SCRIPT = "other_thing.py"\ndef test_behavior():\n    assert True\n', id="similar-filename"),
            pytest.param("import thing\ndef test_behavior():\n    pass\n", id="stub-test"),
            pytest.param("import thing\ndef helper():\n    assert True\n", id="no-test-function"),
        ],
    )
    def test_unrelated_or_stub_indirect_test_stays_uncovered(self, script: Path, source: str) -> None:
        """Substring matches and files without real tests cannot hide a coverage gap.

        ``other_thing`` contains ``thing`` as a substring, and an importing file with only a stub or a non-test helper
        has nothing that exercises the script, so both still report the missing test file.
        """
        (script.parent.parent / "tests" / "test_behavior.py").write_text(source, encoding="utf-8")

        finding = cbtc.check_script(script)

        assert finding is not None
        assert _MISSING_TEST in finding

    def test_missing_test_file_names_expected_path(self, script: Path) -> None:
        """No test file names both the script and the expected path in the finding.

        The finding must say where the test belongs (``test_thing.py``) so the author knows what to create.
        """
        finding = cbtc.check_script(script)

        assert finding is not None
        assert _MISSING_TEST in finding
        assert "test_thing.py" in finding

    @pytest.mark.parametrize(
        ("test_source", "fragments"),
        [
            pytest.param("", ("R4-FAIL (empty test file)",), id="zero-byte-test-file"),
            pytest.param("import os\n", ("no test functions",), id="no-test-functions"),
            pytest.param(
                "def test_a():\n    pass\n\n\ndef test_b():\n    ...\n",
                ("stub tests only", "all 2 test function(s)"),
                id="all-stubs-with-count",
            ),
        ],
    )
    def test_unusable_test_file_is_reported(self, script: Path, test_source: str, fragments: tuple[str, ...]) -> None:
        """A zero-byte file, a file without test_ functions and an all-stub file are distinct failure modes.

        The same-named test file exists, so each finding must describe what is wrong with it; the stub case also reports
        how many functions are stubs.
        """
        (script.parent.parent / "tests" / "test_thing.py").write_text(test_source, encoding="utf-8")

        finding = cbtc.check_script(script)

        assert finding is not None
        assert all(fragment in finding for fragment in fragments)

    @pytest.mark.parametrize(
        "folders",
        [
            pytest.param(("unit",), id="one-level"),
            pytest.param(("audit", "plugin"), id="two-levels"),
        ],
    )
    def test_same_named_test_in_nested_folder_covers_script(self, script: Path, folders: tuple[str, ...]) -> None:
        """A same-named test file under a category folder counts at any depth.

        Tests grouped by category and topic (``tests/audit/plugin/test_thing.py``) sit two folders below ``tests/``; the
        lookup must find them instead of falling back to the weaker reference scan, which would not see this file.
        """
        nested = script.parent.parent.joinpath("tests", *folders)
        nested.mkdir(parents=True)
        (nested / "test_thing.py").write_text(_PASSING_TEST, encoding="utf-8")

        assert cbtc.check_script(script) is None

    def test_stub_test_in_two_level_folder_is_reported(self, script: Path) -> None:
        """A stub-only same-named test two folders down is reported as a stub, not as a missing file.

        The reference scan skips files without real tests, so only the nested lookup reaches this file; it must run the
        same stub check a flat ``tests/test_thing.py`` gets.
        """
        nested = script.parent.parent / "tests" / "audit" / "plugin"
        nested.mkdir(parents=True)
        (nested / "test_thing.py").write_text("import thing\ndef test_a():\n    pass\n", encoding="utf-8")

        finding = cbtc.check_script(script)

        assert finding is not None
        assert "stub tests only" in finding


class TestBinScripts:
    """Covers script discovery and the expected test location."""

    def test_private_scripts_skipped(self, tmp_path: Path) -> None:
        """An `_`-prefixed helper is not required to have a test.

        Only ``public.py`` is discovered from a ``bin/`` holding both a public and a private script.
        """
        plugin = _plugin(tmp_path)
        (plugin / "bin" / "_helper.py").write_text("x = 1\n", encoding="utf-8")
        (plugin / "bin" / "public.py").write_text("x = 1\n", encoding="utf-8")

        assert {p.name for p in cbtc.bin_scripts(tmp_path)} == {"public.py"}

    def test_expected_test_path(self, script: Path) -> None:
        """The expected test path sits in the plugin's tests/ directory, named ``test_<script>.py``."""
        assert cbtc.expected_test(script) == script.parent.parent / "tests" / "test_thing.py"


class TestCli:
    """Covers the command-line gate and exit codes."""

    def test_without_local_skips(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Without --local the check reports a skip and exits 0.

        The check is a local-only gate, so a bare invocation must not scan anything.
        """
        monkeypatch.setattr("sys.argv", ["check_bin_test_coverage.py"])

        assert cbtc.main() == 0
        assert "skipped in non-local mode" in capsys.readouterr().out

    def test_local_clean_tree_passes(
        self, script: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A tree where every script is covered exits 0 with the success line."""
        (script.parent.parent / "tests" / "test_thing.py").write_text(_PASSING_TEST, encoding="utf-8")

        assert _main_local(monkeypatch, script.parent.parent.parent) == 0
        assert "all bin/ Python scripts have non-empty test files" in capsys.readouterr().out

    def test_local_dirty_tree_reports_finding(
        self, script: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An uncovered script prints its finding and still exits 0 (report-only)."""
        assert _main_local(monkeypatch, script.parent.parent.parent) == 0
        assert _MISSING_TEST in capsys.readouterr().out


class TestManifestCopies:
    """Covers manifest_copies(), the MANIFEST-driven split between copies and canonicals."""

    def test_returns_only_copies_of_existing_canonical(self, canonical_tree: Path) -> None:
        """A copy of an on-disk canonical is exempted, and the canonical itself never is.

        ``extract-keep-flag.py`` is exactly this shape in the real repo: a MANIFEST canonical with no test of its own,
        which must keep failing R4, so only the copies may appear in the returned set.
        """
        entries = [{"canonical": "plugins/p/bin/canon.py", "copies": ["plugins/q/bin/copy.py"]}]
        _write_manifest(canonical_tree, entries)

        assert cbtc.manifest_copies(canonical_tree) == {"plugins/q/bin/copy.py"}

    def test_copies_skipped_when_canonical_missing(self, tmp_path: Path) -> None:
        """A copy whose canonical no longer exists on disk is not exempted."""
        _write_manifest(tmp_path, [{"canonical": "plugins/p/bin/gone.py", "copies": ["plugins/q/bin/copy.py"]}])

        assert cbtc.manifest_copies(tmp_path) == set()

    def test_no_propagate_shared_file_returns_empty_set(self, tmp_path: Path) -> None:
        """A tree with no propagate_shared.py degrades to no exemptions, never a crash."""
        assert cbtc.manifest_copies(tmp_path) == set()

    def test_audited_tree_code_is_never_executed(self, canonical_tree: Path) -> None:
        """Reading MANIFEST parses the audited tree's propagate_shared.py instead of running it.

        The checker runs with the audited repository as cwd, so a propagate_shared.py there is untrusted input: this one
        writes a marker file when imported, and the marker must stay absent while the literal MANIFEST is still read.
        """
        marker = canonical_tree / "executed"
        entries = [{"canonical": "plugins/p/bin/canon.py", "copies": ["plugins/q/bin/copy.py"]}]
        source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nMANIFEST = {entries!r}\n"
        _write_propagate_shared(canonical_tree, source)

        assert cbtc.manifest_copies(canonical_tree) == {"plugins/q/bin/copy.py"}
        assert not marker.exists()

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("MANIFEST = [dict(canonical='a.py', copies=[])]\n", id="call-in-manifest-is-not-a-literal"),
            pytest.param("MANIFEST = {'canonical': 'a.py'}\n", id="manifest-not-a-list"),
            pytest.param("def broken(:\n", id="syntax-error"),
            pytest.param("OTHER = []\n", id="no-manifest-assignment"),
        ],
    )
    def test_unreadable_manifest_degrades_to_empty_set(self, tmp_path: Path, source: str) -> None:
        """A propagate_shared.py without a literal MANIFEST list yields no exemptions rather than an error.

        Anything the parser cannot read as data is treated as absent, so a hostile or malformed file can only remove
        exemptions, never add them or crash the check.
        """
        _write_propagate_shared(tmp_path, source)

        assert cbtc.manifest_copies(tmp_path) == set()


class TestManifestCopiesForRepo:
    """Covers _manifest_copies_for_repo(), the git-backed wrapper main() calls."""

    @mock.patch(
        "check_bin_test_coverage.repo_root",
        autospec=True,
        side_effect=subprocess.CalledProcessError(128, ["git", "rev-parse", "--show-toplevel"]),
    )
    def test_degrades_to_empty_on_git_failure(self, mock_repo_root: mock.Mock) -> None:
        """A git failure (not a repo, git absent) yields (None, empty set), never a crash.

        Check R4's contract is exit 0 always; losing the MANIFEST exemption is an acceptable degradation, aborting is
        not. ``repo_root`` is the only git boundary, so it is mocked instead of spawning git outside a repository.
        """
        assert cbtc._manifest_copies_for_repo() == (None, set())


class TestMainManifestExemption:
    """Covers the MANIFEST-copy exemption wired into main()'s R4 loop."""

    @mock.patch("check_bin_test_coverage._manifest_copies_for_repo", autospec=True)
    def test_tested_canonicals_copy_suppressed_but_untested_canonical_still_flagged(
        self,
        mock_copies_for_repo: mock.Mock,
        script: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A copy of a tested canonical is suppressed; the untested canonical itself still fails.

        Mirrors extract-keep-flag.py: canonicals seed the copies list but are never in it, so an untested canonical must
        keep failing R4 even after copies of it stop being flagged. Neither script here has a test file.
        """
        root = script.parent.parent.parent
        canonical_script = script.parent / "canon.py"
        canonical_script.write_text("x = 1\n", encoding="utf-8")
        mock_copies_for_repo.return_value = (root, {script.resolve().relative_to(root).as_posix()})

        assert _main_local(monkeypatch, root) == 0
        out = capsys.readouterr().out
        assert "canon.py" in out
        assert "R4-FAIL" in out
        assert "thing.py" not in out
        assert "(1 MANIFEST copies deferred to their canonical)" in out

    @mock.patch("check_bin_test_coverage.repo_root", autospec=True)
    def test_orphan_copy_still_flagged_when_canonical_missing(
        self,
        mock_repo_root: mock.Mock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Full wiring with manifest_copies() computed for real: orphan copies fail, tested-canonical copies pass.

        A copy whose canonical is gone is still flagged; a copy of a real tested canonical is suppressed. Both are
        exercised together against one real propagate_shared.py module.
        """
        plugin = _plugin(tmp_path, "cc_demo")
        _write_bin_scripts(plugin, "tested_canon", "exempt_copy", "orphan_copy")
        (plugin / "tests" / "test_tested_canon.py").write_text(_PASSING_TEST, encoding="utf-8")
        entries = [
            {"canonical": "cc_demo/bin/tested_canon.py", "copies": ["cc_demo/bin/exempt_copy.py"]},
            {"canonical": "cc_demo/bin/does_not_exist.py", "copies": ["cc_demo/bin/orphan_copy.py"]},
        ]
        _write_manifest(tmp_path, entries)
        mock_repo_root.return_value = tmp_path

        assert _main_local(monkeypatch, tmp_path) == 0
        out = capsys.readouterr().out
        assert "exempt_copy.py" not in out
        assert "orphan_copy.py" in out
        assert "R4-FAIL" in out
        assert "(1 MANIFEST copies deferred to their canonical)" in out
