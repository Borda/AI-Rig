"""Tests for ``codemap_scan.py``.

Covers:
    - Pure module-derivation helpers (find/diff rules, package initializers).
    - ``main()`` entry point: missing ``codemap-py query`` silent skip, missing index silent skip,
      ``--source=find`` end-to-end with the subprocess boundary mocked, ``--source=diff`` modes,
      bad-arg exit codes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import codemap_scan as cs
import pytest

_CODEMAP = "/usr/bin/codemap-py"


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        pytest.param("./src/pkg/mod.py", "pkg.mod", id=".-src-pkg-mod.py"),
        pytest.param("src/pkg/mod.py", "pkg.mod", id="src-pkg-mod.py"),
        pytest.param("./pkg/mod.py", "pkg.mod", id=".-pkg-mod.py"),
        pytest.param("pkg/mod.py", "pkg.mod", id="pkg-mod.py"),
        pytest.param("mod.py", "mod", id="mod.py"),
        pytest.param("pkg/__init__.py", "pkg", id="package-initializer"),
        pytest.param(r".\src\pkg\__init__.py", "pkg", id="windows-package-initializer"),
        pytest.param("./src/a/b/c.py", "a.b.c", id=".-src-a-b-c.py"),
    ],
)
def test_derive_module_from_path(inp: str, expected: str) -> None:
    """A source path becomes its dotted module name, dropping ``./``, ``src/`` and a package ``__init__``.

    Both POSIX and Windows separators are accepted because ``find`` output and Git output differ by host.
    """
    assert cs.derive_module_from_path(inp) == expected


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(["./src/a.py", "", "./src/pkg/b.py"], ["a", "pkg.b"], id="drops-empty-entries"),
        pytest.param([], [], id="empty-input"),
    ],
)
def test_derive_modules_from_find(files: list[str], expected: list[str]) -> None:
    """Modules derived from ``find`` output skip empty entries, and no input yields no modules."""
    assert cs.derive_modules_from_find(files) == expected


@pytest.mark.parametrize(
    ("files", "limit", "expected"),
    [
        # a mixed diff must query the changed package as well as its child modules
        pytest.param(
            ["src/pkg/a.py", "src/pkg/__init__.py", "README.md", "src/other/b.py"],
            10,
            ["pkg.a", "pkg", "other.b"],
            id="preserves-package-initializers",
        ),
        # initializer-only changes use the provider's package name, never src/pkg
        pytest.param(["src/pkg/__init__.py"], 10, ["pkg"], id="src-package-only"),
        # primary diff module derivation dedupes in order and applies the same limit as fallback
        pytest.param(
            ["src/pkg/a.py", "src/pkg/a.py", "src/pkg/b.py", "src/pkg/c.py"],
            2,
            ["pkg.a", "pkg.b"],
            id="dedupes-and-limits-primary-modules",
        ),
        pytest.param(["docs.md", "config.yaml", "src/a.py"], 10, ["a"], id="filters-non-py"),
        # repeated package paths produce one package query
        pytest.param(
            ["lib/__init__.py", "other/__init__.py", "lib/__init__.py"],
            10,
            ["lib", "other"],
            id="package-modules-deduplicated",
        ),
        pytest.param([], 10, [], id="empty"),
    ],
)
def test_derive_modules_from_diff(files: list[str], limit: int, expected: list[str]) -> None:
    """Modules derived from a diff keep package initializers, dedupe in order, honor the limit, skip non-Python files.

    An initializer-only change reports the package name, and an empty diff yields no modules.
    """
    assert cs.derive_modules_from_diff(files, limit=limit) == expected


def test_derive_modules_from_diff_package_modules_respect_limit() -> None:
    """Package-only changes keep their order and honor the query budget.

    Twenty changed packages must not turn into twenty provider queries when the caller allowed three.
    """
    out = cs.derive_modules_from_diff([f"d{i}/__init__.py" for i in range(20)], limit=3)
    assert len(out) == 3
    assert out == sorted(out)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("pkg.sub.mod", True, id="normal-dotted-module"),
        pytest.param("--evil", False, id="leading-double-dash"),
        pytest.param("--top", False, id="option-like-flag"),
        pytest.param("bad name", False, id="embedded-space"),
    ],
)
def test_is_valid_module(name: str, expected: bool) -> None:
    """Reject anything a downstream CLI parser could mistake for an option flag."""
    assert cs.is_valid_module(name) == expected


@pytest.fixture(name="in_tmp_cwd")
def _in_tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Chdir into an isolated tmp dir for each test.

    >>> isolated = getfixture("in_tmp_cwd")
    >>> Path.cwd() == isolated
    True
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(name="indexed_project")
def _indexed_project(in_tmp_cwd: Path) -> Path:
    """Chdir into a project holding the empty index file the scanner looks for under ``.cache/codemap``."""
    index = in_tmp_cwd / ".cache" / "codemap" / f"{in_tmp_cwd.name}.json"
    index.parent.mkdir(parents=True)
    index.write_text("{}")
    return in_tmp_cwd


@pytest.fixture(name="codemap_on_path")
def _codemap_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ``codemap-py`` executable resolve to a fixed path."""
    monkeypatch.setattr(cs.shutil, "which", lambda name: _CODEMAP)


def _stub_git(monkeypatch: pytest.MonkeyPatch, diff_files: list[str] | None = None) -> None:
    """Fake git whose repository top-level is the test's CWD.

    The scanner writes the index under the repository root, so the stub must report the directory where the index
    actually lives. A stub reporting an unrelated root would encode an obsolete CWD-versus-root split.
    """
    top = str(Path.cwd())

    def _fake_check_output(cmd: list[str], **_kw: Any) -> str:
        """Return repository-root and diff responses expected by the scanner."""
        if cmd[:2] == ["git", "rev-parse"]:
            return f"{top}\n"
        if cmd[:2] == ["git", "diff"]:
            return "\n".join(diff_files or []) + ("\n" if diff_files else "")
        raise AssertionError(f"Unexpected cmd: {cmd}")

    monkeypatch.setattr(cs.subprocess, "check_output", _fake_check_output)


def _queries(mock_run: mock.Mock, name: str) -> list[list[str]]:
    """Return the recorded ``codemap-py query <name> ...`` argv lists."""
    calls = [call.args[0] for call in mock_run.call_args_list]
    return [argv for argv in calls if argv[:3] == [_CODEMAP, "query", name]]


class TestMainExitPaths:
    """``main`` exits quietly or with a usage error before any query is made."""

    def test_missing_source_returns_1(self, in_tmp_cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Calling without ``--source`` exits 1 and names the accepted values on stderr."""
        assert cs.main([]) == 1
        assert "--source=find|diff required" in capsys.readouterr().err

    def test_missing_scan_query_exits_0_silently(self, in_tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``codemap-py`` on ``PATH`` the scan is skipped with exit 0.

        The scan is an optional enrichment; a machine without the provider must not fail the calling skill.
        """
        monkeypatch.setattr(cs.shutil, "which", lambda name: None)
        assert cs.main(["--source=diff"]) == 0

    @pytest.mark.usefixtures("in_tmp_cwd", "codemap_on_path")
    def test_missing_index_exits_0_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the provider present but no index built, the scan is skipped with exit 0.

        No ``.cache/codemap/<root>.json`` exists in the tmp project, so there is nothing to query.
        """
        _stub_git(monkeypatch)
        assert cs.main(["--source=diff"]) == 0

    @pytest.mark.usefixtures("indexed_project", "codemap_on_path")
    def test_find_without_target_returns_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--source=find`` without ``--target`` exits 1 and names the missing flag on stderr."""
        _stub_git(monkeypatch)
        assert cs.main(["--source=find"]) == 1
        assert "--target required" in capsys.readouterr().err


class TestSourceOption:
    """``--source`` is validated against the ``ScanSource`` enum."""

    @pytest.mark.parametrize("source", [pytest.param(s, id=s.value) for s in cs.ScanSource])
    def test_accepts_every_enum_member(self, source: cs.ScanSource) -> None:
        """Every ``ScanSource`` member is a valid ``--source`` value, so CLI choices cannot drift from the enum."""
        assert cs._parse_args([f"--source={source.value}"]).source == source

    def test_rejects_value_outside_enum(self) -> None:
        """A ``--source`` value with no ``ScanSource`` member exits 2 (argparse bad-choice)."""
        with pytest.raises(SystemExit) as excinfo:
            cs._parse_args(["--source=bogus"])
        assert excinfo.value.code == 2


class TestScanQuery:
    """The scan boundary runs the resolved ``codemap-py`` executable."""

    @mock.patch("codemap_scan.subprocess.run", autospec=True)
    @mock.patch("codemap_scan.shutil.which", autospec=True, return_value=r"C:\Tools\codemap-py.cmd")
    def test_runs_resolved_executable(self, mock_which, mock_run) -> None:
        """A Windows ``PATHEXT`` lookup result reaches the subprocess instead of a bare command name."""
        cs._scan_query(["rdeps", "pkg.mod"])

        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == [r"C:\Tools\codemap-py.cmd", "query", "rdeps", "pkg.mod"]


@pytest.mark.usefixtures("indexed_project", "codemap_on_path")
class TestMainQueries:
    """With a provider and an index, ``main`` issues the expected queries through the mocked subprocess boundary."""

    @mock.patch("codemap_scan.subprocess.run", autospec=True, return_value=subprocess.CompletedProcess([], 0))
    def test_find_mode_queries_each_module_and_coupled(self, mock_run, indexed_project, monkeypatch) -> None:
        """Find mode runs one ``rdeps`` per module under the target, then one ``coupled --top N`` at the end.

        The target tree holds two Python files, so exactly ``pkg.a`` and ``pkg.b`` are queried; ``--limit 7`` becomes
        the coupled query's top-N.
        """
        target = indexed_project / "src" / "pkg"
        target.mkdir(parents=True)
        (target / "a.py").write_text("")
        (target / "b.py").write_text("")
        _stub_git(monkeypatch)

        assert cs.main(["--source=find", "--target", "src/pkg", "--limit", "7"]) == 0

        assert {argv[3] for argv in _queries(mock_run, "rdeps")} == {"pkg.a", "pkg.b"}
        assert _queries(mock_run, "coupled") == [[_CODEMAP, "query", "coupled", "--top", "7"]]

    @pytest.mark.parametrize(
        ("diff_files", "argv", "expected_modules"),
        [
            pytest.param(
                ["src/pkg/a.py", "src/pkg/b.py"],
                ["--source=diff", "--limit", "10"],
                ["pkg.a", "pkg.b"],
                id="per-module",
            ),
            # only __init__.py files → primary derivation drops them → flat-layout fallback kicks in
            pytest.param(
                ["lib/__init__.py", "other/__init__.py"], ["--source=diff"], ["lib", "other"], id="flat-layout"
            ),
        ],
    )
    @mock.patch("codemap_scan.subprocess.run", autospec=True, return_value=subprocess.CompletedProcess([], 0))
    def test_diff_mode_queries_each_changed_module(
        self, mock_run, monkeypatch, diff_files: list[str], argv: list[str], expected_modules: list[str]
    ) -> None:
        """Diff mode queries reverse dependencies once per changed module, never the coupled query.

        Source-layout files resolve to their dotted modules; a diff of only package initializers falls back to the flat
        layout and queries each package.
        """
        _stub_git(monkeypatch, diff_files=diff_files)

        assert cs.main(argv) == 0

        assert sorted({argv[3] for argv in _queries(mock_run, "rdeps")}) == expected_modules
        assert _queries(mock_run, "coupled") == []

    @mock.patch("codemap_scan.subprocess.run", autospec=True, return_value=subprocess.CompletedProcess([], 0))
    def test_empty_diff_makes_no_query(self, mock_run, monkeypatch) -> None:
        """An empty diff exits 0 without calling the provider at all."""
        _stub_git(monkeypatch, diff_files=[])

        assert cs.main(["--source=diff"]) == 0

        mock_run.assert_not_called()
