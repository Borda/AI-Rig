"""Run resolve's shipped review-intake blocks against real incomplete review publications.

An incomplete review report used to stop ``/oss:resolve`` with a bare ``incomplete-review-report`` block. Intake now
grades the gaps with ``find_review_report.py --assess`` and hands the user a scoped choice: degraded-only reports stay
consumable, critical ones never do, and the normal gate keeps every other outcome (valid, rejected) unchanged. These
tests execute the exact Bash blocks from ``skills/resolve/SKILL.md`` in a scratch project, so a drifted recipe fails
here rather than in a live run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import merge_action_items as mai
import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/resolve/SKILL.md"
_REPORT_INTELLIGENCE = _PLUGIN / "skills/resolve/modes/report-intelligence.md"
_BASH = shutil.which("bash")
#: The Bash tool runs the login shell, zsh on macOS; PR-scoped intake must behave the same under both.
_ZSH = shutil.which("zsh")
_SESSION = "gap-triage"
_REVISION = "abcdef1234567"


def _bash_block_after(marker: str, source: Path = _SKILL) -> str:
    """Return the shipped Bash block immediately following a marker in a resolve skill file."""
    document = source.read_text(encoding="utf-8")
    start = document.index("```bash", document.index(marker)) + len("```bash\n")
    return document[start : document.index("```", start)]


def _publication(run_dir: Path, outcome: str = "NEEDS_WORK") -> Path:
    """Write a complete, empty version-one review publication with one completed QA assessment."""
    run_dir.mkdir(parents=True)
    report = run_dir / "review-report.md"
    gate = f"REVIEW_V1_REJECT_SCOPE @{_REVISION}" if outcome == "REJECT" else "REVIEW_V1_PASS"
    report.write_text(
        f"---\nTitle: Review\nPR: #7\nReview contract: 1\nRating scale: source-5-v1\n"
        f"Readiness: complete\nGate: {gate}\nOutcome: {outcome}\nSummary: Scoped judgment.\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    rating = {"NEEDS_WORK": 3, "REJECT": 5}[outcome]
    envelope = {
        "contract_version": 1,
        "rating_scale": "source-5-v1",
        "source_outcome": outcome,
        "source_rating": rating,
        "readiness": "complete",
        "readiness_reason": "checked",
        "assessments": [_assessment("QA", outcome, rating)],
        "candidates": [],
        "open_concerns": [],
    }
    _write_envelope(run_dir, envelope, [])
    return report


def _assessment(role: str, outcome: str = "NEEDS_WORK", rating: int = 3, readiness: str = "complete") -> dict:
    """Describe one completed reviewer with the given judgment and readiness."""
    return {
        "role": role,
        "presence": "completed",
        "source_outcome": outcome,
        "source_rating": rating,
        "readiness": readiness,
        "coverage": "src/",
        "rationale": "inspected",
        "evidence": ["src/a.py:1"],
        "limits": [],
    }


def _candidate(identifier: str, disposition: str | None) -> dict:
    """Describe one evidence-bound candidate on the reviewed revision."""
    return {
        "id": identifier,
        "claim": "backend drops the last value",
        "revision": _REVISION,
        "contract": "all input values preserved",
        "trigger": "two values on native backend",
        "counterevidence": [],
        "limits": [],
        "checks": [
            {
                "method": "source inspection",
                "result": "control flow drops the tail",
                "revision": _REVISION,
                "environment": "CPU",
                "limits": [],
            }
        ],
        "disposition": disposition,
        "reason": "traced",
        "feasible_checks_complete": disposition is not None,
        "missing_capability": "native backend hardware",
    }


def _concern(candidate_id: str) -> dict:
    """Carry one open candidate as a durable concern."""
    return {"candidate_id": candidate_id, "owner": "maintainer", "next_check": "native run", "closure": "assert passes"}


def _finding_row(candidate_id: str) -> dict:
    """Publish one confirmed candidate as an actionable findings.jsonl row."""
    return {
        "id": f"f-{candidate_id}",
        "candidate_id": candidate_id,
        "contract_version": 1,
        "validation_status": "CONFIRMED",
        "section": "Architecture & Quality",
        "severity": "medium",
        "title": "backend drops value",
        "change": "code",
        "author": "QA",
    }


def _write_envelope(run_dir: Path, data: dict, findings: list[dict]) -> None:
    """Persist the envelope and its actionable rows beside the report."""
    (run_dir / "review-validation.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
    rows = "".join(json.dumps(row) + "\n" for row in findings)
    (run_dir / "findings.jsonl").write_text(rows, encoding="utf-8", newline="\n")


def _envelope(run_dir: Path) -> dict:
    """Read back the envelope a builder wrote, for mutation."""
    return json.loads((run_dir / "review-validation.json").read_text(encoding="utf-8"))


def _valid(run_dir: Path) -> Path:
    """Publish a consistent report: one confirmed candidate with its finding row."""
    report = _publication(run_dir)
    data = _envelope(run_dir)
    data["candidates"] = [_candidate("V2", "CONFIRMED")]
    _write_envelope(run_dir, data, [_finding_row("V2")])
    return report


def _degraded(run_dir: Path) -> Path:
    """Publish a confirmed finding beside an open concern while the header still claims completion.

    The only broken rule is the readiness label: the confirmed row is intact and the open concern is recorded.
    """
    report = _publication(run_dir)
    data = _envelope(run_dir)
    data["candidates"] = [_candidate("V2", "CONFIRMED"), _candidate("V13", "UNVERIFIED")]
    data["open_concerns"] = [_concern("V13")]
    _write_envelope(run_dir, data, [_finding_row("V2")])
    return report


def _critical(run_dir: Path) -> Path:
    """Reproduce the PR 8 shape: blocked QA under a complete header and a confirmed candidate without its row."""
    report = _publication(run_dir)
    data = _envelope(run_dir)
    data["assessments"] = [
        _assessment("Software engineer", readiness="blocked"),
        _assessment("QA specialist", readiness="blocked"),
    ]
    data["candidates"] = [
        _candidate("V1b", "CONFIRMED"),
        _candidate("V2", "CONFIRMED"),
        _candidate("V13", "UNVERIFIED"),
    ]
    data["open_concerns"] = [_concern("V13")]
    _write_envelope(run_dir, data, [_finding_row("V2")])
    return report


def _rejected(run_dir: Path) -> Path:
    """Publish a complete, consistent premise-level rejection."""
    return _publication(run_dir, "REJECT")


def _rejected_incomplete(run_dir: Path) -> Path:
    """Publish a rejection whose envelope also has a degraded-only gap (a blocked non-security reviewer)."""
    report = _publication(run_dir, "REJECT")
    data = _envelope(run_dir)
    data["assessments"].append(_assessment("Software engineer", "REJECT", 5, readiness="blocked"))
    _write_envelope(run_dir, data, [])
    return report


class _Project:
    """A scratch repository root holding one review run and resolve's session sentinels."""

    def __init__(self, root: Path, build: Callable[[Path], Path], pr_number: str, shell: str | None) -> None:
        self.root = root
        self.tmp = root / "tmp"
        self.tmp.mkdir()
        self.report = build(root / ".reports/review/pr-7/run-001")
        self.shell = shell or _BASH
        self.env = {
            **os.environ,
            "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
            "CLAUDE_CODE_SESSION_ID": _SESSION,
            "TMPDIR": self.tmp.as_posix(),
        }
        self.sentinel("resolve-pr-number").write_text(f"{pr_number}\n", encoding="utf-8", newline="\n")
        self.sentinel("resolve-intake-degraded").write_text("", encoding="utf-8")
        self.sentinel("oss-resolve-active").write_text(f"{pr_number}\n", encoding="utf-8", newline="\n")

    def sentinel(self, name: str) -> Path:
        """Return one session-scoped sentinel path."""
        return self.tmp / f"{name}-{_SESSION}"

    def run(self, block: str) -> subprocess.CompletedProcess:
        """Execute a shipped block from the project root, as the Bash tool would."""
        return subprocess.run(
            [self.shell, "-c", block],
            cwd=self.root,
            env=self.env,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=30,
        )

    def impl_dir(self, github_items: list[dict]) -> Path:
        """Create the run's item directory, as Step 3a/3b would, holding the given GitHub items."""
        impl = self.root / "impl"
        impl.mkdir()
        rows = "".join(json.dumps(item) + "\n" for item in github_items)
        (impl / "action-items.jsonl").write_text(rows, encoding="utf-8", newline="\n")
        self.sentinel("resolve-impl-dir").write_text(f"{impl.as_posix()}\n", encoding="utf-8", newline="\n")
        return impl

    def run_all(self, blocks: tuple[Callable[[], str], ...]) -> list[subprocess.CompletedProcess]:
        """Execute several shipped blocks in order, each in its own fresh shell like separate Bash tool calls."""
        return [self.run(block()) for block in blocks]

    def review_bytes(self) -> dict[str, bytes]:
        """Snapshot every file in the review run directory, to prove intake never self-heals it."""
        run_dir = self.report.parent
        return {path.name: path.read_bytes() for path in sorted(run_dir.iterdir())}

    def published(self) -> str:
        """Return the report path the intake sentinel publishes to later steps."""
        return self.sentinel("resolve-report-file").read_text(encoding="utf-8").strip()


@pytest.fixture(name="project")
def _project_factory(tmp_path: Path) -> Callable[..., _Project]:
    """Build a scratch project for one review shape, PR number and shell."""

    def make(build: Callable[[Path], Path], pr_number: str = "n/a", shell: str | None = None) -> _Project:
        return _Project(tmp_path, build, pr_number, shell)

    return make


def _report_source() -> str:
    """Return the shipped Report source resolution block, the single intake triage."""
    return _bash_block_after("### Report source resolution")


def _intake_proceed() -> str:
    """Return the shipped block run only when the user picks Proceed with valid parts."""
    return _bash_block_after("**Intake proceed** — only on")


_REQUIRES_BASH = pytest.mark.skipif(_BASH is None, reason="resolve intake blocks run in Bash")


@_REQUIRES_BASH
@pytest.mark.parametrize(
    ("build", "pr_number", "status", "critical"),
    [
        pytest.param(_degraded, "n/a", "gaps", 0, id="degraded-report-mode"),
        pytest.param(_degraded, "7", "gaps", 0, id="degraded-pr-mode"),
        pytest.param(_critical, "n/a", "gaps-critical", 2, id="critical-report-mode"),
        pytest.param(_critical, "7", "gaps-critical", 2, id="critical-pr-mode"),
    ],
)
def test_incomplete_report_is_triaged_not_blocked(project, build, pr_number: str, status: str, critical: int) -> None:
    """An incomplete publication reaches the triage question with its graded gaps instead of a bare block.

    Both intake routes are covered: the PR-scoped lookup (``--pr``, pr and pr + report modes) and the newest-report
    lookup of bare report mode (``--report``). The block must print the triage status and the assessment JSON, pause the
    liveness sentinel while the user decides, and leave the review run directory byte-identical.
    """
    workspace = project(build, pr_number)
    before = workspace.review_bytes()
    result = workspace.run(_report_source())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"REPORT_STATUS={status}\n" in result.stdout
    assert "⛔ BLOCKED — incomplete-review-report" in result.stdout
    assessment = json.loads(workspace.sentinel("resolve-intake-gaps").read_text(encoding="utf-8"))
    assert assessment["critical"] == critical
    assert assessment["report"] in result.stdout
    assert workspace.sentinel("oss-resolve-active").read_text(encoding="utf-8") == ""
    assert workspace.review_bytes() == before


@_REQUIRES_BASH
@pytest.mark.parametrize(
    ("build", "published"),
    [
        pytest.param(_degraded, True, id="degraded-publishes-path"),
        pytest.param(_critical, False, id="critical-publishes-nothing"),
    ],
)
def test_only_a_degraded_report_stays_consumable(project, build, published: bool) -> None:
    """The report sentinel names a degraded report, and is empty for a critical one, so no later step can consume it.

    Step 3a and 3c read only that sentinel; an empty one sends them back to this gate instead of to the findings.
    """
    workspace = project(build)
    result = workspace.run(_report_source())
    assert result.returncode == 0, result.stdout + result.stderr
    assert bool(workspace.published()) is published


@_REQUIRES_BASH
@pytest.mark.parametrize("pr_number", ["n/a", "7"])
def test_valid_report_keeps_the_normal_path(project, pr_number: str) -> None:
    """A consistent publication is reused as before and never assessed."""
    workspace = project(_valid, pr_number)
    result = workspace.run(_report_source())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REPORT_STATUS=ok\n" in result.stdout
    assert Path(workspace.published()).name == "review-report.md"
    assert not workspace.sentinel("resolve-intake-gaps").exists()


@_REQUIRES_BASH
@pytest.mark.parametrize(
    ("build", "reason", "published"),
    [
        # The normal gate publishes the path before its rejection check; the stop, not the sentinel, blocks (unchanged).
        pytest.param(_rejected, "⛔ BLOCKED — PR # rejected", True, id="complete-rejection-normal-gate"),
        pytest.param(_rejected_incomplete, "still records a rejection", False, id="incomplete-rejection-withdrawn"),
    ],
)
def test_rejection_stays_blocked(project, build, reason: str, published: bool) -> None:
    """A recorded rejection blocks intake whether or not the rest of its envelope is complete.

    The complete rejection never reaches ``--assess``; the incomplete one is graded, but its degraded-only gaps must not
    turn a premise-level rejection into a Proceed offer, so the path ``--assess`` published is withdrawn again.
    """
    workspace = project(build)
    result = workspace.run(_report_source())
    assert result.returncode == 1
    assert "REPORT_STATUS=blocked\n" in result.stdout
    assert reason in result.stdout
    assert bool(workspace.published()) is published
    assert not workspace.sentinel("oss-resolve-active").exists()


@_REQUIRES_BASH
def test_complete_rejection_is_never_assessed(project) -> None:
    """The normal gate alone decides a complete rejection; the gap assessment does not run at all."""
    workspace = project(_rejected)
    result = workspace.run(_report_source())
    assert '"gaps"' not in result.stdout
    assert not workspace.sentinel("resolve-intake-gaps").exists()


_REQUIRES_ZSH = pytest.mark.skipif(_ZSH is None, reason="zsh not installed")


#: Report mode under zsh guards its newest-report glob with ``unsetopt nomatch``: without it a report lineage holding
#: no file aborts the whole block on zsh NOMATCH, so a published report was never found.
@pytest.mark.parametrize(
    ("shell", "pr_number", "active"),
    [
        pytest.param(_BASH, "n/a", "report", id="bash-report-mode", marks=_REQUIRES_BASH),
        pytest.param(_BASH, "7", "7", id="bash-pr-mode", marks=_REQUIRES_BASH),
        pytest.param(_ZSH, "n/a", "report", id="zsh-report-mode", marks=_REQUIRES_ZSH),
        pytest.param(_ZSH, "7", "7", id="zsh-pr-mode", marks=_REQUIRES_ZSH),
    ],
)
def test_proceed_accepts_a_degraded_report_once(project, shell: str, pr_number: str, active: str) -> None:
    """Proceed records the accepted report, re-arms the liveness sentinel, and a later lookup honours it.

    The round trip is one contract: triage pauses, Proceed accepts, and the next Report source resolution — rerun by
    Step 3a/3c on an empty sentinel or after a compaction — continues as ``ok`` without asking again.
    """
    workspace = project(_degraded, pr_number, shell)
    triage = workspace.run(_report_source())
    proceed = workspace.run(_intake_proceed())
    again = workspace.run(_report_source())
    assert "REPORT_STATUS=gaps\n" in triage.stdout
    assert proceed.returncode == 0, proceed.stdout + proceed.stderr
    assert "REPORT_STATUS=ok\n" in proceed.stdout
    accepted = workspace.sentinel("resolve-intake-degraded").read_text(encoding="utf-8").strip()
    assert accepted == workspace.published()
    assert workspace.sentinel("oss-resolve-active").read_text(encoding="utf-8").strip() == active
    assert again.returncode == 0, again.stdout + again.stderr
    assert "accepted earlier this run" in again.stdout
    assert "REPORT_STATUS=ok\n" in again.stdout


@_REQUIRES_BASH
def test_acceptance_survives_report_mode_learning_its_pr(project) -> None:
    """A report accepted in bare report mode stays accepted once Step 3a publishes the PR from its header.

    Bare report mode accepts the relative globbed path; Step 3a then writes the header's PR number, so any later lookup
    takes the PR-scoped route, which publishes an absolute path to the same file. The user must not be asked again.
    """
    workspace = project(_degraded)
    workspace.run_all((_report_source, _intake_proceed))
    workspace.sentinel("resolve-pr-number").write_text("7\n", encoding="utf-8", newline="\n")
    again = workspace.run(_report_source())
    assert again.returncode == 0, again.stdout + again.stderr
    assert "accepted earlier this run" in again.stdout
    assert "REPORT_STATUS=ok\n" in again.stdout


@_REQUIRES_BASH
def test_proceed_refuses_a_critical_report_named_by_a_stray_sentinel(project) -> None:
    """Proceed re-grades the report, so a sentinel naming a critical report can never be accepted.

    Normal triage empties the sentinel for a critical report; this simulates the sentinel pointing at one anyway (a
    stale write, or Proceed run out of order) and expects the block to refuse rather than trust the earlier answer.
    """
    workspace = project(_critical)
    workspace.sentinel("resolve-report-file").write_text(
        f"{workspace.report.as_posix()}\n", encoding="utf-8", newline="\n"
    )
    result = workspace.run(_intake_proceed())
    assert result.returncode == 1
    assert "! BLOCKED — report has critical gaps" in result.stdout
    assert workspace.sentinel("resolve-intake-degraded").read_text(encoding="utf-8") == ""


@_REQUIRES_BASH
@pytest.mark.parametrize(
    ("blocks", "accepted"),
    [
        pytest.param((_report_source, _intake_proceed), True, id="degraded-intake-reported"),
        pytest.param((_report_source,), False, id="unaccepted-triage-silent"),
    ],
)
def test_final_report_block_carries_the_gap_list(project, blocks: tuple, accepted: bool) -> None:
    """Step 11 prints the degraded-intake marker and the gap JSON from disk, never from the model's recollection.

    Only an accepted Proceed marks the run degraded; a triage the user never accepted leaves the final report silent.
    """
    workspace = project(_degraded)
    outputs = workspace.run_all(blocks)
    result = workspace.run(_bash_block_after("## Step 11: Final report"))
    assert "REPORT_STATUS=gaps\n" in outputs[0].stdout
    assert ("INTAKE=degraded" in result.stdout) is accepted
    assert ('"degraded": 1' in result.stdout) is accepted


@_REQUIRES_BASH
def test_report_source_saves_the_planner_rerun_verdict(project) -> None:
    """Triage gets fill-gaps' own rerun verdict beside the gap assessment, so the prose never re-derives admissibility.

    Which subjects are selectable, required or refused is the planner's decision (its contract tests cover the
    verdicts); this pins that the shipped block saves exactly one verdict line and prints where it lives.
    """
    workspace = project(_degraded)
    result = workspace.run(_report_source())
    assert "REPORT_STATUS=gaps\n" in result.stdout
    assert "RERUN_FILE=" in result.stdout
    verdict = json.loads(workspace.sentinel("resolve-intake-rerun").read_text(encoding="utf-8"))
    assert set(verdict) == {
        "admissible",
        "reason",
        "required_roles",
        "required_candidates",
        "roles",
        "candidates",
        "pr",
    }


def _triage_section() -> str:
    """Return the Gap triage prose, from its heading to the Intake proceed block."""
    text = _SKILL.read_text(encoding="utf-8")
    start = text.index("#### Gap triage")
    return text[start : text.index("**Intake proceed** — only on", start)]


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(
            "Invoke `AskUserQuestion` (actual tool call), one question, `multiSelect: false`", id="one-question"
        ),
        pytest.param("(b) **Rerun review for selected gaps** (Recommended)", id="recommended-second"),
        pytest.param("`gaps-critical` → **never offer Proceed.**", id="critical-never-proceeds"),
        pytest.param("`multiSelect: true`", id="rerun-follow-up-multiselect"),
        pytest.param("each carries 2–4 options and the call at most 4 questions", id="option-cap"),
        pytest.param('--fill-gaps "<report>" --roles "<role,role>" --candidates "<id,id>"', id="fill-gaps-command"),
        pytest.param('Neither rerun invokes `Skill(skill="oss:review", …)`', id="no-nested-review"),
        pytest.param("Never self-heal", id="no-self-heal"),
        pytest.param(
            "offered only when the `RERUN_FILE` JSON says `admissible: true`", id="rerun-only-when-admissible"
        ),
        pytest.param("never re-derive them from the gap table", id="subjects-come-from-the-planner"),
        pytest.param("options `Include <subject>` and `Required only`", id="lone-optional-subject-asked"),
        pytest.param("offer the same options but mark none recommended", id="unchecked-head-not-recommended"),
        pytest.param("the `RERUN_FILE` verdict's `reason` when it is not admissible", id="refusal-reason-in-budget"),
        pytest.param(
            "`required_roles` and `required_candidates` are always in the selection", id="required-always-kept"
        ),
    ],
)
def test_gap_triage_question_contract(marker: str) -> None:
    """The triage prose keeps its single question, its critical-gap restriction and the frozen rerun command form.

    This checks the shipped instruction text, not live model adherence; the executable blocks are covered above.
    """
    assert marker in _triage_section()


def test_reject_gate_routes_incomplete_reports_to_triage() -> None:
    """The every-mode reject gate stops on its own exit 1 except for an incomplete report, which goes to triage."""
    text = _SKILL.read_text(encoding="utf-8")
    gate = text[text.index("### Reject-gate check") : text.index("### Report source resolution")]
    assert "is not yet a stop, in any mode: run **Report source resolution** next" in gate
    assert re.search(r"Every other exit 1 .* stops here", gate)


@pytest.mark.parametrize(
    ("build", "flags", "returncode", "merged"),
    [
        pytest.param(_valid, [], 0, 1, id="valid-needs-no-flag"),
        pytest.param(_degraded, [], 1, 0, id="degraded-refused-without-acceptance"),
        pytest.param(_degraded, ["--accept-degraded"], 0, 1, id="degraded-merged-when-accepted"),
        pytest.param(_critical, ["--accept-degraded"], 1, 0, id="critical-refused-even-when-accepted"),
    ],
)
def test_merge_accepts_a_degraded_publication_only_on_request(
    tmp_path: Path, build, flags: list[str], returncode: int, merged: int
) -> None:
    """Confirmed rows of a degraded publication become items only with ``--accept-degraded``; critical ones never do.

    The merge re-checks publication validity before findings turn into fix work: without the flag it keeps requiring a
    strictly valid publication, so a degraded report the user never accepted cannot slip through a published path
    sentinel. It does not re-check a rejection: the reject gate before it owns that, and a moved head may proceed.
    """
    report = build(tmp_path / "review")
    items = tmp_path / "action-items.jsonl"
    items.write_text("", encoding="utf-8")
    argv = ["--items", str(items), "--findings", str(report.with_name("findings.jsonl")), *flags]
    assert mai.main(argv) == returncode
    assert len(mai.read_jsonl(items)) == merged


@pytest.mark.parametrize(
    ("build", "kept"),
    [
        pytest.param(_valid, 1, id="valid-proof-reusable"),
        pytest.param(_degraded, 0, id="degraded-proof-never-reused"),
    ],
)
def test_verdict_recheck_stays_strict_for_degraded_publications(tmp_path: Path, build, kept: int) -> None:
    """Parent candidate proof from an accepted degraded report is dropped at the re-check, at an unchanged head.

    Only the publication's validity differs between the cases, so the challenge phase tests degraded-intake items in
    full instead of reusing proof from an envelope the strict gate refused.
    """
    report = build(tmp_path / "review")
    (report.parent / "head-sha.txt").write_text(f"{_REVISION}\n", encoding="utf-8", newline="\n")
    items = tmp_path / "impl" / "action-items.jsonl"
    findings = str(report.with_name("findings.jsonl"))
    mai.main(["--items", str(items), "--findings", findings, "--accept-degraded"])
    argv = ["--items", str(items), "--recheck-verdicts", "--review-dir", str(report.parent), "--current-head"]
    assert mai.main([*argv, _REVISION]) == 0
    assert sum("validation_status" in item for item in mai.read_jsonl(items)) == kept


def _step3a_merge() -> str:
    """Return report mode's Step 3a merge block."""
    return _bash_block_after("Pick the findings file, mint ids", _REPORT_INTELLIGENCE)


@_REQUIRES_BASH
@pytest.mark.parametrize(
    ("blocks", "returncode", "items"),
    [
        pytest.param((_report_source, _intake_proceed), 0, 1, id="accepted-report-merges"),
        pytest.param((_report_source,), 1, 0, id="unaccepted-report-refused"),
    ],
)
def test_report_mode_merges_only_an_accepted_degraded_report(
    project, blocks: tuple, returncode: int, items: int
) -> None:
    """Report mode turns the degraded report's confirmed row into an item only after Proceed.

    Triage publishes the report path before the user answers; the Step 3a merge must still refuse it until the intake
    sentinel names that report, so an orchestrator skipping the question cannot consume it.
    """
    workspace = project(_degraded)
    impl = workspace.impl_dir([])
    workspace.run_all(blocks)
    result = workspace.run(_step3a_merge())
    assert result.returncode == returncode, result.stdout + result.stderr
    assert len(mai.read_jsonl(impl / "action-items.jsonl")) == items


@_REQUIRES_BASH
def test_pr_report_mode_merges_an_accepted_degraded_report(project) -> None:
    """The pr + report merge (Step 3c candidates, then links) folds the accepted report's confirmed row in.

    The PR-scoped lookup publishes an absolute report path; acceptance still matches it, and the GitHub item keeps id 1.
    """
    workspace = project(_degraded, "7")
    impl = workspace.impl_dir([{"id": 1, "type": "[gh][req]", "summary": "online request", "status": "pending"}])
    workspace.run_all((_report_source, _intake_proceed))
    candidates = workspace.run(_bash_block_after("Step 1 — choose the findings file"))
    (impl / "report-links.jsonl").write_text("", encoding="utf-8")
    merge = workspace.run(_bash_block_after("Step 2 — for each printed pair"))
    assert candidates.returncode == 0, candidates.stdout + candidates.stderr
    assert merge.returncode == 0, merge.stdout + merge.stderr
    assert [item.get("finding_id") for item in mai.read_jsonl(impl / "action-items.jsonl")] == [None, "f-V2"]
