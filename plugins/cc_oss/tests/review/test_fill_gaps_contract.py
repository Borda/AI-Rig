"""Exercise ``/oss:review --fill-gaps`` through its shipped recipes and the real plan/carry helper.

Every bash recipe here is extracted from the shipped ``SKILL.md`` or ``modes/fill-gaps.md`` and run against a prior run
shaped like the rejected PR 8 publication, so a recipe edit that breaks the contract fails here, not in a live review.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import fill_gaps_scope as fgs
import find_review_report as frr
import mint_finding_ids as mfi
import pytest

_BASH = shutil.which("bash")
_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/review/SKILL.md"
_MODE = _PLUGIN / "skills/review/modes/fill-gaps.md"
_SESSION = "fill-gaps-test"
#: Full PR head the snapshot reports; the prior run recorded only its prefix, so the new run must keep the prefix.
_FULL_HEAD = "abcdef1234567890abcdef1234567890abcdef12"
_PRIOR_HEAD = _FULL_HEAD[:12]
_PRIOR_REPORT = ".reports/review/pr-8/run-001/review-report.md"
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _block(path: Path, anchor: str) -> str:
    """Return the shipped bash block of ``path`` that contains ``anchor``."""
    blocks = re.findall(r"```bash\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL)
    return next(block for block in blocks if anchor in block)


def _run(block: str, cwd: Path, sentinels: dict[str, str], arguments: str = "") -> subprocess.CompletedProcess:
    """Seed session sentinels under ``cwd`` (also the TMPDIR) and run one recipe in a fresh shell."""
    for name, value in sentinels.items():
        (cwd / f"{name}-{_SESSION}").write_text(value + "\n", encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "TMPDIR": cwd.as_posix(),
        "ARGUMENTS": arguments,
    }
    return subprocess.run(
        [_BASH, "-c", block], cwd=cwd, env=env, encoding="utf-8", capture_output=True, check=False, timeout=30
    )


def _sentinel(cwd: Path, name: str) -> str:
    """Read one session sentinel's first line."""
    return (cwd / f"{name}-{_SESSION}").read_text(encoding="utf-8").strip()


def _candidate(identifier: str, disposition: str | None) -> dict:
    """Describe one evidence-bound candidate on the prior run's recorded head."""
    return {
        "id": identifier,
        "claim": f"claim {identifier}",
        "revision": _PRIOR_HEAD,
        "contract": "values preserved",
        "trigger": "two values",
        "counterevidence": ["generic CI"],
        "limits": [],
        "checks": [
            {"method": "inspection", "result": "seen", "revision": _PRIOR_HEAD, "environment": "CPU", "limits": []}
        ],
        "disposition": disposition,
        "reason": "evidence",
        "feasible_checks_complete": disposition is not None,
        "missing_capability": "native backend",
    }


def _assessment(role: str, readiness: str) -> dict:
    """Describe one returned reviewer with a NEEDS_WORK judgment and the given readiness."""
    return {
        "role": role,
        "presence": "completed",
        "source_outcome": "NEEDS_WORK",
        "source_rating": 3,
        "readiness": readiness,
        "coverage": "src/",
        "rationale": "inspected",
        "evidence": ["src/a.py:1"],
        "limits": [],
    }


def _row(candidate_id: str) -> dict:
    """Publish one confirmed candidate as an actionable, merge-ready findings row."""
    return {
        "id": f"f-{candidate_id}",
        "candidate_id": candidate_id,
        "contract_version": 1,
        "validation_status": "CONFIRMED",
        "section": "Architecture & Quality",
        "severity": "medium",
        "title": f"finding {candidate_id}",
        "change": "code",
        "author": "Software engineer",
    }


def _concern(candidate_id: str) -> dict:
    """Carry one open candidate as a durable concern."""
    return {"candidate_id": candidate_id, "owner": "maintainer", "next_check": "native run", "closure": "assert"}


def _write_run(run: Path, data: dict, rows: list[dict]) -> None:
    """Persist an envelope and its actionable rows into a run directory."""
    (run / "review-validation.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
    (run / "findings.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")


def _digest(run: Path) -> dict[str, str]:
    """Hash every file in a run directory, to prove a later step never touched it."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(run.iterdir())}


@pytest.fixture(name="prior")
def _prior(tmp_path: Path) -> Path:
    """Publish a PR 8 shaped run-001: four blocked reviewers, three open concerns, V1b confirmed without its row."""
    run = tmp_path / ".reports/review/pr-8/run-001"
    run.mkdir(parents=True)
    (run / "review-report.md").write_text(
        "---\nTitle: Review\nPR: #8\nReview contract: 1\nRating scale: source-5-v1\nReadiness: complete\n"
        "Gate: REVIEW_V1_PASS\nOutcome: NEEDS_WORK\nSummary: partial.\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    (run / "head-sha.txt").write_text(_PRIOR_HEAD + "\n", encoding="utf-8", newline="\n")
    data = {
        "contract_version": 1,
        "rating_scale": "source-5-v1",
        "source_outcome": "NEEDS_WORK",
        "source_rating": 3,
        "readiness": "complete",
        "readiness_reason": "checked",
        "assessments": [
            _assessment("Software engineer", "blocked"),
            _assessment("QA specialist", "blocked"),
            _assessment("Performance engineer", "blocked"),
            _assessment("Challenger", "blocked"),
        ],
        "candidates": [
            *(_candidate(cid, "UNVERIFIED") for cid in ("V13", "V20", "V21")),
            _candidate("V1b", "CONFIRMED"),
            _candidate("V2", "CONFIRMED"),
        ],
        "open_concerns": [_concern(cid) for cid in ("V13", "V20", "V21")],
    }
    _write_run(run, data, [_row("V2")])
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "pr-meta.json").write_text(json.dumps({"headRefOid": _FULL_HEAD}), encoding="utf-8", newline="\n")
    return run / "review-report.md"


def _gate_sentinels(tmp_path: Path, roles: str, candidates: str, pr: str = "8") -> dict[str, str]:
    """Sentinels Step 0 and the snapshot block leave for the fill-gaps gate."""
    return {
        "oss-review-pr-tag": pr,
        "oss-review-snap-dir": (tmp_path / "snap").as_posix(),
        "oss-review-fill-report": _PRIOR_REPORT,
        "oss-review-fill-roles": roles,
        "oss-review-fill-candidates": candidates,
    }


def _gate() -> str:
    """Return the shipped fill-gaps gate recipe."""
    return _block(_MODE, 'fill_gaps_scope.py" plan')


@_skip_no_bash
@pytest.mark.parametrize(
    ("roles", "candidates", "head", "pr", "expected"),
    [
        pytest.param("Security auditor", "", _FULL_HEAD, "8", "unknown role(s) 'Security auditor'", id="unknown-role"),
        pytest.param("", "V13,V99", _FULL_HEAD, "8", "unknown candidate(s) 'V99'", id="unknown-candidate"),
        pytest.param("QA specialist", "", "fedcba9876543210", "8", "run a full /oss:review 8", id="head-moved"),
        pytest.param("", "", _FULL_HEAD, "8", "needs --roles and/or --candidates", id="nothing-selected"),
        pytest.param("QA specialist", "", _FULL_HEAD, "9", "not a report under .reports/review/pr-9", id="other-pr"),
    ],
)
def test_invalid_selection_blocks_before_allocation(
    prior: Path, tmp_path: Path, roles: str, candidates: str, head: str, pr: str, expected: str
) -> None:
    """An unknown name, moved head, empty selection or foreign run stops the gate with a named reason.

    The gate runs before Step 2 allocates a run directory: an allocated but unpublished run would become the PR's
    newest report and block ``/oss:resolve``. So a refusal must leave no plan, no head sentinel and no new run dir.
    """
    (tmp_path / "snap/pr-meta.json").write_text(json.dumps({"headRefOid": head}), encoding="utf-8")
    before = _digest(prior.parent)
    result = _run(_gate(), tmp_path, _gate_sentinels(tmp_path, roles, candidates, pr))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "! BLOCKED" in result.stdout
    assert expected in result.stdout
    assert (_sentinel(tmp_path, "oss-review-fill-plan"), _sentinel(tmp_path, "oss-review-fill-head")) == ("", "")
    assert sorted(p.name for p in prior.parent.parent.iterdir()) == ["run-001"]
    assert _digest(prior.parent) == before


@_skip_no_bash
def test_valid_selection_plans_then_allocates_a_new_run(prior: Path, tmp_path: Path) -> None:
    """A known role and candidates at an unchanged head print the cost line and allocate run-002 beside run-001.

    The new run must keep the prior ``head-sha.txt`` string, not the snapshot's full SHA: the publication gate compares
    carried candidate revisions to it by exact equality. Run-001 stays byte-identical throughout.
    """
    before = _digest(prior.parent)
    gate = _run(_gate(), tmp_path, _gate_sentinels(tmp_path, "QA specialist", "V13,V1b"))
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert "re-run: QA specialist -> review-qa (foundry:qa-specialist)" in gate.stdout
    assert "cost: 2 agent spawn(s) x ~120,851 tok = ~241,702 tok" in gate.stdout
    plan = json.loads((tmp_path / f"oss-review-fill-plan-{_SESSION}").read_text(encoding="utf-8"))
    assert (plan["candidates"], plan["publish_rows"]) == (["V13"], ["V1b"])
    assert plan["carried"] == {
        "assessments": ["Software engineer", "Performance engineer", "Challenger"],
        "candidates": ["V20", "V21", "V1b", "V2"],
        "open_concerns": ["V20", "V21"],
        "findings": ["f-V2"],
    }
    allocation = _run(_block(_SKILL, "next_run_dir.py"), tmp_path, {})
    assert allocation.returncode == 0, allocation.stdout + allocation.stderr
    new_run = Path(_sentinel(tmp_path, "oss-review-report-dir"))
    assert new_run == tmp_path / ".reports/review/pr-8/run-002"
    assert (new_run / "head-sha.txt").read_text(encoding="utf-8").strip() == _PRIOR_HEAD
    assert json.loads((new_run / "fill-gaps.json").read_text(encoding="utf-8")) == plan
    assert _digest(prior.parent) == before


@_skip_no_bash
def test_plain_review_allocation_ignores_a_reset_plan(tmp_path: Path) -> None:
    """Without a fill-gaps plan the allocation records the snapshot head and copies nothing.

    Step 0 empties the plan and head sentinels on every run; the allocation block must then behave exactly as before the
    fill-gaps mode existed.
    """
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "pr-meta.json").write_text(json.dumps({"headRefOid": _FULL_HEAD}), encoding="utf-8")
    sentinels = {"oss-review-pr-tag": "8", "oss-review-snap-dir": snap.as_posix(), "oss-review-fill-head": ""}
    result = _run(_block(_SKILL, "next_run_dir.py"), tmp_path, sentinels)
    assert result.returncode == 0, result.stdout + result.stderr
    new_run = Path(_sentinel(tmp_path, "oss-review-report-dir"))
    assert (new_run / "head-sha.txt").read_text(encoding="utf-8").strip() == _FULL_HEAD
    assert not (new_run / "fill-gaps.json").exists()


@_skip_no_bash
def test_fill_gaps_allocation_without_a_plan_refuses(prior: Path, tmp_path: Path) -> None:
    """A fill-gaps run whose gate never wrote the prior head allocates no report directory.

    Regression: with ``--fill-gaps`` set but the head sentinel empty (gate skipped or its output lost), the allocation
    fell through to a plain run at the current head with no ``fill-gaps.json``; that run became the PR's newest report
    and the carry check then burned the consolidator re-spawn on "not a fill-gaps run".
    """
    snap_dir = (tmp_path / "snap").as_posix()
    sentinels = {
        "oss-review-pr-tag": "8",
        "oss-review-snap-dir": snap_dir,
        "oss-review-fill-report": _PRIOR_REPORT,
        "oss-review-fill-head": "",
    }
    result = _run(_block(_SKILL, "next_run_dir.py"), tmp_path, sentinels)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "! BLOCKED — --fill-gaps has no plan (fill-gaps gate not passed); refusing to allocate a report dir" in (
        result.stdout
    )
    assert sorted(p.name for p in prior.parent.parent.iterdir()) == ["run-001"]
    assert not (tmp_path / f"oss-review-report-dir-{_SESSION}").exists()


def _step0() -> str:
    """Return the shipped Step 0 flag-parsing recipe."""
    return _block(_SKILL, "--value-flags fill-gaps,roles,candidates")


@_skip_no_bash
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param(
            f'8 --fill-gaps {_PRIOR_REPORT} --roles "Software engineer,QA specialist" --candidates V13',
            {"pr": "8", "roles": "Software engineer,QA specialist", "candidates": "V13", "mode": "true"},
            id="full-selection",
        ),
        pytest.param("8", {"pr": "8", "roles": "", "candidates": "", "mode": "false"}, id="plain-review-resets"),
    ],
)
def test_step0_parses_fill_gaps_values(tmp_path: Path, arguments: str, expected: dict) -> None:
    """Step 0 extracts the three value flags and keeps the PR number clean.

    Role names carry spaces, so they arrive quoted and must survive as one comma-separated value. A plain review in the
    same session must rewrite every fill-gaps sentinel, so a crashed fill-gaps run cannot leak its plan into it.
    """
    (tmp_path / f"oss-review-fill-plan-{_SESSION}").write_text("stale plan\n", encoding="utf-8")
    result = _run(_step0(), tmp_path, {}, arguments)
    # The block's last line is `[ DIRECT_PATH_MODE = true ] && echo …`, so its status is 1 on every PR run;
    # completion is proven by the DIRECT_PATH_MODE line it appends last.
    assert "! BLOCKED" not in result.stdout, result.stdout + result.stderr
    flags = (tmp_path / f"oss-review-flags-{_SESSION}").read_text(encoding="utf-8")
    assert "DIRECT_PATH_MODE=false" in flags
    observed = {
        "pr": _sentinel(tmp_path, "oss-review-pr-tag"),
        "roles": _sentinel(tmp_path, "oss-review-fill-roles"),
        "candidates": _sentinel(tmp_path, "oss-review-fill-candidates"),
        "mode": re.search(r"^FILL_GAPS_MODE=(\w+)$", flags, re.MULTILINE).group(1),
    }
    assert observed == expected
    assert _sentinel(tmp_path, "oss-review-fill-plan") == ""


@_skip_no_bash
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param("8 --roles QA", "need --fill-gaps", id="roles-without-fill-gaps"),
        pytest.param(f"8 --fill-gaps {_PRIOR_REPORT} --full", "cannot combine", id="fill-gaps-with-full"),
        pytest.param(f"8 --fill-gaps {_PRIOR_REPORT} --reply", "cannot combine", id="fill-gaps-with-reply"),
    ],
)
def test_step0_refuses_incoherent_fill_gaps_flags(tmp_path: Path, arguments: str, expected: str) -> None:
    """Scope flags without a run to fill, or fill-gaps mixed with a widening or reply mode, stop at Step 0.

    ``--full`` widens a fan-out that fill-gaps pins exactly, and ``--reply`` skips the review entirely; either pairing
    has no coherent meaning, so silently picking one would run something the caller did not ask for.
    """
    result = _run(_step0(), tmp_path, {}, arguments)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "! BLOCKED" in result.stdout
    assert expected in result.stdout


@_skip_no_bash
def test_existing_report_guard_yields_to_fill_gaps(tmp_path: Path) -> None:
    """Under fill-gaps the reuse question never arises and the gate's resolution-ledger sentinel survives.

    A prior report exists by construction in fill-gaps mode; the guard would otherwise ask reuse/re-run and overwrite
    the ledger path the fill-gaps gate already recorded from the selected run.
    """
    (tmp_path / f"oss-review-flags-{_SESSION}").write_text(
        "DIRECT_PATH_MODE=false\nFILL_GAPS_MODE=true\n", encoding="utf-8"
    )
    sentinels = {"oss-review-pr-tag": "8", "oss-review-prior-resolution": "kept/resolution.jsonl"}
    result = _run(_block(_SKILL, "oss-review-prior-report-${CSID}"), tmp_path, sentinels)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "PRIOR_REPORT="
    assert _sentinel(tmp_path, "oss-review-prior-resolution") == "kept/resolution.jsonl"


def _publish_fill_run(prior: Path, tmp_path: Path) -> Path:
    """Plan a QA + V13/V1b fill and publish its merged run-002 the way the consolidator contract asks.

    The merge is honest: the carried blocked reviewers and open candidates make the recomputed aggregate ``blocked``,
    under a full version-one header and the prior run's head string, so the publication gate can accept it too.
    """
    plan = fgs.build_plan(fgs.PlanRequest("8", prior, ["QA specialist"], ["V13", "V1b"], _FULL_HEAD))
    run = prior.parent.with_name("run-002")
    run.mkdir()
    (run / fgs.PLAN_NAME).write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    (run / "review-report.md").write_text(
        "---\nTitle: Review\nPR: #8\nReview contract: 1\nRating scale: source-5-v1\nReadiness: blocked\n"
        "Gate: REVIEW_V1_BLOCK\nOutcome: NEEDS_WORK\nSummary: fill-gaps run.\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    (run / "head-sha.txt").write_text(_PRIOR_HEAD + "\n", encoding="utf-8", newline="\n")
    data = json.loads((prior.parent / "review-validation.json").read_text(encoding="utf-8"))
    data["readiness"] = "blocked"
    data["readiness_reason"] = "three reviewers blocked; V13, V20 and V21 stay open"
    data["assessments"][1] = _assessment("QA specialist", "complete")
    data["candidates"][0]["reason"] = "re-validated: still needs native backend"
    _write_run(run, data, [_row("V2"), _row("V1b")])
    return run / "review-report.md"


def test_honest_fill_gaps_merge_passes_carry_check_and_publication_gate(prior: Path, tmp_path: Path) -> None:
    """A verbatim merge with recomputed aggregate labels passes both the carry check and the producer publication gate.

    The carry check alone proves nothing about publishability: a merge that keeps every carried record can still be
    refused by the gate (wrong header, ``complete`` readiness over blocked reviewers, a carried row resolve cannot
    merge). ``oss:review`` holds its fresh fill-gaps run to the producer gate, so both gates on one merge prove the
    fill-gaps contract is satisfiable end to end.
    """
    report = _publish_fill_run(prior, tmp_path)
    assert fgs.main(["verify", "--report", str(report)]) == 0
    assert frr.publication_verdict(report, producer=True) == ("Gate: BLOCK", "")


def _alter_carried_candidate(run: Path) -> None:
    """Reword a carried candidate's claim."""
    data = json.loads((run / "review-validation.json").read_text(encoding="utf-8"))
    data["candidates"][1]["claim"] = "reworded"
    _write_run(run, data, [json.loads(line) for line in (run / "findings.jsonl").read_text().splitlines()])


def _drop_rerun_role(run: Path) -> None:
    """Lose the re-run QA assessment entirely."""
    data = json.loads((run / "review-validation.json").read_text(encoding="utf-8"))
    data["assessments"] = [a for a in data["assessments"] if a["role"] != "QA specialist"]
    _write_run(run, data, [json.loads(line) for line in (run / "findings.jsonl").read_text().splitlines()])


def _drop_carried_row(run: Path) -> None:
    """Lose the carried V2 findings row."""
    (run / "findings.jsonl").write_text(json.dumps(_row("V1b")) + "\n", encoding="utf-8", newline="\n")


def _drop_published_row(run: Path) -> None:
    """Carry V1b again without publishing the findings row the fill-gaps run exists to add."""
    (run / "findings.jsonl").write_text(json.dumps(_row("V2")) + "\n", encoding="utf-8", newline="\n")


def _drop_revalidated_candidate(run: Path) -> None:
    """Lose re-validated V13 instead of recording its new disposition."""
    data = json.loads((run / "review-validation.json").read_text(encoding="utf-8"))
    data["candidates"] = [c for c in data["candidates"] if c["id"] != "V13"]
    _write_run(run, data, [json.loads(line) for line in (run / "findings.jsonl").read_text().splitlines()])


@_skip_no_bash
@pytest.mark.parametrize(
    ("mutate", "returncode", "expected"),
    [
        pytest.param(None, 0, "[carry] intact", id="carried-verbatim-plus-additions"),
        pytest.param(_alter_carried_candidate, 1, "carried candidate 'V20' altered or dropped", id="altered-carry"),
        pytest.param(_drop_rerun_role, 1, "re-run role 'QA specialist' has no assessment", id="dropped-role"),
        pytest.param(_drop_carried_row, 1, "carried finding row 'f-V2' altered or dropped", id="dropped-row"),
        pytest.param(_drop_revalidated_candidate, 1, "re-validated candidate 'V13' dropped", id="dropped-candidate"),
        pytest.param(_drop_published_row, 1, "finding-missing candidate 'V1b' still has no", id="row-not-published"),
    ],
)
def test_carry_check_proves_untouched_records(prior: Path, tmp_path: Path, mutate, returncode: int, expected: str):
    """The shipped carry check passes a verbatim merge with additions and names every altered or dropped record.

    The consolidator merges carried prior records by instruction only; this deterministic check is what keeps a fill-
    gaps run from silently rewriting evidence it was told to leave alone. V1b gaining its missing row is an addition and
    must pass.
    """
    report = _publish_fill_run(prior, tmp_path)
    (mutate or (lambda _run: None))(report.parent)
    sentinels = {"oss-review-report-dir": report.parent.as_posix()}
    result = _run(_block(_MODE, 'fill_gaps_scope.py" verify'), tmp_path, sentinels)
    assert result.returncode == returncode, result.stdout + result.stderr
    assert expected in result.stdout


def _new_sidecar_is_a_list(run: Path) -> None:
    """Publish a new review-validation.json that is a JSON list, not an envelope."""
    (run / "review-validation.json").write_text("[]\n", encoding="utf-8", newline="\n")


def _plan_is_a_list(run: Path) -> None:
    """Replace fill-gaps.json with a JSON list."""
    (run / fgs.PLAN_NAME).write_text("[]\n", encoding="utf-8", newline="\n")


def _new_assessments_not_a_list(run: Path) -> None:
    """Publish the new assessments as a mapping and add a non-object findings row."""
    data = json.loads((run / "review-validation.json").read_text(encoding="utf-8"))
    data["assessments"] = {"QA specialist": data["assessments"][1]}
    _write_run(run, data, [*(json.loads(line) for line in (run / "findings.jsonl").read_text().splitlines()), 7])


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        pytest.param(_new_sidecar_is_a_list, "new review-validation.json is not a JSON object", id="sidecar-list"),
        pytest.param(_plan_is_a_list, "prior or new sidecar unreadable (TypeError", id="plan-list"),
        pytest.param(_new_assessments_not_a_list, "carried assessment 'Software engineer' altered", id="records-map"),
    ],
)
def test_carry_check_reports_malformed_sidecars_without_raising(
    prior: Path, tmp_path: Path, mutate, expected: str
) -> None:
    """A malformed plan or new sidecar becomes a ``[carry]`` problem line, never a traceback.

    A traceback here reached the carry block as a bare exit 1 with no problem line, so the consolidator re-spawn was
    told to fix nothing.
    """
    report = _publish_fill_run(prior, tmp_path)
    mutate(report.parent)
    assert expected in "\n".join(fgs.verify_carry(report))


def test_minting_with_carry_from_keeps_carried_row_ids(prior: Path, tmp_path: Path) -> None:
    """A published row colliding with a carried row is minted around it, so the carry check still passes.

    Both rows share section, file and title; the published one's text outranks the carried one's, which used to push the
    carried row to the ``-2`` suffix and fail the carry check that its row stay verbatim.
    """
    row = {**_row("V2"), "file": "src/a.py", "full_text": "alpha"}
    prior_rows = prior.parent / "findings.jsonl"
    _write_run(prior.parent, json.loads((prior.parent / "review-validation.json").read_text()), [row])
    assert mfi.main([str(prior_rows)]) == 0
    report = _publish_fill_run(prior, tmp_path)
    carried = mfi.read_jsonl(prior_rows)[0]
    _write_run(report.parent, json.loads(report.with_name("review-validation.json").read_text()), [carried])
    with report.with_name("findings.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({**row, "candidate_id": "V1b", "full_text": "beta"}) + "\n")
    assert mfi.main([str(report.with_name("findings.jsonl")), "--carry-from", str(prior_rows)]) == 0
    assert fgs.verify_carry(report) == []


@_skip_no_bash
def test_finding_missing_candidate_only_publishes_its_row(prior: Path, tmp_path: Path) -> None:
    """Naming a CONFIRMED candidate that lost its findings row carries it and schedules only the row, no re-validation.

    PR 8's V1b was confirmed by the parent on the reviewed head, but its actionable row never reached findings.jsonl —
    the validator's critical ``finding-missing`` gap. The proof is intact, so the run spends no verifier and no
    dimension agent: one consolidator spawn publishes the row while the candidate record stays verbatim.
    """
    gate = _run(_gate(), tmp_path, _gate_sentinels(tmp_path, "", "V1b"))
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert "re-validate: none" in gate.stdout
    assert "publish missing findings row (CONFIRMED, no re-validation): V1b" in gate.stdout
    assert "cost: 1 agent spawn(s)" in gate.stdout
    plan = json.loads((tmp_path / f"oss-review-fill-plan-{_SESSION}").read_text(encoding="utf-8"))
    assert (plan["candidates"], plan["publish_rows"], plan["units"]) == ([], ["V1b"], [])
    assert "V1b" in plan["carried"]["candidates"]


def test_stale_confirmed_candidate_is_revalidated_not_published(prior: Path) -> None:
    """A CONFIRMED candidate lacking its row but bound to another revision needs fresh proof, not just its row.

    Publishing a row on stale evidence would turn an outdated confirmation into an action item; only a confirmation on
    the reviewed head may skip re-validation.
    """
    data = json.loads((prior.parent / "review-validation.json").read_text(encoding="utf-8"))
    data["candidates"][3]["revision"] = "0123456789ab"
    (prior.parent / "review-validation.json").write_text(json.dumps(data), encoding="utf-8")
    plan = fgs.build_plan(fgs.PlanRequest("8", prior, [], ["V1b"], _FULL_HEAD))
    assert (plan["candidates"], plan["publish_rows"]) == (["V1b"], [])


def test_stale_carried_candidate_blocks_unless_selected(prior: Path) -> None:
    """A carried candidate bound to another revision cannot be carried; naming it for re-validation is the way out.

    The publication gate compares every candidate revision to the new run's head exactly, so carrying stale evidence
    would publish a run the gate rejects — the plan refuses it up front instead.
    """
    data = json.loads((prior.parent / "review-validation.json").read_text(encoding="utf-8"))
    data["candidates"][1]["revision"] = "0123456789ab"
    (prior.parent / "review-validation.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(fgs.FillGapsBlocked, match="V20"):
        fgs.build_plan(fgs.PlanRequest("8", prior, ["QA specialist"], [], _FULL_HEAD))
    # V1b is named too: carried without its findings row it would itself break the publication gate.
    plan = fgs.build_plan(fgs.PlanRequest("8", prior, [], ["V20", "V1b"], _FULL_HEAD))
    assert (plan["candidates"], plan["publish_rows"]) == (["V20"], ["V1b"])


def _mutate_prior(prior: Path, mutate) -> None:
    """Rewrite the prior run's envelope through one damaging mutation."""
    sidecar = prior.parent / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    mutate(data)
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")


def _check_without_environment(data: dict) -> None:
    """Strip the environment from carried V2's only check, as in the probe that published a doomed run."""
    data["candidates"][4]["checks"][0].pop("environment")


def _unbacked_software_engineer(data: dict) -> None:
    """Leave the Software engineer assessment without evidence."""
    data["assessments"][0]["evidence"] = []


def _v20_without_capability(data: dict) -> None:
    """Drop the missing-capability note from open V20, a record field the validator grades only as a label slip."""
    data["candidates"][1]["missing_capability"] = ""


def _v20_concern_without_owner(data: dict) -> None:
    """Leave open V20's concern without an owner, which the validator grades only as bookkeeping."""
    data["open_concerns"][1]["owner"] = ""


def _concern_for_confirmed_v2(data: dict) -> None:
    """File an open concern against CONFIRMED V2, which the gate rejects as not matching the open candidates."""
    data["open_concerns"].append({**data["open_concerns"][0], "candidate_id": "V2"})


def _concern_for_unknown_candidate(data: dict) -> None:
    """File an open concern against an id that is no candidate of the run."""
    data["open_concerns"].append({**data["open_concerns"][0], "candidate_id": "V99"})


def _duplicate_v20_concern(data: dict) -> None:
    """File a second, differently owned concern for open V20; the validator grades it only as bookkeeping."""
    data["open_concerns"].append({**_concern("V20"), "owner": "performance"})


def _null_aggregate_missing_engineer(data: dict) -> None:
    """Leave a null aggregate whose only completed reviewer is undecidable while the Software engineer never returned.

    The missing reviewer makes the aggregate ``incomplete``, which outranks the ``blocked`` label a null aggregate
    otherwise needs, so both assessments publish verbatim under ``readiness: incomplete`` without any re-run reviewer.
    """
    data.update(source_outcome=None, source_rating=None, readiness="blocked")
    data["assessments"] = [
        {**_assessment("Performance engineer", "blocked"), "source_outcome": None, "source_rating": None},
        {
            **_assessment("Software engineer", "incomplete"),
            "presence": "missing",
            "source_outcome": None,
            "source_rating": None,
            "rationale": "",
            "evidence": [],
        },
    ]
    data["candidates"] = [record for record in data["candidates"] if record["id"] in {"V1b", "V2"}]
    data["open_concerns"] = []


def _null_aggregate_all_reviewers_missing(data: dict) -> None:
    """Leave a null aggregate whose two reviewers both never returned, so no carried assessment supports any outcome.

    A null aggregate needs a completed undecidable reviewer and a judged one needs a reviewer that judged; a missing
    reviewer is neither, so only a re-run reviewer, either one, can supply the aggregate outcome.
    """
    data.update(source_outcome=None, source_rating=None, readiness="incomplete")
    missing = {"presence": "missing", "source_outcome": None, "source_rating": None, "rationale": "", "evidence": []}
    data["assessments"] = [
        {**_assessment("Performance engineer", "incomplete"), **missing},
        {**_assessment("Software engineer", "incomplete"), **missing},
    ]
    data["candidates"] = [record for record in data["candidates"] if record["id"] in {"V1b", "V2"}]
    data["open_concerns"] = []


def _null_aggregate_over_judged_reviewers(data: dict) -> None:
    """Label the aggregate outcome null although every reviewer judged NEEDS_WORK: an aggregate label slip only.

    The new consolidator recomputes the aggregate outcome over the merged assessments, so the carried judgments let it
    write a judged outcome without re-running any reviewer.
    """
    data.update(source_outcome=None, source_rating=None, readiness="blocked")


def _v1b_check_without_environment(data: dict) -> None:
    """Break the only check of rowless CONFIRMED V1b, so its record cannot be carried verbatim."""
    data["candidates"][3]["checks"][0].pop("environment")


def _concern_for_confirmed_v1b(data: dict) -> None:
    """File an open concern against rowless CONFIRMED V1b, which a carried copy could never drop."""
    data["open_concerns"].append(_concern("V1b"))


@pytest.mark.parametrize(
    ("mutate", "roles", "candidates", "expected"),
    [
        pytest.param(
            _check_without_environment,
            ["QA specialist"],
            ["V13", "V1b"],
            "carried record(s) V2 fail candidate 'V2' has an incomplete check",
            id="carried-invalid-candidate",
        ),
        pytest.param(
            None,
            ["QA specialist"],
            ["V13"],
            "carried record(s) V1b fail CONFIRMED candidate(s) without findings.jsonl row: V1b",
            id="carried-rowless-confirmed",
        ),
        pytest.param(
            _unbacked_software_engineer,
            ["QA specialist"],
            ["V1b"],
            "carried record(s) Software engineer fail completed assessment 'Software engineer' lacks coverage",
            id="carried-unbacked-assessment",
        ),
        pytest.param(
            _v20_without_capability,
            ["QA specialist"],
            ["V13", "V1b"],
            "carried record(s) V20 fail UNVERIFIED candidate 'V20' lacks missing_capability",
            id="carried-unverified-without-capability",
        ),
        pytest.param(
            _v20_concern_without_owner,
            ["QA specialist"],
            ["V13", "V1b"],
            "carried record(s) V20 fail open concern lacks candidate_id, owner, next_check or closure",
            id="carried-incomplete-concern",
        ),
        pytest.param(
            _concern_for_confirmed_v2,
            ["QA specialist"],
            ["V13", "V1b"],
            "carried record(s) V2 fail open_concerns ['V2', 'V20', 'V21'] must match open candidates",
            id="carried-concern-on-confirmed",
        ),
        pytest.param(
            _concern_for_unknown_candidate,
            ["QA specialist"],
            ["V13", "V1b"],
            "carried records cannot pass the publication gate (concern-unknown-candidate V99); run a full",
            id="concern-for-unknown-candidate",
        ),
        pytest.param(
            _duplicate_v20_concern,
            ["QA specialist"],
            ["V13", "V1b"],
            "carried record(s) V20 fail open_concerns ['V20', 'V20', 'V21'] must match open candidates",
            id="carried-duplicate-concern",
        ),
        pytest.param(
            _null_aggregate_all_reviewers_missing,
            [],
            ["V1b"],
            "carried record(s) Software engineer fail no carried assessment supports an aggregate outcome",
            id="no-reviewer-supports-an-aggregate-outcome",
        ),
    ],
)
def test_carried_record_the_gate_rejects_blocks_the_plan(
    prior: Path, mutate, roles: list[str], candidates: list[str], expected: str
) -> None:
    """A record the plan would copy verbatim but the publication gate rejects refuses the plan before any spawn.

    Otherwise the consolidator copies it, the publication gate refuses the new run, the re-spawn is told both to keep it
    verbatim and to correct it, and the dead run becomes the PR's newest report. The refusal names the carried record
    whose addition to the request repairs it, with the publication gate's own reason; a concern naming no candidate of
    the run is repaired by no addition, so it sends the caller to a full review instead.
    """
    _mutate_prior(prior, mutate or (lambda _data: None))
    with pytest.raises(fgs.FillGapsBlocked, match=re.escape(expected)):
        fgs.build_plan(fgs.PlanRequest("8", prior, roles, candidates, _FULL_HEAD))


@pytest.mark.parametrize(
    ("mutate", "roles", "candidates"),
    [
        pytest.param(None, ["QA specialist"], ["V13", "V1b"], id="pr8-shape-coverage-and-labels-only"),
        pytest.param(_check_without_environment, ["QA specialist"], ["V1b", "V2"], id="invalid-candidate-revalidated"),
        pytest.param(_unbacked_software_engineer, ["Software engineer"], ["V1b"], id="unbacked-assessment-rerun"),
        pytest.param(
            _v20_without_capability, ["QA specialist"], ["V1b", "V20"], id="unverified-without-capability-rerun"
        ),
        pytest.param(
            _v20_concern_without_owner, ["QA specialist"], ["V1b", "V20"], id="incomplete-concern-revalidated"
        ),
        pytest.param(_concern_for_confirmed_v2, ["QA specialist"], ["V1b", "V2"], id="extra-concern-revalidated"),
        pytest.param(
            _null_aggregate_missing_engineer,
            ["Software engineer"],
            ["V1b"],
            id="missing-reviewer-rerun-supplies-judgment",
        ),
        pytest.param(_null_aggregate_missing_engineer, [], ["V1b"], id="null-aggregate-missing-reviewer-incomplete"),
        pytest.param(_null_aggregate_over_judged_reviewers, [], ["V1b"], id="null-aggregate-label-slip-recomputed"),
    ],
)
def test_named_or_recomputed_gaps_still_plan(prior: Path, mutate, roles: list[str], candidates: list[str]) -> None:
    """Gaps the new run repairs never refuse: named records, coverage, readiness labels and concern bookkeeping.

    The PR 8 shape keeps blocked reviewers (critical for QA), open candidates under a wrong ``complete`` label and a
    rowless confirmed candidate; naming QA and V1b, or the broken record itself, is exactly what fill-gaps is for. A
    null aggregate over a missing reviewer plans with or without that reviewer re-run: a missing reviewer makes the
    aggregate ``incomplete``, which outranks ``blocked``. A null aggregate over reviewers who all judged is a label the
    new consolidator recomputes from the carried judgments, so it needs no re-run reviewer either.
    """
    _mutate_prior(prior, mutate or (lambda _data: None))
    plan = fgs.build_plan(fgs.PlanRequest("8", prior, roles, candidates, _FULL_HEAD))
    assert plan["roles"] == roles


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_v1b_check_without_environment, id="record-breaks-a-rule"),
        pytest.param(_concern_for_confirmed_v1b, id="record-has-an-open-concern"),
    ],
)
def test_rowless_record_unfit_to_carry_is_revalidated(prior: Path, mutate) -> None:
    """A named rowless CONFIRMED candidate whose record cannot be carried verbatim is re-validated, not row-published.

    Publishing only its row would carry the broken record or its stray concern unchanged, and the plan would then refuse
    the very candidate the caller already named; re-validation replaces the record instead.
    """
    _mutate_prior(prior, mutate)
    plan = fgs.build_plan(fgs.PlanRequest("8", prior, ["QA specialist"], ["V13", "V1b"], _FULL_HEAD))
    assert (plan["candidates"], plan["publish_rows"]) == (["V13", "V1b"], [])


def _write_rows(prior: Path, rows: list) -> None:
    """Replace the prior run's findings rows."""
    (prior.parent / "findings.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _without(row: dict, *keys: str) -> dict:
    """Copy a findings row without the given fields."""
    return {key: value for key, value in row.items() if key not in keys}


@pytest.mark.parametrize(
    ("rows", "candidates", "expected"),
    [
        pytest.param(
            [_without(_row("V2"), "change", "author")],
            ["V13", "V1b"],
            "carried record(s) V2 fail findings.jsonl finding 1 missing change, author (candidate 'V2'; resolve's merge",
            id="pre-producer-row",
        ),
        pytest.param(
            [_without(_row("V2"), "id")],
            ["V13", "V1b"],
            "carried record(s) V2 fail findings.jsonl finding 1 missing id (candidate 'V2'; resolve's merge",
            id="id-less-row-mint-would-re-mint",
        ),
        pytest.param(
            [{**_row("V2"), "id": 7}],
            ["V13", "V1b"],
            "carried record(s) V2 fail findings.jsonl finding 1 field id is int, not text (candidate 'V2';",
            id="non-string-id",
        ),
        pytest.param(
            [{**_row("V2"), "severity": "major"}],
            ["V13", "V1b"],
            "carried record(s) V2 fail findings.jsonl finding 1 has unknown severity 'major' (candidate 'V2';",
            id="unknown-severity",
        ),
        pytest.param(
            [_row("V2"), {**_row("V1b"), "id": "f-V2"}],
            ["V13"],
            "carried record(s) V2 fail findings.jsonl findings 1 and 2 share id 'f-V2' (candidate 'V2', 'V1b';",
            id="shared-id",
        ),
    ],
)
def test_carried_row_the_producer_gate_rejects_blocks_until_its_candidate_is_named(
    prior: Path, rows: list, candidates: list[str], expected: str
) -> None:
    """A carried row resolve's merge cannot use refuses the plan, naming the candidate whose re-validation replaces it.

    The fill-gaps run must carry the row verbatim, and ``oss:review``'s producer gate refuses a run publishing a row the
    merge cannot use: a repaired row fails the carry check, a verbatim one the producer gate, and an id-less one is
    re-minted by ``mint_finding_ids.py --carry-from``. The intake gate never looks at these fields, so only the planner
    stops the dead run before any spawn; naming the candidate re-validates it and replaces its rows.
    """
    _write_rows(prior, rows)
    with pytest.raises(fgs.FillGapsBlocked, match=re.escape(expected)):
        fgs.build_plan(fgs.PlanRequest("8", prior, ["QA specialist"], candidates, _FULL_HEAD))
    plan = fgs.build_plan(fgs.PlanRequest("8", prior, ["QA specialist"], [*candidates, "V2"], _FULL_HEAD))
    assert "V2" in plan["candidates"]


def test_carry_no_addition_repairs_sends_to_full_review(prior: Path) -> None:
    """A carried row no request can name or replace refuses with the gate's reason and a full-review pointer.

    The junk line belongs to no candidate, so adding every carried role and candidate still publishes it.
    """
    with (prior.parent / "findings.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('"junk"\n')
    expected = "carried records cannot pass the publication gate (findings.jsonl row is not contract_version=1"
    with pytest.raises(fgs.FillGapsBlocked, match=re.escape(expected)):
        fgs.build_plan(fgs.PlanRequest("8", prior, ["QA specialist"], ["V13", "V1b"], _FULL_HEAD))


def _triage_header_reject(report: Path) -> Path:
    """Record a terminal rejection in the prior run's header."""
    text = report.read_text(encoding="utf-8").replace("Gate: REVIEW_V1_PASS", "Gate: REVIEW_V1_REJECT_SCOPE @abcdef1")
    report.write_text(text, encoding="utf-8", newline="\n")
    return report


def _triage_pending_row(report: Path) -> None:
    """Publish an extra V2 row that is not CONFIRMED, graded as an unattributed ``finding-invalid``."""
    with report.with_name("findings.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({**_row("V2"), "id": "f-x", "validation_status": "PENDING"}) + "\n")


def _triage_duplicate_row(report: Path) -> None:
    """Publish V2's row twice, graded as ``finding-duplicate`` naming V2."""
    with report.with_name("findings.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(_row("V2")) + "\n")


def _triage_unmergeable_row(report: Path) -> None:
    """Rewrite V2's row without the change and author fields resolve's merge needs, a row no gap grades."""
    _write_rows(report, [_without(_row("V2"), "change", "author")])


def _triage_pending_row_only_gap(report: Path) -> None:
    """Leave the header disagreement and the unattributed V2 row as the only gaps, neither naming a subject."""
    _triage_header_only_gap(report)
    _triage_pending_row(report)


def _triage_no_head(report: Path) -> Path:
    """Remove the recorded head, so an unchanged PR head cannot be proven."""
    report.with_name("head-sha.txt").unlink()
    return report


def _triage_non_hex_head(report: Path) -> Path:
    """Record a head that is no commit id, so it cannot prove the PR head unchanged."""
    report.with_name("head-sha.txt").write_text("not-a-sha\n", encoding="utf-8", newline="\n")
    return report


def _triage_unreadable_sidecar(report: Path) -> Path:
    """Corrupt the prior envelope, so no record of the run can be carried."""
    report.with_name("review-validation.json").write_text("{not json", encoding="utf-8", newline="\n")
    return report


def _triage_legacy_layout(report: Path) -> Path:
    """Move the run to a pre-run-directory flat layout, which no fill-gaps run completes."""
    legacy = report.parents[2] / "2026-10-01T00-00-00Z"
    report.parent.rename(legacy)
    return legacy / report.name


def _triage_no_pr(report: Path) -> Path:
    """Drop the header's ``PR:`` field, so with no ``--pr`` the verdict knows no PR number."""
    report.write_text(report.read_text(encoding="utf-8").replace("PR: #8\n", ""), encoding="utf-8", newline="\n")
    return report


@pytest.mark.parametrize(
    ("damage", "pr", "cause", "pointer"),
    [
        pytest.param(_triage_header_reject, "8", "prior run is a terminal REJECT", "8", id="reject-run"),
        pytest.param(_triage_no_head, "8", "prior run has no head-sha.txt", "8", id="no-recorded-head"),
        pytest.param(
            _triage_non_hex_head, "8", "prior head-sha.txt is not a 7-40 hex commit id", "8", id="non-hex-head"
        ),
        pytest.param(
            _triage_unreadable_sidecar, "8", "prior run has no readable review-validation.json", "8", id="bad-sidecar"
        ),
        pytest.param(
            _triage_legacy_layout, "8", "is not a report under .reports/review/pr-8/", "8", id="legacy-layout"
        ),
        pytest.param(_triage_no_pr, "", "no PR number is known", "<PR>", id="no-pr"),
    ],
)
def test_triage_refusal_points_to_a_full_review(prior: Path, damage, pr: str, cause: str, pointer: str) -> None:
    """A run ``plan`` would refuse outright gets a not-admissible verdict whose reason ends with a full-review pointer.

    Each shape used to get a targeted rerun offered by resolve's prose rules while ``plan`` refused it, then its reason
    came back bare: resolve puts the reason in the question summary as the way forward, and its menu offers no targeted
    rerun, so the reason must never ask for an addition and must always name the full review.
    """
    report = damage(prior)
    verdict = fgs.triage(report, pr, lambda _pr: False)
    assert (verdict.admissible, verdict.required_roles, verdict.required_candidates, verdict.roles) == (
        False,
        [],
        [],
        [],
    )
    assert verdict.reason.startswith("targeted rerun not possible (")
    assert cause in verdict.reason
    assert verdict.reason.endswith(f"); run a full /oss:review {pointer}")
    assert "--roles/--candidates" not in verdict.reason


def _triage_header_only_gap(report: Path) -> None:
    """Leave only a header/sidecar disagreement, a gap that names no role or candidate."""
    _mutate_prior(
        report,
        lambda data: data.update(
            readiness="blocked",
            assessments=[_assessment("Software engineer", "complete")],
            candidates=[record for record in data["candidates"] if record["id"] == "V2"],
            open_concerns=[],
        ),
    )


def _triage_outcome_slip_only_gap(report: Path) -> None:
    """Label the aggregate outcome null over a reviewer who judged, in an envelope the header agrees with."""
    _triage_header_only_gap(report)
    _mutate_prior(report, lambda data: data.update(source_outcome=None, source_rating=None))
    text = report.read_text(encoding="utf-8").replace("Readiness: complete", "Readiness: blocked")
    report.write_text(text.replace("Outcome: NEEDS_WORK", "Outcome: null"), encoding="utf-8", newline="\n")


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(_triage_header_only_gap, id="header-disagreement"),
        pytest.param(_triage_outcome_slip_only_gap, id="aggregate-outcome-slip"),
    ],
)
def test_triage_offers_every_plannable_subject_when_nothing_is_required(prior: Path, damage) -> None:
    """A gap outside every record still admits a rerun: nothing is required and every plannable subject is optional.

    No gap names a role or candidate, yet any rerun publishes a fresh header and recomputes the aggregate labels, so the
    request naming any one recorded subject plans. Triage used to refuse these as needing a full review while ``plan``
    accepted ``--roles "Software engineer"`` and ``--candidates V2``.
    """
    damage(prior)
    verdict = fgs.triage(prior, "8", lambda _pr: False)
    assert verdict == fgs.TriageVerdict(admissible=True, roles=["Software engineer"], candidates=["V2"], pr="8")
    plan = fgs.build_plan(fgs.PlanRequest("8", prior, verdict.roles, verdict.candidates, _FULL_HEAD))
    assert (plan["roles"], plan["candidates"]) == (["Software engineer"], ["V2"])


@pytest.mark.parametrize(
    ("damage", "required_roles", "required_candidates"),
    [
        pytest.param(_triage_pending_row, ["QA specialist"], ["V2", "V1b"], id="unattributed-invalid-row"),
        pytest.param(_triage_duplicate_row, ["QA specialist"], ["V2", "V1b"], id="duplicated-row"),
        pytest.param(_triage_pending_row_only_gap, [], ["V2"], id="unattributed-row-is-the-only-gap"),
        pytest.param(_triage_unmergeable_row, ["QA specialist"], ["V2", "V1b"], id="row-resolve-cannot-merge"),
    ],
)
def test_triage_requires_the_record_whose_rows_need_replacing(
    prior: Path, damage, required_roles: list[str], required_candidates: list[str]
) -> None:
    """A rerun that plans is offered even when the record it must replace is one no gap names.

    A stray non-CONFIRMED row names V2, but the grader attributes it to nobody; a duplicated row names V2 directly. Both
    used to make triage refuse while ``plan`` accepted ``--candidates V2``, so resolve offered only a full rerun. A V2
    row lacking the fields resolve's merge needs passes the intake gate, so no gap names it, yet carrying it would fail
    the producer gate; triage used to offer the rerun without V2 and ``plan`` then refused it. Re-validating V2 replaces
    every one of its rows, so V2 is required, and the required set alone plans.
    """
    damage(prior)
    verdict = fgs.triage(prior, "8", lambda _pr: False)
    assert (verdict.admissible, verdict.required_roles, verdict.required_candidates) == (
        True,
        required_roles,
        required_candidates,
    )
    request = fgs.PlanRequest("8", prior, verdict.required_roles, verdict.required_candidates, _FULL_HEAD)
    assert "V2" in fgs.build_plan(request)["candidates"]


def test_triage_lists_required_and_optional_subjects(prior: Path) -> None:
    """The PR 8 shape is admissible: V1b and the QA specialist are required, every other gap subject is optional.

    Carrying V1b without its row can never pass the gate. The blocked reviewers and mislabelled open candidates carry
    under a ``blocked`` aggregate, so re-running them is optional, except the QA specialist: its per-PR security scan is
    mandatory, so a gap naming it is never left to the caller's choice.
    """
    assert fgs.triage(prior, "8", lambda _pr: None) == fgs.TriageVerdict(
        admissible=True,
        reason="",
        required_roles=["QA specialist"],
        required_candidates=["V1b"],
        roles=["Software engineer", "Performance engineer", "Challenger"],
        candidates=["V13", "V20", "V21"],
        pr="8",
    )


def test_triage_requires_a_candidate_whose_concern_cannot_carry(prior: Path) -> None:
    """An open concern missing its owner can only be rebuilt by re-validating its candidate, so V13 becomes required.

    A selection holding only the blocked reviewer used to pass triage and then fail the planner on V13's concern.
    """
    _mutate_prior(prior, lambda data: data["open_concerns"][0].update(owner=""))
    verdict = fgs.triage(prior, "8", lambda _pr: False)
    assert (verdict.admissible, verdict.required_candidates) == (True, ["V13", "V1b"])


@pytest.mark.parametrize(
    ("role", "bridge"),
    [
        pytest.param("QA, security", "available", id="name-holds-a-comma"),
        pytest.param("Codex security", "absent", id="bridge-role-not-runnable"),
    ],
)
def test_triage_refuses_when_a_gap_names_a_qa_role_no_request_can_include(
    prior: Path, monkeypatch: pytest.MonkeyPatch, role: str, bridge: str
) -> None:
    """A blocked QA or security reviewer that ``--roles`` cannot name or ``plan`` cannot re-run makes the rerun refuse.

    Its per-PR security scan is mandatory, so a gap naming it is never optional. Triage used to drop such a role from
    the selectable subjects silently and admit a rerun that would publish without the security scan.
    """
    monkeypatch.setattr(fgs, "bridge_status", lambda _home, _root: bridge)
    _mutate_prior(prior, lambda data: data["assessments"].append(_assessment(role, "blocked")))
    verdict = fgs.triage(prior, "8", lambda _pr: False)
    assert (verdict.admissible, verdict.required_roles, verdict.roles, verdict.candidates) == (False, [], [], [])
    assert f"QA/security role(s) {role!r} named by a gap cannot be named in --roles or re-run" in verdict.reason
    assert verdict.reason.endswith("; run a full /oss:review 8")


def test_triage_required_subjects_always_plan(prior: Path) -> None:
    """Any request holding the required subjects plans, even when the required reviewer has an interchangeable twin.

    Either reviewer re-run here can supply the aggregate outcome, so neither is needed on its own; triage still names
    one as required, because a request naming only V1b would be refused.
    """
    _mutate_prior(prior, _null_aggregate_all_reviewers_missing)
    verdict = fgs.triage(prior, "8", lambda _pr: False)
    assert (verdict.required_roles, verdict.required_candidates, verdict.roles) == (
        ["Software engineer"],
        ["V1b"],
        ["Performance engineer"],
    )
    request = fgs.PlanRequest("8", prior, verdict.required_roles, verdict.required_candidates, _FULL_HEAD)
    assert fgs.build_plan(request)["publish_rows"] == ["V1b"]
    with pytest.raises(fgs.FillGapsBlocked, match="carried record"):
        fgs.build_plan(fgs.PlanRequest("8", prior, [], ["V1b"], _FULL_HEAD))


def test_triage_cli_drops_the_rerun_when_the_head_moved(
    prior: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A moved PR head makes the targeted rerun inadmissible, checked with gh through the validator's own helper.

    ``plan`` refuses a moved head; triage used to keep offering the rerun for a critical report anyway.
    """
    asked: list[str] = []
    monkeypatch.setattr(frr, "current_head_sha", lambda pr, _timeout: asked.append(pr) or "f" * 40)
    assert fgs.main(["triage", "--report", str(prior), "--pr", "8"]) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert (verdict["admissible"], asked) == (False, ["8"])
    assert verdict["reason"].startswith(f"PR head moved since the run reviewed {_PRIOR_HEAD}")


@pytest.mark.parametrize(
    ("head_moved", "pr", "admissible"),
    [
        pytest.param(None, "n/a", True, id="unknown-head-report-mode"),
        pytest.param(False, "", True, id="unchanged-head-empty-pr"),
        pytest.param(True, "8", False, id="moved-head"),
        pytest.param(False, "#8", True, id="non-digit-pr"),
    ],
)
def test_triage_cli_reuses_the_assessment_line(
    prior: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    head_moved: bool | None,
    pr: str,
    admissible: bool,
) -> None:
    """``--assessment`` takes the report and ``head_moved`` from resolve's saved ``--assess`` line, never asking gh.

    Resolve's intake has emptied a critical report's path sentinel by then, so the saved line is its only handle on the
    report. An empty, ``n/a`` or non-digit ``--pr`` falls back to the header's ``PR: #8``, which the verdict carries as
    ``pr`` so resolve's rerun options need no second header read; only ``head_moved: true`` blocks.
    """
    asked: list[str] = []
    monkeypatch.setattr(frr, "current_head_sha", lambda pr_number, _timeout: asked.append(pr_number) or "f" * 40)
    gaps = tmp_path / "gaps.json"
    gaps.write_text(json.dumps({"report": prior.as_posix(), "head_moved": head_moved}) + "\n", encoding="utf-8")
    assert fgs.main(["triage", f"--assessment={gaps}", f"--pr={pr}"]) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert (verdict["admissible"], verdict["pr"], asked) == (admissible, "8", [])


@pytest.mark.parametrize(
    ("name", "content"),
    [
        pytest.param("absent.json", "{}\n", id="missing-file"),
        pytest.param("gaps.json", "not json\n", id="not-json"),
        pytest.param("gaps.json", "[]\n", id="not-an-object"),
        pytest.param("gaps.json", '{"head_moved": false}\n', id="no-report-path"),
        pytest.param("gaps.json", '{"report": "r.md", "head_moved": "yes"}\n', id="head-moved-not-bool"),
    ],
)
def test_triage_cli_answers_an_unusable_assessment_with_a_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture, name: str, content: str
) -> None:
    """An unreadable or malformed assessment file prints a not-admissible verdict and exits 0, never a traceback.

    Resolve stores whatever triage prints as the rerun verdict; a well-formed refusal keeps its question honest, and
    like every not-admissible reason it ends by pointing to a full review. The content always lands in ``gaps.json``, so
    ``absent.json`` names a file that does not exist.
    """
    (tmp_path / "gaps.json").write_text(content, encoding="utf-8")
    gaps = tmp_path / name
    assert fgs.main(["triage", f"--assessment={gaps}", "--pr=8"]) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert (verdict["admissible"], verdict["required_roles"], verdict["candidates"], verdict["pr"]) == (
        False,
        [],
        [],
        "8",
    )
    assert verdict["reason"].startswith(f"targeted rerun not possible (gap assessment {gaps} unreadable or malformed")
    assert verdict["reason"].endswith("); run a full /oss:review 8")


def test_triage_cli_without_a_source_is_a_usage_error(capsys: pytest.CaptureFixture) -> None:
    """Neither ``--report`` nor ``--assessment`` stops with a blocked line and exit 1, not argparse's exit 2."""
    with pytest.raises(SystemExit) as stop:
        fgs.main(["triage", "--pr", "8"])
    output = capsys.readouterr().out
    assert (stop.value.code, output.startswith("! BLOCKED — ")) == (1, True)
    assert "one of the arguments --report --assessment is required" in output


@pytest.mark.parametrize("head", ["abc", "not-a-commit-id", "ABCDEF1234567"])
def test_prior_head_must_be_a_commit_id(prior: Path, head: str) -> None:
    """A head-sha.txt that is not a 7-40 hex commit id cannot prove the PR head unchanged.

    The head check is a prefix match, so a one-character or empty-ish stray string would match almost any PR head.
    """
    (prior.parent / "head-sha.txt").write_text(head + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(fgs.FillGapsBlocked, match=re.escape("prior head-sha.txt is not a 7-40 hex commit id")):
        fgs.build_plan(fgs.PlanRequest("8", prior, ["QA specialist"], ["V1b"], _FULL_HEAD))


@pytest.mark.parametrize(
    ("role", "unit", "file"),
    [
        pytest.param("Software engineer", "review-arch", "foundry--sw-engineer.md", id="software-engineer"),
        pytest.param("QA specialist", "review-qa", "foundry--qa-specialist.md", id="qa-not-ci"),
        pytest.param(
            "Performance engineer", "review-perf-api", "foundry--perf-optimizer.md", id="perf-before-engineer"
        ),
        pytest.param("Solution architect", "review-perf-api", "foundry--solution-architect.md", id="architect"),
        pytest.param("Documentation reviewer", "review-docs-lint", "foundry--doc-scribe.md", id="docs"),
        pytest.param("Linting expert", "review-docs-lint", "foundry--linting-expert.md", id="lint"),
        pytest.param("CI/CD steward", "review-cicd", "oss--cicd-steward.md", id="cicd"),
        pytest.param("Challenger", "review-challenge", "foundry--challenger.md", id="challenger"),
        pytest.param("Codex", "codex", "foundry--codex.md", id="codex-bridge"),
        pytest.param("Security auditor", "review-qa", "foundry--qa-specialist.md", id="security-is-qa"),
        pytest.param("Test engineer", "review-qa", "foundry--qa-specialist.md", id="test-is-qa"),
    ],
)
def test_role_maps_to_its_spawn_unit(role: str, unit: str, file: str) -> None:
    """Free-text role names reach the dimension agent and output file that re-assesses them."""
    dimension = fgs.map_role(role)
    assert (dimension.unit, dimension.file) == (unit, file)


def test_qa_unit_and_security_grading_share_one_pattern() -> None:
    """The role fill-gaps re-runs as QA is, by construction, the role the validator grades as the security reviewer.

    Two hand-kept patterns drifted: the validator matched only ``qa`` while fill-gaps also routed security and test
    roles to the QA unit, so a blocked "Security auditor" graded degraded.
    """
    qa_pattern = next(pattern for pattern, dimension in fgs._DIMENSIONS if dimension.unit == "review-qa")
    assert qa_pattern is frr.QA_ROLE_RE


def test_unmappable_role_blocks_and_merged_dimensions_share_a_spawn() -> None:
    """A role no dimension covers is refused, while perf and architecture roles collapse into one spawn.

    Guessing an agent for an unknown role would publish a coverage claim nobody made; merging paired dimensions keeps
    the fixed spawn overhead the normal lineup already avoids.
    """
    with pytest.raises(fgs.FillGapsBlocked, match="maps to no review dimension"):
        fgs.map_role("Mystery reviewer")
    units = fgs._units(["Performance engineer", "Solution architect"])
    assert [(u["unit"], u["files"]) for u in units] == [
        ("review-perf-api", ["foundry--perf-optimizer.md", "foundry--solution-architect.md"])
    ]
