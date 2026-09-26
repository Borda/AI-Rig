"""Guard report-mode selection and Codex-direct commit handoff instructions."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


_RESOLVE = Path(__file__).resolve().parents[1] / "skills" / "resolve"
_BASH = shutil.which("bash")


def _bash_path(path: Path) -> str:
    """Return the fixture path in Git Bash syntax on native Windows."""
    if sys.platform != "win32":
        return str(path)
    return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()


def _bash_block_after(document: str, marker: str) -> str:
    """Return the executable Bash block immediately following a resolve step marker."""
    start = document.index("```bash", document.index(marker)) + len("```bash\n")
    return document[start : document.index("```", start)]


@pytest.mark.skipif(_BASH is None, reason="Resolve route uses Bash")
def test_report_header_pr_reaches_step_4_in_fresh_shell(tmp_path: Path) -> None:
    """A bare report invocation persists its header PR for later checkout blocks."""
    report_doc = (_RESOLVE / "modes" / "report-intelligence.md").read_text(encoding="utf-8")
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    report = tmp_path / "review-report.md"
    report.write_text(
        "---\nTitle: review\nPR: #42\nGate: PASS\nOutcome: NEEDS_WORK\nSummary: fix\n---\n\n## Code Review: PR #42\n",
        encoding="utf-8",
        newline="\n",
    )
    session = "report-pr-state-test"
    (tmp_path / f"resolve-report-file-{session}").write_text(f"{_bash_path(report)}\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-pr-number-{session}").write_text("n/a\n", encoding="utf-8", newline="\n")
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    (plugin / "bin" / "resolve_pr_refs.py").write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[-1]).write_text(' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
        newline="\n",
    )
    env = os.environ | {
        "CLAUDE_CODE_SESSION_ID": session,
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "TMPDIR": str(tmp_path),
    }
    producer = subprocess.run(
        [_BASH, "-c", _bash_block_after(report_doc, "Report header state")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert producer.returncode == 0, producer.stderr
    assert (tmp_path / f"resolve-pr-number-{session}").read_text(encoding="utf-8") == "42\n"
    consumer = _bash_block_after(skill, "**Branch-safety pre-check**")
    result = subprocess.run([_BASH, "-c", consumer], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "42").read_text(encoding="utf-8") == "--pr 42"


@pytest.mark.skipif(_BASH is None, reason="Resolve route uses Bash")
@pytest.mark.parametrize(
    ("frontmatter_pr", "heading", "line_ending", "expected_status", "expected_pr"),
    [
        pytest.param("n/a", "## Code Review: branch", "\n", 0, "n/a", id="local-report-clears-stale-pr"),
        pytest.param("#43", "## Code Review: PR #42 — title", "\n", 1, "", id="conflicting-pr-headings-block"),
        pytest.param("#4x", "## Code Review: PR #42", "\n", 1, "", id="malformed-pr-field-blocks"),
        pytest.param("#42", "## Code Review: PR #42", "\r\n", 0, "42", id="crlf-report-header"),
    ],
)
def test_report_header_state_rejects_ambiguous_pr(
    tmp_path: Path, frontmatter_pr: str, heading: str, line_ending: str, expected_status: int, expected_pr: str
) -> None:
    """Report metadata must not inherit a prior PR or route conflicting identities."""
    report_doc = (_RESOLVE / "modes" / "report-intelligence.md").read_text(encoding="utf-8")
    report = tmp_path / "review-report.md"
    report.write_bytes(line_ending.join(("---", f"PR: {frontmatter_pr}", "---", heading, "")).encode("utf-8"))
    session = "report-header-validation-test"
    (tmp_path / f"resolve-report-file-{session}").write_text(f"{_bash_path(report)}\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-pr-number-{session}").write_text("99\n", encoding="utf-8", newline="\n")
    result = subprocess.run(
        [_BASH, "-c", _bash_block_after(report_doc, "Report header state")],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected_status
    persisted = (tmp_path / f"resolve-pr-number-{session}").read_text(encoding="utf-8")
    assert persisted == (f"{expected_pr}\n" if expected_pr else "")


@pytest.mark.skipif(_BASH is None, reason="Resolve route uses Bash")
@pytest.mark.parametrize(
    ("resolved_pr", "expected_status", "expected_ref"),
    [
        pytest.param("42", 0, "https://github.com/owner/repo/pull/42\n", id="matching-pr"),
        pytest.param("43", 1, "", id="mismatched-pr"),
    ],
)
def test_report_header_pr_creates_fork_reference_in_later_shell(
    tmp_path: Path, resolved_pr: str, expected_status: int, expected_ref: str
) -> None:
    """A fork commit uses the URL for the persisted PR rather than an empty parser URL."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    session = "report-fork-state-test"
    (tmp_path / f"resolve-pr-number-{session}").write_text("42\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-is-cross-repo-{session}").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-head-repo-owner-{session}").write_text("alice\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-pr-ref-{session}").write_text("stale\n", encoding="utf-8", newline="\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!/bin/sh\nprintf 'https://github.com/owner/repo/pull/{resolved_pr}\\n'\n", encoding="utf-8", newline="\n"
    )
    gh.chmod(0o755)
    result = subprocess.run(
        [_BASH, "-c", _bash_block_after(skill, "Determine `FORK_REMOTE` for push")],
        cwd=tmp_path,
        env=os.environ
        | {
            "CLAUDE_CODE_SESSION_ID": session,
            "TMPDIR": str(tmp_path),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected_status, result.stderr
    assert (tmp_path / f"resolve-pr-ref-{session}").read_text(encoding="utf-8") == expected_ref


@pytest.mark.skipif(_BASH is None, reason="Resolve route uses Bash")
def test_report_without_pr_persists_base_for_step_9(tmp_path: Path) -> None:
    """A local report carries its actual default base ref into the later QA shell."""
    report_doc = (_RESOLVE / "modes" / "report-intelligence.md").read_text(encoding="utf-8")
    qa_doc = (_RESOLVE / "modes" / "lint-qa-gate.md").read_text(encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Resolve Test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "resolve@example.invalid"], check=True)
    (tmp_path / "note.txt").write_text("base\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "note.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    base_sha = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(tmp_path), "branch", "develop"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "update-ref", "refs/remotes/origin/develop", base_sha], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop"],
        check=True,
    )
    (tmp_path / "note.txt").write_text("changed\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "change"], check=True)
    session = "report-base-state-test"
    env = os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)}
    producer = subprocess.run(
        [_BASH, "-c", _bash_block_after(report_doc, "**`BASE_REF` derivation (no-PR path)**")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert producer.returncode == 0, producer.stderr
    assert (tmp_path / f"resolve-base-ref-{session}").read_text(encoding="utf-8") == "develop\n"
    qa = (
        _bash_block_after(qa_doc, "## Step 9: Lint and QA gate")
        + 'printf "BASE=%s MERGE=%s\\n" "$BASE_REF" "$BASE_REF_MERGE"\n'
    )
    result = subprocess.run([_BASH, "-c", qa], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f"BASE=develop MERGE={base_sha}" in result.stdout


@pytest.mark.skipif(_BASH is None, reason="Resolve route uses Bash")
def test_step_9_blocks_missing_base_ref_state(tmp_path: Path) -> None:
    """The QA range cannot silently become origin/ after a lost base sentinel."""
    qa_doc = (_RESOLVE / "modes" / "lint-qa-gate.md").read_text(encoding="utf-8")
    result = subprocess.run(
        [_BASH, "-c", _bash_block_after(qa_doc, "## Step 9: Lint and QA gate")],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": "missing-base-test", "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "BASE_REF sentinel missing" in result.stdout


@pytest.mark.skipif(_BASH is None, reason="Resolve route uses Bash")
def test_report_without_default_ref_clears_stale_base(tmp_path: Path) -> None:
    """An unknown current default branch cannot reuse an earlier invocation's QA range."""
    report_doc = (_RESOLVE / "modes" / "report-intelligence.md").read_text(encoding="utf-8")
    session = "missing-default-test"
    sentinel = tmp_path / f"resolve-base-ref-{session}"
    sentinel.write_text("old-default\n", encoding="utf-8", newline="\n")
    result = subprocess.run(
        [_BASH, "-c", _bash_block_after(report_doc, "**`BASE_REF` derivation (no-PR path)**")],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Cannot determine a valid origin default branch" in result.stdout
    assert sentinel.read_text(encoding="utf-8") == ""


@pytest.mark.skipif(_BASH is None, reason="Resolve route uses Bash")
@pytest.mark.parametrize("prior_ref", [None, "#42\n"])
def test_no_pr_report_writes_local_commit_reference(tmp_path: Path, prior_ref: str | None) -> None:
    """A local report overwrites a prior PR reference before any commit route can read it."""
    report_doc = (_RESOLVE / "modes" / "report-intelligence.md").read_text(encoding="utf-8")
    report = tmp_path / "review-report.md"
    report.write_text("---\nPR: n/a\n---\n## Code Review: branch\n", encoding="utf-8", newline="\n")
    session = "local-report-reference-test"
    (tmp_path / f"resolve-report-file-{session}").write_text(f"{_bash_path(report)}\n", encoding="utf-8", newline="\n")
    ref_file = tmp_path / f"resolve-pr-ref-{session}"
    if prior_ref is not None:
        ref_file.write_text(prior_ref, encoding="utf-8", newline="\n")
    env = os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)}
    header = subprocess.run(
        [_BASH, "-c", _bash_block_after(report_doc, "Report header state")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert header.returncode == 0, header.stderr
    local_ref = subprocess.run(
        [_BASH, "-c", _bash_block_after(report_doc, "Local report commit reference")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert local_ref.returncode == 0, local_ref.stderr
    assert ref_file.read_text(encoding="utf-8") == "n/a (local report)\n"


@pytest.mark.skipif(_BASH is None, reason="Resolve route uses Bash")
@pytest.mark.parametrize(
    ("marker", "next_line"),
    [
        pytest.param("**Concurrency guard — mutex + HEAD fingerprint**", "_GITDIR=", id="prelude"),
        pytest.param("**SECURITY — every field below comes from", "_BATCH_TAG=", id="each"),
        pytest.param("**SECURITY — file list and per-item summaries", "GROUP_IDS=", id="grouped"),
        pytest.param("**After loop — `COMMIT_MODE=all` only**", "# grep -c", id="all"),
    ],
)
def test_every_commit_route_rejects_missing_or_stale_pr_reference(tmp_path: Path, marker: str, next_line: str) -> None:
    """A fresh shell cannot invent #0 or reuse #42 for a no-PR report commit."""
    dispatch = (_RESOLVE / "modes" / "action-item-dispatch.md").read_text(encoding="utf-8")
    block = _bash_block_after(dispatch, marker)
    prefix = block[: block.index(next_line)]
    session = "local-reference-consumer-test"
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    (tmp_path / f"resolve-impl-dir-{session}").write_text(f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-pr-number-{session}").write_text("n/a\n", encoding="utf-8", newline="\n")
    env = os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)}
    for ref in (None, "#42\n"):
        ref_file = tmp_path / f"resolve-pr-ref-{session}"
        if ref is None:
            ref_file.unlink(missing_ok=True)
        else:
            ref_file.write_text(ref, encoding="utf-8", newline="\n")
        result = subprocess.run([_BASH, "-c", prefix], cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode != 0, (marker, ref, result.stdout, result.stderr)
        assert "PR reference" in result.stdout
    for pr_number, ref in (
        ("n/a", "n/a (local report)\n"),
        ("42", "#42\n"),
        ("42", "https://github.com/owner/repo/pull/42\n"),
    ):
        (tmp_path / f"resolve-pr-number-{session}").write_text(f"{pr_number}\n", encoding="utf-8", newline="\n")
        ref_file.write_text(ref, encoding="utf-8", newline="\n")
        result = subprocess.run([_BASH, "-c", prefix], cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode == 0, (marker, pr_number, ref, result.stdout, result.stderr)


def test_specialist_commit_prompt_reads_current_report_reference() -> None:
    """A child worktree must load the parent run's reference instead of guessing a PR token."""
    dispatch = (_RESOLVE / "modes" / "action-item-dispatch.md").read_text(encoding="utf-8")
    prompt = dispatch[dispatch.index("Per group, mark its items'") : dispatch.index("**Fire all specialist groups")]
    assert "resolve-pr-number-${CSID}" in prompt
    assert "resolve-pr-ref-${CSID}" in prompt
    assert '--pr \\"$_PR_REF\\"' in prompt
    assert '--pr \\"<PR_REF>\\"' not in prompt


def test_report_mode_persists_items_and_reaches_shared_selection_before_dispatch() -> None:
    """Report-only items must survive selection and resolve through Step 8's JSONL lookup."""
    report = (_RESOLVE / "modes" / "report-intelligence.md").read_text(encoding="utf-8")
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    route = report[report.index("PR# found in report header") : report.index("**Challenge Log")]
    pr_route, branch_route = route.split("No PR# in header", maxsplit=1)
    assert "action-items.jsonl" in report
    assert "resolve-impl-dir-${CSID}" in report
    assert "Step 3d" in pr_route and "skip Step 3e" in pr_route and "Step 4" in pr_route
    assert "Step 3d" in branch_route and "skip Step 3e" in branch_route and "Step 8" in branch_route
    assert "skip Step 3d" not in route
    assert "skip to Step 8" not in route
    selection = skill[skill.index("## Step 3d") : skill.index("## Step 3e")]
    assert "**Commit mode**" in selection
    assert "**Over-20 selection gate**" in selection
    tasks = skill[skill.index("## Step 3e") : skill.index("## Step 4")]
    assert "`report` mode skips Step 3e" in tasks


def test_merged_report_items_replace_pr_jsonl_before_selection() -> None:
    """A merged table must not leave Step 8 reading Step 3b's unmerged item IDs."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    merge = skill[skill.index("## Step 3c") : skill.index("## Step 3d")]
    dispatch = (_RESOLVE / "modes" / "action-item-dispatch.md").read_text(encoding="utf-8")
    assert "action-items.jsonl" in merge
    assert "rewrite" in merge
    assert "before Step 3d" in merge
    assert "full_comment_text" in merge
    assert "location" in merge
    assert '"$IMPL_DIR/action-items.jsonl"' in dispatch


def test_c1_each_commit_requires_credit_and_success_before_item_ledger() -> None:
    """A failed Codex-direct commit must leave no completed per-item record."""
    dispatch = (_RESOLVE / "modes" / "action-item-dispatch.md").read_text(encoding="utf-8")
    block_start = dispatch.index("```bash", dispatch.index("**Only `each` mode commits here.**")) + len("```bash\n")
    block = dispatch[block_start : dispatch.index("```", block_start)]
    each = block[
        block.index('if [ "$COMMIT_MODE" = "each" ]') : block.index(
            "else", block.index('if [ "$COMMIT_MODE" = "each" ]')
        )
    ]
    assert "--codex" in each
    assert "|| {" in each
    assert block.index("commit_action_item.py") < block.index('>> "$IMPL_DIR/c1-item-summary.tsv"')
    assert block.index("commit_action_item.py") < block.index('>> "$IMPL_DIR/c1-item-files.tsv"')


@pytest.mark.skipif(any(shutil.which(name) is None for name in ("bash", "jq")), reason="Step 8 needs Bash and jq")
def test_step_8_blocks_missing_commit_choice_before_dispatch(tmp_path: Path) -> None:
    """A skipped selection step cannot start item implementation with an unset mode."""
    dispatch = (_RESOLVE / "modes" / "action-item-dispatch.md").read_text(encoding="utf-8")
    start = dispatch.index("```bash", dispatch.index("Substitute the Step 3d selection")) + len("```bash\n")
    prelude = dispatch[start : dispatch.index("```", start)].replace(
        'SELECTED_ITEMS="<space-separated selected ids>"', 'SELECTED_ITEMS="1"'
    )
    impl_dir = tmp_path / "implementation"
    impl_dir.mkdir()
    (impl_dir / "action-items.jsonl").write_text('{"id":1}\n', encoding="utf-8", newline="\n")
    (tmp_path / "resolve-impl-dir-report-choice-test").write_text(
        _bash_path(impl_dir) + "\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "resolve-agent-override-report-choice-test").write_text("\n", encoding="utf-8", newline="\n")
    result = subprocess.run(
        [_BASH, "-c", prelude],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": "report-choice-test", "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Step 3d did not finish" in result.stdout
    assert not (impl_dir / "selected-items.txt").exists()


@pytest.mark.skipif(any(shutil.which(name) is None for name in ("bash", "jq")), reason="Step 8 needs Bash and jq")
def test_step_8_rejects_selection_missing_from_persisted_report_items(tmp_path: Path) -> None:
    """A stale or unmerged JSONL cannot silently dispatch another item under the selected ID."""
    dispatch = (_RESOLVE / "modes" / "action-item-dispatch.md").read_text(encoding="utf-8")
    start = dispatch.index("```bash", dispatch.index("Substitute the Step 3d selection")) + len("```bash\n")
    prelude = dispatch[start : dispatch.index("```", start)].replace(
        'SELECTED_ITEMS="<space-separated selected ids>"', 'SELECTED_ITEMS="2"'
    )
    impl_dir = tmp_path / "implementation"
    impl_dir.mkdir()
    (impl_dir / "action-items.jsonl").write_text('{"id":1}\n', encoding="utf-8", newline="\n")
    (tmp_path / "resolve-impl-dir-report-stale-test").write_text(
        _bash_path(impl_dir) + "\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "resolve-agent-override-report-stale-test").write_text("\n", encoding="utf-8", newline="\n")
    (tmp_path / "resolve-commit-mode-report-stale-test").write_text("each\n", encoding="utf-8", newline="\n")
    result = subprocess.run(
        [_BASH, "-c", prelude],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": "report-stale-test", "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "selected item 2 missing from action-items.jsonl" in result.stdout
    assert not (impl_dir / "selected-items.txt").exists()


@pytest.mark.skipif(
    any(shutil.which(name) is None for name in ("bash", "git", "jq")), reason="C1 fence needs Bash, Git, and jq"
)
def test_failed_c1_each_commit_leaves_no_item_ledger(tmp_path: Path) -> None:
    """A failed C1 commit leaves no ledger when native jq emits CRLF."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    source = repo / "note.txt"
    source.write_text("before\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(repo), "add", "note.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    source.write_text("after\n", encoding="utf-8", newline="\n")

    impl_dir = tmp_path / "implementation"
    impl_dir.mkdir()
    (impl_dir / "c1-head-1.txt").write_text(head + "\n", encoding="utf-8", newline="\n")
    (impl_dir / "action-items.jsonl").write_text(
        '{"id":1,"author":"reviewer","full_comment_text":"fix note"}\n', encoding="utf-8", newline="\n"
    )
    (impl_dir / "c1-reply-1.json").write_text(
        '{"status":"complete","verdict":"DONE","findings":["fixed note"],"files_touched":["note.txt"],"remaining":[],"blockers":[]}',
        encoding="utf-8",
        newline="\n",
    )
    (tmp_path / "resolve-impl-dir-c1-fail-test").write_text(_bash_path(impl_dir) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "resolve-commit-mode-c1-fail-test").write_text("each\n", encoding="utf-8", newline="\n")
    (tmp_path / "resolve-pr-number-c1-fail-test").write_text("42\n", encoding="utf-8", newline="\n")
    (tmp_path / "resolve-pr-ref-c1-fail-test").write_text("#42\n", encoding="utf-8", newline="\n")
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    (plugin / "bin" / "commit_action_item.py").write_text("raise SystemExit(1)\n", encoding="utf-8", newline="\n")

    dispatch = (_RESOLVE / "modes" / "action-item-dispatch.md").read_text(encoding="utf-8")
    start = dispatch.index("```bash", dispatch.index("**Only `each` mode commits here.**")) + len("```bash\n")
    fence = dispatch[start : dispatch.index("```", start)].replace(
        '_BATCH_TAG="<this batch\'s first item id — same value used for the brief file above>"', '_BATCH_TAG="1"'
    )
    # Model native Windows jq: only --binary suppresses CRLF in Git Bash pipes.
    jq_crlf = r"""jq() {
    for option in "$@"; do
        if [ "$option" = "-b" ] || [ "$option" = "--binary" ]; then
            command jq "$@"
            return
        fi
    done
    command jq "$@" | sed 's/$/\r/'
}
"""
    result = subprocess.run(
        [_BASH, "-c", jq_crlf + fence],
        cwd=repo,
        env=os.environ
        | {
            "CLAUDE_CODE_SESSION_ID": "c1-fail-test",
            "CLAUDE_PLUGIN_ROOT": str(plugin),
            "TMPDIR": str(tmp_path),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "C1 per-item commit failed" in result.stdout
    assert not (impl_dir / "c1-item-summary.tsv").exists()
    assert not (impl_dir / "c1-item-files.tsv").exists()
    assert source.read_text(encoding="utf-8") == "after\n"
