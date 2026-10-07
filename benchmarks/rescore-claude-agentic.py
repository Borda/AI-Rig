#!/usr/bin/env python3
"""Offline answer-envelope rescore for a completed run-claude-agentic snapshot.

Re-assesses each stored ``output_text`` with the CURRENT ``agentic_contracts.assess_answer_response``
and re-scores ONLY the rows whose original runtime parse failed (``answer_error`` non-empty).
Rows that parsed at runtime keep their exact stored scores — the report-text boundary
(``last_tool_text_offset``) is not persisted in the snapshot, so re-scoring parsed-OK rows
would replace exact values with approximations for no benefit.

Approximation (rescored rows only): ``report_text`` is taken from the last
``BEGIN_ANSWER_JSON`` marker onward instead of "all text after the last tool call". The
marker may precede the actual final response, so recovered semantic scores and RREC remain
diagnostic only; they cannot restore strict-envelope or pooling eligibility.

The input snapshot is never modified; output is a sibling ``<stem>-rescored.json`` with a
``rescore`` provenance block recording parser hash, changed rows, and the approximation.

Examples:
    REPO=path/to/codemap-provider-parity-pl-2.6.5 python3 benchmarks/rescore-claude-agentic.py \
        --snapshot benchmarks/results/code-2026-08-04.json \
        --repo-path "$REPO"
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path

import fire

#: Benchmarks directory, added to the import path and used to locate the runner and default task suite.
BENCHMARKS = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCHMARKS))

from _bench_common import agentic_contracts  # noqa: E402


def _load_runner_module():
    """Import the hyphen-named agentic runner as a module.

    Returns:
        The loaded ``run-claude-agentic`` module (task loading + ToolCounts).

    Examples:
        >>> mod = _load_runner_module()
        >>> hasattr(mod, "load_tasks_with_provenance")
        True
    """
    spec = importlib.util.spec_from_file_location("run_claude_agentic", BENCHMARKS / "run-claude-agentic.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_claude_agentic"] = module
    spec.loader.exec_module(module)
    return module


def _approximate_report_text(output_text: str) -> str:
    """Return the final-answer region used as the rrec corpus for rescored rows.

    The exact report boundary (text after the last tool call) is not persisted in the
    snapshot, so the region from the LAST answer-envelope marker onward stands in for it:
    that region is always part of the final report, making rrec a lower bound.

    Args:
        output_text: Full agent output text stored in the snapshot row.

    Returns:
        Substring from the last ``BEGIN_ANSWER_JSON`` onward, or the full text when the
        marker is absent (parse will fail again in that case anyway).

    Examples:
        >>> _approximate_report_text("preamble BEGIN_ANSWER_JSON {} END_ANSWER_JSON tail")
        'BEGIN_ANSWER_JSON {} END_ANSWER_JSON tail'
        >>> _approximate_report_text("no marker at all")
        'no marker at all'
    """
    marker = "BEGIN_ANSWER_JSON"
    pos = output_text.rfind(marker)
    return output_text[pos:] if pos >= 0 else output_text


def _rescore_row(row: dict, task, oracle, module) -> dict | None:
    """Re-assess and re-score one failed-parse row; return the change record or None.

    Args:
        row: Mutable snapshot result row (updated in place when the new parse succeeds).
        task: Task projection whose ``answer_task`` carries the answer contract.
        oracle: Pre-built ``AgenticOracle`` for the task.
        module: Loaded runner module (for ``ToolCounts`` reconstruction).

    Returns:
        A per-row change record for the provenance block, or ``None`` when the row still
        fails to parse under the current parser (row left untouched).
    """
    output_text = row.get("output_text", "")
    report_text = _approximate_report_text(output_text)
    assessment = agentic_contracts.assess_answer_response(task.answer_task, report_text)
    if assessment.answer is None:
        return {
            "task_id": row["task_id"],
            "model": row["model"],
            "arm": row["arm"],
            "still_failed": assessment.error or "answer response is unscorable",
        }
    tool_total = module.ToolCounts(**row.get("tools", {})).total
    score = agentic_contracts.score_answer(
        oracle,
        assessment.answer,
        exposure_text=output_text,
        report_text=report_text,
        tool_calls=tool_total,
    )
    before = {
        "answer_quality_score": row.get("answer_quality_score"),
        "answer_correct": row.get("answer_correct"),
        "answer_error": row.get("answer_error"),
        "erec": row["quality"].get("erec"),
        "rrec": row["quality"].get("rrec"),
    }
    row["answer_quality_score"] = score.quality_score
    row["answer_correct"] = score.correct
    row["answer_components"] = dict(score.components)
    row["answer_contract_valid"] = False
    row["answer_diagnostic_only"] = True
    row["answer_pooling_eligible"] = False
    row["answer_error"] = assessment.error or (
        "offline rescore uses an approximate report boundary; strict envelope and pooling eligibility cannot be recovered"
    )
    row["answer_scored"] = True
    row["quality"]["scored"] = score.scored
    row["quality"]["erec"] = score.erec
    row["quality"]["rrec"] = score.rrec
    row["quality"]["deff"] = score.deff
    row["rescored"] = True
    return {
        "task_id": row["task_id"],
        "model": row["model"],
        "arm": row["arm"],
        "before": before,
        "after": {
            "answer_quality_score": score.quality_score,
            "answer_correct": score.correct,
            "erec": score.erec,
            "rrec": f"{score.rrec} (lower bound — approximated report region)",
        },
    }


def main(
    snapshot: str = "",
    repo_path: str = "",
    tasks_path: str = "",
    manifest_path: str = "",
    output: str = "",
) -> None:
    """Rescore failed answer-envelope parses in a run-claude-agentic snapshot.

    Args:
        snapshot: Path to the ``code-YYYY-MM-DD.json`` rolling snapshot. Required.
        repo_path: Target repo checkout used to rebuild the AST answer oracles. Required.
        tasks_path: Agentic task suite (default ``benchmarks/suites/tasks-agentic.json``).
        manifest_path: Locked methodology manifest (default: runner's own default).
        output: Output path (default ``<snapshot stem>-rescored.json``; never overwrites
            the input snapshot).

    Examples:
        REPO=path/to/codemap-provider-parity-pl-2.6.5
        python3 benchmarks/rescore-claude-agentic.py --snapshot .../code-2026-08-04.json --repo-path "$REPO"
    """
    if not snapshot or not repo_path:
        sys.exit("ERROR: --snapshot and --repo-path are required")
    snapshot_path = Path(snapshot)
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    module = _load_runner_module()
    suite = Path(tasks_path) if tasks_path else BENCHMARKS / "suites" / "tasks-agentic.json"
    if manifest_path:
        tasks = module.load_tasks_with_provenance(suite, Path(manifest_path))
    else:
        tasks = module.load_tasks_with_provenance(suite)
    task_by_id = {t.id: t for t in tasks}
    oracles = {
        t.id: agentic_contracts.build_oracle(t.answer_task, Path(repo_path))
        for t in tasks
        if t.answer_task.get("answer_contract") is not None
    }

    changes: list[dict] = []
    candidates = [r for r in data["results"] if r.get("answer_scored") and r.get("answer_error")]
    for row in candidates:
        task = task_by_id.get(row["task_id"])
        oracle = oracles.get(row["task_id"])
        if task is None or oracle is None:
            changes.append({"task_id": row["task_id"], "skipped": "no task/oracle available"})
            continue
        record = _rescore_row(row, task, oracle, module)
        if record:
            changes.append(record)

    parser_src = inspect.getsource(agentic_contracts.assess_answer_response)
    data["rescore"] = {
        "source_snapshot": str(snapshot_path),
        "parser": "agentic_contracts.assess_answer_response",
        "parser_sha256": hashlib.sha256(parser_src.encode("utf-8")).hexdigest(),
        "policy": "only rows with a non-empty runtime answer_error are rescored; parsed-OK rows keep exact runtime scores",
        "rrec_approximation": "rescored rows use text from the last BEGIN_ANSWER_JSON onward as a diagnostic report corpus; strict envelope and pooling eligibility remain unrecoverable",
        "rows_considered": len(candidates),
        "changes": changes,
    }
    out_path = Path(output) if output else snapshot_path.with_name(snapshot_path.stem + "-rescored.json")
    if out_path.resolve() == snapshot_path.resolve():
        sys.exit("ERROR: output path must differ from the input snapshot")
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    fixed = [c for c in changes if "after" in c]
    still = [c for c in changes if "still_failed" in c]
    print(f"rescored {len(fixed)}/{len(candidates)} failed-parse rows; {len(still)} still fail under current parser")
    for c in fixed:
        b, a = c["before"], c["after"]
        print(
            f"  {c['task_id']} {c['model']} {c['arm']}: "
            f"aqs {b['answer_quality_score']}->{a['answer_quality_score']:.3f} "
            f"correct {b['answer_correct']}->{a['answer_correct']} "
            f"erec {b['erec']}->{a['erec']:.3f}"
        )
    for c in still:
        print(f"  {c['task_id']} {c['model']} {c['arm']}: still failed — {c['still_failed'][:100]}")
    print(f"wrote: {out_path}")


if __name__ == "__main__":
    fire.Fire(main)
