"""Tests for the ``size_importance`` bin script (audit check E11).

Covers block splitting, the obligation count, the spine/complement judgment (spine on any doubt), the move and trim
actions per load class, the own-gate exemption, copy collapsing, rendering and the command line.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import size_importance as si

MODES = Path(si.__file__).resolve().parents[1] / "skills" / "audit" / "modes"
#: A finding row as ``score`` emits it, for the render tests.
_FINDING = {
    "action": "move", "severity": "low", "file": "f.md", "lines": "1-2", "title": "t", "load": "lazy",
    "tokens": 500, "obligations": 1, "copies": 1, "reason": "r",
}  # fmt: skip


def pad(tokens: int) -> str:
    """Return filler prose of about ``tokens`` tokens (bytes / 3) with no normative word and no marker word."""
    return " ".join(["lorem"] * (tokens * 3 // 6))


def section(path: str, text: str, load: str = "lazy") -> dict[str, object]:
    """Return an inventory item covering all of ``text`` (written by the caller at ``path``)."""
    return {"file": path, "start_line": 1, "end_line": len(text.split("\n")), "load": {"hierarchy": load}}


#: A rationale block above a step, both large enough to clear the token floor in always-loaded text.
_RATIONALE_RULE = f"## Rule\n\n**Rationale**: {pad(600)}\n\n**Step**: do it. {pad(900)}\n"
#: Spine phase A beside a flag-gated phase B3, the shape a move finding is built from.
_GATED_MODE = f"## Mode\n\n**Phase A**: audit, never skip it. {pad(900)}\n\n**Phase B3**: only with `--x`. {pad(600)}\n"


def run(tmp_path: Path, files: dict[str, tuple[str, str]], **kwargs: object) -> dict[str, object]:
    """Write ``{name: (text, load)}`` under ``tmp_path`` and score them as one inventory."""
    items = []
    for name, (text, load) in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
        items.append(section(name, text, load))
    return si.score({"items": items}, tmp_path, **kwargs)  # type: ignore[arg-type]


class TestSplitBlocks:
    """Where a section breaks into blocks."""

    @pytest.mark.parametrize(
        ("lines", "first_line", "expected"),
        [
            pytest.param(
                ["## T", "intro", "**Lead** one", "more", "- item", "  - nested", "1. step"],
                10,
                [(10, 11), (12, 13), (14, 15), (16, 16)],
                id="bold-lead-and-top-level-items",
            ),
            pytest.param(
                ["**Lead**", "```", "- not a start", "**not a lead**", "```", "- real"],
                1,
                [(1, 5), (6, 6)],
                id="fenced-lines-never-start-a-block",
            ),
            pytest.param(
                ["**Lead**", "~~~~", "```", "- x", "```", "~~~~", "- real"],
                1,
                [(1, 6), (7, 7)],
                id="long-tilde-fence-holds-inner-backtick-fence",
            ),
            pytest.param([], 5, [], id="empty-section"),
        ],
    )
    def test_block_ranges(self, lines: list[str], first_line: int, expected: list[tuple[int, int]]) -> None:
        """Blocks start at a bold lead or a top-level list item, never inside a fence, and an empty section has none.

        Block boundaries decide what is later judged as one unit, so a marker inside a code fence (or a fence nested in
        a longer fence) must not split a block, and an empty section must not yield a phantom block.
        """
        assert [(s, e) for s, e, _ in si.split_blocks(lines, first_line)] == expected


class TestObligations:
    """The obligation proxy that weights a block."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("Plain description.", 1, id="floor-of-one"),
            pytest.param("Never push. Always ask. Describe it.", 2, id="normative-sentences"),
            pytest.param("Run it.\n```bash\nls\n```\n```bash\npwd\n```", 2, id="each-fence-counts-once"),
            pytest.param("Never skip.\n```bash\nls\n```", 2, id="prose-plus-fence"),
            pytest.param("```\nNever x. Never y.\n```", 1, id="code-prose-not-counted"),
        ],
    )
    def test_counts_normative_sentences_and_fences(self, text: str, expected: int) -> None:
        """Normative sentences outside code plus one per fenced block, with a floor of one.

        Tokens per obligation is the trim yardstick, so a block must never count as zero obligations and prose that sits
        inside a code fence must not be mistaken for an instruction.
        """
        assert si.obligations(text) == expected


class TestClassify:
    """The judgment: complement only on positive proof, spine otherwise."""

    @pytest.mark.parametrize(
        ("text", "role", "gated"),
        [
            pytest.param("**Phase X**: runs only with `--measured`, three steps.", "complement", True, id="gated-flag"),
            pytest.param("**Mode** (opt-in): extra checks.", "complement", True, id="gated-opt-in"),
            pytest.param("**Worked example**: a long case.", "complement", False, id="example-lead"),
            pytest.param("Never delete the ledger; see the measured rationale.", "spine", False, id="high-stakes-wins"),
            pytest.param("**Opt-in** flag that may delete the cache.", "spine", False, id="high-stakes-beats-gate"),
            pytest.param("- Never suggest it. Measured on 2026-08 over 41 runs.", "spine", False, id="directive-first"),
            pytest.param("**Example**: write the envelope to a file.", "spine", False, id="structural-beats-example"),
            pytest.param("Compose the document from the notes.", "spine", False, id="no-evidence-is-spine"),
            pytest.param(
                "Compose the document from the notes and add a worked example somewhere in the running prose.",
                "spine",
                False,
                id="vocab-not-in-lead",
            ),
        ],
    )
    def test_role_and_gate(self, text: str, role: str, gated: bool) -> None:
        """Each case pins the role and whether the block is gated.

        Complement needs proof (a flag gate or rationale vocabulary in the lead) and high-stakes wording or an
        executable instruction outranks it, so everything without proof must stay spine.
        """
        got_role, _reason, got_gated = si.classify(text)
        assert (got_role, got_gated) == (role, gated)

    def test_gate_only_counts_in_the_lead(self) -> None:
        """An `opt-in` word inside a later code line does not gate the block.

        Gate vocabulary buried in a fenced example describes the example, not when the block itself is loaded.
        """
        text = "**Path rule** before mypy, check the path.\n```bash\n# opt-in only with `--strict`\n```"
        assert si.classify(text)[2] is False

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("**Phase B3**: only with `--measured`.\n```bash\nls\n```", id="gate-outranks-executable-step"),
            pytest.param(
                "**Phase B3 — Measured evidence for inline `model:` pins**: three steps after the Phase B scan block, "
                "only with `--measured` (zero model tokens; installed and `--local` context alike). Without the flag "
                "nothing reads a transcript or writes a ledger.",
                id="real-phase-b3-opening",
            ),
        ],
    )
    def test_flag_only_phase_is_gated_complement(self, text: str) -> None:
        """A flag-only phase is complement for the stated gate reason, even when it holds bash steps.

        The second case is the real pre-trim Phase B3 opening, the shape that motivated the check: the gate outranks the
        executable-step rule.
        """
        assert si.classify(text)[:2] == ("complement", "gated by an opt-in flag")

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("## Worked example\nA long walk through one case.", id="heading-label"),
            pytest.param("- **Rationale**: a long explanation of why.", id="bold-label-in-list"),
            pytest.param("Background — how the old path worked.", id="dash-label"),
            pytest.param("**Why**: Recall dropped when the cache was cold, so the run reread it.", id="re-nouns"),
        ],
    )
    def test_label_vocabulary_proves_complement(self, text: str) -> None:
        """Rationale vocabulary in the label, with nothing against it, is the proof complement needs.

        The last case guards a false command match: "Recall dropped" starts with `Re` but is an explanation, not an
        instruction.
        """
        assert si.classify(text)[0] == "complement"

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("**Phase B3** (only when `--measured`): three steps.", id="paren-only-when"),
            pytest.param("**Phase B3**: only with `--top=N`; three steps.", id="flag-with-value"),
            pytest.param("**Phase B3**: three steps, only with --measured.", id="unquoted-flag"),
            pytest.param("**Phase B3**: three steps, only if `--measured` is set.", id="only-if"),
            pytest.param("**Phase B3**: three steps, only when `--measured` is passed.", id="only-when"),
            pytest.param("**Worktree isolation (opt-in `--worktree`)** — run first.", id="opt-in-flag-in-label"),
            pytest.param("**Phase M**: only with `--measured`, reads the missing transcripts.", id="missing-as-noun"),
            pytest.param("**Phase D**: only with `--deep` (off by default), runs the long pass.", id="off-by-default"),
            pytest.param("**Phase T**: only with `--team`, always spawning three teammates.", id="always-spawning"),
            pytest.param("**Step 8**: only when `--reply`, never in batch mode.", id="extra-condition"),
            pytest.param("**Phase T** (opt-in `--trace`): logs each call to the trace file.", id="each-call-object"),
            pytest.param("**Phase M**: only with `--measured`, reads every run's transcript.", id="every-runs-object"),
            pytest.param(
                "**Phase D**: only with `--deep`, scans each file regardless of size.", id="regardless-of-size"
            ),
            pytest.param(
                "**Phase D** (opt-in `--deep`): still experimental, runs the long pass.", id="descriptive-still"
            ),
        ],
    )
    def test_gate_shapes_are_gated_complement(self, text: str) -> None:
        """Every documented gate shape reads as complement, gated, even with unrelated negative words nearby.

        Polarity is read from phrases about the flag itself; words such as `missing`, `always` or `every` that describe
        what the step does must not cancel a real gate.
        """
        assert si.classify(text)[::2] == ("complement", True)


class TestSpineGuards:
    """Shapes that once scored as complement in review and must stay spine (fail-safe on doubt)."""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("- Record why the run failed in the report.", id="vocab-in-running-prose"),
            pytest.param("**Step 4**: run the example command and check the output.", id="vocab-after-label"),
            pytest.param("**Step 6**: attach each skill's measured dollars to its row.", id="measured-in-prose"),
            pytest.param("- Read the history of the file before editing it.", id="history-in-prose"),
            pytest.param(
                "**Rule**: never edit the plan once the build starts. Example: x.", id="directive-after-label"
            ),
            pytest.param(
                "**Check 41d — Legacy numbering**: scan for it.\n\n```bash\ngrep -n x f\n```\n\nPrecedent: one incident.",
                id="check-with-executable-block",
            ),
            pytest.param("**Rationale**: why it works.\n```bash\nls\n```", id="rationale-label-but-executable"),
            pytest.param(
                "**Background agents**: spawn each agent in the background, end the turn.", id="background-as-noun"
            ),
            pytest.param("**History check**: read the log and halt on mismatch.", id="history-as-noun"),
            pytest.param(
                "**Example output check**: compare the output and halt on mismatch.", id="example-in-longer-label"
            ),
            pytest.param("**Measured run**: run the collector and print the report.", id="measured-in-longer-label"),
            pytest.param("- Record why the run failed: write it to the report.", id="why-in-plain-label"),
            pytest.param("**Rationale**: never skip it; you must always ask first.", id="rationale-label-but-rules"),
            pytest.param("Forbidden regardless of who asks: reading the token file.", id="forbidden-is-high-stakes"),
            pytest.param(
                "**Note**: run `make sync-claude` after every push and check the cache.", id="note-instruction"
            ),
            pytest.param(
                "**Background**: spawn each agent in the background, end the turn.", id="background-instruction"
            ),
            pytest.param("1. **Why**: motivation and context (README, architecture docs)", id="ordered-item"),
            pytest.param(
                '- Example: `{"status":"done","file":"<path>"}` is what the agent writes.', id="json-deliverable"
            ),
            pytest.param("**Why**: you must run `make sync` after every push.", id="why-with-must"),
            pytest.param("**Example**: Don't pass `--force` to the publish helper.", id="example-dont"),
            pytest.param("**Example**: first, run the linter; then the tests.", id="example-first-run"),
            pytest.param("**Why**: please re-run the scan whenever the cache version changes.", id="why-please"),
            pytest.param(
                '**Worked example**: `{ "status": "done", "file": "<path>" }` is the reply.', id="spaced-json"
            ),
            pytest.param(
                "**Phase B4 — Size to importance (E11)**: deterministic, zero model tokens, the source tree only; it "
                "also covers `rules/`. Each block is judged spine or complement (complement only on proof: an opt-in "
                "flag gate or rationale vocabulary; doubt means spine).",
                id="real-phase-b4-opening",
            ),
        ],
    )
    def test_rationale_vocabulary_without_proof_stays_spine(self, text: str) -> None:
        """Rationale words in running prose, a later label, or a block that gives instructions stay spine.

        Each shape is an earlier review's false complement: a vocabulary word counts only as the block's own label and
        never over an instruction, a high-stakes word or an executable fence. The last case is the real Phase B4
        opening, which only describes the classifier.
        """
        assert si.classify(text)[0] == "spine"

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("**Phase B** runs always, not only with `--measured`: scan every file.", id="negated-gate"),
            pytest.param("**Phase A** is not opt-in: spawn one curator per file.", id="negated-opt-in"),
            pytest.param(
                "**Step 2**: parse `$ARGUMENTS` and run the scan; the JSON copy is written only with `--json`.",
                id="gate-in-later-subclause",
            ),
            pytest.param(
                "**Step 3**: list every opt-in flag the user passed and write its sentinel.", id="bare-opt-in-mention"
            ),
            pytest.param(
                "**Phase B** runs on every invocation, unlike the opt-in Phase B3.", id="opt-in-of-another-block"
            ),
            pytest.param("**Step 1**: opt-in flags are parsed first, then run the scan.", id="colon-opt-in-adjective"),
            pytest.param(
                "**Phase A** (opt-in flags are validated here) runs on every call.", id="paren-opt-in-adjective"
            ),
            pytest.param("**Step 2**: run the scan, and only then write output with `--json`.", id="only-then-with"),
            pytest.param(
                "**Step 3**: pass it to every helper, only those invoked with `--dry-run` skip.", id="only-those"
            ),
            pytest.param("**Step 4** - opt-in and default flags both reach the parser.", id="dash-opt-in-and"),
            pytest.param(
                "**Phase A** is opt-in for forks and runs on every call for the main repository.", id="is-opt-in-for"
            ),
            pytest.param("**Step 4**: print the summary, only if `--quiet` is not set.", id="only-if-not-set"),
            pytest.param("**Step 2**: write the report — only when `--dry-run` is absent.", id="only-when-absent"),
            pytest.param("**Step 5** only when `--json` is not passed, print the table.", id="bold-edge-not-passed"),
            pytest.param(
                "**Step 2 — Fetch PR** (requires `--repo` or a git remote): check it out.", id="requires-prerequisite"
            ),
            pytest.param("**Step 6**: run the self-only if `--x` mode check before any write.", id="hyphen-compound"),
            pytest.param(
                "**Step 2**: open the database read-only with `--dry-run` forwarded to helpers.", id="read-only-with"
            ),
            pytest.param(
                "**Step 3 — Lint** (opt-in `--strict`): ruff always runs; the rest is optional.", id="always-after-gate"
            ),
            pytest.param(
                "**Step 1**: the review is opt-in, the lint below always runs and must pass.", id="opt-in-then-always"
            ),
            pytest.param(
                "**Step 3**: lint every file, and run the **slow suite** only when `--full` is set.", id="second-bold"
            ),
            pytest.param("**Telemetry**: opt-in, and each run still writes the local log.", id="each-run-still"),
            pytest.param(
                "**Step 1**: this step is never skipped by the default path, only with `--x` extra.",
                id="never-in-descriptor",
            ),
        ],
    )
    def test_gate_wording_that_is_not_the_blocks_own_condition_stays_spine(self, text: str) -> None:
        """A gate phrase that is negated, qualifies a sub-clause, or sits behind an every-run statement is no gate.

        The flag must be the block's own condition: a mention of another block's flag, a flag that only changes what the
        step does, or a step that also always runs leaves the block loaded on every run, so it stays spine.
        """
        assert si.classify(text)[0] == "spine"

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(
                "## Step 5: Lint (always runs, only with `--deep` adds the slow suite)", id="always-runs-in-heading"
            ),
            pytest.param("## Step 5 — never skipped, only with `--deep` it adds a pass", id="never-in-heading"),
            pytest.param("## Step 5: Lint every file — only with `--fix` it rewrites them", id="it-after-dash-gate"),
            pytest.param(
                "## Step 5: Lint every file (only with `--fix` it also rewrites them)", id="it-after-paren-gate"
            ),
        ],
    )
    def test_heading_with_a_default_path_stays_spine(self, text: str) -> None:
        """A heading that says the step always runs, or that the flag only changes what it does, is not a gate.

        The step runs without the flag, so its section is part of the default path and cannot be moved behind one.
        """
        assert si.classify(text)[0] == "spine"


class TestGatedTokens:
    """How many tokens of a block sit behind its gate."""

    def test_run_scope_phrases_only_end_the_gated_lead(self) -> None:
        """A sentence that merely uses "every" or "always" inside the gated step stays counted.

        Only a statement that the step applies on every run ends the gated lead; ordinary uses of the words describe the
        gated step's behaviour and must not shrink the gated share.
        """
        text = (
            f"**Phase M**: only with `--measured`. Reads every session transcript. Always write the ledger. {pad(300)}"
        )
        assert si.gated_tokens(text) > 300

    def test_unconditional_sentence_ends_the_gated_lead(self) -> None:
        """A sentence in the lead paragraph that applies on every run is not counted behind the gate.

        Text that runs regardless of flags cannot move into a flag-loaded file, so it is excluded from the gated count.
        """
        text = f"**Phase B3**: only with `--measured`. {pad(200)}. Every run, regardless of flags, must write index.md. {pad(600)}"
        assert si.gated_tokens(text) < 260

    def test_fence_straight_after_the_lead_is_not_counted(self) -> None:
        """The loader or guard fence under a gated lead stays behind when the rest moves, so it is not counted.

        The fence is what loads the moved content, so only the lead sentence itself counts as gated.
        """
        text = f"**Phase B3**: only with `--measured`.\n\n```bash\n{pad(600)}\n```\n"
        assert si.gated_tokens(text) == si.estimate_tokens("**Phase B3**: only with `--measured`.")

    def test_gated_heading_section_stops_at_a_default_path_subsection_and_skips_the_loader(self) -> None:
        """A level-5 heading ends the gated section, and a unit that loads a mode file stays behind.

        The flag-only heading owns its body until a default-path subsection begins, and the `loads:` line is the stub
        that remains in place when the body moves.
        """
        text = (
            f"## Step 7: Draft reply (only when --reply)\n\nWrite it. {pad(200)}\n\n> loads: modes/team.md\n\n"
            f"##### Default path\n\nWithout the flag print the table. {pad(400)}\n"
        )
        assert 150 < si.gated_tokens(text) < 300


class TestScore:
    """Actions per load class, severity, copy collapsing and the exemptions."""

    def test_gated_block_in_a_mixed_file_is_moved(self, tmp_path: Path) -> None:
        """A flag-gated block beside ungated spine in one file is a move candidate.

        Phase A is spine every run needs; Phase B3 is large and only used with a flag, so it should move to its own file
        with low severity because the file is lazily loaded.
        """
        text = f"## Mode\n\n**Phase A**: run the audit, never skip the gate. {pad(900)}\n\n**Phase B3**: only with `--measured`. {pad(600)}\n"
        found = run(tmp_path, {"m.md": (text, "lazy")})["findings"]
        assert [(f["action"], f["severity"], f["reason"]) for f in found] == [
            ("move", "low", "gated by an opt-in flag")
        ]

    def test_complement_in_always_loaded_text_is_a_medium_move(self, tmp_path: Path) -> None:
        """Rationale in always-loaded text is moved, with medium severity.

        Always-loaded text is paid for on every turn, so a large rationale block there is a worse offender than the same
        block in a lazily loaded file.
        """
        text = _RATIONALE_RULE
        found = run(tmp_path, {"r.md": (text, "always")})["findings"]
        assert [(f["action"], f["severity"]) for f in found] == [("move", "medium")]

    @pytest.mark.parametrize(
        ("name", "text", "load"),
        [
            pytest.param(
                "skills/x/modes/m.md",
                f"**Phase B3**: only with `--measured`. {pad(700)}\n\n**Step two**: only with `--measured`. {pad(700)}\n",
                "lazy",
                id="file-that-is-the-gated-unit",
            ),
            pytest.param(
                "r.md",
                f"## Rule\n\n**Rationale**: {pad(100)}\n\n**Step**: do it. {pad(900)}\n",
                "always",
                id="small-complement-below-token-floor",
            ),
            pytest.param(
                "s.md",
                "**Worktree isolation (opt-in `--worktree`)** — run first.\n\n```bash\nls\n```\n\n"
                f"*Skip only when the mode is report. {pad(500)}*\n\nWhen skipping, drop both tasks. {pad(500)}\n",
                "lazy",
                id="paragraphs-governing-other-runs-not-counted",
            ),
            pytest.param(
                "skills/x/modes/m.md",
                "**Phase B3 — Measured evidence**: three steps, only with `--measured`.\n\n"
                f"B3a — list: {pad(300)}\n\n```bash\nls\n```\n\nB3b — ask: {pad(300)}\n\n"
                f"It records every skill: {pad(450)}\n",
                "lazy",
                id="flag-loaded-file-ignores-its-paragraphs",
            ),
            pytest.param(
                "skills/_shared/x.md",
                f"**Phase A**: never skip it. {pad(200)}\n\n**Phase B3**: only with `--x`. {pad(1200)}\n",
                "lazy",
                id="shared-file-is-the-gated-unit",
            ),
        ],
    )
    def test_no_finding(self, tmp_path: Path, name: str, text: str, load: str) -> None:
        """Nothing is flagged when the gated text is the file's purpose, too small, or not provably behind the gate.

        A flag-loaded mode or shared file is already the gated unit, a small complement is below the token floor, and
        paragraphs that govern other runs are not counted behind the gate, so a move would save nothing.
        """
        assert run(tmp_path, {name: (text, load)})["findings"] == []

    def test_spine_is_never_flagged_for_size(self, tmp_path: Path) -> None:
        """A huge spine block (no complement proof) produces no finding at any size.

        Size alone is never evidence a block is optional, so the two blocks are counted as spine and left alone.
        """
        text = f"## Rule\n\n**Step**: never skip the approval. {pad(5000)}\n"
        report = run(tmp_path, {"r.md": (text, "always")})
        assert (report["findings"], report["spine"]) == ([], 2)

    def test_lazy_complement_is_trimmed_only_above_the_median(self, tmp_path: Path) -> None:
        """Among lazy complement blocks, the one far above the median per-obligation weight is trimmed.

        Four small examples set the median, so only the single oversized worked example is worth trimming.
        """
        small = "\n\n".join(f"**Example**: case {i} {pad(150)}" for i in range(4))
        big = f"**Worked example**: {pad(900)}"
        text = f"## Notes\n\n{small}\n\n{big}\n"
        found = run(tmp_path, {"n.md": (text, "lazy")}, min_tokens=400)["findings"]
        assert [(f["action"], f["tokens"] > 800) for f in found] == [("trim", True)]

    def test_identical_blocks_collapse_with_a_copy_count(self, tmp_path: Path) -> None:
        """Byte-identical shared copies are reported once.

        Propagated files repeat verbatim across plugins; one finding with a copy count is actionable, N rows are not.
        """
        text = _RATIONALE_RULE
        found = run(tmp_path, {"a.md": (text, "always"), "b.md": (text, "always")})["findings"]
        assert [f["copies"] for f in found] == [2]

    def test_unreadable_file_yields_no_blocks(self, tmp_path: Path) -> None:
        """A section whose file is gone is skipped, not an error.

        The inventory may be older than the tree, so a missing file must degrade to zero blocks instead of raising.
        """
        item = section("gone.md", "x")
        assert si.score({"items": [item]}, tmp_path)["blocks"] == 0

    def test_pre_trim_shape_is_flagged_and_trimmed_shape_is_not(self, tmp_path: Path) -> None:
        """Real-case shape: a large flag-gated phase inside a mode is flagged; split out into its own file it is not.

        Scores the same content before and after the trim the check recommends, proving the finding clears once the
        gated phase lives in its own flag-loaded file.
        """
        body = f"**Phase A**: audit every file, never skip the gate. {pad(3000)}\n\n"
        gated = f"**Phase B3**: only with `--measured`. {pad(1500)}\n"
        before = run(tmp_path, {"mode.md": (body + gated, "lazy")})["findings"]
        after = run(tmp_path, {"mode.md": (body, "lazy"), "skills/x/modes/measured.md": (gated, "lazy")})["findings"]
        assert (len(before), after) == (1, [])

    def test_trim_saving_is_the_excess_over_the_median_weight(self) -> None:
        """A trim saves the tokens above what the block would weigh at the class median per obligation.

        Calls the decision helper directly with a known median so the saving arithmetic is pinned exactly.
        """
        block = si.Block("f.md", 1, 2, "t", "lazy", "x " * 1000)
        tokens = si.estimate_tokens(block.text)
        item = si.Scored(block, "complement", "r", False, 4, tokens)
        assert si._decide(item, 50.0, 400, 2.0) == ("trim", "low", tokens - 200)

    def test_copies_keep_the_worst_severity_and_every_location(self, tmp_path: Path) -> None:
        """The same gated block in a lazy and an always-loaded file is one finding at medium, with both locations.

        The always-loaded copy costs more, so the merged row must carry its severity and still list both places.
        """
        text = _GATED_MODE
        found = run(tmp_path, {"a.md": (text, "lazy"), "b.md": (text, "always")})["findings"]
        assert [(f["severity"], f["copies"], len(f["locations"])) for f in found] == [("medium", 2, 2)]

    def test_path_outside_the_root_is_skipped_and_counted(self, tmp_path: Path) -> None:
        """An inventory file path that leaves the root is never read.

        A hostile or stale inventory entry like `../out.md` must be counted as unreadable rather than followed.
        """
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "out.md").write_text("**Lead** text\n", encoding="utf-8")
        report = si.score({"items": [section("../out.md", "x\n")]}, root)
        assert (report["blocks"], report["unreadable"]) == (0, 1)

    def test_copy_that_raises_severity_takes_over_the_row(self, tmp_path: Path) -> None:
        """When a later copy is the worse one, the row names that copy's file and load, and the render lists the other.

        The always-loaded copy is read second, so the row must switch to it and the render must mention the lazy one.
        """
        text = _GATED_MODE
        report = run(tmp_path, {"a.md": (text, "lazy"), "b.md": (text, "always")})
        finding = report["findings"][0]
        assert (finding["file"], finding["load"]) == ("b.md", "always")
        assert "(also a.md:" in si.render(report)

    def test_substep_paragraphs_count_and_the_unrelated_tail_does_not(self, tmp_path: Path) -> None:
        """Sub-steps named after the gated label stay in the span; the closing paragraph with no gate does not.

        B3a-B3c belong to the gated phase and move with it, while the closing paragraph is not behind the gate, so the
        gated share lands between the sub-steps alone and the whole file.
        """
        text = (
            f"**Phase A**: audit every file, never skip the gate. {pad(1500)}\n\n"
            "**Phase B3 — Measured evidence**: three steps, only with `--measured`.\n\n"
            "> loads: bin/x.py — reads transcripts.\n\n"
            f"B3a — list the projects: {pad(200)}\n\n```bash\nls\n```\n\n"
            f"B3b — ask once: {pad(300)}\n\n"
            f"B3c — measure: {pad(100)}\n\n```bash\npwd\n```\n\n"
            f"It records every skill the scan flags: {pad(800)}\n"
        )
        found = run(tmp_path, {"m.md": (text, "lazy")})["findings"]
        assert [(f["action"], 400 <= f["tokens"] < 900) for f in found] == [("move", True)]

    def test_a_gated_heading_counts_its_whole_section(self, tmp_path: Path) -> None:
        """A section under a flag-only heading is conditional as a whole, so a large one is a move candidate.

        The heading itself names the flag, so both following paragraphs count even though neither mentions it.
        """
        text = (
            f"## Step 7: Draft the reply (only when --reply)\n\nWrite it. {pad(300)}\n\nReuse the report. {pad(300)}\n"
        )
        found = run(tmp_path, {"skills/x/SKILL.md": (text, "lazy")})["findings"]
        assert [(f["action"], f["tokens"] > 550) for f in found] == [("move", True)]

    def test_a_dominant_gated_block_in_a_loaded_rule_is_still_flagged(self, tmp_path: Path) -> None:
        """Own-gate is decided by the load mechanism, so an always-loaded rule never counts as the gated unit.

        The rule is mostly one gated block, but it is loaded on every turn, so the block is still a medium move.
        """
        text = f"## Rule\n\n**Rule A**: never skip it. {pad(300)}\n\n**Phase B3**: only with `--x`. {pad(1200)}\n"
        found = run(tmp_path, {"rules/r.md": (text, "always")})["findings"]
        assert [(f["action"], f["severity"]) for f in found] == [("move", "medium")]

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("skills/x/SKILL.md", id="skill-body"),
            pytest.param("skills/modes/SKILL.md", id="skill-folder-named-modes"),
        ],
    )
    def test_skill_file_is_never_the_gated_unit(self, tmp_path: Path, name: str) -> None:
        """A SKILL.md that is mostly one gated block is still scanned, even in a folder called `modes`.

        A skill body is loaded on invocation and is never a flag-loaded unit, so its gated block remains a move
        candidate; a skill folder merely named `modes` must not be mistaken for a mode file.
        """
        text = f"**Phase A**: never skip it. {pad(200)}\n\n**Phase B3**: only with `--x`. {pad(1200)}\n"
        found = run(tmp_path, {name: (text, "lazy")})["findings"]
        assert [f["action"] for f in found] == ["move"]

    def test_gated_rows_do_not_set_the_trim_median(self, tmp_path: Path) -> None:
        """Only ungated complement blocks set the lazy median, so gated rows cannot lower the trim bar.

        Six small gated blocks and one ungated example: the median must equal the example's own weight.
        """
        gated = "\n\n".join(f"**Step {i}**: only with `--x{i}`. {pad(30)}" for i in range(6))
        example = f"**Worked example**: {pad(450)}"
        report = run(tmp_path, {"n.md": (f"## Notes\n\n{gated}\n\n{example}\n", "lazy")})
        assert report["medians"] == {"lazy": float(si.estimate_tokens(example))}

    def test_unknown_load_class_in_an_external_inventory_does_not_crash_the_merge(self, tmp_path: Path) -> None:
        """Two byte-identical copies under a load class outside always, scoped and lazy are merged, not a KeyError.

        The inventory comes from outside this script, so an unexpected load label must degrade, not abort the audit.
        """
        text = _GATED_MODE
        found = run(tmp_path, {"a.md": (text, "lazy"), "b.md": (text, "on-demand")})["findings"]
        assert [f["copies"] for f in found] == [2]

    def test_equal_copies_name_the_most_read_load(self, tmp_path: Path) -> None:
        """Of two medium copies the row names the always-loaded one, whatever the file order.

        A scoped and an always-loaded copy tie on severity, so the tie must break toward the most-read load class.
        """
        text = _RATIONALE_RULE
        found = run(tmp_path, {"a.md": (text, "scoped"), "b.md": (text, "always")})["findings"]
        assert [(f["file"], f["load"], f["copies"]) for f in found] == [("b.md", "always", 2)]

    @pytest.mark.parametrize("name", ["efficiency.md", "efficiency-measured.md"])
    def test_shipped_efficiency_mode_files_flag_nothing(self, tmp_path: Path, name: str) -> None:
        """Real case: the trimmed mode file and its flag-only companion produce no finding.

        Reads the shipped audit mode files, so a regression in either the classifier or the files shows up here.
        """
        text = (MODES / name).read_text(encoding="utf-8")
        assert run(tmp_path, {f"skills/audit/modes/{name}": (text, "lazy")})["findings"] == []


class TestRender:
    """The markdown section built from a report."""

    def test_without_findings_says_so(self) -> None:
        """An empty finding list prints the all-clear line.

        A clean audit still has to say so, otherwise an empty section reads as a crashed check.
        """
        report = {"blocks": 3, "spine": 3, "complement": 0, "findings": []}
        assert "No complement block is out of proportion." in si.render(report)

    def test_truncates_to_top(self) -> None:
        """Rows past ``top`` are summarised, not printed.

        Three findings with ``top=2`` must print two table rows and a pointer to the JSON output for the third.
        """
        out = si.render({"blocks": 9, "spine": 5, "complement": 4, "findings": [_FINDING] * 3}, top=2)
        assert out.count("| move |") == 2
        assert "1 more in the JSON output." in out

    def test_escapes_pipes_in_a_title_and_reports_skipped_sections(self) -> None:
        """A pipe in a block title must not split the table row, and skipped sections are named.

        Titles come from arbitrary headings; an unescaped `|` would shift every later column of its row.
        """
        out = si.render(
            {"blocks": 2, "spine": 1, "complement": 1, "unreadable": 3, "findings": [{**_FINDING, "title": "a | b"}]}
        )
        assert "a \\| b" in out
        assert "3 section(s) skipped" in out


class TestMain:
    """The command line."""

    def test_prints_section_and_writes_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A given inventory is scored, the section printed and the JSON written.

        End to end through ``main``: the inventory file is read, one rationale block is found, and both outputs agree.
        """
        text = _RATIONALE_RULE
        (tmp_path / "r.md").write_text(text, encoding="utf-8")
        inv = tmp_path / "inv.json"
        inv.write_text(json.dumps({"items": [section("r.md", text, "always")]}), encoding="utf-8")
        out = tmp_path / "out.json"
        code = si.main(["--root", str(tmp_path), "--inventory", str(inv), "--json", str(out)])
        assert (code, "## Size to importance (E11)" in capsys.readouterr().out) == (0, True)
        assert json.loads(out.read_text())["findings"][0]["action"] == "move"

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("not json", id="bad-json"),
            pytest.param("{}", id="no-items"),
            pytest.param('{"items": []}', id="empty-items"),
        ],
    )
    def test_rejects_a_bad_inventory(self, tmp_path: Path, content: str) -> None:
        """An unreadable or wrongly shaped inventory exits 1.

        Malformed, itemless and empty inventories are all a blocked run rather than a silent all-clear.
        """
        inv = tmp_path / "inv.json"
        inv.write_text(content, encoding="utf-8")
        assert si.main(["--root", str(tmp_path), "--inventory", str(inv)]) == 1

    def test_missing_inventory_file_exits_1(self, tmp_path: Path) -> None:
        """An inventory path that does not exist is a blocked run, not a traceback.

        The path is given on the command line, so a typo must produce a clean non-zero exit.
        """
        assert si.main(["--root", str(tmp_path), "--inventory", str(tmp_path / "nope.json")]) == 1

    def test_later_item_missing_its_line_range_exits_1(self, tmp_path: Path) -> None:
        """Every item is checked, not only the first.

        The first item is valid and the second lacks its line range; validating only the head would let it through.
        """
        items = [section("a.md", "x\n"), {"file": "b.md", "load": {"hierarchy": "lazy"}}]
        inv = tmp_path / "inv.json"
        inv.write_text(json.dumps({"items": items}), encoding="utf-8")
        assert si.main(["--root", str(tmp_path), "--inventory", str(inv)]) == 1

    def test_runs_in_isolated_mode(self) -> None:
        """The script finds its sibling import under `python -I`, which drops the script directory from the path.

        Run as a real subprocess because the import-path behaviour of the installed script is the contract.
        """
        script = Path(si.__file__)
        done = subprocess.run(
            [sys.executable, "-I", str(script), "--help"], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0
