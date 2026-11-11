"""Guard report-mode selection and Codex-direct commit handoff instructions.

Prose checks read the shipped Markdown with whitespace collapsed, so reflowing a paragraph never breaks an assertion.
Executable checks run the shipped Bash blocks, unedited, in scratch repositories under ``tmp_path`` with session-scoped
sentinels, the way separate Bash tool calls would.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _resolve_source import read_resolve_source
from _text_io import bash_block_after, bash_path, write_lf

_RESOLVE = Path(__file__).resolve().parents[3] / "skills" / "resolve"
_BASH = shutil.which("bash")
_ZSH = shutil.which("zsh")
_DISPATCH_PHASES = ("prelude", "phase1", "phase2", "phase3")
_REPORT_MODE = "modes/report-intelligence.md"
_QA_GATE = "modes/lint-qa-gate.md"
_LOCAL_REPORT_REF = "n/a (local report)\n"
#: The Bash tool runs the login shell — zsh on macOS, which never sets ``BASH_REMATCH`` — so header parsing runs both.
_SHELLS = [
    pytest.param(_BASH, id="bash", marks=pytest.mark.skipif(_BASH is None, reason="bash not installed")),
    pytest.param(_ZSH, id="zsh", marks=pytest.mark.skipif(_ZSH is None, reason="zsh not installed")),
]
_requires_bash = pytest.mark.skipif(_BASH is None, reason="Resolve route uses Bash")
_requires_bash_jq = pytest.mark.skipif(
    any(shutil.which(name) is None for name in ("bash", "jq")), reason="Step 8 needs Bash and jq"
)
_requires_bash_git_jq = pytest.mark.skipif(
    any(shutil.which(name) is None for name in ("bash", "git", "jq")), reason="C1 fence needs Bash, Git, and jq"
)
#: Every dispatch block that reads the PR reference, with the first line that follows its reference check.
_COMMIT_ROUTES = [
    pytest.param("**Concurrency guard — mutex + HEAD fingerprint**", "_GITDIR=", id="prelude"),
    pytest.param("**SECURITY — every field below comes from", "IFS= read -r _BATCH_TAG <", id="each"),
    pytest.param("**SECURITY — file list and per-item summaries", "_GROUP_NOW=", id="grouped"),
    pytest.param("**After loop — `COMMIT_MODE=all` only**", "# grep -c", id="all"),
]


def _doc(relative: str) -> str:
    """Read one resolve skill file verbatim, for extracting fenced blocks that must keep their newlines."""
    return read_resolve_source(_RESOLVE / relative)


def _prose(relative: str) -> str:
    """Read one resolve skill file with whitespace collapsed, so assertions never depend on where prose wraps."""
    return " ".join(_doc(relative).split())


def _dispatch() -> str:
    """Return the four dispatch mode files joined, verbatim."""
    return "\n".join(_doc(f"modes/dispatch-{phase}.md") for phase in _DISPATCH_PHASES)


def _write_sentinels(tmp_path: Path, session: str, values: dict[str, str]) -> None:
    """Write each ``resolve-<name>-<session>`` sentinel under TMPDIR with its value verbatim."""
    for name, value in values.items():
        write_lf(tmp_path / f"resolve-{name}-{session}", value)


def _env(tmp_path: Path, session: str, **extra: str) -> dict[str, str]:
    """Build the environment a resolve block runs in: session id, scratch TMPDIR and any extra variables."""
    return os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)} | extra


def _python_path() -> str:
    """Return a PATH with the test interpreter ahead of the system one."""
    return f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"


def _run(block: str, cwd: Path, env: dict[str, str], shell: str | None = None) -> subprocess.CompletedProcess[str]:
    """Execute a shipped block in a fresh shell (Bash unless given), as a separate Bash tool call would."""
    return subprocess.run([shell or _BASH, "-c", block], cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _git(repo: Path, *args: str) -> str:
    """Run one git command in a repository with a fixed identity and no signing; return its stripped stdout."""
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    done = subprocess.run(["git", "-C", str(repo), *identity, *args], check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _stub_plugin(tmp_path: Path, script: str, body: str) -> Path:
    """Create a fake plugin root whose ``bin/<script>`` holds ``body``; return the root."""
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    write_lf(plugin / "bin" / script, body)
    return plugin


def _at_pr_head(tmp_path: Path, session: str, *, is_cross_repo: bool) -> Path:
    """Create a repository already on the PR branch, with the sentinels Step 4's reference helper writes.

    The local SHA equals the PR head SHA, so the one Step 4 block skips ``gh pr checkout`` and runs through to its end.
    """
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "feature", str(repo)], check=True)
    _git(repo, "commit", "--allow-empty", "-m", "PR head")
    head = _git(repo, "rev-parse", "HEAD")
    sentinels = {
        "head-ref": "feature",
        "pr-head-oid": head,
        "local-sha": head,
        "is-cross-repo": str(is_cross_repo).lower(),
    }
    _write_sentinels(tmp_path, session, {name: f"{value}\n" for name, value in sentinels.items()})
    return repo


def _step_4_path(tmp_path: Path, gh_body: str) -> str:
    """Return a PATH whose ``gh`` is a stub running ``gh_body``, with the test interpreter ahead of the system one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    write_lf(gh, f"#!/bin/sh\n{gh_body}\n")
    gh.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{_python_path()}"


class TestReportHeaderState:
    """Pin report-header state across fresh shells and PR reference resolution."""

    @_requires_bash
    def test_reaches_step_4_in_fresh_shell(self, tmp_path: Path) -> None:
        """A bare report invocation persists its header PR for the one Step 4 checkout block.

        The block runs to its end on a repository already at the PR head, so its reference helper, checkout verification
        and ``FORK_REMOTE`` derivation all complete after the persisted number is read in a fresh shell.
        """
        report = tmp_path / "review-report.md"
        write_lf(
            report,
            "---\nTitle: review\nPR: #42\nGate: PASS\nOutcome: NEEDS_WORK\nSummary: fix\n---\n\n## Code Review: PR #42\n",
        )
        session = "report-pr-state-test"
        _write_sentinels(tmp_path, session, {"report-file": f"{bash_path(report)}\n", "pr-number": "n/a\n"})
        plugin = _stub_plugin(
            tmp_path,
            "resolve_pr_refs.py",
            "import pathlib, sys\npathlib.Path(sys.argv[-1]).write_text(' '.join(sys.argv[1:]))\n",
        )
        env = _env(tmp_path, session, CLAUDE_PLUGIN_ROOT=str(plugin))
        producer = _run(bash_block_after(_doc(_REPORT_MODE), "Report header state"), tmp_path, env)
        assert producer.returncode == 0, producer.stderr
        assert (tmp_path / f"resolve-pr-number-{session}").read_text(encoding="utf-8") == "42\n"
        repo = _at_pr_head(tmp_path, session, is_cross_repo=False)
        consumer = bash_block_after(_doc("SKILL.md"), "**Branch-safety pre-check**")
        result = _run(consumer, repo, env | {"PATH": _step_4_path(tmp_path, "exit 1")})
        assert result.returncode == 0, result.stdout + result.stderr
        assert (repo / "42").read_text(encoding="utf-8") == "--pr 42"

    @pytest.mark.parametrize("shell", _SHELLS)
    @pytest.mark.parametrize(
        ("frontmatter_pr", "heading", "line_ending", "expected_status", "expected_pr"),
        [
            pytest.param("n/a", "## Code Review: branch", "\n", 0, "n/a", id="local-report-clears-stale-pr"),
            pytest.param("#43", "## Code Review: PR #42 — title", "\n", 1, "", id="conflicting-pr-headings-block"),
            pytest.param("#4x", "## Code Review: PR #42", "\n", 1, "", id="malformed-pr-field-blocks"),
            pytest.param("#042", "## Code Review: branch", "\n", 1, "", id="zero-led-pr-field-blocks"),
            pytest.param("#42", "## Code Review: PR #42", "\r\n", 0, "42", id="crlf-report-header"),
        ],
    )
    def test_rejects_ambiguous_pr(
        self,
        tmp_path: Path,
        frontmatter_pr: str,
        heading: str,
        line_ending: str,
        expected_status: int,
        expected_pr: str,
        shell: str,
    ) -> None:
        """Report metadata must not inherit a prior PR or route conflicting identities, under bash and zsh alike.

        Under zsh the former ``[[ =~ ]]`` match left ``BASH_REMATCH`` unset, so a valid ``PR: #42`` header persisted an
        empty PR number and every later block stopped on a missing PR.
        """
        report = tmp_path / "review-report.md"
        report.write_bytes(line_ending.join(("---", f"PR: {frontmatter_pr}", "---", heading, "")).encode("utf-8"))
        session = "report-header-validation-test"
        _write_sentinels(tmp_path, session, {"report-file": f"{bash_path(report)}\n", "pr-number": "99\n"})
        block = bash_block_after(_doc(_REPORT_MODE), "Report header state")
        result = _run(block, tmp_path, _env(tmp_path, session), shell)
        assert result.returncode == expected_status
        persisted = (tmp_path / f"resolve-pr-number-{session}").read_text(encoding="utf-8")
        assert persisted == (f"{expected_pr}\n" if expected_pr else "")

    @_requires_bash
    @pytest.mark.parametrize(
        ("resolved_pr", "expected_status", "expected_ref"),
        [
            pytest.param("42", 0, "https://github.com/owner/repo/pull/42\n", id="matching-pr"),
            pytest.param("43", 1, "", id="mismatched-pr"),
        ],
    )
    def test_creates_fork_reference_in_later_shell(
        self, tmp_path: Path, resolved_pr: str, expected_status: int, expected_ref: str
    ) -> None:
        """A fork commit uses the URL for the persisted PR rather than an empty parser URL.

        The reference derivation is the tail of the one Step 4 block, so the repository sits at the PR head and the
        block reaches it without a checkout; a stale reference is truncated before the URL check, so a mismatch leaves
        it empty.
        """
        session = "report-fork-state-test"
        _write_sentinels(tmp_path, session, {"pr-number": "42\n"})
        repo = _at_pr_head(tmp_path, session, is_cross_repo=True)
        _write_sentinels(tmp_path, session, {"head-repo-owner": "alice\n", "pr-ref": "stale\n"})
        plugin = _stub_plugin(tmp_path, "resolve_pr_refs.py", "")
        gh_path = _step_4_path(tmp_path, f"printf 'https://github.com/owner/repo/pull/{resolved_pr}\\n'")
        env = _env(tmp_path, session, CLAUDE_PLUGIN_ROOT=str(plugin), PATH=gh_path)
        result = _run(bash_block_after(_doc("SKILL.md"), "**Branch-safety pre-check**"), repo, env)
        assert result.returncode == expected_status, result.stdout + result.stderr
        assert (tmp_path / f"resolve-pr-ref-{session}").read_text(encoding="utf-8") == expected_ref

    @_requires_bash
    @pytest.mark.parametrize(
        "prior",
        [
            pytest.param({}, id="no-prior-reference"),
            pytest.param({"pr-ref": "#42\n"}, id="stale-pr-reference"),
        ],
    )
    def test_no_pr_report_writes_local_commit_reference(self, tmp_path: Path, prior: dict[str, str]) -> None:
        """A local report overwrites a prior PR reference before any commit route can read it.

        The report header says ``PR: n/a``; after the header block and the local-reference block run, the reference
        sentinel must name the local report whether it was absent or left over from an earlier PR run.
        """
        report = tmp_path / "review-report.md"
        write_lf(report, "---\nPR: n/a\n---\n## Code Review: branch\n")
        session = "local-report-reference-test"
        _write_sentinels(tmp_path, session, {"report-file": f"{bash_path(report)}\n", **prior})
        env = _env(tmp_path, session)
        report_doc = _doc(_REPORT_MODE)
        header = _run(bash_block_after(report_doc, "Report header state"), tmp_path, env)
        assert header.returncode == 0, header.stderr
        local_ref = _run(bash_block_after(report_doc, "Local report commit reference"), tmp_path, env)
        assert local_ref.returncode == 0, local_ref.stderr
        assert (tmp_path / f"resolve-pr-ref-{session}").read_text(encoding="utf-8") == _LOCAL_REPORT_REF


class TestBaseRefState:
    """Pin how the base ref persists and clears across report-mode steps."""

    @_requires_bash
    def test_local_report_persists_base_for_step_9(self, tmp_path: Path) -> None:
        """A local report carries its actual default base ref into the later QA shell.

        The scratch repository's origin default branch is ``develop``; the no-PR derivation must persist that name and
        Step 9.1 must resolve the merge-base commit from it in a separate shell.
        """
        _git(tmp_path, "init", "-q")
        write_lf(tmp_path / "note.txt", "base\n")
        _git(tmp_path, "add", "note.txt")
        _git(tmp_path, "commit", "-qm", "base")
        base_sha = _git(tmp_path, "rev-parse", "HEAD")
        _git(tmp_path, "branch", "develop")
        _git(tmp_path, "update-ref", "refs/remotes/origin/develop", base_sha)
        _git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
        write_lf(tmp_path / "note.txt", "changed\n")
        _git(tmp_path, "commit", "-qam", "change")
        session = "report-base-state-test"
        env = _env(tmp_path, session, CLAUDE_PLUGIN_ROOT=str(_RESOLVE.parents[1]), PATH=_python_path())
        producer = _run(bash_block_after(_doc(_REPORT_MODE), "**`BASE_REF` derivation (no-PR path)**"), tmp_path, env)
        assert producer.returncode == 0, producer.stderr
        assert (tmp_path / f"resolve-base-ref-{session}").read_text(encoding="utf-8") == "develop\n"
        qa = bash_block_after(_doc(_QA_GATE), "### 9.1: Lint and QA")
        result = _run(qa + 'printf "BASE=%s MERGE=%s\\n" "$BASE_REF" "$BASE_REF_MERGE"\n', tmp_path, env)
        assert result.returncode == 0, result.stderr
        assert f"BASE=develop MERGE={base_sha}" in result.stdout

    @_requires_bash
    @pytest.mark.parametrize(
        ("marker", "sentinels", "ok", "message"),
        [
            pytest.param("### 9.1: Lint and QA", {}, False, "BASE_REF sentinel missing", id="lost-base-ref-blocks-qa"),
            pytest.param(
                "### 9.0: Target-branch drift gate",
                {"head-ref": "", "base-ref": "develop\n"},
                True,
                "BASE_FRESH=n/a",
                id="no-pr-branch-skips-drift-gate",
            ),
        ],
    )
    def test_qa_gate_without_pr_state(
        self, tmp_path: Path, marker: str, sentinels: dict[str, str], ok: bool, message: str
    ) -> None:
        """The QA range never silently becomes ``origin/``, and a no-PR run never re-merges the target before QA.

        Without a base sentinel Step 9.1 must stop; with an empty head ref (report mode, no PR branch) the Step 9.0
        drift gate must exit cleanly before calling git instead of merging the default branch into local work.
        """
        session = "qa-gate-state-test"
        _write_sentinels(tmp_path, session, sentinels)
        result = _run(bash_block_after(_doc(_QA_GATE), marker), tmp_path, _env(tmp_path, session))
        assert (result.returncode == 0) is ok, result.stdout + result.stderr
        assert message in result.stdout

    @_requires_bash
    def test_unknown_default_ref_clears_stale_base(self, tmp_path: Path) -> None:
        """An unknown current default branch cannot reuse an earlier invocation's QA range.

        The scratch directory has no origin default branch, so the derivation must fail and empty the sentinel an older
        report run left behind instead of keeping its ``old-default``.
        """
        session = "missing-default-test"
        _write_sentinels(tmp_path, session, {"base-ref": "old-default\n"})
        env = _env(tmp_path, session, CLAUDE_PLUGIN_ROOT=str(_RESOLVE.parents[1]), PATH=_python_path())
        result = _run(bash_block_after(_doc(_REPORT_MODE), "**`BASE_REF` derivation (no-PR path)**"), tmp_path, env)
        assert result.returncode != 0
        assert "Cannot determine a valid origin default branch" in result.stdout
        assert (tmp_path / f"resolve-base-ref-{session}").read_text(encoding="utf-8").strip() == ""

    def test_step_1_clears_prior_pr_head_ref(self) -> None:
        """Step 1 empties the head-ref sentinel so a no-PR run never inherits an earlier PR's branch.

        A stale head ref would make the Step 9.0 drift gate treat a local report run as a PR run and merge the default
        branch into the user's own work.
        """
        skill = _prose("SKILL.md")
        step_1 = skill[skill.index("## Step 1: Pre-flight") : skill.index("## Step 2:")]
        assert ': > "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}"' in step_1


class TestCommitReference:
    """Pin the commit reference check that rejects missing or stale references."""

    @_requires_bash
    @pytest.mark.parametrize(("marker", "next_line"), _COMMIT_ROUTES)
    @pytest.mark.parametrize(
        "prior",
        [
            pytest.param({}, id="missing-reference"),
            pytest.param({"pr-ref": "#42\n"}, id="stale-reference"),
        ],
    )
    def test_route_rejects_missing_or_stale_reference(
        self, tmp_path: Path, marker: str, next_line: str, prior: dict[str, str]
    ) -> None:
        """A fresh shell cannot invent #0 or reuse #42 for a no-PR report commit.

        The run is a local report (``PR: n/a``) whose reference sentinel is absent or still holds an earlier PR's
        ``#42``; each commit route's reference check, run alone, must stop and say which reference is wrong.
        """
        block = bash_block_after(_dispatch(), marker)
        session = "local-reference-consumer-test"
        impl_dir = tmp_path / "impl"
        impl_dir.mkdir()
        _write_sentinels(tmp_path, session, {"impl-dir": f"{bash_path(impl_dir)}\n", "pr-number": "n/a\n", **prior})
        result = _run(block[: block.index(next_line)], tmp_path, _env(tmp_path, session))
        assert result.returncode != 0, (marker, prior, result.stdout, result.stderr)
        assert "PR reference" in result.stdout

    @_requires_bash
    @pytest.mark.parametrize(("marker", "next_line"), _COMMIT_ROUTES)
    @pytest.mark.parametrize(
        ("pr_number", "ref"),
        [
            pytest.param("n/a", _LOCAL_REPORT_REF, id="local-report-reference"),
            pytest.param("42", "#42\n", id="pr-number-reference"),
            pytest.param("42", "https://github.com/owner/repo/pull/42\n", id="pr-url-reference"),
        ],
    )
    def test_route_accepts_matching_reference(
        self, tmp_path: Path, marker: str, next_line: str, pr_number: str, ref: str
    ) -> None:
        """Every commit route accepts the reference form that matches its run: local marker, ``#N`` or PR URL.

        Counterpart of the rejection cases: the same check, run with a consistent PR number and reference, must pass.
        """
        block = bash_block_after(_dispatch(), marker)
        session = "local-reference-consumer-test"
        impl_dir = tmp_path / "impl"
        impl_dir.mkdir()
        sentinels = {"impl-dir": f"{bash_path(impl_dir)}\n", "pr-number": f"{pr_number}\n", "pr-ref": ref}
        _write_sentinels(tmp_path, session, sentinels)
        result = _run(block[: block.index(next_line)], tmp_path, _env(tmp_path, session))
        assert result.returncode == 0, (marker, pr_number, ref, result.stdout, result.stderr)

    def test_specialist_prompt_reads_current_reference(self) -> None:
        """A child worktree must load the parent run's reference instead of guessing a PR token."""
        dispatch = " ".join(_dispatch().split())
        prompt = dispatch[dispatch.index("Per group, mark its items'") : dispatch.index("**Fire all specialist groups")]
        assert "resolve-pr-number-${CSID}" in prompt
        assert "resolve-pr-ref-${CSID}" in prompt
        assert '--pr \\"$_PR_REF\\"' in prompt
        assert '--pr \\"<PR_REF>\\"' not in prompt


class TestReportModeFlow:
    """Pin the report-mode flow from item persistence to dispatch and handoff."""

    def test_dispatch_uses_selected_ids_instead_of_pending_count(self) -> None:
        """The report consumer must preserve explicit closed IDs selected in the common picker.

        This guards the declarative producer/consumer boundary; live model adherence remains a separate acceptance gate.
        Both PR-header and local reports share this consumer after Step 3d.
        """
        report = _prose(_REPORT_MODE)
        section = report[report.index("**Report mode — Step 8 behavior**") : report.index("**Challenge Log")]
        assert "zero pending" not in section
        assert "`SELECTED_ITEMS` is empty" in section
        assert "explicitly selected resolved/addressed IDs" in section

    def test_persists_items_and_reaches_shared_selection_before_dispatch(self) -> None:
        """Report-only items must survive selection and resolve through Step 8's JSONL lookup.

        Both routes (PR found in the header, and no PR) must go through Step 3d selection and skip only Step 3e.
        """
        report = _prose(_REPORT_MODE)
        skill = _prose("SKILL.md")
        route = report[report.index("PR# found in report header") : report.index("**Challenge Log")]
        pr_route, branch_route = route.split("No PR# in header", maxsplit=1)
        selection = skill[skill.index("## Step 3d") : skill.index("## Step 3e")]
        tasks = skill[skill.index("## Step 3e") : skill.index("## Step 4")]
        assert "action-items.jsonl" in report
        assert "resolve-impl-dir-${CSID}" in report
        assert "Step 3d" in pr_route
        assert "skip Step 3e" in pr_route
        assert "Step 4" in pr_route
        assert "Step 3d" in branch_route
        assert "skip Step 3e" in branch_route
        assert "Step 8" in branch_route
        assert "skip Step 3d" not in route
        assert "skip to Step 8" not in route
        assert "**Commit mode**" in selection
        assert "**No per-pass item cap**" in selection
        assert "`report` mode skips Step 3e" in tasks

    def test_merged_items_fold_into_pr_jsonl_before_selection(self) -> None:
        """Step 3c merges review findings into Step 3b's jsonl through the helper, keeping GitHub ids, before Step
        3d."""
        skill = _prose("SKILL.md")
        merge = skill[skill.index("## Step 3c") : skill.index("## Step 3d")]
        assert "action-items.jsonl" in merge
        assert "merge_action_items.py" in merge
        assert "--candidates" in merge
        assert "report-links.jsonl" in merge
        assert "no renumbering" in merge
        assert "never inherits `verify_verdict`" in merge
        assert "One finding per item" in merge
        assert "thin: true" in merge
        assert "Step 3d" in merge
        assert "Renumber every merged record" not in merge
        assert '"$IMPL_DIR/action-items.jsonl"' in _dispatch()

    def test_handoff_wiring_reaches_each_consumer(self) -> None:
        """The verdict re-check runs before Phase 1 item lines, Step 11 writes the ledger, and the review reads it back.

        Script-level tests cannot see these call sites; a dropped block would silently disable the re-check or the
        ledger.
        """
        dispatch = " ".join(_dispatch().split())
        skill = _prose("SKILL.md")
        review = " ".join((_RESOLVE.parent / "review" / "SKILL.md").read_text(encoding="utf-8").split())
        consolidator_file = _RESOLVE.parent / "review" / "templates" / "consolidator-prompt.md"
        consolidator = " ".join(consolidator_file.read_text(encoding="utf-8").split())
        step11 = skill[skill.index("## Step 11") :]
        assert dispatch.index("--recheck-verdicts") < dispatch.index("Build each item line")
        assert "append_resolution.py" in step11
        assert '--base-sha "$BASE_SHA"' in step11
        assert '[ -z "$_PRIOR_RES" ] || echo' in review
        assert "Prior resolution ledger:" in review
        assert "Prior resolution ledger" in consolidator
        assert "mint_finding_ids.py" in consolidator


class TestStep8Guards:
    """Pin the step 8 guards that block before dispatch."""

    @_requires_bash_jq
    @pytest.mark.parametrize(
        ("selected", "sentinels", "message"),
        [
            pytest.param("1", {}, "Step 3d did not finish", id="missing-commit-choice"),
            pytest.param(
                "2",
                {"commit-mode": "each\n"},
                "selected item 2 missing from action-items.jsonl",
                id="selection-missing-from-persisted-items",
            ),
        ],
    )
    def test_blocks_before_dispatch(
        self, tmp_path: Path, selected: str, sentinels: dict[str, str], message: str
    ) -> None:
        """A skipped selection step or a stale items file cannot start implementation with the wrong item set.

        The persisted items hold only id 1. Without a commit-mode choice Step 8 must stop on the unfinished selection; a
        selected id absent from the file must stop instead of dispatching another item under that id.
        """
        dispatch = _dispatch()
        block = bash_block_after(dispatch, "Substitute the Step 3d selection")
        prelude = block.replace('SELECTED_ITEMS="<space-separated selected ids>"', f'SELECTED_ITEMS="{selected}"')
        impl_dir = tmp_path / "implementation"
        impl_dir.mkdir()
        write_lf(impl_dir / "action-items.jsonl", '{"id":1}\n')
        session = "report-step-8-test"
        _write_sentinels(
            tmp_path, session, {"impl-dir": f"{bash_path(impl_dir)}\n", "agent-override": "\n", **sentinels}
        )
        result = _run(prelude, tmp_path, _env(tmp_path, session))
        assert result.returncode != 0
        assert message in result.stdout
        assert not (impl_dir / "selected-items.txt").exists()


class TestCodexDirectCommit:
    """Pin the Codex direct-commit rule that credits each commit before the item ledger."""

    def test_each_commit_requires_credit_and_success_before_item_ledger(self) -> None:
        """A failed Codex-direct commit must leave no completed per-item record.

        The ``each`` branch must pass ``--codex``, handle a failed helper, and write both per-item ledgers only after
        the helper call in the block.
        """
        dispatch = _dispatch()
        block = bash_block_after(dispatch, "**Only `each` mode commits here.**")
        each_start = block.index('if [ "$COMMIT_MODE" = "each" ]')
        each = block[each_start : block.index("else", each_start)]
        assert "--codex" in each
        assert "|| {" in each
        assert block.index("commit_action_item.py") < block.index('>> "$IMPL_DIR/c1-item-summary.tsv"')
        assert block.index("commit_action_item.py") < block.index('>> "$IMPL_DIR/c1-item-files.tsv"')

    @_requires_bash_git_jq
    def test_failed_each_commit_leaves_no_item_ledger(self, tmp_path: Path) -> None:
        """A failed C1 commit leaves no ledger when native jq emits CRLF.

        The commit helper is stubbed to exit 1 and ``jq`` is shadowed by a function that appends CRLF unless
        ``--binary`` is passed, as native Windows jq does. The block must report the failure, keep the item marker for
        the rerun and leave the working tree uncommitted.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        source = repo / "note.txt"
        write_lf(source, "before\n")
        _git(repo, "add", "note.txt")
        _git(repo, "commit", "-qm", "baseline")
        head = _git(repo, "rev-parse", "HEAD")
        write_lf(source, "after\n")
        impl_dir = tmp_path / "implementation"
        impl_dir.mkdir()
        reply = '{"status":"complete","verdict":"DONE","findings":["fixed note"],"files_touched":["note.txt"],"remaining":[],"blockers":[]}'
        write_lf(impl_dir / "c1-head-1.txt", head + "\n")
        write_lf(impl_dir / "c1-item-current.txt", "1\n")
        write_lf(impl_dir / "action-items.jsonl", '{"id":1,"author":"reviewer","full_comment_text":"fix note"}\n')
        write_lf(impl_dir / "c1-reply-1.json", reply)
        session = "c1-fail-test"
        sentinels = {
            "impl-dir": f"{bash_path(impl_dir)}\n",
            "commit-mode": "each\n",
            "pr-number": "42\n",
            "pr-ref": "#42\n",
        }
        _write_sentinels(tmp_path, session, sentinels)
        plugin = _stub_plugin(tmp_path, "commit_action_item.py", "raise SystemExit(1)\n")
        # unedited: the item id comes from c1-item-current.txt, recorded by the brief block
        fence = bash_block_after(_dispatch(), "**Only `each` mode commits here.**")
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
        env = _env(tmp_path, session, CLAUDE_PLUGIN_ROOT=str(plugin), PATH=_python_path())
        result = _run(jq_crlf + fence, repo, env)
        assert result.returncode != 0
        assert "C1 per-item commit failed" in result.stdout
        assert not (impl_dir / "c1-item-summary.tsv").exists()
        assert not (impl_dir / "c1-item-files.tsv").exists()
        assert (impl_dir / "c1-item-current.txt").read_text(encoding="utf-8") == "1\n"  # kept for the rerun
        assert source.read_text(encoding="utf-8") == "after\n"


class TestFinalReport:
    """Pin final-report branch restoration after push."""

    @_requires_bash
    @pytest.mark.parametrize(
        ("push_status", "expected_branch"),
        [
            pytest.param("pushed", "main", id="pushed-returns-to-saved-branch"),
            pytest.param("skipped-by-user", "pr-7", id="declined-push-stays"),
            pytest.param("blocked-guard", "pr-7", id="blocked-push-stays"),
            pytest.param("blocked-needs-manual-push", "pr-7", id="handed-over-push-stays"),
        ],
    )
    def test_branch_restored_only_after_push(self, tmp_path: Path, push_status: str, expected_branch: str) -> None:
        """Step 11 returns to the saved branch only when the commits were pushed.

        Switching away from unpushed commits hides them from the user who chose not to push and wants to review or push
        by hand; the block must leave the session on the PR branch instead.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        _git(repo, "switch", "-q", "-c", "pr-7")
        session = "final-branch-restore-test"
        sentinels = {"saved-branch": "main\n", "commit-mode": "each\n", "push-status": f"{push_status}\n"}
        _write_sentinels(tmp_path, session, sentinels)
        block = bash_block_after(_doc("SKILL.md"), "Omit section when `--no-challenge`.")
        result = _run(block, repo, _env(tmp_path, session))
        assert result.returncode == 0, result.stderr
        assert _git(repo, "branch", "--show-current") == expected_branch
