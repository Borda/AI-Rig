"""Exercise agent shell handoffs and preserve boundaries for untrusted contributor content."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_BASH = shutil.which("bash")
_SESSION = "agent-workflow-test"
_SHELL_AVAILABLE = pytest.mark.skipif(_BASH is None, reason="Agent fences require Bash")
_GH_STUB = """
gh() {
    printf '%s\\n' "$*" >> "$TMPDIR/gh-calls"
    printf '%s\\n' "${GH_HOST:-}" >> "$TMPDIR/gh-hosts"
    if [ "$1" = auth ]; then
        [ "$*" = 'auth status --hostname github.com' ] || return "${OTHER_HOST_RC:-0}"
        return "${AUTH_RC:-0}"
    fi
    if [ "$1 $2" = 'api rate_limit' ]; then
        if [ "${RATE_RC:-0}" != 0 ]; then
            printf '%s\\n' "$RATE_DATA"
            return "$RATE_RC"
        fi
        if [ "${3:-}" = --jq ]; then
            printf '%s\\n' "$RATE_DATA" | python -c '
import json, sys
resource = json.load(sys.stdin)["resources"][sys.argv[1].split(".")[2]]
print(resource.get("remaining" if "remaining" in sys.argv[1] else "reset", "unknown"))
' "$4"
            return $?
        else
            printf '%s\\n' "$RATE_DATA"
        fi
        return 0
    fi
    printf '%s\\n' package
}
"""
_NO_JQ_STUB = """
jq() {
    printf '%s\\n' "$*" >> "$TMPDIR/jq-calls"
    return 127
}
"""


def _section(agent: str, heading: str, end: str) -> list[str]:
    """Read one instruction section without depending on prose wrapping."""
    lines = (_PLUGIN / "agents" / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(heading) + 1
    return lines[start : lines.index(end, start)]


def _bash_block(agent: str, heading: str, directory: str = "agents") -> str:
    """Extract the first Bash fence after the named section or paragraph."""
    lines = (_PLUGIN / directory / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i in range(lines.index(heading), len(lines)) if lines[i].strip() == "```bash") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "```")
    return "\n".join(lines[start:end])


@pytest.fixture
def shell_env(tmp_path: Path) -> dict[str, str]:
    """Isolate all sentinels and use the active test interpreter for fenced Python calls."""
    return os.environ | {
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "TMPDIR": tmp_path.as_posix(),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "RATE_DATA": json.dumps(
            {"resources": {bucket: {"remaining": 1000} for bucket in ("core", "search", "graphql")}}
        ),
        "GH_OWNER": "",
        "GH_REPO": "",
        "DATA_FILE": "",
        "DIFF_RANGE": "",
        "AUTH_RC": "0",
        "OTHER_HOST_RC": "0",
        "RATE_RC": "0",
    }


def _sentinels(tmp_path: Path, data_file: Path) -> None:
    """Write the three caller-owned values consumed by the scraper setup fence."""
    for name, value in (
        ("analyse-gh-owner", "example"),
        ("analyse-gh-repo", "project"),
        ("vitality-data-file", data_file.as_posix()),
    ):
        (tmp_path / f"{name}-{_SESSION}").write_text(f"{value}\n", encoding="utf-8", newline="\n")


def _run(block: str, tmp_path: Path, env: dict[str, str], prelude: str = "") -> subprocess.CompletedProcess[str]:
    """Run a shipped fence with only external service calls replaced by controlled responses."""
    return subprocess.run(
        [_BASH, "-c", prelude + "\n" + block],
        cwd=tmp_path,
        env=env,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )


@pytest.fixture
def controlled_git(tmp_path: Path, shell_env: dict[str, str]) -> dict[str, str]:
    """Replace only Git responses while running the shipped symbol extractor and caller fence."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    response = tools_dir / "git_response.py"
    response.write_text(
        "import os, sys\n"
        "if sys.argv[1] == 'rev-parse':\n"
        "    print('a' * 40)\n"
        "elif '--name-only' in sys.argv:\n"
        "    print('__init__.py')\n"
        "else:\n"
        "    print(os.environ['DIFF_CONTENT'])\n"
        "    sys.exit(int(os.environ['DIFF_RC']))\n",
        encoding="utf-8",
        newline="\n",
    )
    launcher = tools_dir / "git"
    launcher.write_text('#!/bin/sh\nexec "$TEST_PYTHON" "$GIT_RESPONSE" "$@"\n', encoding="utf-8", newline="\n")
    launcher.chmod(0o755)
    (tools_dir / "git.cmd").write_text('@"%TEST_PYTHON%" "%GIT_RESPONSE%" %*\n', encoding="utf-8", newline="\n")
    (tmp_path / f"shepherd-diff-range-{_SESSION}").write_text("base..head\n", encoding="utf-8", newline="\n")
    return shell_env | {
        "PATH": f"{tools_dir}{os.pathsep}{shell_env['PATH']}",
        "TEST_PYTHON": sys.executable,
        "GIT_RESPONSE": response.as_posix(),
        "DIFF_RANGE": "base..head",
        "GH_TOKEN": "dummy",
        "GH_HOST": "x.invalid",
    }


@_SHELL_AVAILABLE
def test_scraper_initializes_from_caller_sentinels(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """A fresh shell loads the actual repository and output path before truncating or persisting anything."""
    data_file = tmp_path / "raw data.jsonl"
    data_file.write_text("old snapshot\n", encoding="utf-8")
    _sentinels(tmp_path, data_file)
    result = _run(_bash_block("gh-scraper", "## Step 1 — Setup"), tmp_path, shell_env, _GH_STUB)
    assert result.returncode == 0, result.stderr
    assert "WARN:" not in result.stdout + result.stderr
    assert data_file.read_text(encoding="utf-8") == ""
    assert (tmp_path / f"gh-scraper-owner-{_SESSION}").read_text(encoding="utf-8") == "example\n"
    assert (tmp_path / f"gh-scraper-repo-{_SESSION}").read_text(encoding="utf-8") == "project\n"
    assert (tmp_path / f"gh-scraper-data-file-{_SESSION}").read_text(encoding="utf-8") == f"{data_file.as_posix()}\n"


@_SHELL_AVAILABLE
@pytest.mark.parametrize(
    ("sentinel", "value"),
    [
        pytest.param("vitality-data-file", "", id="empty-output-path"),
        pytest.param("vitality-data-file", "$DATA_FILE", id="unsubstituted-output-path"),
        pytest.param("analyse-gh-owner", "", id="empty-owner"),
        pytest.param("analyse-gh-owner", "$GH_OWNER", id="unsubstituted-owner"),
        pytest.param("analyse-gh-repo", "", id="empty-repo"),
        pytest.param("analyse-gh-repo", "$GH_REPO", id="unsubstituted-repo"),
    ],
)
def test_scraper_invalid_input_returns_error_before_auth(
    tmp_path: Path, shell_env: dict[str, str], sentinel: str, value: str
) -> None:
    """Missing or unsubstituted sentinel values cannot become API arguments or output filenames."""
    _sentinels(tmp_path, tmp_path / "raw.jsonl")
    (tmp_path / f"{sentinel}-{_SESSION}").write_text(f"{value}\n", encoding="utf-8", newline="\n")
    result = _run(_bash_block("gh-scraper", "## Step 1 — Setup"), tmp_path, shell_env, _GH_STUB)
    assert result.returncode != 0
    envelope = json.loads(result.stdout.splitlines()[-1])
    assert envelope["status"] == "error"
    assert envelope["error"]
    assert not (tmp_path / "gh-calls").exists()
    assert not (tmp_path / "$DATA_FILE").exists()


@_SHELL_AVAILABLE
def test_scraper_auth_failure_returns_error_without_truncation(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Auth failure returns a valid envelope even when the output path contains JSON-sensitive characters."""
    # Native Windows separators need JSON escaping; POSIX permits a literal backslash in the filename.
    data_file = tmp_path / ("raw data.jsonl" if sys.platform == "win32" else r"raw \data.jsonl")
    data_file.write_text("keep snapshot\n", encoding="utf-8")
    _sentinels(tmp_path, data_file)
    (tmp_path / f"vitality-data-file-{_SESSION}").write_text(str(data_file) + "\n", encoding="utf-8", newline="\n")
    shell_env["AUTH_RC"] = "1"
    result = _run(_bash_block("gh-scraper", "## Step 1 — Setup"), tmp_path, shell_env, _GH_STUB)
    assert result.returncode != 0
    envelope = json.loads(result.stdout.splitlines()[-1])
    assert envelope["status"] == "error"
    assert envelope["file"] == str(data_file)
    assert envelope["error"]
    assert data_file.read_text(encoding="utf-8") == "keep snapshot\n"
    assert (tmp_path / "gh-calls").read_text(encoding="utf-8").splitlines() == ["auth status --hostname github.com"]


@_SHELL_AVAILABLE
def test_scraper_ignores_unrelated_host_auth_failure(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """A bad token on a different configured host does not abort a github.com scrape."""
    _sentinels(tmp_path, tmp_path / "raw.jsonl")
    shell_env["OTHER_HOST_RC"] = "1"
    shell_env["GH_HOST"] = "unrelated.example"
    result = _run(_bash_block("gh-scraper", "## Step 1 — Setup"), tmp_path, shell_env, _GH_STUB)
    assert result.returncode == 0, result.stderr
    assert "auth status --hostname github.com" in (tmp_path / "gh-calls").read_text(encoding="utf-8").splitlines()
    assert set((tmp_path / "gh-hosts").read_text(encoding="utf-8").splitlines()) == {"github.com"}


@_SHELL_AVAILABLE
def test_scraper_output_failure_returns_error_envelope(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """A failed output redirect stops setup with an error instead of persisting an unusable handoff."""
    _sentinels(tmp_path, tmp_path)
    result = _run(_bash_block("gh-scraper", "## Step 1 — Setup"), tmp_path, shell_env, _GH_STUB)
    assert result.returncode != 0
    envelope = json.loads(result.stdout.splitlines()[-1])
    assert envelope["status"] == "error"
    assert envelope["file"] == tmp_path.as_posix()
    assert not (tmp_path / f"gh-scraper-data-file-{_SESSION}").exists()


@_SHELL_AVAILABLE
@pytest.mark.parametrize("bucket", ["core", "search", "graphql"])
def test_scraper_warns_for_each_exhausted_budget(tmp_path: Path, shell_env: dict[str, str], bucket: str) -> None:
    """Each independent API budget is inspected, even while the other two have plenty of quota."""
    _sentinels(tmp_path, tmp_path / "raw.jsonl")
    budgets = {name: {"remaining": 1000, "reset": 2000000000} for name in ("core", "search", "graphql")}
    budgets[bucket] = {"remaining": 0, "reset": 2000000000}
    shell_env["RATE_DATA"] = json.dumps({"resources": budgets})
    result = _run(_bash_block("gh-scraper", "## Step 1 — Setup"), tmp_path, shell_env, _GH_STUB)
    assert result.returncode == 0, result.stderr
    warnings = [line for line in (result.stdout + result.stderr).splitlines() if "WARN:" in line]
    assert len(warnings) == 1
    assert f"0 {bucket}" in warnings[0]


@_SHELL_AVAILABLE
@pytest.mark.parametrize(
    ("rate_data", "rate_rc"),
    [
        pytest.param(
            '{"resources":{"core":{"remaining":1000},"search":{"remaining":1000},"graphql":{"remaining":1000}}}',
            "0",
            id="no-external-jq",
        ),
        pytest.param("<html>502 Bad Gateway</html>", "0", id="non-json-body"),
        pytest.param('{"message":"Bad credentials"}', "1", id="failed-response-with-body"),
        pytest.param(
            '{"resources":{"core":{"remaining":"unknown"},"search":{"remaining":null},"graphql":{"remaining":""}}}',
            "0",
            id="nonnumeric-budgets",
        ),
    ],
)
def test_scraper_advisory_failure_does_not_abort_setup(
    tmp_path: Path, shell_env: dict[str, str], rate_data: str, rate_rc: str
) -> None:
    """Unavailable quota data and a missing external jq cannot prevent an authenticated scrape."""
    data_file = tmp_path / "raw.jsonl"
    data_file.write_text("old snapshot\n", encoding="utf-8")
    _sentinels(tmp_path, data_file)
    shell_env.update({"RATE_DATA": rate_data, "RATE_RC": rate_rc})
    result = _run(_bash_block("gh-scraper", "## Step 1 — Setup"), tmp_path, shell_env, _GH_STUB + _NO_JQ_STUB)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "WARN:" not in result.stdout
    assert data_file.read_text(encoding="utf-8") == ""
    assert (tmp_path / f"gh-scraper-data-file-{_SESSION}").read_text(encoding="utf-8") == f"{data_file.as_posix()}\n"
    assert not (tmp_path / "jq-calls").exists()
    assert (tmp_path / "gh-calls").read_text(encoding="utf-8").splitlines() == [
        "auth status --hostname github.com",
        "api rate_limit --jq .resources.core.remaining",
        "api rate_limit --jq .resources.search.remaining",
        "api rate_limit --jq .resources.graphql.remaining",
    ]


@_SHELL_AVAILABLE
def test_scraper_auth_error_envelope_needs_no_external_jq(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Setup failure still returns JSON and preserves the snapshot when jq is unavailable."""
    # Native Windows separators need JSON escaping; POSIX permits a literal backslash in the filename.
    data_file = tmp_path / ("raw data.jsonl" if sys.platform == "win32" else r"raw \data.jsonl")
    data_file.write_text("keep snapshot\n", encoding="utf-8")
    _sentinels(tmp_path, data_file)
    (tmp_path / f"vitality-data-file-{_SESSION}").write_text(str(data_file) + "\n", encoding="utf-8", newline="\n")
    shell_env["AUTH_RC"] = "1"
    result = _run(_bash_block("gh-scraper", "## Step 1 — Setup"), tmp_path, shell_env, _GH_STUB + _NO_JQ_STUB)
    assert result.returncode != 0
    assert json.loads(result.stdout) == {
        "status": "error",
        "file": str(data_file),
        "error": "not authenticated — run gh auth login --hostname github.com",
    }
    assert data_file.read_text(encoding="utf-8") == "keep snapshot\n"
    assert not (tmp_path / "jq-calls").exists()


@_SHELL_AVAILABLE
@pytest.mark.parametrize("diff_range", ["", "missing..HEAD", "HEAD...HEAD"])
def test_downstream_refuses_missing_or_unsupported_range(
    tmp_path: Path, shell_env: dict[str, str], diff_range: str
) -> None:
    """Missing, invalid and unsupported ranges cannot be reported as an empty successful symbol check."""
    (tmp_path / f"shepherd-diff-range-{_SESSION}").write_text(f"{diff_range}\n", encoding="utf-8", newline="\n")
    shell_env["DIFF_RANGE"] = "ignored..environment"
    result = _run(_bash_block("shepherd", "### Downstream Impact Assessment"), tmp_path, shell_env, _GH_STUB)
    assert result.returncode != 0
    assert "No changed symbols" not in result.stdout


@_SHELL_AVAILABLE
def test_downstream_passes_explicit_range_to_extractor(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """The caller's two-dot range reaches extraction without falling back to the last local commit."""
    bin_dir = tmp_path / "plugin" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "extract_changed_symbols.py").write_text("import sys\nprint(sys.argv[1])\n", encoding="utf-8")
    (bin_dir / "search_downstream_consumers.py").write_text(
        "import sys\nprint('searched=' + sys.stdin.read().strip())\n", encoding="utf-8"
    )
    (tmp_path / f"shepherd-diff-range-{_SESSION}").write_text(
        "release-base..release-head\n", encoding="utf-8", newline="\n"
    )
    shell_env.update({"CLAUDE_PLUGIN_ROOT": bin_dir.parent.as_posix(), "DIFF_RANGE": ""})
    result = _run(
        _bash_block("shepherd", "### Downstream Impact Assessment"),
        tmp_path,
        shell_env,
        _GH_STUB + "\ngit() { return 0; }\n",
    )
    assert result.returncode == 0, result.stderr
    assert "searched=release-base..release-head" in result.stdout
    assert "HEAD~1..HEAD" not in result.stdout


@_SHELL_AVAILABLE
@pytest.mark.parametrize(
    ("diff_content", "diff_rc"),
    [
        pytest.param("-from .api import PublicAPI", "0", id="removed-public-re-export"),
        pytest.param("", "1", id="failed-per-file-diff"),
    ],
)
def test_downstream_empty_real_extraction_is_incomplete(
    tmp_path: Path, controlled_git: dict[str, str], diff_content: str, diff_rc: str
) -> None:
    """An empty result from the actual bounded helper cannot certify a breaking change as safe."""
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    controlled_git.update({"DIFF_CONTENT": diff_content, "DIFF_RC": diff_rc})
    result = _run(_bash_block("shepherd", "### Downstream Impact Assessment"), tmp_path, controlled_git, _GH_STUB)
    assert result.returncode != 0
    assert "INCOMPLETE:" in result.stdout
    assert "downstream impact unverified" in result.stdout
    assert "skipping ecosystem check" not in result.stdout


@_SHELL_AVAILABLE
@pytest.mark.integration
def test_downstream_real_unchanged_range_still_reports_bounded_coverage(
    tmp_path: Path, shell_env: dict[str, str]
) -> None:
    """Read real Git history from a private working directory and report an empty diff as unverified."""
    git_dir = subprocess.run(
        ["git", "-C", str(_PLUGIN.parents[1]), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    shell_env.update({"GIT_DIR": git_dir, "GIT_WORK_TREE": tmp_path.as_posix(), "GIT_OPTIONAL_LOCKS": "0"})
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / f"shepherd-diff-range-{_SESSION}").write_text("HEAD..HEAD\n", encoding="utf-8", newline="\n")
    result = _run(_bash_block("shepherd", "### Downstream Impact Assessment"), tmp_path, shell_env, _GH_STUB)
    assert result.returncode != 0
    assert "INCOMPLETE:" in result.stdout
    assert "downstream impact unverified" in result.stdout
    assert (tmp_path / "gh-calls").read_text(encoding="utf-8").splitlines() == ["repo view --json name --jq .name"]


@_SHELL_AVAILABLE
def test_voice_guard_refuses_missing_voice_even_with_semver(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """The always-loaded sidecar guard names and checks voice rather than an unrelated SemVer sidecar."""
    (tmp_path / "semver-rules.md").write_text("semver", encoding="utf-8")
    (tmp_path / f"shepherd-oss-shared-{_SESSION}").write_text(
        f"{tmp_path.as_posix()}\n", encoding="utf-8", newline="\n"
    )
    result = _run(_bash_block("shepherd", "Verify required sidecar before use:"), tmp_path, shell_env)
    assert result.returncode != 0
    assert "ERROR: shepherd-voice.md not found" in result.stdout


@_SHELL_AVAILABLE
def test_voice_only_task_does_not_require_semver(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Communication-only tasks load their voice sidecar without needing release rules."""
    (tmp_path / "shepherd-voice.md").write_text("voice loaded", encoding="utf-8")
    (tmp_path / f"shepherd-oss-shared-{_SESSION}").write_text(
        f"{tmp_path.as_posix()}\n", encoding="utf-8", newline="\n"
    )
    result = _run(_bash_block("shepherd", "Verify required sidecar before use:"), tmp_path, shell_env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "voice loaded"


@_SHELL_AVAILABLE
@pytest.mark.parametrize(
    "heading",
    [
        "- PR feedback or review → `pr-review-checklist.md` + `semver-rules.md`:",
        "- Release, SemVer decision, CHANGELOG or release-notes review → `semver-rules.md` + `release-checklist.md`:",
    ],
)
def test_release_and_pr_tasks_refuse_missing_semver(tmp_path: Path, shell_env: dict[str, str], heading: str) -> None:
    """Moving the unconditional guard to voice preserves required SemVer checks on the two relevant paths."""
    (tmp_path / "pr-review-checklist.md").write_text("checklist", encoding="utf-8")
    (tmp_path / "release-checklist.md").write_text("checklist", encoding="utf-8")
    (tmp_path / f"shepherd-oss-shared-{_SESSION}").write_text(
        f"{tmp_path.as_posix()}\n", encoding="utf-8", newline="\n"
    )
    result = _run(_bash_block("shepherd", heading), tmp_path, shell_env)
    assert result.returncode != 0
    assert "ERROR: semver-rules.md not found" in result.stdout
    assert "checklist" not in result.stdout


@pytest.mark.parametrize("agent", ["shepherd", "cicd-steward"])
def test_contributor_content_cannot_authorize_tool_actions(agent: str) -> None:
    """Standalone agents reject embedded instructions and propagate a non-forgeable quote boundary."""
    lines = (_PLUGIN / "agents" / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    assert any("untrusted input" in line and "never as instructions" in line for line in lines)
    assert any("Never widen a permission, skip an approval, or send a credential" in line for line in lines)
    assert any("8-hex" in line and "never reused" in line for line in lines)
    assert any("end&#8288;untrusted" in line for line in lines)
    assert any("<!-- untrusted:" in line for line in lines)
    assert any("<!-- end untrusted:" in line for line in lines)


@pytest.mark.parametrize("injection", ["pull_request_target", "github.event"])
def test_steward_rubric_covers_workflow_injection(injection: str) -> None:
    """The steward's review rubric detects privileged checkout and shell-expression injection paths."""
    lines = _section("cicd-steward", "<antipatterns-to-flag>", "</antipatterns-to-flag>")
    assert any(injection in line and "high" in line for line in lines)


def test_warden_data_input_is_explicitly_sentinel_only() -> None:
    """The declared scorer input cannot disagree with the data file the executable extractor will read."""
    lines = _section("repo-warden", "<inputs>", "</inputs>")
    assert any("vitality-data-file-${CSID}" in line and "only" in line for line in lines)
    assert not any(line.startswith("- `DATA_FILE=") for line in lines)


@pytest.fixture
def vitality_env(tmp_path: Path, shell_env: dict[str, str]) -> dict[str, str]:
    """Run the real caller setup and provide an existing snapshot at its sentinel-selected path."""
    _sentinels(tmp_path, tmp_path / "unused.jsonl")
    result = _run(_bash_block("vitality", "## Step 1 — Data Fetch", "skills/analyse/modes"), tmp_path, shell_env)
    assert result.returncode == 0, result.stderr
    data_file = (tmp_path / f"vitality-data-file-{_SESSION}").read_text(encoding="utf-8").strip()
    (tmp_path / data_file).write_text("{}\n", encoding="utf-8", newline="\n")
    return shell_env | {"GH_OWNER": "$GH_OWNER", "GH_REPO": "$GH_REPO", "PARTIAL_FILE": "$PARTIAL_FILE"}


@_SHELL_AVAILABLE
@pytest.mark.parametrize("group", ["A", "B", "C"])
def test_warden_loads_caller_paths_in_fresh_shells(tmp_path: Path, vitality_env: dict[str, str], group: str) -> None:
    """Each scorer reads its own timestamped output sentinel without unresolved prompt assignments."""
    setup = _run(_bash_block("repo-warden", "## Step 1 — Setup"), tmp_path, vitality_env)
    assert setup.returncode == 0, setup.stderr
    values = dict(line.split("=", 1) for line in setup.stdout.splitlines() if "=" in line)
    assert values["GH_OWNER"] == "example"
    assert values["GH_REPO"] == "project"
    assert values["DATA_FILE"] == (tmp_path / f"vitality-data-file-{_SESSION}").read_text(encoding="utf-8").strip()
    selected = _run(_bash_block("repo-warden", f"Group {group}:"), tmp_path, vitality_env)
    assert selected.returncode == 0, selected.stderr
    partial_file = (tmp_path / f"vitality-partial-{group.lower()}-{_SESSION}").read_text(encoding="utf-8").strip()
    assert selected.stdout == f"PARTIAL_FILE={partial_file}\n"
    assert f"partial-{group}-example-project-" in partial_file
    assert not (tmp_path / "$PARTIAL_FILE").exists()


@_SHELL_AVAILABLE
@pytest.mark.parametrize(
    ("sentinel", "value"),
    [
        pytest.param("vitality-gh-owner", "", id="empty-owner"),
        pytest.param("vitality-gh-repo", "$GH_REPO", id="unsubstituted-repo"),
        pytest.param("vitality-data-file", "missing.jsonl", id="missing-snapshot"),
    ],
)
def test_warden_refuses_invalid_shared_sentinels(
    tmp_path: Path, vitality_env: dict[str, str], sentinel: str, value: str
) -> None:
    """Invalid session inputs stop a scorer before extraction or writing a partial file."""
    (tmp_path / f"{sentinel}-{_SESSION}").write_text(f"{value}\n", encoding="utf-8", newline="\n")
    result = _run(_bash_block("repo-warden", "## Step 1 — Setup"), tmp_path, vitality_env)
    assert result.returncode != 0
    assert "ERROR:" in result.stdout


@_SHELL_AVAILABLE
@pytest.mark.parametrize("group", ["A", "B", "C"])
@pytest.mark.parametrize("value", ["", "$PARTIAL_FILE", "<path>"])
def test_warden_refuses_invalid_group_output_path(
    tmp_path: Path, vitality_env: dict[str, str], group: str, value: str
) -> None:
    """Every fixed group block rejects an empty or unresolved output sentinel before Write can use it."""
    (tmp_path / f"vitality-partial-{group.lower()}-{_SESSION}").write_text(f"{value}\n", encoding="utf-8", newline="\n")
    result = _run(_bash_block("repo-warden", f"Group {group}:"), tmp_path, vitality_env)
    assert result.returncode != 0
    assert "ERROR:" in result.stdout
    assert "PARTIAL_FILE=" not in result.stdout


@pytest.mark.parametrize("group", ["A", "B", "C"])
def test_vitality_scorer_spawns_supply_only_literal_group(group: str) -> None:
    """Scorers get their group as text and resolve all runtime paths from caller sentinels."""
    lines = (_PLUGIN / "skills/analyse/modes/vitality.md").read_text(encoding="utf-8").splitlines()
    spawn = next(line for line in lines if f'name="repo-warden-{group}"' in line)
    assert spawn.split('prompt="', 1)[1].split('"', 1)[0] == f"AXIS_GROUP={group}"


def test_vitality_fetch_spawn_uses_session_inputs() -> None:
    """The scraper prompt requests a fetch without passing unresolved shell values as inputs."""
    lines = (_PLUGIN / "skills/analyse/modes/vitality.md").read_text(encoding="utf-8").splitlines()
    spawn = next(line for line in lines if 'subagent_type="oss:gh-scraper"' in line)
    prompt = spawn.split('prompt="', 1)[1].split('"', 1)[0]
    assert "$" not in prompt
    assert "sentinels" in prompt


def test_warden_unavailable_group_has_axis_scores_and_group_confidence() -> None:
    """Two unavailable Group C axes use the declared envelope rather than inventing a group score field."""
    lines = _section("repo-warden", "<notes>", "</notes>")
    assert any("both axes" in line and "confidence" in line and "0.0" in line for line in lines)
    assert not any("return `score: null` for whole group" in line for line in lines)


def test_shepherd_pr_workflow_consumes_supplied_review_results() -> None:
    """PR feedback cannot silently turn into a single-agent code review."""
    lines = _section("shepherd", "## Workflow", "</workflow>")
    assert any(line.startswith("3.") and "supplied review report" in line for line in lines)
    assert any(line.startswith("4.") and "report" in line for line in lines)
    assert not any("Review diff before description" in line for line in lines)


def test_shepherd_accepts_analysis_reports_and_returns_blocked_envelope() -> None:
    """Either analysis report can support PR feedback; missing evidence yields only a blocked envelope."""
    lines = _section("shepherd", "## Workflow", "</workflow>")
    step = next(line for line in lines if line.startswith("3."))
    assert "/oss:analyse" in step
    assert "/oss:review" in step
    assert "supplied analysis report" in step
    assert "Return ONLY" in step
    assert '"status":"blocked"' in step
    assert "one-line reason" in step
    assert "never ask a prose question" in step


def test_shepherd_thread_reports_route_to_analyse() -> None:
    """A thread triage report has a different owner from a contributor-facing reply draft."""
    lines = _section("shepherd", "<routing-boundaries>", "</routing-boundaries>")
    assert any("thread triage reports" in line and "/oss:analyse" in line for line in lines)


def test_shepherd_duplicate_search_requires_sufficient_list_limit() -> None:
    """Duplicate searches must request enough candidates instead of accepting the CLI's default page."""
    lines = _section("shepherd", "<tool-usage>", "</tool-usage>")
    assert any("gh issue list" in line and "--limit" in line and "10× expected results" in line for line in lines)
