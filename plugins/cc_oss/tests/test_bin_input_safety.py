"""Exercise bounded repository-text parsing and safe subprocess arguments in OSS helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
import resolve_test_plan as rtp
import review_impact_tier as rit
import run_audit_checks as rac
import search_downstream_consumers as sdc
import setup_release_dir as srd
import symbol_exists_at_ref as sear
import vitality_extract as vx
from _vitality_data import data_view


@pytest.mark.timeout(2)
@pytest.mark.parametrize(
    "description",
    [
        pytest.param("*" * 42, id="emphasis-prefix"),
        pytest.param("Deprecated" + "*" * 42 + " APIs", id="emphasis-suffix"),
    ],
)
def test_description_markup_does_not_stall(description: str) -> None:
    """Unmatched emphasis runs must terminate without an abandonment signal."""
    assert vx.abandonment_statements(None, description, None) == []


def test_description_scan_stops_at_github_description_limit() -> None:
    """Text beyond the repository-description bound cannot introduce a status."""
    assert vx.abandonment_statements(None, "x" * 400 + " This project is unmaintained", None) == []


@pytest.mark.timeout(2)
@pytest.mark.parametrize(
    "text", [pytest.param("9" * 64_000, id="digits"), pytest.param("a" * 64_000, id="address-characters")]
)
def test_security_scan_handles_long_tokens(text: str) -> None:
    """Long nonmatching tokens retain presence credit without stalling either regex."""
    result = vx.axis8_security(data_view({"type": "security_text", "data": text}))
    assert result["security_md_depth"] == 1


def test_security_scan_stops_at_policy_text_limit() -> None:
    """An email and SLA beyond the policy prefix do not affect depth."""
    view = data_view({"type": "security_text", "data": "x" * 65_536 + " security@example.org 24 hours"})
    assert vx.axis8_security(view)["security_md_depth"] == 1


@pytest.mark.timeout(2)
def test_test_selection_handles_multiline_indentation(tmp_path: Path) -> None:
    """A whitespace-only test file cannot make import selection quadratic."""
    (tmp_path / "test_other.py").write_text(" \n" * 80_000, encoding="utf-8")
    assert rtp.heuristic_tests("src/pkg/core.py", ["test_other.py"], tmp_path) == []


@pytest.mark.timeout(2)
def test_test_selection_handles_long_import_line(tmp_path: Path) -> None:
    """A padded import without the source module cannot stall test selection."""
    (tmp_path / "test_other.py").write_text("from a import" + " " * 100_000 + "z\n", encoding="utf-8")
    assert rtp.heuristic_tests("src/pkg/core.py", ["test_other.py"], tmp_path) == []


def test_test_selection_finds_imported_leaf(tmp_path: Path) -> None:
    """Importing a source module by its leaf name selects the importing test."""
    (tmp_path / "test_other.py").write_text("from pkg import core\n", encoding="utf-8")
    assert rtp.heuristic_tests("src/pkg/core.py", ["test_other.py"], tmp_path) == ["test_other.py"]


def test_test_selection_reads_only_bounded_prefix(tmp_path: Path) -> None:
    """An import beyond the test-text cap is not read into the heuristic search."""
    (tmp_path / "test_other.py").write_text("#" * 1_048_576 + "\nfrom pkg.core import run\n", encoding="utf-8")
    assert rtp.heuristic_tests("src/pkg/core.py", ["test_other.py"], tmp_path) == []


@pytest.mark.parametrize(
    "path",
    [
        "tests/conftest.py",
        "docs/conf.py",
        "setup.py",
        "release.py",
        "tools/release.py",
        "scripts/release.py",
        "scripts/build.py",
        "build_hooks/backend.py",
        "pkg.conftest",
        "docs.conf",
    ],
)
@pytest.mark.parametrize(
    "symbols", [pytest.param([], id="module-level"), pytest.param(["pkg::_configure"], id="private-function")]
)
def test_executable_tooling_gets_full_review(path: str, symbols: list[str]) -> None:
    """Test, documentation and release entrypoints require full review even without callers."""
    payload = {"changed_modules": [{"path": path, "changed_symbols": symbols}]}
    assert rit.classify(payload, lambda _name: {"blast_radius": []})[0] == rit.FULL


@pytest.mark.timeout(2)
@pytest.mark.parametrize("manifest", ["requirements.txt", "pyproject.toml"])
@pytest.mark.parametrize(
    "space_count", [pytest.param(16_000, id="under-line-cap"), pytest.param(64_000, id="over-line-cap")]
)
def test_requirement_whitespace_does_not_stall(manifest: str, space_count: int) -> None:
    """Nonmatching whitespace after a requirement cannot cause quadratic parsing."""
    quote = '"' if manifest.endswith(".toml") else ""
    diff = f"+++ b/{manifest}\n+{quote}package" + " " * space_count + "?\n"
    assert rac._introduced_dependencies(diff) == set()


@pytest.mark.timeout(2)
def test_repeated_todo_markers_do_not_stall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long line of TODOs without release text is scanned in linear time."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "module.py").write_text("# " + "TODO " * 64_000, encoding="utf-8")
    assert rac._grep_code_signals() == []


@pytest.fixture(params=[False, True])
def release_project(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, Path, Path]:
    """Prepare a release directory with either no changelog link or an existing absolute link."""
    project = tmp_path / "project"
    release = project / "releases" / "v1.0.0"
    release.mkdir(parents=True)
    changelog = project / "CHANGELOG.md"
    changelog.write_text("release notes", encoding="utf-8")
    link = release / "CHANGELOG.md"
    if request.param:
        link.symlink_to(changelog)
    return project, release, changelog


def test_release_link_is_relative_and_survives_project_move(
    tmp_path: Path, release_project: tuple[Path, Path, Path]
) -> None:
    """Both initial linking and relinking keep the changelog usable after moving the project."""
    project, release, changelog = release_project
    link = release / "CHANGELOG.md"
    assert srd.main([str(release), str(changelog)]) == 0
    assert link.readlink() == Path("../../CHANGELOG.md")
    project.rename(tmp_path / "moved")
    assert (tmp_path / "moved/releases/v1.0.0/CHANGELOG.md").read_text(encoding="utf-8") == "release notes"


@mock.patch.object(sdc, "which", autospec=True, return_value="/fake/gh")
@mock.patch.object(sdc.subprocess, "run", autospec=True)
def test_downstream_search_uses_get(run: mock.Mock, which: mock.Mock) -> None:
    """The code-search request must keep query fields in a GET request."""
    run.return_value = subprocess.CompletedProcess([], 0, "consumer/project\n", "")
    assert sdc.search_consumers("pkg", ["Legacy"]) == (1, {"consumer/project"})
    which.assert_called_once_with("gh")
    args = run.call_args.args[0]
    assert args[args.index("--method") + 1] == "GET"
    assert "q=from pkg import Legacy language:python" in args


@mock.patch.object(sear.subprocess, "run", autospec=True)
def test_option_shaped_ref_is_rejected(run: mock.Mock) -> None:
    """An output-option-shaped ref must be refused before invoking git."""
    assert sear.probe("--output=sink", "Legacy", "api.py") == sear.EXIT_INCONCLUSIVE
    run.assert_not_called()


@mock.patch.object(sear.subprocess, "run", autospec=True)
def test_symbol_probe_ends_options_before_ref(run: mock.Mock) -> None:
    """Valid revision expressions are sent to git after its option delimiter."""
    run.return_value = subprocess.CompletedProcess([], 0, b"Legacy = 1", b"")
    assert sear.probe("HEAD~1", "Legacy", "api.py") == sear.EXIT_FOUND
    assert run.call_args.args[0] == ["git", "show", "--end-of-options", "HEAD~1:api.py"]


@pytest.mark.parametrize("error", [RecursionError, MemoryError])
@mock.patch.object(sear.subprocess, "run", autospec=True)
def test_parser_resource_errors_are_inconclusive(run: mock.Mock, error: type[Exception]) -> None:
    """Parser resource failures keep the documented inconclusive exit status."""
    run.return_value = subprocess.CompletedProcess([], 0, b"Legacy = 1", b"")
    with mock.patch.object(sear.ast, "parse", autospec=True, side_effect=error("parser limit")):
        assert sear.probe("HEAD", "Legacy", "api.py") == sear.EXIT_INCONCLUSIVE
