"""Protect engineering review boundaries and source-grounded external lookups."""

import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_AGENTS = Path(__file__).resolve().parents[2] / "agents"
_requires_bash = pytest.mark.skipif(not _bash_runs_posix_script(), reason="requires Bash executing POSIX scripts")


def _section(agent: str, start: str, end: str) -> list[str]:
    """Read one agent section without depending on prose wrapping."""
    lines = (_AGENTS / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    first = next(index for index, line in enumerate(lines) if line.startswith(start))
    last = next(index for index in range(first + 1, len(lines)) if lines[index].startswith(end))
    return lines[first:last]


def test_engineering_review_skips_mutating_steps_and_handoffs() -> None:
    """Keep review dispatches from writing tests, applying fixes or requesting fix agents."""
    lines = _section("sw-engineer", "<workflow>", "</workflow>")
    gate = next(index for index, line in enumerate(lines) if "**Task mode**" in line)
    preflight = next(index for index, line in enumerate(lines) if line.startswith("00."))
    assert gate < preflight
    assert "00–04, 08–09, and 13 only" in lines[gate]
    assert "do not edit" in lines[gate]
    assert "non-mutating" in lines[gate]
    assert "no implementation handoffs" in lines[gate]


def test_engineering_diagnostics_use_pinned_targeted_hooks() -> None:
    """Prevent repository-wide autofixes and bypassing pinned tool versions."""
    lines = _section("sw-engineer", "<workflow>", "</workflow>")
    check = next(line for line in lines if line.startswith("07."))
    assert "pre-commit run <hook-id> --files <changed>" in check
    assert "do not bypass" in check
    assert "ruff check <changed>" in check
    assert "pre-existing user edits" in check
    assert "only non-rewriting checks" in check
    assert "args lack a fix flag" in check
    assert "verify hook behavior" in check
    assert "default-rewriting formatters" in check
    assert "no `--fix`" in check
    assert "report suggested fixes" in check
    assert "disposable-copy" not in check
    assert "its resolved project runner" not in check
    assert check.index("`.venv` executable") < check.index("`uv run --no-sync`") < check.index("PATH")
    assert "verified with `--version`" in check
    assert "ruff check ." not in check
    assert "mypy src/" not in check
    assert "path detection" in check


def test_engineering_preserves_lines_present_before_its_first_edit() -> None:
    """Distinguish user changes before diagnostics or cleanup can modify a dirty file."""
    lines = _section("sw-engineer", "<workflow>", "</workflow>")
    baseline = next(line for line in lines if line.startswith("02."))
    assert "git status --porcelain" in baseline
    assert "git diff --name-only HEAD" in baseline
    assert "before any edit" in baseline
    assert "user-edited" in baseline
    antipatterns = _section("sw-engineer", "<antipatterns-to-flag>", "</antipatterns-to-flag>")
    cleanup = next(line for line in antipatterns if "**Opportunistic side-editing**" in line)
    assert "revert only lines you added outside the target" in cleanup
    assert "never revert lines present before your first edit" in cleanup
    assert "revert any non-target lines" not in cleanup


def test_engineering_blocker_substeps_preserve_the_ordered_actions() -> None:
    """Keep nested blocker actions distinct from the top-level workflow numbers."""
    lines = _section("sw-engineer", "<workflow>", "</workflow>")
    first = next(index for index, line in enumerate(lines) if line.startswith("10."))
    assert lines[first + 1].startswith("    - a. State blocker explicitly.")
    assert lines[first + 2].startswith("    - b. Think creatively:")
    assert lines[first + 3].startswith("    - c. No clean unblock path")


def test_engineering_validation_conditions_bin_checks_on_plugin_edits() -> None:
    """Keep generic implementations and reviews from running plugin-only wiring checks."""
    lines = _section("sw-engineer", "<workflow>", "</workflow>")
    check = next(line for line in lines if line.startswith("08."))
    assert "before handing back" in check
    assert "before committing" not in check
    assert "Inside a plugin source tree" in check
    assert "when `bin/` files are involved" in check
    assert "check_orphaned_bin.py" in check


def test_engineering_handoffs_have_an_ordered_return_field() -> None:
    """Make QA then linting actionable by the caller after implementation returns."""
    lines = _section("sw-engineer", "<output-format>", "</output-format>")
    handoff = next(line for line in lines if "**handoff**" in line)
    assert "return envelope" in handoff
    assert handoff.index("foundry:qa-specialist") < handoff.index("foundry:linting-expert")
    assert "sequential" in handoff
    assert "review" in handoff
    assert "only when" in handoff
    assert "schema" in handoff
    assert "one line" in handoff
    assert "prose summary" in handoff
    assert "Never add keys" in handoff
    assert "Return ONLY" in handoff
    assert "caller may decline" in handoff
    assert "required return envelope field" not in handoff
    workflow = _section("sw-engineer", "<workflow>", "</workflow>")
    qa = next(line for line in workflow if line.startswith("11."))
    lint = next(line for line in workflow if line.startswith("12."))
    assert "Recommend" in qa
    assert "only when" in qa
    assert "schema" in qa
    assert "prose summary" in qa
    assert "Return ONLY" in qa
    assert "Recommend" in lint
    assert "caller may decline" in lint
    assert "must spawn" not in qa
    assert "must spawn" not in lint
    notes = _section("sw-engineer", "<notes>", "</notes>")
    qa_scope = next(line for line in notes if line.startswith("- `foundry:qa-specialist`"))
    lint_scope = next(line for line in notes if line.startswith("- `foundry:linting-expert`"))
    assert "recommended" in qa_scope
    assert "recommended" in lint_scope
    assert "mandatory" not in qa_scope
    assert "mandatory" not in lint_scope


def test_engineering_precommit_protocol_reference_resolves() -> None:
    """Point setup instructions at the shipped version-pinning protocol."""
    lines = _section("sw-engineer", "<notes>", "</notes>")
    reference = next(line for line in lines if line.startswith("**pre-commit versioning**"))
    target = "references/linting-expert/precommit-patterns.md"
    assert target in reference
    assert (_AGENTS.parent / target).is_file()
    assert "versioning section" not in reference


def test_web_url_questions_require_fetched_content() -> None:
    """Do not skip URL fetches or API verification based on recalled knowledge."""
    lines = (_AGENTS / "web-explorer.md").read_text(encoding="utf-8").splitlines()
    description = next(line for line in lines if line.startswith("description:"))
    routing = _section("web-explorer", "<routing-boundaries>", "</routing-boundaries>")
    assert "page content already fetched in context" in description
    assert any("user pastes a URL" in line for line in routing)
    assert not any("training knowledge" in line for line in routing)


def test_web_scope_allows_manifest_pins_and_external_readmes() -> None:
    """Allow version pin lookup and external README extraction without local code analysis."""
    lines = _section("web-explorer", "<routing-boundaries>", "</routing-boundaries>")
    assert any("Exception" in line and "pyproject.toml" in line and "uv.lock" in line for line in lines)
    assert any("NOT for code implementation" in line for line in lines)
    assert any("local README" in line and "foundry:doc-scribe" in line for line in lines)
    assert any("external README" in line and "in scope" in line for line in lines)
    workflow = _section("web-explorer", "<workflow>", "</workflow>")
    scope = next(line for line in workflow if line.startswith("1."))
    assert "<routing-boundaries>" in scope
    assert "Stop" in scope
    assert not any("NOT:" in line for line in workflow)


def test_web_package_release_lookup_uses_pypi_json() -> None:
    """Use supported release and project URL fields instead of nonexistent uv subcommands."""
    lines = _section("web-explorer", "<oss-python-patterns>", "</oss-python-patterns>")
    assert any("https://pypi.org/pypi/<package>/json" in line and "WebFetch" in line for line in lines)
    assert any("info.version" in line and "releases" in line for line in lines)
    assert not any("uv pip index" in line for line in lines)
    lookup = _section("web-explorer", "## Finding Docs Pages", "## Search Queries")
    assert any("info.project_urls" in line and "not installed" in line for line in lookup)
    assert not any("uv pip show" in line for line in lookup)


def _metadata_recipe() -> str:
    """Read the executable metadata block without substituting runtime values."""
    lines = _section("web-explorer", "## Finding Docs Pages", "## Search Queries")
    first = lines.index("```bash") + 1
    last = lines.index("```", first)
    return "\n".join(lines[first:last])


@_requires_bash
@pytest.mark.parametrize("project_urls", [False, True])
def test_web_installed_metadata_recipe_reads_all_project_urls(tmp_path: Path, project_urls: bool) -> None:
    """Execute the lookup on real distribution metadata with repeated or absent URL headers."""
    distribution = tmp_path / "sample_library-1.2.3.dist-info"
    distribution.mkdir()
    urls = "Project-URL: Documentation, https://example.org/docs\nProject-URL: Source, https://example.org/source\n"
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample-library\nVersion: 1.2.3\n" + (urls if project_urls else ""),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", f'python() {{ {shlex.quote(sys.executable)} "$@"; }}\n' + _metadata_recipe()],
        cwd=tmp_path,
        env=os.environ | {"PYTHONPATH": str(tmp_path), "LIB": "sample-library"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == (
        ["1.2.3", "Documentation, https://example.org/docs", "Source, https://example.org/source"]
        if project_urls
        else ["1.2.3", ""]
    )


def test_web_release_comparison_paginates_and_handles_access_failure() -> None:
    """Require full release history and a fetch fallback when CLI access is unavailable."""
    lines = _section("web-explorer", "## GitHub Release Notes Extraction", "## Ecosystem")
    assert any("command -v gh" in line and "gh api" in line for line in lines)
    assert any("gh api --paginate" in line and "/releases" in line for line in lines)
    assert any("WebFetch" in line and "authentication" in line for line in lines)
    assert any("partial" in line and "incomplete" in line for line in lines)
    assert not any("--limit 10" in line for line in lines)


def _release_recipe() -> str:
    """Read the executable release block without substituting runtime values."""
    lines = _section("web-explorer", "## GitHub Release Notes Extraction", "## Ecosystem")
    first = lines.index("```bash") + 1
    last = lines.index("```", first)
    return "\n".join(lines[first:last])


_GH_BOUNDARY = """
gh() {
    printf '%s\\n' "$*" >> "$GH_LOG"
    case "$*" in
        'api repos/example/library --silent') return "$ACCESS_STATUS" ;;
        'release view v1.2.3 --repo example/library') return "$VIEW_STATUS" ;;
        'api --paginate repos/example/library/releases --jq .[] | [.tag_name, .published_at, .prerelease] | @tsv') return "$RELEASE_STATUS" ;;
        *) return 91 ;;
    esac
}
"""


@_requires_bash
@pytest.mark.parametrize(
    ("access_status", "view_status", "release_status", "expected_calls", "fallback"),
    [
        pytest.param(0, 0, 0, 3, False, id="all-release-pages-read"),
        pytest.param(1, 0, 0, 1, True, id="access-denied"),
        pytest.param(0, 1, 0, 3, True, id="release-view-failed"),
        pytest.param(0, 0, 1, 2, True, id="paginated-read-failed"),
    ],
)
def test_web_release_recipe_falls_back_on_each_read_failure(
    tmp_path: Path, access_status: int, view_status: int, release_status: int, expected_calls: int, fallback: bool
) -> None:
    """Exercise the real shell sequence while replacing only the external GitHub CLI boundary."""
    log = tmp_path / "gh-requests.txt"
    result = subprocess.run(
        ["bash", "-c", _GH_BOUNDARY + _release_recipe()],
        cwd=tmp_path,
        env=os.environ
        | {
            "GH_LOG": str(log),
            "GH_TOKEN": "dummy",
            "GH_HOST": "x.invalid",
            "REPO": "example/library",
            "TAG": "v1.2.3",
            "ACCESS_STATUS": str(access_status),
            "VIEW_STATUS": str(view_status),
            "RELEASE_STATUS": str(release_status),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(log.read_text(encoding="utf-8").splitlines()) == expected_calls
    assert ("WebFetch" in result.stdout) == fallback
    assert result.stderr == ""


@_requires_bash
def test_web_release_recipe_falls_back_without_cli(tmp_path: Path) -> None:
    """Avoid executing a release command when the executable availability probe fails."""
    result = subprocess.run(
        ["bash", "-c", "command() { return 1; }\n" + _release_recipe()],
        cwd=tmp_path,
        env=os.environ | {"REPO": "example/library", "TAG": "v1.2.3", "GH_TOKEN": "dummy", "GH_HOST": "x.invalid"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "use WebFetch" in result.stdout
    assert result.stderr == ""


@_requires_bash
@pytest.mark.parametrize(
    ("recipe", "variable", "value"),
    [
        pytest.param(_metadata_recipe, "LIB", "", id="missing-library"),
        pytest.param(_metadata_recipe, "LIB", "<library>", id="unsubstituted-library"),
        pytest.param(_release_recipe, "REPO", "", id="missing-repository"),
        pytest.param(_release_recipe, "REPO", "<org>/<repo>", id="unsubstituted-repository"),
        pytest.param(_release_recipe, "TAG", "", id="missing-tag"),
        pytest.param(_release_recipe, "TAG", "v<version>", id="unsubstituted-tag"),
    ],
)
def test_web_recipes_abort_before_tools_for_unset_runtime_values(
    tmp_path: Path, recipe: Callable[[], str], variable: str, value: str
) -> None:
    """Reject missing or placeholder input without misdiagnosing a CLI access failure."""
    log = tmp_path / "unexpected-tool-call.txt"
    boundary = 'gh() { echo gh >> "$GH_LOG"; return 91; }\npython() { echo python >> "$GH_LOG"; return 91; }\n'
    result = subprocess.run(
        ["bash", "-c", boundary + recipe()],
        cwd=tmp_path,
        env=os.environ
        | {
            "GH_LOG": str(log),
            "GH_TOKEN": "dummy",
            "GH_HOST": "x.invalid",
            "REPO": "example/library",
            "TAG": "v1.2.3",
            "LIB": "sample-library",
        }
        | {variable: value},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.strip() == f"! BLOCKED — set {variable}"
    assert result.stderr == ""
    assert not log.exists()


@_requires_bash
def test_web_release_recipe_bounds_index_before_reading_selected_tag(tmp_path: Path) -> None:
    """Read all release pages as a compact index before fetching the selected release body."""
    log = tmp_path / "gh-requests.txt"
    result = subprocess.run(
        ["bash", "-c", _GH_BOUNDARY + _release_recipe()],
        cwd=tmp_path,
        env=os.environ
        | {
            "GH_LOG": str(log),
            "GH_TOKEN": "dummy",
            "GH_HOST": "x.invalid",
            "REPO": "example/library",
            "TAG": "v1.2.3",
            "ACCESS_STATUS": "0",
            "VIEW_STATUS": "0",
            "RELEASE_STATUS": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "api repos/example/library --silent",
        "api --paginate repos/example/library/releases --jq .[] | [.tag_name, .published_at, .prerelease] | @tsv",
        "release view v1.2.3 --repo example/library",
    ]
    lines = _section("web-explorer", "## GitHub Release Notes Extraction", "## Ecosystem")
    assert any("only for tags inside the requested range" in line for line in lines)
