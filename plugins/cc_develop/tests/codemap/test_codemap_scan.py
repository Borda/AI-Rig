"""Tests for ``codemap_scan.py``.

Covers:
    - Pure module-derivation helpers (find/diff rules, package initializers).
    - ``main()`` entry point: missing ``codemap-py query`` silent skip, missing index silent skip,
      ``--source=find`` end-to-end with subprocess monkeypatching, ``--source=diff`` modes,
      bad-arg exit codes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import codemap_scan as cs
import pytest

# ---------- Pure helpers ----------


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
    """Modules derived from a diff keep package initializers, dedupe in order, honor the limit and skip non-Python
    files.

    An initializer-only change reports the package name, and an empty diff yields no modules.
    """
    assert cs.derive_modules_from_diff(files, limit=limit) == expected


def test_derive_modules_from_diff_package_modules_respect_limit() -> None:
    """Package-only changes retain input order and honor the query budget."""
    files = [f"d{i}/__init__.py" for i in range(20)]
    out = cs.derive_modules_from_diff(files, limit=3)
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


# ---------- main() ----------


@pytest.fixture(name="in_tmp_cwd")
def _in_tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Chdir into an isolated tmp dir for each test.

    >>> isolated = getfixture("in_tmp_cwd")
    >>> Path.cwd() == isolated
    True
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _stub_scan_query_present(monkeypatch: pytest.MonkeyPatch, present: bool = True) -> None:
    """Control whether the codemap scanner executable appears on ``PATH``."""
    monkeypatch.setattr(cs.shutil, "which", lambda name: "/usr/bin/codemap-py" if present else None)


def _stub_git(monkeypatch: pytest.MonkeyPatch, diff_files: list[str] | None = None) -> None:
    """Fake git whose repository top-level is the test's CWD.

    The scanner writes the index under the repository root, so the previous stub — which reported the root as
    ``/fake/<name>`` while the fixture created the index in the CWD — encoded the obsolete CWD-versus-root split. With
    resolution now anchored on the repository root, the stub must report the directory where the index actually lives.
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


def test_main_missing_source_returns_1(in_tmp_cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cs.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--source=find|diff required" in err


@pytest.mark.parametrize("source", [pytest.param(s, id=s.value) for s in cs.ScanSource])
def test_source_accepts_every_enum_member(source: cs.ScanSource) -> None:
    """Every ScanSource member is a valid ``--source`` value — CLI choices cannot drift from the enum."""
    assert cs._parse_args([f"--source={source.value}"]).source == source


def test_source_rejects_value_outside_enum() -> None:
    """A ``--source`` value with no ScanSource member exits 2 (argparse bad-choice)."""
    with pytest.raises(SystemExit) as excinfo:
        cs._parse_args(["--source=bogus"])
    assert excinfo.value.code == 2


def test_main_missing_scan_query_silent_exit_0(in_tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_scan_query_present(monkeypatch, present=False)
    rc = cs.main(["--source=diff"])
    assert rc == 0


def test_main_missing_index_silent_exit_0(in_tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_scan_query_present(monkeypatch, present=True)
    _stub_git(monkeypatch)
    # No .cache/codemap/<root>.json created → exit 0.
    rc = cs.main(["--source=diff"])
    assert rc == 0


def test_main_find_mode_invokes_scan_query_per_module_and_coupled(
    in_tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange filesystem: project + index + .py target tree.
    project = in_tmp_cwd
    (project / ".cache" / "codemap").mkdir(parents=True)
    (project / ".cache" / "codemap" / f"{project.name}.json").write_text("{}")

    target = project / "src" / "pkg"
    target.mkdir(parents=True)
    (target / "a.py").write_text("")
    (target / "b.py").write_text("")

    _stub_scan_query_present(monkeypatch, present=True)
    _stub_git(monkeypatch)

    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kw: Any) -> Any:
        """Record scanner execution and return a successful process result."""
        calls.append(cmd)
        return type("CP", (), {"returncode": 0})()

    monkeypatch.setattr(cs.subprocess, "run", _fake_run)

    rc = cs.main(["--source=find", "--target", "src/pkg", "--limit", "7"])
    assert rc == 0

    # Each module → one codemap-py query rdeps call; plus one coupled ``--top N`` at end.
    rdep_calls = [c for c in calls if c[:3] == ["codemap-py", "query", "rdeps"]]
    coupled_calls = [c for c in calls if c[:3] == ["codemap-py", "query", "coupled"]]
    rdep_modules = {c[3] for c in rdep_calls}
    assert rdep_modules == {"pkg.a", "pkg.b"}
    assert coupled_calls == [["codemap-py", "query", "coupled", "--top", "7"]]


def test_main_find_missing_target_returns_1(
    in_tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = in_tmp_cwd
    (project / ".cache" / "codemap").mkdir(parents=True)
    (project / ".cache" / "codemap" / f"{project.name}.json").write_text("{}")

    _stub_scan_query_present(monkeypatch, present=True)
    _stub_git(monkeypatch)

    rc = cs.main(["--source=find"])
    assert rc == 1
    assert "--target required" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("diff_files", "argv", "expected_modules"),
    [
        pytest.param(
            ["src/pkg/a.py", "src/pkg/b.py"], ["--source=diff", "--limit", "10"], ["pkg.a", "pkg.b"], id="per-module"
        ),
        # only __init__.py files → primary derivation drops them → flat-layout fallback kicks in
        pytest.param(["lib/__init__.py", "other/__init__.py"], ["--source=diff"], ["lib", "other"], id="flat-layout"),
    ],
)
def test_main_diff_mode_invokes_per_module(
    in_tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    diff_files: list[str],
    argv: list[str],
    expected_modules: list[str],
) -> None:
    """Diff mode queries reverse dependencies once per changed module, never the coupled query.

    Source-layout files resolve to their dotted modules; a diff of only package initializers falls back to the flat
    layout and queries each package.
    """
    project = in_tmp_cwd
    (project / ".cache" / "codemap").mkdir(parents=True)
    (project / ".cache" / "codemap" / f"{project.name}.json").write_text("{}")

    _stub_scan_query_present(monkeypatch, present=True)
    _stub_git(monkeypatch, diff_files=diff_files)

    calls: list[list[str]] = []
    monkeypatch.setattr(cs.subprocess, "run", lambda cmd, **_kw: calls.append(cmd) or type("CP", (), {})())

    rc = cs.main(argv)
    assert rc == 0
    modules = sorted({c[3] for c in calls if c[:3] == ["codemap-py", "query", "rdeps"]})
    assert modules == expected_modules
    # diff mode does NOT call coupled.
    assert not any(c[:3] == ["codemap-py", "query", "coupled"] for c in calls)


def test_main_diff_empty_silent_exit_0(in_tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = in_tmp_cwd
    (project / ".cache" / "codemap").mkdir(parents=True)
    (project / ".cache" / "codemap" / f"{project.name}.json").write_text("{}")

    _stub_scan_query_present(monkeypatch, present=True)
    _stub_git(monkeypatch, diff_files=[])

    calls: list[list[str]] = []
    monkeypatch.setattr(cs.subprocess, "run", lambda cmd, **_kw: calls.append(cmd) or type("CP", (), {})())

    rc = cs.main(["--source=diff"])
    assert rc == 0
    assert calls == []
