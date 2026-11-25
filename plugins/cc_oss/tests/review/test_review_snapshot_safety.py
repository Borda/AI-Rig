"""Exercise review recipes against failed snapshots and mismatched source identities."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from _oss_shell_plugin import render_shell
from _skill_source import read_skill_source
from _text_io import bash_path

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"
_PLUGIN = _REVIEW.parents[1]
_BASH = shutil.which("bash")
_ZSH = shutil.which("zsh")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="Review recipes require Bash")
_SHA = "a" * 40


def _block(relative: str, marker: str) -> str:
    """Extract a fenced recipe by a unique line marker."""
    blocks: list[str] = []
    active: list[str] | None = None
    for line in read_skill_source(_REVIEW / relative).splitlines():
        if line == "```bash":
            active = []
        elif line == "```" and active is not None:
            blocks.append("\n".join(active))
            active = None
        elif active is not None:
            active.append(line)
    return next(block for block in blocks if marker in block)


def _run(tmp_path: Path, script: str, shell: str | None = _BASH, **values: str) -> subprocess.CompletedProcess[str]:
    """Execute a shipped recipe with isolated sentinel storage and external command doubles.

    The recipe runs from a script file kept outside ``tmp_path``: Git Bash on Windows cuts a ``-c`` argument near 8191
    characters, which truncates the longest recipes mid-statement.
    """
    with tempfile.TemporaryDirectory() as scripts:
        file = Path(scripts) / "recipe.sh"
        file.write_text(render_shell(script, _PLUGIN, "oss"), encoding="utf-8", newline="\n")
        return subprocess.run(
            [shell, bash_path(file)],
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
@pytest.mark.parametrize("failure", ["diff", "names", "head", "empty-diff", "empty-names", "moved-head"])
def test_snapshot_read_failure_blocks(snapshot: Path, tmp_path: Path, failure: str) -> None:
    """A failed external read cannot become an empty successful snapshot."""
    stub = """
gh() {
  case "$*" in
    *'pr view'*) printf '%s' "$META" ;;
    *'pr diff'*--name-only*) [ "$FAILURE" != names ] || return 1; [ "$FAILURE" != empty-names ] || return 0; echo package/a.py ;;
    *'pr diff'*) [ "$FAILURE" != diff ] || return 1; [ "$FAILURE" != empty-diff ] || return 0; echo 'diff --git a/package/a.py b/package/a.py' ;;
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
            [{"number": 7, "state": "MERGED", "url": "https://github.com/owner/upstream/pull/7"}],
            "true",
            id="same-number-other-repository",
        ),
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
    """Only another merged closer supplies linked comparison evidence for a solving issue."""
    meta = json.loads((snapshot / "pr-meta.json").read_text())
    meta["body"] = body
    meta["url"] = "https://github.com/owner/project/pull/7"
    (snapshot / "pr-meta.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")
    issue_url = "https://github.com/owner/project/issues/40"
    closers = [{"url": f"https://github.com/owner/project/pull/{node['number']}", **node} for node in nodes]
    issue = {"url": issue_url, "state": "CLOSED", "closedByPullRequestsReferences": {"nodes": closers}}
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
        CLOSERS=json.dumps({"data": {"repository": {"issue": issue}}}),
    )
    assert result.returncode == 0, result.stderr
    assert f"duplicate={expected}" in result.stdout
    assert (issue_url in result.stdout) == (expected == "true")
    assert (closers[-1]["url"] in result.stdout) == (expected == "true")


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
    *'--jq .headRefOid'*) echo "$HEAD_SHA" ;;
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
        stub
        + _block("SKILL.md", 'SNAP_DIR="${TMPDIR:-/tmp}/oss-review-snap-')
        + "\n"
        + _block("modes/ci-status.md", "for _CHECK_SET"),
        META_FILE=bash_path(snapshot / "pr-meta.json"),
        CHECKS=json.dumps([{"name": "test", "bucket": bucket}]),
        CHECK_EXIT=exit_code,
        HEAD_SHA=_SHA,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "oss-review-snap-7-safety/checks.json").read_text())[0]["bucket"] == bucket


_CI_STUB = """
gh() {
  case "$*" in
    'pr view https://github.com/owner/project/pull/7 --json headRefOid'*)
      if [ -f "$TMPDIR/read-head" ]; then echo "$LAST_HEAD"; else echo "$FIRST_HEAD"; touch "$TMPDIR/read-head"; fi ;;
    'pr checks https://github.com/owner/project/pull/7 --required'*) echo "$REQUIRED" ;;
    'pr checks https://github.com/owner/project/pull/7 --json'*) echo "$CHECKS"; return 8 ;;
    *) return 90 ;;
  esac
}
"""
_HELD_FAILURE = "gate held from earlier same-head observation: failing — unit"
#: Step 0 outcomes: the checks GitHub reports and the head read after collecting them (a moved head is unavailable).
_INITIAL_CI = {
    "failing": ([{"name": "unit", "bucket": "fail"}], _SHA),
    "passing": ([{"name": "unit", "bucket": "pass"}], _SHA),
    "unavailable": ([{"name": "unit", "bucket": "fail"}], "b" * 40),
}


def _observe_ci(
    tmp_path: Path, prefix: str, checks: object, required: object, last_head: str
) -> subprocess.CompletedProcess[str]:
    """Run the shipped CI observation after ``prefix`` against a GitHub double reporting the given checks."""
    (tmp_path / "read-head").unlink(missing_ok=True)
    return _run(
        tmp_path,
        _CI_STUB + prefix + "\n" + _block("modes/ci-status.md", "for _CHECK_SET in checks checks-required"),
        FIRST_HEAD=_SHA,
        LAST_HEAD=last_head,
        CHECKS=json.dumps(checks),
        REQUIRED=json.dumps(required),
    )


def _grant_initial_ci(tmp_path: Path) -> None:
    """Leave the initial-observation grant unconsumed, as the preflight snapshot block writes it."""
    (tmp_path / "oss-review-ci-initial-safety").write_text("granted\n", encoding="utf-8", newline="\n")


def _point_ci_at_snapshot(snapshot: Path, tmp_path: Path) -> None:
    """Point the CI observation into the snapshot directory and grant it, as the preflight snapshot block does."""
    (tmp_path / "oss-review-ci-dir-safety").write_text(bash_path(snapshot) + "\n", encoding="utf-8", newline="\n")
    _grant_initial_ci(tmp_path)


def _observe_initial_ci(snapshot: Path, tmp_path: Path, outcome: str) -> subprocess.CompletedProcess[str]:
    """Run the Step 0 observation into the snapshot directory, as the preflight snapshot block points it."""
    _point_ci_at_snapshot(snapshot, tmp_path)
    checks, last_head = _INITIAL_CI[outcome]
    result = _observe_ci(tmp_path, "", checks, [], last_head)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@_skip_no_bash
@pytest.mark.parametrize(
    ("checks", "required", "last_head", "status", "gate"),
    [
        pytest.param([{"name": "unit", "bucket": "pass"}], [], _SHA, "passing (1/1)", "PASS", id="rerun-green"),
        pytest.param([{"name": "unit", "bucket": "pending"}], [], _SHA, "pending", "PASS", id="rerun-running"),
        pytest.param([{"name": "unit", "bucket": "fail"}], [], _SHA, "failing", "BLOCK", id="still-red"),
        pytest.param([{"name": "unit", "bucket": "pass"}], [], "b" * 40, "unavailable", "BLOCK", id="head-changed"),
        pytest.param([], [], _SHA, "no checks reported", "PASS", id="no-checks"),
        pytest.param({}, [], _SHA, "unavailable", "BLOCK", id="malformed-full-valid-required"),
        pytest.param([], {}, _SHA, "unavailable", "BLOCK", id="malformed-required-valid-full"),
        pytest.param(
            [{"name": "optional", "bucket": "fail"}, {"name": "required", "bucket": "pass"}],
            [{"name": "required", "bucket": "pass"}],
            _SHA,
            "failing",
            "PASS",
            id="only-optional-failure",
        ),
        pytest.param(
            [{"name": "unit", "bucket": "fail"}],
            [{"name": "unit", "bucket": "pass"}],
            _SHA,
            "unavailable",
            "BLOCK",
            id="rerun-during-refresh",
        ),
    ],
)
def test_consolidation_refreshes_ci(
    snapshot: Path, tmp_path: Path, checks: object, required: object, last_head: str, status: str, gate: str
) -> None:
    """A readable rerun supersedes the initial failure, while an unreadable refresh keeps it blocking."""
    _observe_initial_ci(snapshot, tmp_path, "failing")
    result = _observe_ci(tmp_path, _block("modes/post-agent.md", "BRANCH=$(git branch"), checks, required, last_head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"CI_STATUS={status}" in result.stdout
    assert f"CI_GATE={gate}" in result.stdout
    assert (_HELD_FAILURE in result.stdout) == (status == "unavailable")
    assert json.loads((snapshot / "checks.json").read_text()) == [{"name": "unit", "bucket": "fail"}]


@_skip_no_bash
@pytest.mark.parametrize(
    ("initial", "refresh", "gate"),
    [
        pytest.param("failing", "head-changed", "BLOCK", id="failure-then-moved-head"),
        pytest.param("failing", "snapshot-lost", "BLOCK", id="failure-then-lost-snapshot"),
        pytest.param("passing", "head-changed", "PASS", id="green-then-moved-head"),
        pytest.param("unavailable", "head-changed", "PASS", id="unavailable-then-moved-head"),
        pytest.param("unavailable", "snapshot-lost", "PASS", id="unavailable-then-lost-snapshot"),
    ],
)
def test_unavailable_refresh_keeps_only_an_established_failure(
    snapshot: Path, tmp_path: Path, initial: str, refresh: str, gate: str
) -> None:
    """An unreadable final observation blocks only when the reviewed head already failed earlier in the run."""
    _observe_initial_ci(snapshot, tmp_path, initial)
    if refresh == "snapshot-lost":
        (tmp_path / "oss-review-snap-dir-safety").unlink()
        prefix, reason = "", "unavailable (check snapshot unavailable)"
    else:
        prefix, reason = _block("modes/post-agent.md", "BRANCH=$(git branch"), "unavailable (PR head changed)"
    result = _observe_ci(tmp_path, prefix, [{"name": "unit", "bucket": "pass"}], [], "b" * 40)
    assert result.returncode == 0, result.stdout + result.stderr
    held = f" · {_HELD_FAILURE}" if gate == "BLOCK" else ""
    assert f"CI_STATUS={reason}{held}\nCI_GATE={gate}\n" in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    ("head", "expected"),
    [
        pytest.param("a" * 40, "CI_STATUS=failing — unit\nCI_GATE=BLOCK\n", id="sha1-head"),
        pytest.param("c" * 64, "CI_STATUS=failing — unit\nCI_GATE=BLOCK\n", id="sha256-head"),
        pytest.param(
            "a" * 12, "CI_STATUS=unavailable (check snapshot unavailable)\nCI_GATE=PASS\n", id="abbreviated-head"
        ),
    ],
)
def test_ci_observation_accepts_the_full_head_ids_resolve_accepts(
    snapshot: Path, tmp_path: Path, head: str, expected: str
) -> None:
    """The CI observation binds a full SHA-1 or SHA-256 head, the ids resolve's push check takes, never a prefix."""
    meta = json.loads((snapshot / "pr-meta.json").read_text(encoding="utf-8"))
    (snapshot / "pr-meta.json").write_text(json.dumps(meta | {"headRefOid": head}), encoding="utf-8", newline="\n")
    _point_ci_at_snapshot(snapshot, tmp_path)
    result = _run(
        tmp_path,
        _CI_STUB + _block("modes/ci-status.md", "for _CHECK_SET in checks checks-required"),
        FIRST_HEAD=head,
        LAST_HEAD=head,
        CHECKS=json.dumps([{"name": "unit", "bucket": "fail"}]),
        REQUIRED="[]",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout


_MISSING_RECORD = "gate held: no complete gate record from the earlier observation"


@_skip_no_bash
@pytest.mark.parametrize("initial", ["failing", "passing"])
@pytest.mark.parametrize(
    "still_unwritable",
    [pytest.param(True, id="record-path-still-unwritable"), pytest.param(False, id="record-path-left-empty")],
)
def test_unwritten_gate_record_is_reported_and_blocks_an_unavailable_refresh(
    snapshot: Path, tmp_path: Path, initial: str, still_unwritable: bool
) -> None:
    """A gate record that cannot be written is named at once, and a later unreadable refresh never reports PASS."""
    record = tmp_path / "oss-review-ci-gate-safety"
    record.mkdir()  # a directory in its place makes the write fail without root-dependent permissions
    observed = _observe_initial_ci(snapshot, tmp_path, initial)
    gate = "BLOCK" if initial == "failing" else "PASS"
    assert f" · gate record not written\nCI_GATE={gate}\n" in observed.stdout
    if not still_unwritable:
        record.rmdir()
    result = _observe_ci(
        tmp_path,
        _block("modes/post-agent.md", "BRANCH=$(git branch"),
        [{"name": "unit", "bucket": "pass"}],
        [],
        "b" * 40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    unwritten = " · gate record not written" if still_unwritable else ""
    expected = f"CI_STATUS=unavailable (PR head changed) · {_MISSING_RECORD}{unwritten}\nCI_GATE=BLOCK\n"
    assert expected in result.stdout


@_skip_no_bash
def test_unavailable_observation_that_cannot_record_blocks_without_a_separate_destination(
    snapshot: Path, tmp_path: Path
) -> None:
    """An unreadable observation whose own record write fails blocks, even when it still points at the snapshot."""
    (tmp_path / "oss-review-ci-gate-safety").mkdir()
    _observe_initial_ci(snapshot, tmp_path, "failing")
    result = _observe_ci(tmp_path, "", [{"name": "unit", "bucket": "pass"}], [], "b" * 40)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = f"CI_STATUS=unavailable (PR head changed) · {_MISSING_RECORD} · gate record not written\nCI_GATE=BLOCK\n"
    assert expected in result.stdout


@_skip_no_bash
@pytest.mark.parametrize("initial", ["failing", "passing"])
@pytest.mark.parametrize(
    "route",
    [
        pytest.param("final-block-not-run", id="final-block-not-run"),
        pytest.param("destination-switch-lost", id="destination-switch-lost"),
        pytest.param("grant-left-unconsumed", id="grant-left-unconsumed-and-destination-switch-lost"),
    ],
)
def test_lost_gate_record_blocks_a_final_observation_left_on_the_snapshot(
    snapshot: Path, tmp_path: Path, initial: str, route: str
) -> None:
    """A lost gate record blocks an unreadable final observation even when it still writes into the snapshot.

    A failed Step 5 destination switch leaves the CI sentinel where Step 0 put it, so only the initial-observation
    grant, consumed by Step 0 and withdrawn again by the Step 5 block, tells the final observation from the first.
    """
    _observe_initial_ci(snapshot, tmp_path, initial)
    (tmp_path / "oss-review-ci-gate-safety").unlink()
    prefix = ""
    if route != "final-block-not-run":
        restore = f'printf "%s\\n" "{bash_path(snapshot)}" > "$TMPDIR/oss-review-ci-dir-safety"'
        prefix = _block("modes/post-agent.md", "BRANCH=$(git branch") + "\n" + restore
    if route == "grant-left-unconsumed":
        _grant_initial_ci(tmp_path)
    result = _observe_ci(tmp_path, prefix, [{"name": "unit", "bucket": "pass"}], [], "b" * 40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"CI_STATUS=unavailable (PR head changed) · {_MISSING_RECORD}\nCI_GATE=BLOCK\n" in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    "grant", [pytest.param(None, id="grant-missing"), pytest.param("consumed\n", id="grant-already-consumed")]
)
@pytest.mark.parametrize(
    ("last_head", "expected"),
    [
        pytest.param(
            "b" * 40, f"CI_STATUS=unavailable (PR head changed) · {_MISSING_RECORD}\nCI_GATE=BLOCK\n", id="unavailable"
        ),
        pytest.param(_SHA, "CI_STATUS=passing (1/1)\nCI_GATE=PASS\n", id="readable"),
    ],
)
def test_observation_without_an_unconsumed_grant_cannot_pass_unread(
    snapshot: Path, tmp_path: Path, grant: str | None, last_head: str, expected: str
) -> None:
    """Without preflight's unconsumed grant an unreadable observation blocks, while a readable one sets its own gate."""
    (tmp_path / "oss-review-ci-dir-safety").write_text(bash_path(snapshot) + "\n", encoding="utf-8", newline="\n")
    if grant is not None:
        (tmp_path / "oss-review-ci-initial-safety").write_text(grant, encoding="utf-8", newline="\n")
    result = _observe_ci(tmp_path, "", [{"name": "unit", "bucket": "pass"}], [], last_head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout


_SNAPSHOT_CI_STUB = """
gh() {
  case "$*" in
    *'--jq .headRefOid'*) echo "$CI_HEAD" ;;
    *'pr view'*) cat "$META_FILE" ;;
    *'pr diff'*--name-only*) echo package/a.py ;;
    *'pr diff'*) echo 'diff --git a/package/a.py b/package/a.py' ;;
    *'pr checks'*--required*) echo '[]' ;;
    *'pr checks'*) echo '[{"name":"unit","bucket":"fail"}]' ;;
    *) return 91 ;;
  esac
}
git() { printf '%s\\n' "$HEAD_SHA"; }
"""


@_skip_no_bash
@pytest.mark.parametrize(
    ("head", "ci_head", "expected"),
    [
        pytest.param(
            _SHA, "b" * 40, "CI_STATUS=unavailable (PR head changed)\nCI_GATE=PASS\n", id="sha1-ci-unavailable"
        ),
        pytest.param(
            "c" * 64, "b" * 40, "CI_STATUS=unavailable (PR head changed)\nCI_GATE=PASS\n", id="sha256-ci-unavailable"
        ),
        pytest.param("c" * 64, "c" * 64, "CI_STATUS=failing — unit\nCI_GATE=BLOCK\n", id="sha256-ci-failing"),
    ],
)
def test_snapshot_block_starts_the_initial_ci_observation(
    snapshot: Path, tmp_path: Path, head: str, ci_head: str, expected: str
) -> None:
    """The preflight snapshot takes a full SHA-1 or SHA-256 head and lets its first CI observation start unrecorded.

    An unreadable initial observation has no earlier failure to keep, so it passes and says why CI is unavailable.
    """
    meta = json.loads((snapshot / "pr-meta.json").read_text(encoding="utf-8"))
    (snapshot / "pr-meta.json").write_text(json.dumps(meta | {"headRefOid": head}), encoding="utf-8", newline="\n")
    result = _run(
        tmp_path,
        _SNAPSHOT_CI_STUB
        + _block("SKILL.md", 'SNAP_DIR="${TMPDIR:-/tmp}/oss-review-snap-')
        + "\n"
        + _block("modes/ci-status.md", "for _CHECK_SET"),
        META_FILE=bash_path(snapshot / "pr-meta.json"),
        HEAD_SHA=head,
        CI_HEAD=ci_head,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    "partial",
    [
        pytest.param("", id="empty"),
        pytest.param("CI_GATE=BL", id="cut-inside-gate"),
        pytest.param("CI_GATE=BLOCK", id="block-without-line-end"),
        pytest.param("CI_GATE=PASS", id="pass-without-line-end"),
    ],
)
def test_partial_gate_record_blocks_an_unavailable_refresh(snapshot: Path, tmp_path: Path, partial: str) -> None:
    """A gate record cut short by a failed write reads as missing, so it can never turn into PASS."""
    _observe_initial_ci(snapshot, tmp_path, "failing")
    (tmp_path / "oss-review-ci-gate-safety").write_text(partial, encoding="utf-8", newline="\n")
    result = _observe_ci(
        tmp_path,
        _block("modes/post-agent.md", "BRANCH=$(git branch"),
        [{"name": "unit", "bucket": "pass"}],
        [],
        "b" * 40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"CI_STATUS=unavailable (PR head changed) · {_MISSING_RECORD}\nCI_GATE=BLOCK\n" in result.stdout


@_skip_no_bash
def test_direct_path_consolidation_skips_ci(tmp_path: Path) -> None:
    """A saved report needs neither a network read nor a PR snapshot to consolidate."""
    (tmp_path / "oss-review-pr-tag-safety").write_text("review-report.md\n", encoding="utf-8")
    script = (
        _block("modes/post-agent.md", "BRANCH=$(git branch") + "\n" + _block("modes/ci-status.md", "for _CHECK_SET")
    )
    result = _run(tmp_path, 'gh() { touch "$TMPDIR/unexpected-read"; return 1; }\n' + script)
    assert result.returncode == 0, result.stderr
    assert "CI_STATUS=not applicable\nCI_GATE=PASS" in result.stdout
    assert not (tmp_path / "unexpected-read").exists()


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
  case "$*" in 'rev-parse --show-toplevel') printf '%s\\n' "$PROJECT_ROOT" ;; 'worktree add --detach '*) return 0 ;; *) return 92 ;; esac
}
"""
    result = _run(tmp_path, stub + _block("SKILL.md", "git worktree add --detach"), PROJECT_ROOT=tmp_path.as_posix())
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
    _git(checkout, "config", "core.autocrlf", "false")
    _git(checkout, "remote", "set-url", "origin", fork.as_posix())
    _git(checkout, "remote", "add", "upstream", remote.as_posix())
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
    result = _run(tmp_path, "jq() { echo unexpected-ci-read >&2; return 99; }\n" + _block("SKILL.md", "PR_LABELS=$(jq"))
    assert result.returncode == expected, result.stdout + result.stderr
    assert ("! BLOCKED" in result.stdout) == (expected != 0)
    assert "unexpected-ci-read" not in result.stderr


@_skip_no_bash
def test_flag_parse_clears_prior_snapshot(tmp_path: Path) -> None:
    """Starting a report reply removes a crashed PR run's snapshot, CI directory and held CI gate."""
    report = tmp_path / "report.md"
    report.write_text("Outcome: APPROVE\n", encoding="utf-8", newline="\n")
    sentinels = [tmp_path / f"oss-review-{name}-safety" for name in ("snap-dir", "ci-dir", "ci-gate")]
    for sentinel in sentinels:
        sentinel.write_text("old-pr\n", encoding="utf-8", newline="\n")
    result = _run(
        tmp_path,
        _block("SKILL.md", "DIRECT_PATH_MODE=false\nif [["),
        ARGUMENTS=f"{bash_path(report)} --reply",
        CLAUDE_PLUGIN_ROOT=bash_path(_REVIEW.parents[1]),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert [sentinel.name for sentinel in sentinels if sentinel.exists()] == []


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
    41) printf '%s' '{"data":{"repository":{"issue":{"state":"CLOSED","url":"https://github.com/owner/project/issues/41","closedByPullRequestsReferences":{"nodes":[{"number":9,"state":"MERGED","url":"https://github.com/owner/project/pull/9"}]}}}}}' ;;
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
