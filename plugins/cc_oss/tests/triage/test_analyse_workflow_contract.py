"""Exercise analysis fetch, cache and execution boundaries from the shipped instructions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/analyse/SKILL.md"
_THREAD = _PLUGIN / "skills/analyse/modes/thread.md"
_BASH = shutil.which("bash")
_SHELL = pytest.mark.skipif(_BASH is None or shutil.which("jq") is None, reason="Bash and jq are required")


def _block(path: Path, anchor: str) -> str:
    """Extract the Bash block following a literal line anchor."""
    lines = path.read_text(encoding="utf-8").splitlines()
    anchor_index = next(i for i, line in enumerate(lines) if anchor in line)
    start = lines.index("```bash", anchor_index) + 1
    return "\n".join(lines[start : lines.index("```", start)])


def _section(path: Path, heading: str, end: str) -> list[str]:
    """Read instruction lines between named headings."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    stop = next(i for i in range(start + 1, len(lines)) if lines[i].startswith(end))
    return lines[start:stop]


@pytest.fixture
def shell_env(tmp_path: Path) -> dict[str, str]:
    """Supply private sentinels and the project interpreter to executable fences."""
    return os.environ | {
        "CLAUDE_CODE_SESSION_ID": "analyse-contract",
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "TMPDIR": tmp_path.as_posix(),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "TZ": "Europe/Prague",
    }


def _sentinels(tmp_path: Path, values: dict[str, str]) -> None:
    """Write parent state consumed by a fresh analysis shell."""
    for name, value in values.items():
        (tmp_path / f"{name}-analyse-contract").write_text(value + "\n", encoding="utf-8", newline="\n")


def _run(script: str, env: dict[str, str], prefix: str = "") -> subprocess.CompletedProcess[str]:
    """Run a shipped fence with only the external GitHub boundary replaced."""
    return subprocess.run(
        [_BASH, "-c", prefix + "\n" + script], cwd=env["TMPDIR"], env=env, capture_output=True, text=True, timeout=30
    )


@_SHELL
@pytest.mark.parametrize(
    ("updated", "cache_time", "tentative", "valid", "fast"),
    [
        pytest.param("2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z", "true", "true", "true", id="fresh-report"),
        pytest.param("2026-01-02T12:00:00Z", "2026-01-02T11:00:00Z", "true", "false", "false", id="new-activity"),
        pytest.param("2026-01-02T11:00:00Z", "2026-01-02T11:00:00Z", "true", "false", "false", id="same-second-update"),
        pytest.param("2026-01-02T12:00:00Z", "2026-01-02T11:00:00Z", "false", "false", "false", id="no-report-drift"),
        pytest.param("2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z", "false", "true", "false", id="cache-only"),
        pytest.param("", "2026-01-02T11:00:00Z", "true", "false", "false", id="empty-api-result"),
        pytest.param("null", "2026-01-02T11:00:00Z", "false", "false", "false", id="null-api-result"),
        pytest.param("not-a-date", "2026-01-02T11:00:00Z", "false", "false", "false", id="bad-api-date"),
        pytest.param("2026-01-02T10:00:00Z", "", "false", "false", "false", id="missing-cache-date"),
    ],
)
def test_cache_reuse_requires_current_activity(
    tmp_path: Path, shell_env: dict[str, str], updated: str, cache_time: str, tentative: str, valid: str, fast: str
) -> None:
    """Reject stale or unverifiable data and never reuse a nonexistent report."""
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"type": "issue", "ts": cache_time}), encoding="utf-8")
    _sentinels(
        tmp_path,
        {
            "analyse-clean-args": "42",
            "analyse-cache-file": cache.as_posix(),
            "analyse-fast-path-tentative": tentative,
            "analyse-report-mtime": "1767351600",
            "analyse-fast-path": "true",
        },
    )
    result = _run(
        _block(_SKILL, "**Cache hit**"),
        shell_env | {"REMOTE_UPDATED": updated},
        'gh() { printf "%s\\n" "$REMOTE_UPDATED"; }',
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "analyse-cache-valid-analyse-contract").read_text().strip() == valid
    assert (tmp_path / "analyse-fast-path-analyse-contract").read_text().strip() == fast
    assert "integer expression expected" not in result.stderr


@_SHELL
def test_cache_timestamp_precedes_fetch(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Do not hide an update arriving between the item fetch and cache serialization."""
    cache = tmp_path / "cache.json"
    (tmp_path / "analyse-item-analyse-contract").write_text('{"number":42}', encoding="utf-8")
    (tmp_path / "analyse-comments-analyse-contract").write_text("discussion", encoding="utf-8")
    _sentinels(
        tmp_path,
        {
            "analyse-clean-args": "42",
            "analyse-cache-file": cache.as_posix(),
            "oss-detect-type": "issue",
            "analyse-fetch-started": "2026-01-02T10:00:00Z",
        },
    )
    result = _run(_block(_SKILL, "**Cache miss** — after fetching"), shell_env)
    assert result.returncode == 0, result.stderr
    assert json.loads(cache.read_text())["ts"] == "2026-01-02T10:00:00Z"


@_SHELL
def test_pr_fetch_includes_conversation_and_live_reviews(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Collect general conversation alongside full review bodies and inline comments."""
    _sentinels(tmp_path, {"analyse-clean-args": "42"})
    result = _run(
        _block(_THREAD, "PR conversation and checks (always live)"),
        shell_env,
        'gh() { printf "%s\\n" "$*" >> "$TMPDIR/calls"; }',
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert any("--json" in line and "comments" in line.split("--json", 1)[1].split()[0].split(",") for line in calls)
    assert any("--json comments,reviews" in line for line in calls)
    assert not any("issues/42/comments" in line or "pulls/42/reviews" in line for line in calls)
    assert any("pulls/42/comments" in line and "--paginate" in line for line in calls)
    assert any("pulls/42/comments" in line and "--jq" in line for line in calls)
    assert (tmp_path / "analyse-comments-analyse-contract").is_file()


@_SHELL
def test_pr_report_reuse_keeps_live_fetch_route(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Route a PR through thread mode even when its saved report predates no known activity."""
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"type": "pr", "ts": "2026-01-02T11:00:00Z"}), encoding="utf-8")
    _sentinels(
        tmp_path,
        {
            "analyse-clean-args": "42",
            "analyse-cache-file": cache.as_posix(),
            "analyse-fast-path-tentative": "true",
            "analyse-report-mtime": "1767351600",
        },
    )
    result = _run(_block(_SKILL, "**Cache hit**"), shell_env, 'gh() { printf "2026-01-02T10:00:00Z\\n"; }')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "analyse-cache-valid-analyse-contract").read_text().strip() == "true"
    assert (tmp_path / "analyse-fast-path-analyse-contract").read_text().strip() == "false"


@_SHELL
def test_new_run_clears_prior_report_state(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Keep another target's fast-path state from promoting a no-report analysis to reply mode."""
    _sentinels(tmp_path, {"analyse-fast-path-tentative": "true", "analyse-fast-path": "true"})
    result = _run(
        _block(_SKILL, "## Step 1:"),
        shell_env | {"ARGUMENTS": "42"},
        'python() { case "$*" in *parse-skill-flags.py*|*"--mode classify"*) command python "$@";; *) return 0;; esac; }',
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "analyse-fast-path-tentative-analyse-contract").read_text().strip() == "false"
    assert (tmp_path / "analyse-fast-path-analyse-contract").read_text().strip() == "false"


@_SHELL
@pytest.mark.parametrize("package", ["", "mypackage"])
def test_ecosystem_placeholder_never_queries_github(tmp_path: Path, shell_env: dict[str, str], package: str) -> None:
    """Stop before a search when no concrete package name has been bound."""
    _sentinels(tmp_path, {"analyse-package-name": package})
    result = _run(
        _block(_PLUGIN / "skills/analyse/modes/ecosystem.md", "Search downstream consumers"),
        shell_env | {"PACKAGE_NAME": package},
        'gh() { printf called > "$TMPDIR/calls"; }',
    )
    assert result.returncode != 0
    assert not (tmp_path / "calls").exists()


@_SHELL
@pytest.mark.parametrize(
    "args", ["vitality --reply", "ecosystem --reply", "evolution org/project --reply", "evolution org/repo.md --reply"]
)
def test_ignored_reply_does_not_require_shared_directory(tmp_path: Path, shell_env: dict[str, str], args: str) -> None:
    """Ignore thread-only reply mode before testing its shared-directory prerequisite."""
    result = _run(
        _block(_SKILL, "## Step 1:"),
        shell_env | {"ARGUMENTS": args},
        'python() { case "$*" in *parse-skill-flags.py*|*"--mode classify"*) command python "$@";; *) return 0;; esac; }',
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "analyse-reply-mode-analyse-contract").read_text().strip() == "false"


def test_reproduction_requires_explicit_approval_in_project_environment() -> None:
    """Keep third-party code inert until the foreground user approves a bounded execution."""
    lines = _section(_THREAD, "### Step R3:", "### Step R4:")
    assert any("AskUserQuestion" in line and "exact" in line and "per run" in line for line in lines)
    assert any("project environment" in line and "approval" in line for line in lines)
    assert any("Declined approval" in line and "partial" in line for line in lines)
    assert not any("verified isolat" in line or "no-network" in line for line in lines)
    assert any("never run thread-supplied install steps or hooks" in line for line in lines)
    assert any("<!-- untrusted:" in line for line in lines)
    assert any("<!-- end untrusted:" in line for line in lines)
    assert any("approved" in line and "only" in line for line in lines)


def test_discussion_fetch_keeps_reply_cursors() -> None:
    """Expose both comment and reply cursors so long conversations cannot appear complete."""
    lines = _section(_THREAD, "`TYPE=discussion`", "Wide-net duplicate search")
    assert any("$after: String" in line for line in lines)
    assert any("comments(first: 50, after: $after)" in line for line in lines)
    assert any("replies(first: 50)" in line for line in lines)
    assert any("replies(first: 50, after: $after)" in line for line in lines)
    assert any("200" in line and "replies" in line and "Summary" in line for line in lines)


def test_followup_prints_manual_only_commands() -> None:
    """Honor implementation follow-ups without invoking user-only skills through the model."""
    lines = _section(_SKILL, "### 6a", "### 6b")
    assert any("disable-model-invocation: true" in line for line in lines)
    assert any("Run:" in line and "manually" in line and "no `Skill()`" in line for line in lines)


@_SHELL
def test_title_search_escapes_syntax(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Keep quoted title punctuation from becoming search qualifiers."""
    _sentinels(tmp_path, {"analyse-clean-args": "42", "oss-detect-type": "issue"})
    title = 'Failure "label:bug" in C:\\data'
    result = _run(
        _block(_THREAD, "Wide-net duplicate search"),
        shell_env | {"THREAD_TITLE": title},
        'gh() { case "$1 $2" in "issue view") printf "%s\\n" "$THREAD_TITLE";; '
        '*) printf "%s\\n" "$*" >> "$TMPDIR/calls"; printf "[]\\n";; esac; }',
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    search = calls.split("--search ", 1)[1].split(" --json", 1)[0]
    assert search.split() == ["Failure", "label", "bug", "in", "C", "data"]
    assert not any(char in search for char in '\\:"')
    assert '--search "' not in calls
    assert "label:bug" not in calls


@_SHELL
def test_title_search_failure_is_visible(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Report a rejected search rather than treating it as an empty candidate set."""
    _sentinels(tmp_path, {"analyse-clean-args": "42", "oss-detect-type": "issue"})
    result = _run(
        _block(_THREAD, "Wide-net duplicate search"),
        shell_env,
        'gh() { case "$1 $2" in "issue view") printf "Failure in parser\\n";; *) return 9;; esac; }',
    )
    assert "search failed" in result.stdout.lower()


@_SHELL
def test_discussion_search_needs_two_shared_tokens(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Reject the first-word coincidence and repeated single-token matches."""
    _sentinels(tmp_path, {"analyse-clean-args": "42", "analyse-thread-title": "Failure parser parser"})
    candidates = {
        "data": {
            "repository": {
                "discussions": {
                    "nodes": [
                        {"number": 1, "title": "Failure network", "body": "different"},
                        {"number": 2, "title": "Parser failure", "body": "shared"},
                        {"number": 3, "title": "Parser update", "body": "single token"},
                    ]
                }
            }
        }
    }
    result = _run(
        _block(_THREAD, "Discussion wide-net"),
        shell_env | {"CANDIDATES": json.dumps(candidates)},
        'gh() { case "$1 $2" in "repo view") printf "org/project\\n";; *) printf "%s\\n" "$CANDIDATES";; esac; }',
    )
    assert result.returncode == 0, result.stderr
    assert [item["number"] for item in json.loads(result.stdout)] == [2]


@_SHELL
def test_cached_discussion_title_ignores_stale_item(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Use this discussion's cached title rather than another item's transient file."""
    cache = tmp_path / "cache.json"
    cache.write_text('{"item":{"title":"Current parser failure"}}', encoding="utf-8")
    (tmp_path / "analyse-item-analyse-contract").write_text('{"title":"Unrelated old issue"}', encoding="utf-8")
    _sentinels(
        tmp_path,
        {
            "analyse-clean-args": "42",
            "oss-detect-type": "discussion",
            "analyse-cache-valid": "true",
            "analyse-cache-file": cache.as_posix(),
        },
    )
    result = _run(_block(_THREAD, "Wide-net duplicate search"), shell_env, 'gh() { printf "[]\\n"; }')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "analyse-thread-title-analyse-contract").read_text().strip() == "Current parser failure"


def test_reply_target_and_output_are_bound() -> None:
    """Use the verified item type and concrete output path in the reply spawn."""
    lines = _section(_SKILL, "## Step 7:", "</workflow>")
    assert any('OUTPUT_PATH=".reports/analyse/thread/' in line for line in lines)
    assert any("pr → `gh pr view" in line and "issue → `gh issue view" in line for line in lines)
    assert any("Direct report path:" in line and "Never infer" in line for line in lines)


@_SHELL
@pytest.mark.parametrize(
    "name", ["analyse-item", "analyse-comments", "analyse-thread-title", "analyse-fetch-started", "analyse-reply-repo"]
)
def test_new_run_clears_transient_inputs(tmp_path: Path, shell_env: dict[str, str], name: str) -> None:
    """Never reuse another target's data after a failed fetch in the same session."""
    _sentinels(tmp_path, {name: "stale"})
    result = _run(
        _block(_SKILL, "## Step 1:"),
        shell_env | {"ARGUMENTS": "42"},
        'python() { case "$*" in *parse-skill-flags.py*|*"--mode classify"*) command python "$@";; *) return 0;; esac; }',
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / f"{name}-analyse-contract").read_text() == ""


@_SHELL
def test_failed_title_fetch_clears_prior_title(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """A failed current title must prevent searches with a previous thread's title."""
    _sentinels(tmp_path, {"analyse-clean-args": "42", "oss-detect-type": "issue", "analyse-thread-title": "stale"})
    result = _run(_block(_THREAD, "Wide-net duplicate search"), shell_env, "gh() { return 1; }")
    assert result.returncode != 0
    assert (tmp_path / "analyse-thread-title-analyse-contract").read_text() == ""


@_SHELL
def test_discussion_search_skips_empty_title(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Do not query GitHub when the title fetch produced no searchable input."""
    _sentinels(tmp_path, {"analyse-clean-args": "42", "analyse-thread-title": ""})
    result = _run(_block(_THREAD, "Discussion wide-net"), shell_env, 'gh() { printf called > "$TMPDIR/calls"; }')
    assert result.returncode == 0, result.stderr
    assert "no title" in result.stdout
    assert not (tmp_path / "calls").exists()


@_SHELL
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        pytest.param("Crash", [1], id="single-keyword"),
        pytest.param("Problem in the docs", [], id="stopwords-only-overlap"),
        pytest.param("in the", [], id="no-meaningful-tokens"),
    ],
)
def test_discussion_search_uses_meaningful_tokens(
    tmp_path: Path, shell_env: dict[str, str], title: str, expected: list[int]
) -> None:
    """Keep single-word matches while excluding coincidental grammatical overlap."""
    _sentinels(tmp_path, {"analyse-clean-args": "42", "analyse-thread-title": title})
    candidates = {
        "data": {
            "repository": {
                "discussions": {
                    "nodes": [
                        {"number": 1, "title": "Crash", "body": "same failure"},
                        {"number": 2, "title": "How to use the model in training", "body": "unrelated"},
                    ]
                }
            }
        }
    }
    result = _run(
        _block(_THREAD, "Discussion wide-net"),
        shell_env | {"CANDIDATES": json.dumps(candidates)},
        'gh() { case "$1 $2" in "repo view") printf "org/project\\n";; *) printf "%s\\n" "$CANDIDATES";; esac; }',
    )
    assert result.returncode == 0, result.stderr
    assert [item["number"] for item in json.loads(result.stdout)] == expected


def test_reproduction_question_and_fence_precede_spawn() -> None:
    """Show code outside the bounded approval question and neutralize it before prompt filling."""
    lines = _section(_THREAD, "### Step R3:", "### Step R4:")
    assert any("last text before" in line and "AskUserQuestion" in line for line in lines)
    assert any("exact snippet" in line and "untrusted fence" in line for line in lines)
    assert any("1,500" in line and "snippet above" in line for line in lines)
    neutralize = next(i for i, line in enumerate(lines) if "Before filling the prompt" in line)
    prompt = lines.index("Reproduction prompt:")
    assert neutralize < prompt


def test_agent_resolution_loads_once() -> None:
    """Use the parent's resolved fallback table without repeating its full load in thread mode."""
    lines = _section(_THREAD, "## Agent Resolution", "`TYPE=issue`")
    assert not any('cat "$_OSS_SHARED/agent-resolution.md"' in line for line in lines)
    assert any("Step 1" in line and "agent-resolution.md" in line for line in lines)


def test_reply_prompt_carries_verified_repository() -> None:
    """Prevent a saved report from another repository from fetching local numbered threads."""
    lines = _section(_SKILL, "## Step 7:", "</workflow>")
    prompt = next(line for line in lines if 'prompt="Draft contributor reply' in line)
    assert "REPO=<REPO>" in prompt
    assert "gh pr view <CLEAN_ARGS> -R <REPO> --comments" in prompt
    assert "gh issue view <CLEAN_ARGS> -R <REPO> --comments" in prompt
    assert "GH_SLUG=<REPO>" in prompt
    assert any(line.startswith("**Replace") and "`<REPO>`" in line for line in lines)
    assert any('name="shepherd-reply"' in line for line in lines)


@_SHELL
@pytest.mark.parametrize(
    ("number", "kind", "repo", "valid"),
    [
        pytest.param("42", "discussion", "org/other", True, id="cross-repository"),
        pytest.param("<N>", "issue", "org/other", False, id="number-placeholder"),
        pytest.param("42", "<issue|pr|discussion>", "org/other", False, id="type-placeholder"),
        pytest.param("42", "issue", "<owner/repo>", False, id="repo-placeholder"),
    ],
)
def test_direct_reply_binding_validates_before_writes(
    tmp_path: Path, shell_env: dict[str, str], number: str, kind: str, repo: str, valid: bool
) -> None:
    """Persist verified direct-report context through shell writes and reject unfilled values."""
    script = _block(_SKILL, "Direct-report sentinel binding")
    script = script.replace("<N>", number).replace("<issue|pr|discussion>", kind).replace("<owner/repo>", repo)
    _sentinels(tmp_path, {"analyse-clean-args": "previous"})
    result = _run(script, shell_env)
    assert (result.returncode == 0) is valid, result.stderr
    assert (tmp_path / "analyse-clean-args-analyse-contract").read_text().strip() == (number if valid else "previous")


@_SHELL
def test_reply_block_resolves_current_repository(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """A numeric reply without a direct-report override resolves the active repository."""
    _sentinels(tmp_path, {"analyse-clean-args": "42", "oss-detect-type": "issue", "analyse-reply-repo": ""})
    result = _run(_block(_SKILL, "One block reloads the vars"), shell_env, 'gh() { printf "org/current\\n"; }')
    assert result.returncode == 0, result.stderr
    assert "REPO=org/current" in result.stdout


@_SHELL
def test_ecosystem_reads_package_from_prior_shell(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Bind a verified package once and reuse it in both invariant search commands."""
    _sentinels(tmp_path, {"analyse-package-name": "real_package"})
    result = _run(
        _block(_PLUGIN / "skills/analyse/modes/ecosystem.md", "Search downstream consumers"),
        shell_env | {"PACKAGE_NAME": "stale"},
        'gh() { printf "%s\\n" "$*" >> "$TMPDIR/calls"; }',
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    assert "from real_package import" in calls
    assert "q=real_package repo:conda-forge" in calls
    assert "stale" not in calls


@_SHELL
def test_inline_review_projection_preserves_finding_location(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Trim transport metadata without losing the body, code context or link used for deduplication."""
    lines = _block(_THREAD, "PR conversation and checks (always live)").splitlines()
    command = next(line for line in lines if line.startswith('gh api "repos/'))
    payload = [
        {
            "id": 7,
            "user": {"login": "reviewer", "avatar_url": "omit"},
            "body": "Finding",
            "path": "module.py",
            "line": 12,
            "diff_hunk": "@@ context",
            "html_url": "https://example.invalid/comment/7",
        }
    ]
    result = _run(
        command,
        shell_env | {"REVIEW_PAYLOAD": json.dumps(payload)},
        'NUMBER=42; gh() { while [ "$1" != "--jq" ]; do shift; done; shift; printf "%s" "$REVIEW_PAYLOAD" | jq "$1"; }',
    )
    assert result.returncode == 0, result.stderr
    comment = json.loads(result.stdout)
    assert comment["id"] == 7
    assert comment["author"] == "reviewer"
    assert comment["body"] == "Finding"
    assert (comment["path"], comment["line"]) == ("module.py", 12)
    assert comment["diff_hunk"] == "@@ context"
    assert comment["html_url"] == "https://example.invalid/comment/7"
    assert "user" not in comment
