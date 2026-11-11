"""Exercise review recipes against failed snapshots and mismatched source identities."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _text_io import bash_path

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"
_BASH = shutil.which("bash")
_ZSH = shutil.which("zsh")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="Review recipes require Bash")
_SHA = "a" * 40


def _block(relative: str, marker: str) -> str:
    """Extract a fenced recipe by a unique line marker."""
    blocks: list[str] = []
    active: list[str] | None = None
    for line in (_REVIEW / relative).read_text(encoding="utf-8").splitlines():
        if line == "```bash":
            active = []
        elif line == "```" and active is not None:
            blocks.append("\n".join(active))
            active = None
        elif active is not None:
            active.append(line)
    return next(block for block in blocks if marker in block)


def _run(tmp_path: Path, script: str, shell: str | None = _BASH, **values: str) -> subprocess.CompletedProcess[str]:
    """Execute a shipped recipe with isolated sentinel storage and external command doubles."""
    return subprocess.run(
        [shell, "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {"TMPDIR": bash_path(tmp_path), "CLAUDE_CODE_SESSION_ID": "safety", "CSID": "safety", **values},
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    """Provide the frozen metadata and flags shared by the executable recipes."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "pr-meta.json").write_text(
        json.dumps(
            {
                "headRefOid": _SHA,
                "baseRefOid": "b" * 40,
                "baseRefName": "main",
                "body": "Closes #40",
                "url": "https://github.com/owner/project/pull/7",
                "title": "A change",
                "labels": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    (snap / "files.txt").write_text("package/a.py\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-pr-tag-safety").write_text("7\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-flags-safety").write_text("DIRECT_PATH_MODE=false\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-snap-dir-safety").write_text(bash_path(snap) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-pr-labels-safety").write_text("\n", encoding="utf-8", newline="\n")
    return snap


@_skip_no_bash
@pytest.mark.parametrize(
    "failure", ["diff", "names", "checks", "required", "head", "empty-diff", "empty-names", "moved-head"]
)
def test_snapshot_read_failure_blocks(snapshot: Path, tmp_path: Path, failure: str) -> None:
    """A failed external read cannot become an empty successful snapshot."""
    stub = """
gh() {
  case "$*" in
    *'pr view'*) printf '%s' "$META" ;;
    *'pr diff'*--name-only*) [ "$FAILURE" != names ] || return 1; [ "$FAILURE" != empty-names ] || return 0; echo package/a.py ;;
    *'pr diff'*) [ "$FAILURE" != diff ] || return 1; [ "$FAILURE" != empty-diff ] || return 0; echo 'diff --git a/package/a.py b/package/a.py' ;;
    *'pr checks'*--required*) [ "$FAILURE" != required ] || return 1; echo '[]' ;;
    *'pr checks'*) [ "$FAILURE" != checks ] || return 1; echo '[]' ;;
    *) return 91 ;;
  esac
}
git() {
  [ "$FAILURE" != head ] || return 1
  case "$1" in remote) return 0 ;; diff) [ "$FAILURE" != diff ] || return 1 ;; esac
  printf '%s\\n' "$HEAD_SHA"
}
"""
    result = _run(
        tmp_path,
        stub + _block("SKILL.md", 'SNAP_DIR="${TMPDIR:-/tmp}/oss-review-snap-'),
        FAILURE=failure,
        META=(snapshot / "pr-meta.json").read_text(),
        HEAD_SHA={"moved-head": "c" * 40}.get(failure, _SHA),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "! BLOCKED" in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    ("required", "all_checks", "expected"),
    [
        pytest.param([], [{"name": "lint", "bucket": "fail"}], "true", id="no-required-red-ci"),
        pytest.param(
            [{"name": "test", "bucket": "pass"}], [{"name": "lint", "bucket": "fail"}], "false", id="optional-failure"
        ),
    ],
)
def test_ci_uses_full_checks_only_without_required_set(
    snapshot: Path, tmp_path: Path, required: list[dict], all_checks: list[dict], expected: str
) -> None:
    """Only an absent required set falls back to all reported checks."""
    (snapshot / "checks-required.json").write_text(json.dumps(required), encoding="utf-8", newline="\n")
    (snapshot / "checks.json").write_text(json.dumps(all_checks), encoding="utf-8", newline="\n")
    script = _block("SKILL.md", "CI_FAILING_CHECKS=")
    result = _run(tmp_path, script)
    assert result.returncode == 0, result.stderr
    assert f"CI_RED={expected}" in result.stdout


@_skip_no_bash
def test_python_churn_excludes_docs_and_counts_blank_lines(snapshot: Path, tmp_path: Path) -> None:
    """Scope churn measures Python hunks including blank additions and removals."""
    (tmp_path / "oss-review-changed-files-safety").write_text(
        "package/a.py\nREADME.md\n", encoding="utf-8", newline="\n"
    )
    (snapshot / "pr.diff").write_text(
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/package/a.py b/package/a.py\n--- a/package/a.py\n+++ b/package/a.py\n@@ -1 +1,2 @@\n-x\n+y\n+\n",
        encoding="utf-8",
        newline="\n",
    )
    result = _run(tmp_path, "python() { echo FIX; }\n" + _block("SKILL.md", "PY_LOC_DELTA="))
    assert "py_loc=3," in result.stdout


@_skip_no_bash
def test_flat_package_init_is_api_surface(snapshot: Path, tmp_path: Path) -> None:
    """Flat package exports count as API additions while test package initializers do not."""
    (snapshot / "pr.diff").write_text(
        "diff --git a/pkg/__init__.py b/pkg/__init__.py\n+++ b/pkg/__init__.py\n@@ -0,0 +1 @@\n+from .a import A\n"
        "diff --git a/tests/__init__.py b/tests/__init__.py\n+++ b/tests/__init__.py\n@@ -0,0 +1 @@\n+from .b import B\n",
        encoding="utf-8",
        newline="\n",
    )
    result = _run(tmp_path, "python() { echo FEATURE; }\n" + _block("SKILL.md", "PY_LOC_DELTA="))
    assert "new_api=1)" in result.stdout


@_skip_no_bash
def test_report_allocation_failure_stops_before_publishing(snapshot: Path, tmp_path: Path) -> None:
    """A failed run allocator cannot publish a blank report-dir sentinel or continue loading prompts."""
    result = _run(tmp_path, "python() { return 1; }\n" + _block("SKILL.md", "next_run_dir.py"))
    assert result.returncode == 1
    assert "! BLOCKED" in result.stdout
    assert not (tmp_path / "oss-review-report-dir-safety").exists()


@_skip_no_bash
def test_reply_gate_reads_frozen_output_path(tmp_path: Path) -> None:
    """A reply written across midnight is checked at the path persisted when it was spawned."""
    reply = tmp_path / "reply with spaces.md"
    reply.write_text("| Importance | Confidence | File | Line | Comment |\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-reply-out-safety").write_text(bash_path(reply) + "\n", encoding="utf-8", newline="\n")
    result = _run(tmp_path, _block("modes/reply.md", "PART2_PRESENT="))
    assert result.returncode == 0, result.stderr
    assert "PART2_PRESENT=true" in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    ("body", "nodes", "expected"),
    [
        pytest.param("Closes #40", [{"number": 8, "state": "OPEN"}], "false", id="open-competitor"),
        pytest.param("Closes #40", [{"number": 8, "state": "CLOSED"}], "false", id="unmerged-competitor"),
        pytest.param("Closes #40", [{"number": 7, "state": "MERGED"}], "false", id="self"),
        pytest.param(
            "Closes #40",
            [{"number": 8, "state": "OPEN"}, {"number": 9, "state": "MERGED"}],
            "true",
            id="later-merged-closer",
        ),
        pytest.param("refs #40", [{"number": 9, "state": "MERGED"}], "false", id="reference-is-not-solution"),
    ],
)
def test_duplicate_gate_requires_a_merged_other_solution(
    snapshot: Path, tmp_path: Path, body: str, nodes: list[dict], expected: str
) -> None:
    """Only a solving link to a closed issue with another merged closer rejects a PR."""
    meta = json.loads((snapshot / "pr-meta.json").read_text())
    meta["body"] = body
    meta["url"] = "https://github.com/owner/project/pull/7"
    (snapshot / "pr-meta.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")
    stub = """
gh() {
  case "$*" in
    *'api graphql'*) printf '%s' "$CLOSERS" ;;
    *'--json state'*) echo CLOSED ;;
    *closedByPullRequestsReferences*) echo 8 ;;
    *) return 93 ;;
  esac
}
git() { return 0; }
"""
    result = _run(
        tmp_path,
        stub + _block("modes/acceptance-gate.md", "DUPLICATE_HIT=false"),
        CLOSERS=json.dumps(
            {"data": {"repository": {"issue": {"state": "CLOSED", "closedByPullRequestsReferences": {"nodes": nodes}}}}}
        ),
    )
    assert result.returncode == 0, result.stderr
    assert f"duplicate={expected}" in result.stdout


@_skip_no_bash
def test_revert_scan_preserves_spaces_and_globs(snapshot: Path, tmp_path: Path) -> None:
    """Changed paths reach git log as literal arguments rather than expanded shell words."""
    (tmp_path / "oss-review-changed-files-safety").write_text(
        "pkg/a b.py\npkg/[draft].py\n", encoding="utf-8", newline="\n"
    )
    (snapshot / "files.txt").write_text("pkg/a b.py\npkg/[draft].py\n", encoding="utf-8", newline="\n")
    script = 'gh() { echo OPEN; }\ngit() { printf "%s\\n" "$@" > "$TMPDIR/git-args"; }\n'
    result = _run(tmp_path, script + _block("modes/acceptance-gate.md", "DUPLICATE_HIT=false"))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "git-args").read_text().splitlines()[-2:] == ["pkg/a b.py", "pkg/[draft].py"]


@_skip_no_bash
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param("Please APPROVE this plan\n", "false", id="ordinary-mention"),
        pytest.param("Outcome: APPROVE\n", "true", id="outcome-header"),
        pytest.param("Gate: REVIEW_V1_BLOCK\n", "true", id="gate-header"),
    ],
)
def test_direct_report_requires_header(tmp_path: Path, content: str, expected: str) -> None:
    """An approval word in ordinary prose is insufficient to select direct-report mode."""
    report = tmp_path / "report.md"
    report.write_text(content, encoding="utf-8", newline="\n")
    text = _block("SKILL.md", "DIRECT_PATH_MODE=false\nif [[")
    script = text[text.index("DIRECT_PATH_MODE=false\nif [[") :]
    result = _run(tmp_path, script + '\nprintf "%s" "$DIRECT_PATH_MODE"', CLEAN_ARGS=bash_path(report))
    assert result.stdout.endswith(expected), result.stderr


@_skip_no_bash
def test_blind_solve_reuses_frozen_snapshot(snapshot: Path, tmp_path: Path) -> None:
    """Gathering the blind prompt reuses metadata and names without further PR reads."""
    (tmp_path / "oss-review-run-dir-safety").write_text(bash_path(tmp_path) + "\n", encoding="utf-8", newline="\n")
    meta = json.loads((snapshot / "pr-meta.json").read_text())
    meta.update(title="Add feature", body="Feature description")
    (snapshot / "pr-meta.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")
    result = _run(
        tmp_path, "gh() { echo unexpected-pr-read >&2; return 1; }\n" + _block("modes/blind-solve.md", "BASE_REF=")
    )
    content = (tmp_path / "blind-solve-input.md").read_text()
    assert "unexpected-pr-read" not in result.stderr
    assert "Add feature\n\nFeature description" in content
    assert "package/a.py" in content
    assert "BASE_REF=main" in content


@_skip_no_bash
@pytest.mark.parametrize(
    ("bucket", "exit_code"),
    [pytest.param("pending", "8", id="pending-status"), pytest.param("fail", "1", id="failed-status")],
)
def test_snapshot_accepts_status_exit_with_valid_checks(
    snapshot: Path, tmp_path: Path, bucket: str, exit_code: str
) -> None:
    """A valid checks response is retained when the CLI exits with the reported check status."""
    stub = """
gh() {
  case "$*" in
    *'pr view'*) cat "$META_FILE" ;;
    *'pr diff'*--name-only*) echo package/a.py ;;
    *'pr diff'*) echo 'diff --git a/package/a.py b/package/a.py' ;;
    *'pr checks'*) printf '%s' "$CHECKS"; return "$CHECK_EXIT" ;;
    *) return 91 ;;
  esac
}
git() { printf '%s\\n' "$HEAD_SHA"; }
"""
    result = _run(
        tmp_path,
        stub + _block("SKILL.md", 'SNAP_DIR="${TMPDIR:-/tmp}/oss-review-snap-'),
        META_FILE=bash_path(snapshot / "pr-meta.json"),
        CHECKS=json.dumps([{"name": "test", "bucket": bucket}]),
        CHECK_EXIT=exit_code,
        HEAD_SHA=_SHA,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "oss-review-snap-7-safety/checks.json").read_text())[0]["bucket"] == bucket


@_skip_no_bash
def test_revert_history_failure_is_visible(snapshot: Path, tmp_path: Path) -> None:
    """An unreadable history cannot masquerade as no reverted change."""
    script = "gh() { return 1; }\ngit() { echo history-unavailable >&2; return 1; }\n"
    result = _run(tmp_path, script + _block("modes/acceptance-gate.md", "DUPLICATE_HIT=false"))
    assert result.returncode == 1
    assert "revert history read failed" in result.stdout
    assert "history-unavailable" in result.stderr


@_skip_no_bash
def test_usage_search_failure_is_unknown(snapshot: Path, tmp_path: Path) -> None:
    """A failed search of a flat package export reports unknown usage rather than no usage."""
    (snapshot / "pr.diff").write_text(
        "diff --git a/pkg/__init__.py b/pkg/__init__.py\n@@ -0,0 +1 @@\n+from .a import PublicName\n",
        encoding="utf-8",
        newline="\n",
    )
    result = _run(tmp_path, "gh() { echo HTTP-429 >&2; return 1; }\n" + _block("SKILL.md", "CHANGED_EXPORTS="))
    assert "Usage search failed for PublicName — downstream usage unknown" in result.stdout
    assert "HTTP-429" in result.stderr


@_skip_no_bash
def test_reply_spawn_recovers_report_path_from_flags(tmp_path: Path) -> None:
    """Direct-report reply mode computes its path from persisted inputs in a fresh shell."""
    report = tmp_path / "report.md"
    report.write_text("PR: #7\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-pr-tag-safety").write_text(bash_path(report) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-flags-safety").write_text(
        f"REVIEW_FILE={bash_path(report)}\n", encoding="utf-8", newline="\n"
    )
    script = 'python() { [ -f "$2" ] || return 1; echo 7; }\n'
    result = _run(tmp_path, script + _block("modes/reply.md", "REPLY_OUT="))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "/output-reply-7-" in (tmp_path / "oss-review-reply-out-safety").read_text()


@_skip_no_bash
def test_malformed_full_checks_cannot_hide_behind_valid_required(snapshot: Path, tmp_path: Path) -> None:
    """A valid required response cannot override an invalid full response's validation result."""
    stub = """
gh() {
  case "$*" in
    *'pr view'*) cat "$META_FILE" ;;
    *'pr diff'*--name-only*) echo package/a.py ;;
    *'pr diff'*) echo 'diff --git a/package/a.py b/package/a.py' ;;
    *'pr checks'*--required*) echo '[]' ;;
    *'pr checks'*) echo '{}' ;;
    *) return 91 ;;
  esac
}
git() { printf '%s\\n' "$HEAD_SHA"; }
"""
    result = _run(
        tmp_path,
        stub + _block("SKILL.md", 'SNAP_DIR="${TMPDIR:-/tmp}/oss-review-snap-'),
        META_FILE=bash_path(snapshot / "pr-meta.json"),
        HEAD_SHA=_SHA,
    )
    assert result.returncode == 1
    assert "invalid CI checks snapshot" in result.stdout


@_skip_no_bash
def test_run_directory_creation_failure_does_not_publish_sentinel(snapshot: Path, tmp_path: Path) -> None:
    """An unwritable run location blocks before exposing a nonexistent handoff directory."""
    result = _run(tmp_path, "mkdir() { return 1; }\npython() { return 1; }\n" + _block("SKILL.md", "next_run_dir.py"))
    assert result.returncode == 1
    assert not (tmp_path / "oss-review-run-dir-safety").exists()


@_skip_no_bash
def test_head_change_during_diff_collection_blocks(snapshot: Path, tmp_path: Path) -> None:
    """The post-diff remote-head check rejects content collected across a contributor push."""
    stub = """
gh() {
  case "$*" in
    *'pr view'*) cat "$META_FILE" ;;
    *'pr diff'*--name-only*) echo new-head-only.py ;;
    *'pr diff'*) echo 'diff --git a/new-head-only.py b/new-head-only.py' ;;
    *'pr checks'*) echo '[]' ;;
    *) return 91 ;;
  esac
}
git() { case "$1" in ls-remote) printf '%040d\\trefs/pull/7/head\\n' 1 ;; *) echo "$HEAD_SHA" ;; esac; }
"""
    result = _run(
        tmp_path,
        stub + _block("SKILL.md", 'SNAP_DIR="${TMPDIR:-/tmp}/oss-review-snap-'),
        META_FILE=bash_path(snapshot / "pr-meta.json"),
        HEAD_SHA=_SHA,
    )
    assert result.returncode == 1
    assert "head moved while collecting diff and files" in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    ("tree_sha", "dirty", "strict", "expected"),
    [
        pytest.param("b" * 40, "", "false", "force-off", id="base-checkout"),
        pytest.param(_SHA, " M package/a.py", "false", "force-off", id="dirty-head"),
        pytest.param(_SHA, "", "false", "arguments", id="clean-head"),
        pytest.param("b" * 40, "", "true", "BLOCKED", id="strict-base-checkout"),
    ],
)
def test_codemap_requires_clean_frozen_head(
    snapshot: Path, tmp_path: Path, tree_sha: str, dirty: str, strict: str, expected: str
) -> None:
    """A local structural index cannot be attributed to a different or modified PR checkout."""
    (tmp_path / "oss-review-codemap-safety").write_text(strict + "\n", encoding="utf-8", newline="\n")
    stub = """
git() { case "$*" in 'rev-parse HEAD') echo "$TREE_SHA" ;; 'status --porcelain') printf '%s' "$DIRTY" ;; *) return 92 ;; esac; }
python() { printf '%s\\n' "$@"; }
"""
    block = _block("SKILL.md", "_DETECT_CODEMAP=").split("IFS= read -r CODEMAP_ENABLED", 1)[0]
    result = _run(tmp_path, stub + block, TREE_SHA=tree_sha, DIRTY=dirty)
    assert expected in result.stdout, result.stderr


@_skip_no_bash
def test_worktree_is_detached_at_snapshot_head(snapshot: Path, tmp_path: Path) -> None:
    """The optional worktree starts at the PR object rather than the invoking HEAD."""
    (tmp_path / "oss-review-flags-safety").write_text(
        "DIRECT_PATH_MODE=false\nWT_ENABLED=true\n", encoding="utf-8", newline="\n"
    )
    stub = """
git() {
  printf '%s\\n' "$*" >> "$TMPDIR/git-calls"
  case "$*" in 'rev-parse --show-toplevel') pwd ;; 'worktree add --detach '*) return 0 ;; *) return 92 ;; esac
}
"""
    result = _run(tmp_path, stub + _block("SKILL.md", "git worktree add --detach"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "git-calls").read_text().splitlines()[1].endswith(" " + _SHA)
    worktree = Path((tmp_path / "oss-review-wt-safety").read_text().strip())
    assert worktree.parent == tmp_path / ".claude/worktrees"
    assert worktree.name.startswith(f"oss-review-7-{_SHA[:8]}-")


def _git(repository: Path, *args: str) -> str:
    """Run local fixture Git without inheriting user configuration or reaching a remote service."""
    result = subprocess.run(
        ["git", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always", *args],
        cwd=repository,
        env=os.environ | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def local_pr(snapshot: Path, tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Create separate base and fork remotes with a real PR ref and a base checkout."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    _git(seed, "config", "user.name", "Fixture")
    _git(seed, "config", "user.email", "fixture@example.invalid")
    (seed / "module.py").write_text("value = 'base'\n", encoding="utf-8", newline="\n")
    _git(seed, "add", "module.py")
    _git(seed, "commit", "-m", "Base")
    base = _git(seed, "rev-parse", "HEAD")
    (seed / "module.py").write_text("value = 'reviewed'\n", encoding="utf-8", newline="\n")
    _git(seed, "commit", "-am", "Reviewed change")
    head = _git(seed, "rev-parse", "HEAD")
    remote = tmp_path / "remotes/owner/project.git"
    remote.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "--bare", str(seed), str(remote))
    _git(remote, "update-ref", "refs/pull/7/head", head)
    _git(remote, "update-ref", "refs/heads/main", base)
    fork = tmp_path / "fork.git"
    _git(tmp_path, "init", "--bare", str(fork))
    checkout = tmp_path / "work dir"
    _git(tmp_path, "clone", str(remote), str(checkout))
    _git(checkout, "remote", "set-url", "origin", str(fork))
    _git(checkout, "remote", "add", "upstream", str(remote))
    _git(checkout, "config", "protocol.allow", "never")
    _git(checkout, "config", "protocol.file.allow", "always")
    meta = json.loads((snapshot / "pr-meta.json").read_text())
    meta.update(headRefOid=head, baseRefOid=base)
    (snapshot / "pr-meta.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")
    return checkout, remote, base, head


@_skip_no_bash
@pytest.mark.parametrize(
    ("layout", "diff_mode"),
    [
        pytest.param("fork", "normal", id="fork-origin"),
        pytest.param("base", "406", id="406-fallback"),
        pytest.param("url", "406", id="repository-url-fallback"),
    ],
)
def test_snapshot_reads_the_pr_repository(
    local_pr: tuple[Path, Path, str, str], snapshot: Path, tmp_path: Path, layout: str, diff_mode: str
) -> None:
    """Fetch and recheck the base repository even when origin is a fork or no named remote matches."""
    checkout, remote, base, head = local_pr
    setup = {
        "fork": ":",
        "base": 'git remote set-url origin "$PR_REMOTE"; git remote remove upstream',
        "url": 'git remote remove upstream; git config "url.$PR_REMOTE.insteadOf" https://github.com/owner/project.git',
    }[layout]
    stub = """
cd "$CHECKOUT" || exit 1
git() { printf '%s\\n' "$*" >> "$TMPDIR/git-calls"; command git "$@"; }
gh() {
  case "$*" in
    'pr view '*) cat "$META_FILE" ;;
    'pr diff '*--name-only*) echo module.py ;;
    'pr diff '*) [ "$DIFF_MODE" != 406 ] || { echo HTTP-406 >&2; return 1; }; git diff "$BASE_SHA...$HEAD_SHA" ;;
    'pr checks '*) echo '[]' ;;
    *) return 91 ;;
  esac
}
"""
    result = _run(
        tmp_path,
        stub + setup + "\n" + _block("SKILL.md", 'SNAP_DIR="${TMPDIR:-/tmp}/oss-review-snap-'),
        CHECKOUT=bash_path(checkout),
        META_FILE=bash_path(snapshot / "pr-meta.json"),
        PR_REMOTE=bash_path(remote),
        BASE_SHA=base,
        HEAD_SHA=head,
        DIFF_MODE=diff_mode,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = tmp_path / "oss-review-snap-7-safety"
    assert (collected / "files.txt").read_text().splitlines() == ["module.py"]
    assert (collected / "pr.diff").read_text() == _git(checkout, "diff", f"{base}...{head}") + "\n"
    source = {"fork": "upstream", "base": "origin", "url": "https://github.com/owner/project.git"}[layout]
    calls = (tmp_path / "git-calls").read_text().splitlines()
    assert f"fetch --no-tags {source} refs/pull/7/head" in calls
    assert f"ls-remote {source} refs/pull/7/head" in calls
    assert (f"fetch --no-tags {source} {base}" in calls) == (diff_mode == "406")
    assert ("PR diff via local git (gh diff unavailable)" in result.stdout) == (diff_mode == "406")


@_skip_no_bash
@pytest.mark.parametrize(
    ("direct", "expected"),
    [pytest.param("true", 0, id="direct-report"), pytest.param("false", 1, id="pr-missing-snapshot")],
)
def test_direct_report_skips_ci_snapshot_reads(tmp_path: Path, direct: str, expected: int) -> None:
    """A snapshot is mandatory in PR mode and is never read when replying from a report."""
    (tmp_path / "oss-review-flags-safety").write_text(f"DIRECT_PATH_MODE={direct}\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-pr-tag-safety").write_text("7\n", encoding="utf-8", newline="\n")
    result = _run(
        tmp_path, "jq() { echo unexpected-ci-read >&2; return 99; }\n" + _block("SKILL.md", "CI_FAILING_CHECKS=")
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert ("! BLOCKED" in result.stdout) == (expected != 0)
    assert "unexpected-ci-read" not in result.stderr
    assert not (tmp_path / "oss-review-ci-red-safety").exists()


@_skip_no_bash
def test_flag_parse_clears_prior_snapshot(tmp_path: Path) -> None:
    """Starting a report reply removes a crashed PR run's snapshot pointer."""
    report = tmp_path / "report.md"
    report.write_text("Outcome: APPROVE\n", encoding="utf-8", newline="\n")
    sentinel = tmp_path / "oss-review-snap-dir-safety"
    sentinel.write_text("old-pr\n", encoding="utf-8", newline="\n")
    result = _run(
        tmp_path,
        _block("SKILL.md", "DIRECT_PATH_MODE=false\nif [["),
        ARGUMENTS=f"{bash_path(report)} --reply",
        CLAUDE_PLUGIN_ROOT=bash_path(_REVIEW.parents[1]),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not sentinel.exists()


@_skip_no_bash
@pytest.mark.parametrize(
    ("shell", "body", "repo"),
    [
        pytest.param(_BASH, "Closes #40, fixes #41", "project", id="bash-two-issues"),
        pytest.param(
            _ZSH,
            "Closes #40, fixes #41",
            "project",
            id="zsh-two-issues",
            marks=pytest.mark.skipif(_ZSH is None, reason="zsh is not installed"),
        ),
        pytest.param(_BASH, "Closes #41", "2048", id="numeric-repository"),
        pytest.param(_BASH, "Closes #41", "true", id="boolean-repository"),
    ],
)
def test_duplicate_query_preserves_issue_numbers_and_string_names(
    snapshot: Path, tmp_path: Path, shell: str, body: str, repo: str
) -> None:
    """Each solving issue is queried separately with string repository coordinates and an integer issue."""
    meta = json.loads((snapshot / "pr-meta.json").read_text())
    owner = {"project": "owner", "2048": "2048", "true": "2048"}[repo]
    meta.update(body=body, url=f"https://github.com/{owner}/{repo}/pull/7")
    (snapshot / "pr-meta.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")
    stub = """
gh() {
  _ISSUE=""
  while [ "$#" -gt 0 ]; do
    case "$1:$2" in
      -F:owner=2048|-F:repo=2048|-F:repo=true) echo typed-repository-name >&2; return 91 ;;
      -F:number=*) _ISSUE="${2#number=}" ;;
    esac
    shift
  done
  case "$_ISSUE" in
    40) printf '%s' '{"data":{"repository":{"issue":{"state":"OPEN","closedByPullRequestsReferences":{"nodes":[]}}}}}' ;;
    41) printf '%s' '{"data":{"repository":{"issue":{"state":"CLOSED","closedByPullRequestsReferences":{"nodes":[{"number":9,"state":"MERGED"}]}}}}}' ;;
    *) echo invalid-issue-number >&2; return 92 ;;
  esac
}
git() { return 0; }
"""
    result = _run(tmp_path, stub + _block("modes/acceptance-gate.md", "DUPLICATE_HIT=false"), shell=shell)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "duplicate=true" in result.stdout
    assert "Duplicate check unavailable" not in result.stdout
    assert result.stderr == ""


@_skip_no_bash
@pytest.mark.parametrize(
    ("available", "fetch_exit", "expected"),
    [
        pytest.param(True, "0", 0, id="base-present"),
        pytest.param(False, "0", 0, id="base-fetched"),
        pytest.param(False, "1", 1, id="base-unavailable"),
    ],
)
def test_blind_solve_fetches_base_only_when_missing(
    snapshot: Path, tmp_path: Path, available: bool, fetch_exit: str, expected: int
) -> None:
    """The executable blind preflight fetches an absent base from the frozen PR source."""
    (snapshot / "repo-source.txt").write_text("upstream\n", encoding="utf-8", newline="\n")
    stub = """
git() {
  printf '%s\\n' "$*" >> "$TMPDIR/base-calls"
  case "$1" in cat-file) [ "$AVAILABLE" = true ] ;; fetch) [ "$3" = upstream ] && return "$FETCH_EXIT" ;; *) return 92 ;; esac
}
"""
    result = _run(
        tmp_path,
        stub + _block("modes/blind-solve.md", "git cat-file -e"),
        AVAILABLE=str(available).lower(),
        FETCH_EXIT=fetch_exit,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert ("base commit unavailable" in result.stdout) == (expected != 0)
    calls = (tmp_path / "base-calls").read_text().splitlines()
    assert (
        calls
        == [f"cat-file -e {'b' * 40}^{{commit}}"]
        + {True: [], False: [f"fetch --no-tags upstream {'b' * 40}"]}[available]
    )


@_skip_no_bash
def test_codemap_forced_off_is_a_successful_skip(snapshot: Path, tmp_path: Path) -> None:
    """An invoking base checkout disables codemap without making the shell tool fail."""
    (tmp_path / "review-codemap-forced-off-safety").write_text("true\n", encoding="utf-8", newline="\n")
    stub = "git() { echo base-checkout; }\npython() { return 0; }\n"
    result = _run(tmp_path, stub + _block("SKILL.md", "_DETECT_CODEMAP="))
    assert result.returncode == 0, result.stdout + result.stderr


@_skip_no_bash
@pytest.mark.parametrize(
    "shell",
    [
        pytest.param(_BASH, id="bash"),
        pytest.param(_ZSH, id="zsh", marks=pytest.mark.skipif(_ZSH is None, reason="zsh is not installed")),
    ],
)
def test_real_worktree_is_clean_detached_and_discoverable(
    local_pr: tuple[Path, Path, str, str], tmp_path: Path, shell: str
) -> None:
    """The shipped recipe creates an actual detached PR checkout under the healer's default root."""
    checkout, _, base, head = local_pr
    (tmp_path / "oss-review-flags-safety").write_text(
        "DIRECT_PATH_MODE=false\nWT_ENABLED=true\n", encoding="utf-8", newline="\n"
    )
    result = _run(
        tmp_path,
        'cd "$CHECKOUT" || exit 1\n' + _block("SKILL.md", "git worktree add --detach"),
        shell=shell,
        CHECKOUT=bash_path(checkout),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    worktree = Path((tmp_path / "oss-review-wt-safety").read_text().strip())
    assert worktree in list((checkout / ".claude/worktrees").glob("oss-*/"))
    assert _git(worktree, "rev-parse", "HEAD") == head
    assert _git(worktree, "status", "--porcelain") == ""
    assert f"HEAD {head}\ndetached\n" in _git(checkout, "worktree", "list", "--porcelain") + "\n"
    assert _git(checkout, "rev-parse", "HEAD") == base
