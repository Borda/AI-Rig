"""Guard calibrate's batched Claude scorers and the Phase 1c, budget and diagram text that changed with them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_CALIBRATE = Path(__file__).resolve().parents[2] / "skills/calibrate"
_PIPELINE = "templates/pipeline-prompt.md"


def _lines(relative: str) -> list[str]:
    """Return a calibrate file's lines so assertions never depend on prose wrapping."""
    return (_CALIBRATE / relative).read_text(encoding="utf-8").splitlines()


def _section(relative: str, heading: str) -> list[str]:
    """Return one ``### Phase`` section: its heading line through the line before the next phase heading."""
    lines = _lines(relative)
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("### Phase ")), len(lines))
    return lines[start:end]


def test_batch_size_is_a_constant_not_a_substituted_placeholder() -> None:
    """No caller supplies a batch size, so the template fixes it at three instead of defaulting a placeholder."""
    section = _section(_PIPELINE, "### Phase 3a")
    assert any(line.startswith("Scorer batch size `SCORER_BATCH` = 3 problems per scorer") for line in section)
    for path in _CALIBRATE.rglob("*.md"):
        assert "<SCORER_BATCH>" not in path.read_text(encoding="utf-8"), path.name
    spawn = next(index for index, line in enumerate(section) if line.startswith("Spawn one `general-purpose` scorer"))
    assert not any(line.strip() == "```bash" for line in section[:spawn])


def test_phase_3a_scores_several_problems_per_scorer_without_mixing_them() -> None:
    """One scorer covers up to ``SCORER_BATCH`` problems, scores each from its own files, writes one file each."""
    section = _section(_PIPELINE, "### Phase 3a")
    text = "\n".join(section)
    assert "(parallel)" not in section[0]
    assert any("up to `SCORER_BATCH` problems" in line for line in section)
    assert any(
        "never compare or rank problems" in line and "using only that problem's ground truth" in line
        for line in section
    )
    assert "`Scored: <PROBLEM_ID_LIST>`" in text
    assert "score-<PROBLEM_ID>-claude.json" in text


def test_problem_id_after_the_problem_block_is_filled_per_problem() -> None:
    """The rules after the repeated block keep ``<PROBLEM_ID>`` literal: one ID for the chunk would overwrite files."""
    section = _section(_PIPELINE, "### Phase 3a")
    assert any("Leave `<PROBLEM_ID>` literal in the rules after the problem block" in line for line in section)
    assert any("never with one ID for the whole chunk" in line for line in section)


def test_false_positive_and_role_gate_stay_inside_one_problem() -> None:
    """With several problems in one context, rule (b) and the per-role group may only use the scored problem's items."""
    text = "\n".join(_section(_PIPELINE, "### Phase 3a"))
    assert "no matching ground-truth item of this problem" in text
    assert "ground-truth items of this problem sharing one `role`" in text


def test_missing_result_retry_is_single_shot_and_batch_of_one_is_never_retried() -> None:
    """A missing file in a multi-problem chunk is re-scored once alone; a chunk of one adds no extra scorer."""
    line = next(line for line in _section(_PIPELINE, "### Phase 3a") if line.startswith("**Context discipline**"))
    assert "more than one problem" in line
    assert "scored once more by its own scorer (a batch of one)" in line
    assert "never retried" in line
    assert "a chunk of one is never re-scored" in line


def test_probe_scorers_stay_single_problem() -> None:
    """Weak-probe scorers score one response: their result decides keep or downgrade."""
    line = next(line for line in _lines(_PIPELINE) if line.startswith("Score each weak response"))
    assert "one Phase 3a Claude scorer" in line
    assert "batch of one" in line


def test_codex_scoring_stays_sequential_per_problem() -> None:
    """Phase 3b keeps one Codex call per problem: it is the independent second opinion and writes shared files."""
    section = _section(_PIPELINE, "### Phase 3b")
    assert any("Run **sequentially**" in line for line in section)
    assert not any("SCORER_BATCH" in line for line in section)


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("templates/rules-pipeline-prompt.md", id="rules-scorers"),
        pytest.param("templates/routing-pipeline-prompt.md", id="routing-selectors"),
    ],
)
def test_rules_and_routing_never_name_the_batch_constant(relative: str) -> None:
    """Rules scorers return inline results over a channel known to drop them; routing selectors judge one task each.

    The check is on the constant's name only, so batching reworded without it is caught by review, not by this test.
    """
    assert "SCORER_BATCH" not in "\n".join(_lines(relative))


def test_skill_describes_the_batch_constant_and_its_evidence_limits() -> None:
    """SKILL.md names the ``SCORER_BATCH`` constant, the cap of three, its limited evidence and ``scorer_batch``."""
    line = next(line for line in _lines("SKILL.md") if "**Scorer delegation**" in line)
    assert "`SCORER_BATCH` is a constant" in line
    assert "nothing above 5 was tested" in line
    assert "3 is a conservative cap" in line
    assert "`scorer_batch`" in line
    assert "per-problem `general-purpose` subagents" not in line


def test_result_and_return_lines_record_the_scorer_batch() -> None:
    """The run result line and the compact return line carry ``scorer_batch``; the ledger copies result.jsonl."""
    lines = _lines(_PIPELINE)
    specs = [line for line in lines if line.startswith("`{") and '"scoring":"dual|single"' in line]
    assert len(specs) == 2
    assert all('"scorer_batch":N' in line for line in specs)
    rule = next(line for line in lines if line.startswith("- `scorer_batch` ="))
    assert "`SCORER_BATCH`" in rule
    ledger = next(line for line in _lines("SKILL.md") if line.startswith("Runs from Step 3, before its Follow-up gate"))
    assert "this run's per-target `result.jsonl` files are appended" in ledger


def test_precision_gate_line_shows_the_bare_value_to_the_scorer() -> None:
    """The scorer sees ``per-role`` or ``none`` only: an "(empty = none)" note made scorers apply the gate loosely."""
    section = _section(_PIPELINE, "### Phase 3a")
    gate = next(line for line in section if line.startswith("> **Precision gate**"))
    assert gate.startswith("> **Precision gate**: `<PRECISION_GATE>`. When `per-role`:")
    assert "empty = none" not in gate
    assert any("`<PRECISION_GATE>` is its `precision_gate:` value or `none` when absent" in line for line in section)


def test_a_result_file_counts_only_when_it_belongs_to_its_problem() -> None:
    """A score file with another problem's ID or a wrong item count is missing, so the single re-score covers it."""
    section = _section(_PIPELINE, "### Phase 3a")
    line = next(line for line in section if line.startswith("**Context discipline**"))
    assert "counts as present only when it is valid" in line
    assert "it parses," in line
    assert "`problem_id` equals the problem ID in its file name" in line
    assert "`len(found)` equals that problem's ground-truth item count" in line
    assert "treated as missing" in line


def test_chunk_deliverable_is_its_last_file_and_a_passed_deadline_never_starts_a_retry() -> None:
    """A chunk's deadline row names its last file; only a finished scorer retries, an overdue one may still write."""
    line = next(line for line in _section(_PIPELINE, "### Phase 3a") if line.startswith("**Context discipline**"))
    assert "deliverable (for any deadline row armed for it) is its last problem's file" in line
    assert "(notification or idle) and a problem's file is missing or invalid" in line
    assert "A passed deadline never starts a retry" in line


def test_every_reader_of_a_score_file_applies_the_same_validity_rule() -> None:
    """The completion check, the weak-probe check and the Phase 3c merge all use the one Phase 3a file rule."""
    lines = _lines(_PIPELINE)
    discipline = next(line for line in _section(_PIPELINE, "### Phase 3a") if line.startswith("**Context discipline**"))
    assert "the one rule every reader of a score file applies" in discipline
    merge = next(line for line in _section(_PIPELINE, "### Phase 3c") if line.startswith("For each problem, read"))
    assert "merged only when valid per Phase 3a's file rule" in merge
    assert "not merged" in merge
    probe = next(line for line in lines if line.startswith("Score each weak response"))
    assert "read only when valid per Phase 3a's file rule" in probe


def test_weak_probe_scorer_renders_its_own_paths() -> None:
    """The weak-probe scorer reads the weak response and writes the weak score, with the literal ID filled in."""
    line = next(line for line in _lines(_PIPELINE) if line.startswith("Score each weak response"))
    assert "`response-<problem_id>-weak.md`" in line
    assert "`score-<problem_id>-weak.json`" in line
    assert "filled with that problem's ID" in line
    assert "`AB_MODE` false so it never reads a `-general` response" in line


def test_codex_scoring_keeps_an_empty_precision_gate_when_the_domain_has_none() -> None:
    """Phase 3b substitutes the gate as Phase 3a does except that an absent gate stays empty, as before batching."""
    section = _section(_PIPELINE, "### Phase 3b")
    assert any("`<PRECISION_GATE>` stays empty when the domain has none" in line for line in section)


def test_a_rejected_weak_probe_score_has_a_defined_outcome() -> None:
    """A missing or invalid weak score keeps the problem unprobed and records a gap, as the other readers decide."""
    line = next(line for line in _lines(_PIPELINE) if line.startswith("- weak score missing or invalid"))
    assert "keep the problem unprobed (no `probe_weak_recall`)" in line
    assert "gap `weak probe unscored: <problem_id>`" in line


def test_a_twice_contaminated_reprobe_leaves_the_problem_unprobed() -> None:
    """A re-probe that quotes a shipped file again is void, so the problem stays unprobed instead of downgraded."""
    line = next(line for line in _lines(_PIPELINE) if line.startswith("- weak score missing or invalid"))
    assert "the re-probe's response again quotes a shipped file" in line
    assert "keep the problem unprobed" in line


def test_a_fully_solved_weak_probe_downgrades_without_regenerating() -> None:
    """Phase 1c has no regeneration, so no second generation can collide on ids or be rebuilt away by the validator."""
    lines = _lines(_PIPELINE)
    keep = next(line for line in lines if line.startswith("- weak recall < 1.0"))
    assert "record `probe_weak_recall` on the problem in `problems.json`" in keep
    downgrade = next(line for line in lines if line.startswith("- weak recall = 1.0"))
    assert 'set `difficulty: "medium"` and `"probe": "downgraded"` on that problem' in downgrade
    assert "regenerate" not in downgrade
    note = next(line for line in lines if line.startswith("This phase never regenerates a problem"))
    assert "never reruns the validator" in note
    assert "only Phase 1b writes `problems.json` from the problem files" in note
    phase_1c = "\n".join(_section(_PIPELINE, "### Phase 1c"))
    assert "regenerate once" not in phase_1c
    skill = "\n".join(_lines("SKILL.md"))
    assert "regenerates or downgrades" not in skill


def test_wake_up_count_accepts_only_valid_weak_scores_and_not_discarded_responses() -> None:
    """The Phase 1c wake-up deliverables are valid score files; a response discarded for contamination is not one."""
    wake_up = next(line for line in _lines(_PIPELINE) if line.startswith("- At every wake-up count"))
    assert "Phase 3a and the Phase 1c weak scorers: only a valid score file, see Phase 3a" in wake_up
    assert "a weak response discarded for quoting a shipped file counts as not present" in wake_up


def test_codex_score_file_is_merged_only_when_valid() -> None:
    """The Phase 3c merge applies the validity rule to the Codex file too; an invalid one leaves Claude's alone."""
    merge = next(line for line in _section(_PIPELINE, "### Phase 3c") if line.startswith("For each problem, read"))
    assert "used only when valid per the same rule: an invalid Codex file counts as absent" in merge
    single = next(line for line in _lines(_PIPELINE) if line.startswith("**When only Claude score present**"))
    assert "Codex unavailable, failed or invalid" in single


def test_fast_budget_text_counts_batched_scorers_and_states_headroom() -> None:
    """Fast-run child counts show the batched scorers; retries sit on top and the regeneration figures are headroom."""
    lines = _lines(_PIPELINE)
    assert any(line.startswith("- About 11 initial fast children") for line in lines)
    assert any("(scorer-chunk retries come on top)" in line for line in lines)
    assert any("one call, two kept as headroom" in line for line in lines)
    skill = "\n".join(_lines("SKILL.md"))
    assert "about 11 children (2 probes, 2 probe scorers, 4 targets, 2 batched scorers, 1 curator)" in skill
    assert "about 13 children" not in skill
    assert "(scorer-chunk retries come on top)" in skill
    assert "and replacement of a probe that reads forbidden context" not in skill


def test_diagram_shows_the_batched_flow_and_no_regeneration() -> None:
    """The diagram carries the single retry and drops the regeneration loop and its two pills."""
    svg = (_CALIBRATE.parents[1] / "assets/images/calibrate-arch.svg").read_text(encoding="utf-8")
    assert "retry ×1 · alone" in svg
    assert "recall 1.0 → regenerate" not in svg
    assert "after re-probe" not in svg
    plain = re.sub(r"<[^>]+>", "", svg)
    assert "recall 1.0 → loop ×1" not in plain
    assert "recall 1.0 → medium" in plain


def test_comparability_note_separates_incomplete_exits_from_older_complete_rows() -> None:
    """Incomplete exits carry no scorer_batch since nothing was scored; only complete older rows predate the change."""
    line = next(line for line in _lines("SKILL.md") if line.startswith("- **Historical comparability**"))
    assert "incomplete exits, verdict `incomplete` with null metrics, carry none either" in line
    assert "Complete standard-pipeline rows without it also predate the downgrade-only Phase 1c" in line
