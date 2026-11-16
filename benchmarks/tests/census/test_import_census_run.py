"""Census orchestration: coordinates, scanner checkout, index build, end-to-end CLI on local git fixtures (offline)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from _bench_census.run import (
    CensusError,
    RepoCoordinate,
    Scanner,
    build_index,
    load_coordinates,
    run_census,
    scanner_checkout,
    write_results,
)

BENCHMARKS_DIR = Path(__file__).resolve().parents[2]


def _load_cli():
    """Load the hyphen-named census script as a module without relying on a shared ``conftest`` import."""
    path = BENCHMARKS_DIR / "run-import-census.py"
    spec = importlib.util.spec_from_file_location("run_import_census", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Fake scanner entry: indexes every ``.py`` file, with ``import X`` lines as its only direct imports.
_FAKE_ENTRY = """\
import json, pathlib
root = pathlib.Path.cwd()
modules = []
for path in sorted(root.rglob("*.py")):
    rel = path.relative_to(root).as_posix()
    if rel.startswith(".git/"):
        continue
    name = rel[:-3].replace("/", ".")
    name = name[: -len(".__init__")] if name.endswith(".__init__") else name
    imports = [line.split()[1] for line in path.read_text().splitlines() if line.startswith("import ")]
    modules.append({"name": name, "path": rel, "direct_imports": imports})
out = root / ".cache" / "codemap"
out.mkdir(parents=True)
(out / (root.name + ".json")).write_text(json.dumps({"scan_version": 7, "modules": modules}))
"""


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo* with a fixed identity, no signing and no hooks; return standard output."""
    command = ["git", "-c", "user.name=census", "-c", "user.email=census@example.invalid", "-c", "commit.gpgsign=false"]
    command += ["-c", f"core.hooksPath={repo / '.no-hooks'}", *args]
    return subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _commit_files(repo: Path, files: dict[str, str], message: str) -> str:
    """Write *files* into *repo*, commit them, and return the new commit SHA."""
    for relative, text in files.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text, encoding="utf-8")
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def scanner_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a git repository whose first commit writes generation 7 and whose second declares generation 8."""
    repo = tmp_path / "scanner-src"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    plugin = "plugins/codemap-py/"
    first = _commit_files(
        repo,
        {
            plugin + "scripts/codemap_py_entry.py": _FAKE_ENTRY,
            plugin + "src/codemap_py/schema.py": "SCAN_VERSION: int = 7\n",
        },
        "first",
    )
    second = _commit_files(repo, {plugin + "src/codemap_py/schema.py": "SCAN_VERSION: int = 8\n"}, "second")
    return repo, first, second


@pytest.fixture
def upstream_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a local 'public' repository: one package, one script directory with a bare sibling import."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    sha = _commit_files(
        repo,
        {
            "lib/__init__.py": "",
            "lib/core.py": "import lib.util\n",
            "lib/util.py": "",
            "tools/run.py": "import helper\nimport lib.core\n",
            "tools/helper.py": "",
        },
        "upstream",
    )
    return repo, sha


def _coordinates_file(path: Path, upstream: Path, sha: str) -> Path:
    """Write a one-repository coordinates file pointing at the local *upstream* repository."""
    entry = {"name": "local__upstream", "label": "upstream", "group": "held-out", "url": str(upstream), "commit": sha}
    path.write_text(json.dumps({"repositories": [entry]}), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        pytest.param(
            [{"name": "a", "label": "a", "group": "g", "url": "u", "commit": "abc123"}], "full 40", id="short-sha"
        ),
        pytest.param([{"name": "a", "label": "a", "group": "g", "url": "u"}], "lacks 'commit'", id="missing-field"),
        pytest.param(
            [{"name": "a", "label": "a", "group": "g", "url": "u", "commit": "a" * 40}] * 2,
            "twice",
            id="duplicate-name",
        ),
        pytest.param([], "no repositories", id="empty"),
    ],
)
def test_load_coordinates_rejects_unpinnable_entries(tmp_path: Path, entries: list[dict], message: str) -> None:
    """A coordinate that cannot pin one exact commit, or a repeated name, stops the census before any fetch."""
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"repositories": entries}), encoding="utf-8")
    with pytest.raises(CensusError, match=message):
        load_coordinates(path)


def test_tracked_coordinates_pin_full_commits_in_both_groups() -> None:
    """The default coordinates file parses through the validator: five full SHAs, three held-out, two tuned."""
    coordinates = load_coordinates(BENCHMARKS_DIR / "import-census-repos.json")
    assert [c.group for c in coordinates] == ["held-out", "held-out", "held-out", "tuned", "tuned"]


def test_run_census_scores_and_classes_with_injected_fetch_and_index(tmp_path: Path) -> None:
    """Offline pipeline: the fake index misses the script-directory edge and adds a parent-package edge.

    The written JSON and Markdown carry only repository-relative paths, never the work directory.
    """
    coordinate = RepoCoordinate("demo__repo", "demo", "held-out", "https://example.invalid/demo.git", "b" * 40)
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "import pkg.b\n",
        "pkg/b.py": "",
        "s/run.py": "import h\n",
        "s/h.py": "",
    }

    def fetch(_: RepoCoordinate, destination: Path) -> Path:
        for relative, text in files.items():
            (destination / relative).parent.mkdir(parents=True, exist_ok=True)
            (destination / relative).write_text(text, encoding="utf-8")
        return destination

    def index(_: Scanner, clone: Path) -> Path:
        modules = [
            {"name": "pkg", "path": "pkg/__init__.py", "direct_imports": []},
            {"name": "pkg.a", "path": "pkg/a.py", "direct_imports": ["pkg.b", "pkg"]},
            {"name": "pkg.b", "path": "pkg/b.py", "direct_imports": []},
        ]
        path = clone / "index.json"
        path.write_text(json.dumps({"modules": modules}), encoding="utf-8")
        return path

    scanner = Scanner(ref="HEAD", sha="c" * 40, plugin_root=tmp_path, scan_version=15)
    result = run_census([coordinate], scanner, tmp_path / "work", fetch=fetch, index=index)
    json_path, markdown_path = write_results(result, tmp_path / "out")
    repo = json.loads(json_path.read_text(encoding="utf-8"))["repos"][0]
    assert (repo["true_positive"], repo["false_negative"], repo["false_positive"]) == (1, 1, 1)
    assert repo["class_counts"] == {"fn:script_directory": 1, "fp:parent_package_init": 1}
    assert repo["parent_package_adjusted_precision"]["value"] == 1.0
    assert str(tmp_path) not in json_path.read_text(encoding="utf-8") + markdown_path.read_text(encoding="utf-8")


@pytest.mark.integration
def test_scanner_checkout_uses_the_named_revision_and_removes_its_worktree(
    tmp_path: Path, scanner_repo: tuple[Path, str, str]
) -> None:
    """A worktree starts at the revision named, not at the current HEAD, and is unregistered on exit."""
    repo, first, _ = scanner_repo
    with scanner_checkout(repo, "HEAD~1", tmp_path / "work") as scanner:
        checkout = scanner.plugin_root
        assert (scanner.sha, scanner.scan_version, checkout.is_dir()) == (first, 7, True)
    assert not checkout.exists()
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.integration
def test_build_index_rejects_an_index_of_another_generation(
    tmp_path: Path, scanner_repo: tuple[Path, str, str], upstream_repo: tuple[Path, str]
) -> None:
    """A scanner declaring generation 8 whose index records 7 fails loudly instead of scoring a stale index."""
    repo, _, second = scanner_repo
    upstream, _ = upstream_repo
    with scanner_checkout(repo, second, tmp_path / "work") as scanner, pytest.raises(CensusError, match="generation 7"):
        build_index(scanner, upstream)


@pytest.mark.integration
def test_cli_writes_census_and_prints_tables_without_arm_rows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    scanner_repo: tuple[Path, str, str],
    upstream_repo: tuple[Path, str],
) -> None:
    """End to end: fetch a local repository at its pinned commit, index it with the fake scanner, score, report.

    The census has no A/B/C arms, so it prints Markdown tables only and never routes through the shared arm-row renderer
    that ``benchmarks/AGENTS.md`` requires for provider studies.
    """
    repo, first, _ = scanner_repo
    upstream, sha = upstream_repo
    cli = _load_cli()
    cli.main(
        scanner_ref=first,
        out=str(tmp_path / "out"),
        workdir=str(tmp_path / "work"),
        coordinates=str(_coordinates_file(tmp_path / "repos.json", upstream, sha)),
        scanner_repo=str(repo),
    )
    printed = capsys.readouterr().out
    result = json.loads((tmp_path / "out" / "census.json").read_text(encoding="utf-8"))
    assert result["scanner"] == {"ref": first, "sha": first, "scan_version": 7}
    assert result["repos"][0]["class_counts"] == {"fn:script_directory": 1}
    assert "| held-out | **pooled**" in printed
    assert not any(arm in printed for arm in ("A_plain", "B_auto", "C_strict"))


def test_cli_rejects_a_ref_fire_parsed_as_a_number() -> None:
    """Fire turns an all-digit SHA prefix into an int; the CLI refuses it instead of resolving a wrong revision."""
    cli = _load_cli()
    with pytest.raises(CensusError, match="must be a string"):
        cli.main(scanner_ref=1234567)


def test_cli_module_runs_as_a_script_with_help() -> None:
    """The Fire entry point imports cleanly from its own directory and answers ``--help`` without network."""
    completed = subprocess.run(
        [sys.executable, str(BENCHMARKS_DIR / "run-import-census.py"), "--help"],
        cwd=BENCHMARKS_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    # Fire writes help to standard error when standard output is not a terminal.
    assert "scanner_ref" in (completed.stdout + completed.stderr).replace("-", "_")
