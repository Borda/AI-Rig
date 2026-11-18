"""Guard the PR-code clause wiring: every route that can run PR code follows one recorded decision.

The review preamble and Agent 2 name the clause file, the Step 1 consumers run nothing because no clause exists yet, and
the resolve QA gate writes the direct-run clause for a run that has no PR.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_REVIEW = _SKILLS / "review"
_DIRECT = "PR code: run tests directly in the checkout; no guard."
_REFUSAL = (
    "PR code: run no command that loads or executes PR code (a test, a probe, an overlay run, a type checker or linter "
    "that loads project plugins, a build or a dependency export); report an execution gap."
)
_RESOLVE_REFUSAL = (
    "PR code: run no tests and no PR code; report tests not run — PR-code execution not approved as a gap."
)
_GUARDED = (
    'PR code: run every command that loads or executes checkout code only as python -I "/g.py" run --approval-file '
    '"/i/code-approval.txt" -- <command>, adding -p no:xdist to a pytest command; a refusal (exit 3 or 4) means the '
    "command did not run: report a gap, never run it bare."
)
_GUARDED_REVIEW = (
    'PR code: run every command that loads or executes PR code only as python -I "/g.py" run --approval-file '
    '"/r/code-approval.txt" -- <command>, adding -p no:xdist to a pytest command; a refusal (exit 3 or 4) means it did '
    "not run: report a gap, never run it bare."
)
_BASH = shutil.which("bash")


def _lines(path: Path) -> list[str]:
    """Return the file's lines; the shipped text does not change during a run."""
    return path.read_text(encoding="utf-8").splitlines()


def _line_starting(path: Path, prefix: str) -> str:
    """Return the one line that begins with ``prefix``."""
    hits = [line for line in _lines(path) if line.startswith(prefix)]
    assert len(hits) == 1, (path.name, prefix, len(hits))
    return hits[0]


class TestReviewClause:
    """Review agents obey the clause file or, before it exists, run nothing."""

    def test_preamble_names_the_clause_file(self) -> None:
        """Every dimension agent receives the clause rule, not only the overlay and issue prompts."""
        preamble = _line_starting(_REVIEW / "templates/agent-prompts.md", "**Read-only preamble")
        assert "code-clause.txt" in preamble
        assert "missing file" in preamble

    def test_targeted_tests_follow_the_clause(self) -> None:
        """Agent 2 runs targeted tests only as the clause directs and records a gap when it forbids a run."""
        agent = _line_starting(_REVIEW / "templates/agent-prompts.md", "**Agent 2")
        assert "clause file" in agent
        assert "PR code not approved" in agent

    def test_no_claim_that_targeted_tests_run_ungated(self) -> None:
        """The overlay text no longer treats Agent 2's tests as an already-accepted execution."""
        text = (_REVIEW / "templates/agent-prompts.md").read_text(encoding="utf-8")
        assert "This review already executes PR code" not in text

    @pytest.mark.parametrize("name", ["modes/scope-context.md", "modes/acceptance-gate.md"])
    def test_step_one_consumers_run_no_pr_code(self, name: str) -> None:
        """The issue agent and confirmation prompt run before the clause exists, so they never execute PR code."""
        text = (_REVIEW / name).read_text(encoding="utf-8")
        assert "PR-code decision is recorded in Step 2" in text
        assert "code-clause.txt` verbatim" not in text
        assert "follows $RUN_DIR/code-clause.txt" not in text

    def test_clause_is_written_before_step_two_agents(self) -> None:
        """The gate paragraph orders the clause write ahead of every Step 2 spawn."""
        gate = _line_starting(_REVIEW / "templates/agent-prompts.md", "**PR-code execution gate")
        assert "before spawning any Step 2 agent" in gate


@pytest.mark.skipif(_BASH is None, reason="The clause line is a Bash block line")
class TestResolveNoPrClause:
    """The QA gate records the direct-run clause when the sentinel says the checkout is the user's own code."""

    @staticmethod
    def _run(tmp_path: Path, sentinel: str | None, existing: str | None) -> str | None:
        """Run the sentinel read and two clause lines of the QA setup; return the clause text or None."""
        lines = _lines(_SKILLS / "resolve/modes/lint-qa-gate.md")
        start = next(i for i, line in enumerate(lines) if line.startswith("IFS= read -r _XREPO"))
        script = "\n".join(lines[start : start + 3])
        impl = tmp_path / "impl"
        impl.mkdir()
        if sentinel is not None:
            (tmp_path / "resolve-is-cross-repo-s").write_text(sentinel + "\n", encoding="utf-8", newline="\n")
        if existing is not None:
            (impl / "pr-code-clause.txt").write_text(existing, encoding="utf-8", newline="\n")
        env = {**os.environ, "TMPDIR": str(tmp_path), "CSID": "s", "IMPL_DIR": str(impl)}
        done = subprocess.run([_BASH, "-c", script], env=env, capture_output=True, text=True, timeout=20, check=False)
        assert done.returncode == 0, done.stderr
        clause = impl / "pr-code-clause.txt"
        return clause.read_text(encoding="utf-8") if clause.exists() else None

    def test_own_code_gets_the_direct_clause(self, tmp_path: Path) -> None:
        """A no-PR run (sentinel ``false``) with no earlier clause records the direct-run text."""
        assert self._run(tmp_path, "false", None) == _DIRECT + "\n"

    @pytest.mark.parametrize(
        ("sentinel", "existing"),
        [
            pytest.param("true", None, id="fork"),
            pytest.param(None, None, id="missing-sentinel"),
            pytest.param("", None, id="empty-sentinel"),
            pytest.param("maybe", None, id="unexpected-value"),
        ],
    )
    def test_anything_but_false_writes_no_clause(
        self, tmp_path: Path, sentinel: str | None, existing: str | None
    ) -> None:
        """A fork, a missing or empty sentinel and any other value leave no clause, so no test runs."""
        assert self._run(tmp_path, sentinel, existing) is None

    def test_an_earlier_decision_is_kept(self, tmp_path: Path) -> None:
        """A clause the dispatch prelude already wrote is never replaced."""
        refusal = "PR code: run no tests, probes or overlay runs; report an execution gap.\n"
        assert self._run(tmp_path, "false", refusal) == refusal


class TestEveryRunPermissionNamesTheClause:
    """Each place that permits a targeted test run also names the clause, so a new restatement cannot slip past it."""

    @pytest.mark.parametrize("folder", ["modes", "templates"])
    def test_targeted_test_permissions_name_the_clause(self, folder: str) -> None:
        """A review line that permits targeted tests names the clause file or says no run happens at that step."""
        offenders: list[str] = []
        for path in sorted((_REVIEW / folder).glob("*.md")):
            for number, line in enumerate(_lines(path), start=1):
                lowered = line.lower()
                if "targeted test" not in lowered or ("require" not in lowered and "permitted" not in lowered):
                    continue
                if "clause" not in lowered and "no pr code runs" not in lowered:
                    offenders.append(f"{path.name}:{number}")
        assert offenders == []


@pytest.mark.skipif(_BASH is None, reason="The clause line is a Bash block line")
class TestDirectClauseNeedsTheRecordedAnswer:
    """A direct-run clause survives only beside a recorded same-repo answer; anything else turns it into the refusal."""

    @pytest.mark.parametrize(
        "sentinel",
        [
            pytest.param("true", id="fork"),
            pytest.param(None, id="missing-sentinel"),
            pytest.param("", id="empty-sentinel"),
            pytest.param("maybe", id="unexpected-value"),
        ],
    )
    def test_resolve_replaces_a_direct_clause(self, tmp_path: Path, sentinel: str | None) -> None:
        """The QA gate overwrites a direct-run clause the sentinel does not support, so a slip cannot run fork tests."""
        assert TestResolveNoPrClause._run(tmp_path, sentinel, _DIRECT + "\n") == _RESOLVE_REFUSAL + "\n"

    @pytest.mark.parametrize("clause", [_GUARDED + "\n", _RESOLVE_REFUSAL + "\n"], ids=["guarded", "refusal"])
    def test_resolve_keeps_other_clauses_for_a_fork(self, tmp_path: Path, clause: str) -> None:
        """An approved-guard or refusal clause is the parent's own decision and stays."""
        assert TestResolveNoPrClause._run(tmp_path, "true", clause) == clause

    @staticmethod
    def _review(tmp_path: Path, meta: str | None, clause: str | None) -> str | None:
        """Run the review reconcile block against a scratch snapshot and run directory; return the clause or None."""
        lines = _lines(_REVIEW / "templates/agent-prompts.md")
        gate = next(i for i, line in enumerate(lines) if line.startswith("**PR-code execution gate"))
        start = next(i for i in range(gate, len(lines)) if lines[i] == "```bash") + 1
        end = next(i for i in range(start, len(lines)) if lines[i] == "```")
        snap, run = tmp_path / "snap", tmp_path / "run"
        snap.mkdir()
        run.mkdir()
        if meta is not None:
            (snap / "pr-meta.json").write_text(meta, encoding="utf-8", newline="\n")
        if clause is not None:
            (run / "code-clause.txt").write_text(clause, encoding="utf-8", newline="\n")
        (tmp_path / "oss-review-snap-dir-s").write_text(f"{snap}\n", encoding="utf-8", newline="\n")
        (tmp_path / "oss-review-run-dir-s").write_text(f"{run}\n", encoding="utf-8", newline="\n")
        env = {**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "s"}
        done = subprocess.run(
            [_BASH, "-c", "\n".join(lines[start:end])], env=env, capture_output=True, text=True, timeout=20, check=False
        )
        assert done.returncode == 0, done.stderr
        file = run / "code-clause.txt"
        return file.read_text(encoding="utf-8") if file.exists() else None

    @pytest.mark.skipif(shutil.which("jq") is None, reason="The reconcile block reads the snapshot with jq")
    @pytest.mark.parametrize(
        ("meta", "clause", "expected"),
        [
            pytest.param('{"isCrossRepository": false}', _DIRECT + "\n", _DIRECT + "\n", id="same-repo-keeps-direct"),
            pytest.param('{"isCrossRepository": true}', _DIRECT + "\n", _REFUSAL + "\n", id="fork-direct-refused"),
            pytest.param('{"isCrossRepository": null}', _DIRECT + "\n", _REFUSAL + "\n", id="null-direct-refused"),
            pytest.param("{}", _DIRECT + "\n", _REFUSAL + "\n", id="missing-field-direct-refused"),
            pytest.param(None, _DIRECT + "\n", _REFUSAL + "\n", id="no-snapshot-direct-refused"),
            pytest.param(
                '{"isCrossRepository": true}', _GUARDED_REVIEW + "\n", _GUARDED_REVIEW + "\n", id="fork-guarded-kept"
            ),
            pytest.param('{"isCrossRepository": false}', None, None, id="no-clause-stays-absent"),
        ],
    )
    def test_review_reconciles_the_clause(
        self, tmp_path: Path, meta: str | None, clause: str | None, expected: str | None
    ) -> None:
        """The review parent's reconcile block keeps a direct clause only for a snapshot that says same-repo."""
        assert self._review(tmp_path, meta, clause) == expected

    @pytest.mark.skipif(shutil.which("jq") is None, reason="The review reconcile block reads the snapshot with jq")
    @pytest.mark.parametrize(
        "clause",
        [
            pytest.param("PR code: run the tests directly; no guard.\n", id="reworded-direct"),
            pytest.param("PR code: tests may run directly in the checkout.\n", id="reworded-permission"),
            pytest.param("Run tests directly.\n", id="no-prefix"),
            pytest.param(_DIRECT + "\n" + _RESOLVE_REFUSAL + "\n", id="direct-line-before-a-refusal"),
            pytest.param(
                "PR code: run every test command only as python -I; then run tests directly.\n",
                id="guard-prefix-granting-more",
            ),
        ],
    )
    def test_a_reworded_direct_clause_is_refused_for_a_fork(self, tmp_path: Path, clause: str) -> None:
        """For a fork every reconcile block keeps only the guard clause or a refusal, so any other wording is replaced.

        The parent is told to write one of three texts, and the check exists for the run where it did not; matching only
        the exact direct-run wording would let a reworded permission through to every consumer.
        """
        for name in ("qa", "step8", "review"):
            (tmp_path / name).mkdir()
        assert TestResolveNoPrClause._run(tmp_path / "qa", "true", clause) == _RESOLVE_REFUSAL + "\n"
        assert TestResolveStepEightReconcile._run(tmp_path / "step8", "true", clause) == _RESOLVE_REFUSAL + "\n"
        assert self._review(tmp_path / "review", '{"isCrossRepository": true}', clause) == _REFUSAL + "\n"


class TestResolveRoutesNameTheClause:
    """Every resolve prompt that can run tests names the clause file, so no route follows its own rule."""

    @pytest.mark.parametrize(
        ("relative", "marker"),
        [
            pytest.param("modes/lint-qa-gate.md", 'name="qa-review"', id="qa-review-agent"),
            pytest.param("modes/lint-qa-gate.md", "2. Fix — apply fixes", id="qa-fix-agent"),
            pytest.param("modes/dispatch-prelude.md", '"Tests: only when', id="c1-brief"),
            pytest.param("modes/dispatch-phase2.md", "GUARD", id="item-agents"),
        ],
    )
    def test_prompt_names_the_clause_file(self, relative: str, marker: str) -> None:
        """The line that carries the prompt text mentions the recorded clause file."""
        hits = [line for line in _lines(_SKILLS / "resolve" / relative) if marker in line]
        assert hits, (relative, marker)
        assert all("pr-code-clause.txt" in line for line in hits)


class TestEarlyAndVerifierRoutesRunNoUnclaused:
    """Routes that run before or beside the recorded decision name the clause or run nothing."""

    def test_review_verifier_prompt_names_the_clause(self) -> None:
        """The Step 4 verifier contract carries the clause rule, since it builds the prompt after Step 2 loaded."""
        line = next(row for row in _lines(_REVIEW / "modes/post-agent.md") if row.startswith("Spawn verifier agent"))
        assert "code-clause.txt" in line
        assert "missing file" in line

    def test_conflict_resolver_runs_no_pr_code(self) -> None:
        """The merge-conflict agent runs before the PR-code decision exists, so its prompt forbids any run."""
        text = (_SKILLS / "resolve/modes/conflict-resolution.md").read_text(encoding="utf-8")
        assert "Read-only for execution: run no tests and no PR code" in text

    def test_qa_lint_names_the_clause(self) -> None:
        """The lint agent follows the clause for checks that load project code."""
        line = next(row for row in _lines(_SKILLS / "resolve/modes/lint-qa-gate.md") if 'name="qa-lint"' in row)
        assert "pr-code-clause.txt" in line


@pytest.mark.skipif(_BASH is None, reason="The reconcile line is a Bash block line")
class TestResolveStepEightReconcile:
    """The clause written at Step 8 is reconciled with the recorded same-repo answer before any spawn."""

    @staticmethod
    def _run(tmp_path: Path, sentinel: str | None, existing: str) -> str:
        """Run the Step 8 reconcile block against a scratch sentinel and clause file; return the clause text."""
        lines = _lines(_SKILLS / "resolve/modes/dispatch-prelude.md")
        anchor = next(i for i, line in enumerate(lines) if line.startswith("Right after writing it"))
        start = next(i for i in range(anchor, len(lines)) if lines[i] == "```bash") + 1
        end = next(i for i in range(start, len(lines)) if lines[i] == "```")
        impl = tmp_path / "impl"
        impl.mkdir()
        (tmp_path / "resolve-impl-dir-s").write_text(str(impl) + "\n", encoding="utf-8", newline="\n")
        if sentinel is not None:
            (tmp_path / "resolve-is-cross-repo-s").write_text(sentinel + "\n", encoding="utf-8", newline="\n")
        (impl / "pr-code-clause.txt").write_text(existing, encoding="utf-8", newline="\n")
        env = {**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "s"}
        script = "\n".join(lines[start:end])
        done = subprocess.run([_BASH, "-c", script], env=env, capture_output=True, text=True, timeout=20, check=False)
        assert done.returncode == 0, done.stderr
        return (impl / "pr-code-clause.txt").read_text(encoding="utf-8")

    @pytest.mark.parametrize("sentinel", ["true", None, "", "maybe"], ids=["fork", "missing", "empty", "unexpected"])
    def test_direct_clause_is_replaced_without_the_same_repo_answer(self, tmp_path: Path, sentinel: str | None) -> None:
        """A direct-run clause written for anything but a recorded same-repo answer becomes the refusal."""
        assert self._run(tmp_path, sentinel, _DIRECT + "\n") == _RESOLVE_REFUSAL + "\n"

    def test_direct_clause_is_kept_for_a_same_repo_pr(self, tmp_path: Path) -> None:
        """The recorded same-repo answer leaves the direct clause alone."""
        assert self._run(tmp_path, "false", _DIRECT + "\n") == _DIRECT + "\n"

    @pytest.mark.parametrize("clause", [_GUARDED + "\n", _RESOLVE_REFUSAL + "\n"], ids=["guarded", "refusal"])
    def test_other_clauses_are_kept(self, tmp_path: Path, clause: str) -> None:
        """Guarded and refusal clauses are the parent's own decision and stay for a fork."""
        assert self._run(tmp_path, "true", clause) == clause


class TestEverySpawnSiteStatesItsExecutionRule:
    """Every spawn site in the review and resolve modes names the clause or forbids execution."""

    _TOKENS: ClassVar[tuple[str, ...]] = (
        "pr-code-clause",
        "code-clause",
        "run no tests",
        "run no pr code",
    )
    # Sites that never reach PR code through their own prompt, each with the reason it is safe.
    _EXEMPT: ClassVar[dict[tuple[str, str], str]] = {
        (
            "resolve/modes/pr-intelligence.md",
            "intel-classify",
        ): "classifier of fetched feedback; its tools run no tests",
        (
            "resolve/modes/comment-dispatch.md",
            "comment-impl",
        ): "reads the comment-task brief, which carries the run text",
        (
            "review/modes/blind-solve.md",
            "review-blind",
        ): "reads the base only through frozen git reads, no checkout",
    }

    @staticmethod
    def _spawn_sites() -> list[tuple[str, str, str]]:
        """Return (relative path, agent name, prompt window) for every line that starts an Agent call."""
        sites: list[tuple[str, str, str]] = []
        for folder in ("review", "resolve"):
            for path in sorted((_SKILLS / folder / "modes").glob("*.md")):
                lines = _lines(path)
                for number, line in enumerate(lines):
                    if not line.startswith("Agent("):
                        continue
                    end = next((i for i in range(number + 1, len(lines)) if lines[i].strip() == "```"), len(lines))
                    name = re.search(r'name="([^"]+)"', line)
                    rel = f"{folder}/modes/{path.name}"
                    sites.append((rel, name.group(1) if name else "?", "\n".join(lines[number:end]).lower()))
        return sites

    def test_every_spawn_site_is_covered_or_exempt(self) -> None:
        """A spawn prompt that names neither the clause nor a no-run rule must be listed with its reason."""
        offenders = [
            f"{rel}:{name}"
            for rel, name, window in self._spawn_sites()
            if (rel, name) not in self._EXEMPT and not any(token in window for token in self._TOKENS)
        ]
        assert offenders == []

    def test_exempt_sites_still_exist(self) -> None:
        """A stale exemption would hide a renamed spawn site, so each listed site must still be found."""
        found = {(rel, name) for rel, name, _ in self._spawn_sites()}
        assert set(self._EXEMPT) <= found

    def test_a_bridge_implement_task_body_that_runs_tests_names_the_clause(self) -> None:
        """A task body written for ``bridge:implement`` is not an ``Agent(`` line, so it is scanned by what it says.

        Any resolve or review mode line telling a task to run its focused verification must read the recorded clause and
        say what happens without one; a line that runs tests directly on its own authority is a bypass route.
        """
        offenders = []
        for folder in ("review", "resolve"):
            for path in sorted((_SKILLS / folder / "modes").glob("*.md")):
                for number, line in enumerate(_lines(path), start=1):
                    lowered = line.lower()
                    if "${focused_verification}" not in lowered:
                        continue
                    if "pr-code-clause" not in lowered or "run no tests" not in lowered:
                        offenders.append(f"{folder}/modes/{path.name}:{number}")
        assert offenders == []

    def test_comment_brief_carries_the_run_text(self) -> None:
        """The comment-impl exemption holds only while the brief it reads says how the fix runs tests."""
        brief = _line_starting(_SKILLS / "resolve/modes/comment-dispatch.md", "Apply the review comment below")
        assert "This run has no PR" in brief
        assert "run directly" in brief or "tests run directly" in brief


class TestCheckoutCodeLoadsAreGated:
    """The clause covers anything that loads checkout code, not only test, probe and overlay runs."""

    def test_review_preamble_names_the_property(self) -> None:
        """The dimension-agent preamble gates type checkers, build backends and exports, not just tests."""
        preamble = _line_starting(_REVIEW / "templates/agent-prompts.md", "**Read-only preamble")
        assert "loads checkout code" in preamble
        assert "type checker" in preamble

    def test_static_analysis_agent_follows_the_clause(self) -> None:
        """Agent 5 runs mypy, which can load a plugin file the PR adds, so it follows the clause."""
        agent = _line_starting(_REVIEW / "templates/agent-prompts.md", "**Agent 5")
        assert "mypy" in agent
        assert "clause" in agent

    def test_review_consolidator_runs_no_pr_code_unclaused(self) -> None:
        """The consolidator prompt names the clause, since its protocol permits bounded real execution."""
        rows = _lines(_REVIEW / "modes/post-agent.md")
        assert any("Run no PR code" in row and "code-clause.txt" in row for row in rows)

    @pytest.mark.parametrize(
        ("name", "anchor"),
        [
            pytest.param("resolve/modes/dispatch-phase2.md", "GUARD:", id="item-agent-guard"),
            pytest.param("resolve/modes/dispatch-prelude.md", '    "Tests: only when', id="c1-brief"),
            pytest.param("resolve/modes/lint-qa-gate.md", "2. Fix", id="qa-fix-prompt"),
        ],
    )
    def test_resolve_prompts_cover_type_checks_and_builds(self, name: str, anchor: str) -> None:
        """Every resolve prompt that gates test runs also gates type checks and builds that load checkout code."""
        lines = [row for row in _lines(_SKILLS / name) if row.lstrip().startswith(anchor.lstrip())]
        assert lines, (name, anchor)
        assert any("type check" in row for row in lines)

    @pytest.mark.parametrize(
        ("name", "anchor"),
        [
            pytest.param("review/modes/post-agent.md", "Spawn verifier agent", id="step-4-verifier"),
            pytest.param("review/modes/agent-launch.md", "Append to the bridge `args`", id="codex-bridge"),
            pytest.param("review/modes/scope-context.md", "**Frozen PR source**", id="frozen-source-rule"),
            pytest.param("review/templates/agent-prompts.md", "**PR-code execution gate", id="gate-and-refusal"),
        ],
    )
    def test_review_execution_texts_name_the_same_property(self, name: str, anchor: str) -> None:
        """Each review text that gates execution names what loads checkout code, not only tests and probes.

        A narrower list lets a verifier or the Codex bridge run a type checker or a dependency export, which loads
        contributor code, after the user answered Do not run.
        """
        line = _line_starting(_SKILLS / name, anchor)
        assert "loads or executes" in line
        assert "type checker" in line
        assert "tests, probes or overlay runs" not in line
        assert "a run of PR code" not in line
        assert "a run that executes PR code" not in line.lower()
