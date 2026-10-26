"""Tests for size_importance bin script.

Covers block splitting, the obligation count, the spine/complement judgment (spine on any doubt), the move and trim
actions per load class, the own-gate exemption, copy collapsing, and the command line.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import size_importance as si


def pad(tokens: int) -> str:
    """Return filler prose of about ``tokens`` tokens (bytes / 3) with no normative word and no marker word."""
    return " ".join(["lorem"] * (tokens * 3 // 6))


def section(path: str, text: str, load: str = "lazy") -> dict[str, object]:
    """Return an inventory item covering all of ``text`` (written by the caller at ``path``)."""
    return {"file": path, "start_line": 1, "end_line": len(text.split("\n")), "load": {"hierarchy": load}}


def run(tmp_path: Path, files: dict[str, tuple[str, str]], **kwargs: object) -> dict[str, object]:
    """Write ``{name: (text, load)}`` under ``tmp_path`` and score them as one inventory."""
    items = []
    for name, (text, load) in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
        items.append(section(name, text, load))
    return si.score({"items": items}, tmp_path, **kwargs)  # type: ignore[arg-type]


class TestSplitBlocks:
    """Covers where a section breaks into blocks."""

    def test_breaks_at_bold_lead_and_top_level_items(self) -> None:
        """A bold-lead paragraph and a top-level list item each start a block."""
        lines = ["## T", "intro", "**Lead** one", "more", "- item", "  - nested", "1. step"]
        assert [(s, e) for s, e, _ in si.split_blocks(lines, 10)] == [(10, 11), (12, 13), (14, 15), (16, 16)]

    def test_fenced_lines_never_start_a_block(self) -> None:
        """A list marker inside a code fence stays in its block."""
        lines = ["**Lead**", "```", "- not a start", "**not a lead**", "```", "- real"]
        assert [(s, e) for s, e, _ in si.split_blocks(lines, 1)] == [(1, 5), (6, 6)]


class TestObligations:
    """Covers the obligation proxy."""

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
    def test_counts(self, text: str, expected: int) -> None:
        """Normative sentences outside code plus one per fenced block."""
        assert si.obligations(text) == expected


class TestClassify:
    """Covers the judgment: complement only on positive proof, spine otherwise."""

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
    def test_role(self, text: str, role: str, gated: bool) -> None:
        """Each case pins the role and whether the block is gated."""
        got_role, _reason, got_gated = si.classify(text)
        assert (got_role, got_gated) == (role, gated)

    def test_gate_only_counts_in_the_lead(self) -> None:
        """An `opt-in` word inside a later code line does not gate the block."""
        text = "**Path rule** before mypy, check the path.\n```bash\n# opt-in only with `--strict`\n```"
        assert si.classify(text)[2] is False


class TestScore:
    """Covers actions per load class and the exemptions."""

    def test_gated_block_in_a_mixed_file_is_moved(self, tmp_path: Path) -> None:
        """A flag-gated block beside ungated spine in one file is a move candidate."""
        text = f"## Mode\n\n**Phase A**: run the audit, never skip the gate. {pad(900)}\n\n**Phase B3**: only with `--measured`. {pad(600)}\n"
        found = run(tmp_path, {"m.md": (text, "lazy")})["findings"]
        assert [(f["action"], f["severity"], f["reason"]) for f in found] == [
            ("move", "low", "gated by an opt-in flag")
        ]

    def test_file_that_is_the_gated_unit_is_not_flagged(self, tmp_path: Path) -> None:
        """A file made mostly of gated blocks loads only behind the gate, so there is nothing to move."""
        text = f"**Phase B3**: only with `--measured`. {pad(700)}\n\n**Step two**: only with `--measured`. {pad(700)}\n"
        assert run(tmp_path, {"skills/x/modes/m.md": (text, "lazy")})["findings"] == []

    def test_complement_in_always_loaded_text_is_a_medium_move(self, tmp_path: Path) -> None:
        """Rationale in always-loaded text is moved, with medium severity."""
        text = f"## Rule\n\n**Rationale**: {pad(600)}\n\n**Step**: do it. {pad(900)}\n"
        found = run(tmp_path, {"r.md": (text, "always")})["findings"]
        assert [(f["action"], f["severity"]) for f in found] == [("move", "medium")]

    def test_small_complement_is_left_alone(self, tmp_path: Path) -> None:
        """Below the token floor a complement block is not worth a move."""
        text = f"## Rule\n\n**Rationale**: {pad(100)}\n\n**Step**: do it. {pad(900)}\n"
        assert run(tmp_path, {"r.md": (text, "always")})["findings"] == []

    def test_spine_is_never_flagged_for_size(self, tmp_path: Path) -> None:
        """A huge spine block (no complement proof) produces no finding at any size."""
        text = f"## Rule\n\n**Step**: never skip the approval. {pad(5000)}\n"
        report = run(tmp_path, {"r.md": (text, "always")})
        assert (report["findings"], report["spine"]) == ([], 2)

    def test_lazy_complement_is_trimmed_only_above_the_median(self, tmp_path: Path) -> None:
        """Among lazy complement blocks, the one far above the median per-obligation weight is trimmed."""
        small = "\n\n".join(f"**Example**: case {i} {pad(150)}" for i in range(4))
        big = f"**Worked example**: {pad(900)}"
        text = f"## Notes\n\n{small}\n\n{big}\n"
        found = run(tmp_path, {"n.md": (text, "lazy")}, min_tokens=400)["findings"]
        assert [(f["action"], f["tokens"] > 800) for f in found] == [("trim", True)]

    def test_identical_blocks_collapse_with_a_copy_count(self, tmp_path: Path) -> None:
        """Byte-identical shared copies are reported once."""
        text = f"## Rule\n\n**Rationale**: {pad(600)}\n\n**Step**: do it. {pad(900)}\n"
        found = run(tmp_path, {"a.md": (text, "always"), "b.md": (text, "always")})["findings"]
        assert [f["copies"] for f in found] == [2]

    def test_unreadable_file_yields_no_blocks(self, tmp_path: Path) -> None:
        """A section whose file is gone is skipped, not an error."""
        item = section("gone.md", "x")
        assert si.score({"items": [item]}, tmp_path)["blocks"] == 0

    def test_pre_trim_shape_is_flagged_and_trimmed_shape_is_not(self, tmp_path: Path) -> None:
        """Real-case shape: a large flag-gated phase inside a mode is flagged; split out into its own file it is not."""
        body = f"**Phase A**: audit every file, never skip the gate. {pad(3000)}\n\n"
        gated = f"**Phase B3**: only with `--measured`. {pad(1500)}\n"
        before = run(tmp_path, {"mode.md": (body + gated, "lazy")})["findings"]
        after = run(tmp_path, {"mode.md": (body, "lazy"), "skills/x/modes/measured.md": (gated, "lazy")})["findings"]
        assert (len(before), after) == (1, [])


class TestRenderAndMain:
    """Covers the markdown section and the command line."""

    def test_render_without_findings_says_so(self) -> None:
        """An empty finding list prints the all-clear line."""
        report = {"blocks": 3, "spine": 3, "complement": 0, "findings": []}
        assert "No complement block is out of proportion." in si.render(report)

    def test_render_truncates_to_top(self) -> None:
        """Rows past ``top`` are summarised, not printed."""
        finding = {
            "action": "move", "severity": "low", "file": "f.md", "lines": "1-2", "title": "t", "load": "lazy",
            "tokens": 500, "obligations": 1, "copies": 1, "reason": "r",
        }  # fmt: skip
        out = si.render({"blocks": 9, "spine": 5, "complement": 4, "findings": [finding] * 3}, top=2)
        assert out.count("| move |") == 2
        assert "1 more in the JSON output." in out

    def test_main_prints_section_and_writes_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A given inventory is scored, the section printed and the JSON written."""
        text = f"## Rule\n\n**Rationale**: {pad(600)}\n\n**Step**: do it. {pad(900)}\n"
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
    def test_main_rejects_a_bad_inventory(self, tmp_path: Path, content: str) -> None:
        """An unreadable or wrongly shaped inventory exits 1."""
        inv = tmp_path / "inv.json"
        inv.write_text(content, encoding="utf-8")
        assert si.main(["--root", str(tmp_path), "--inventory", str(inv)]) == 1


class TestFailSafe:
    """Covers the misclassifications a review found: spine text must not read as complement."""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("**Phase B** runs always, not only with `--measured`: scan every file.", id="negated-gate"),
            pytest.param("**Phase A** is not opt-in: spawn one curator per file.", id="negated-opt-in"),
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
        ],
    )
    def test_stays_spine(self, text: str) -> None:
        """Each shape that once scored as complement is spine now."""
        assert si.classify(text)[0] == "spine"

    def test_gated_block_with_fences_is_still_gated(self) -> None:
        """A flag-only phase holding bash steps is complement: the gate outranks the executable-step rule."""
        text = "**Phase B3**: only with `--measured`.\n```bash\nls\n```"
        assert si.classify(text)[:2] == ("complement", "gated by an opt-in flag")

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("## Worked example\nA long walk through one case.", id="heading-label"),
            pytest.param("- **Rationale**: a long explanation of why.", id="bold-label-in-list"),
            pytest.param("Background — how the old path worked.", id="dash-label"),
        ],
    )
    def test_label_vocabulary_proves_complement(self, text: str) -> None:
        """Rationale vocabulary in the label, with nothing against it, is the proof complement needs."""
        assert si.classify(text)[0] == "complement"


class TestFences:
    """Covers the fence and line model shared with the inventory."""

    def test_long_tilde_fence_holds_an_inner_backtick_fence(self) -> None:
        """A four-tilde fence is closed only by four tildes, so a backtick fence inside it stays inside."""
        lines = ["**Lead**", "~~~~", "```", "- x", "```", "~~~~", "- real"]
        assert [(s, e) for s, e, _ in si.split_blocks(lines, 1)] == [(1, 6), (7, 7)]

    def test_empty_section_has_no_block(self) -> None:
        """An empty section yields no phantom block."""
        assert si.split_blocks([], 5) == []


class TestScoreEdges:
    """Covers saving arithmetic, copy collapsing, the root boundary and rendering."""

    def test_trim_saving_is_the_excess_over_the_median_weight(self) -> None:
        """A trim saves the tokens above what the block would weigh at the class median per obligation."""
        block = si.Block("f.md", 1, 2, "t", "lazy", "x " * 1000)
        tokens = si.estimate_tokens(block.text)
        item = si.Scored(block, "complement", "r", False, 4, tokens)
        assert si._decide(item, 50.0, 400, 2.0) == ("trim", "low", tokens - 200)

    def test_copies_keep_the_worst_severity_and_every_location(self, tmp_path: Path) -> None:
        """The same gated block in a lazy and an always-loaded file is one finding at medium, with both locations."""
        text = (
            f"## Mode\n\n**Phase A**: audit, never skip it. {pad(900)}\n\n**Phase B3**: only with `--x`. {pad(600)}\n"
        )
        found = run(tmp_path, {"a.md": (text, "lazy"), "b.md": (text, "always")})["findings"]
        assert [(f["severity"], f["copies"], len(f["locations"])) for f in found] == [("medium", 2, 2)]

    def test_path_outside_the_root_is_skipped_and_counted(self, tmp_path: Path) -> None:
        """An inventory file path that leaves the root is never read."""
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "out.md").write_text("**Lead** text\n", encoding="utf-8")
        report = si.score({"items": [section("../out.md", "x\n")]}, root)
        assert (report["blocks"], report["unreadable"]) == (0, 1)

    def test_render_escapes_pipes_in_a_title_and_reports_skipped_sections(self) -> None:
        """A pipe in a block title must not split the table row, and skipped sections are named."""
        finding = {
            "action": "move", "severity": "low", "file": "f.md", "lines": "1-2", "title": "a | b", "load": "lazy",
            "tokens": 500, "obligations": 1, "copies": 1, "reason": "r",
        }  # fmt: skip
        out = si.render({"blocks": 2, "spine": 1, "complement": 1, "unreadable": 3, "findings": [finding]})
        assert "a \\| b" in out
        assert "3 section(s) skipped" in out


class TestMainEdges:
    """Covers the command line beyond the happy path."""

    def test_missing_inventory_file_exits_1(self, tmp_path: Path) -> None:
        """An inventory path that does not exist is a blocked run, not a traceback."""
        assert si.main(["--root", str(tmp_path), "--inventory", str(tmp_path / "nope.json")]) == 1

    def test_a_later_item_missing_its_line_range_exits_1(self, tmp_path: Path) -> None:
        """Every item is checked, not only the first."""
        items = [section("a.md", "x\n"), {"file": "b.md", "load": {"hierarchy": "lazy"}}]
        inv = tmp_path / "inv.json"
        inv.write_text(json.dumps({"items": items}), encoding="utf-8")
        assert si.main(["--root", str(tmp_path), "--inventory", str(inv)]) == 1

    def test_runs_in_isolated_mode(self) -> None:
        """The script finds its sibling import under `python -I`, which drops the script directory from the path."""
        script = Path(si.__file__)
        done = subprocess.run(
            [sys.executable, "-I", str(script), "--help"], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0


class TestGateAndLabelPosition:
    """Covers round 2: a gate or rationale word counts only where it sits as the block's own condition or label."""

    @pytest.mark.parametrize(
        "text",
        [
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
        ],
    )
    def test_stays_spine(self, text: str) -> None:
        """Each shape the second review found scored as complement is spine now."""
        assert si.classify(text)[0] == "spine"

    def test_real_phase_b3_opening_is_gated(self) -> None:
        """The opening of the pre-trim Phase B3 paragraph (a flag-only phase) is complement, gated."""
        text = (
            "**Phase B3 — Measured evidence for inline `model:` pins**: three steps after the Phase B scan block, only "
            "with `--measured` (zero model tokens; installed and `--local` context alike). Without the flag nothing "
            "reads a transcript or writes a ledger."
        )
        assert si.classify(text)[:2] == ("complement", "gated by an opt-in flag")

    def test_real_phase_b4_opening_is_spine(self) -> None:
        """The opening of the Phase B4 paragraph only describes the classifier, so it stays spine."""
        text = (
            "**Phase B4 — Size to importance (E11)**: deterministic, zero model tokens, the source tree only; it also "
            "covers `rules/`. Each block is judged spine or complement (complement only on proof: an opt-in flag gate "
            "or rationale vocabulary; doubt means spine)."
        )
        assert si.classify(text)[0] == "spine"

    def test_copy_that_raises_severity_takes_over_the_row(self, tmp_path: Path) -> None:
        """When a later copy is the worse one, the row names that copy's file and load, and the render lists the
        other."""
        text = (
            f"## Mode\n\n**Phase A**: audit, never skip it. {pad(900)}\n\n**Phase B3**: only with `--x`. {pad(600)}\n"
        )
        report = run(tmp_path, {"a.md": (text, "lazy"), "b.md": (text, "always")})
        finding = report["findings"][0]
        assert (finding["file"], finding["load"]) == ("b.md", "always")
        assert "(also a.md:" in si.render(report)


MODES = Path(si.__file__).resolve().parents[1] / "skills" / "audit" / "modes"


class TestRound4Gate:
    """Covers round 3: a gate counts only in its exact shapes, and only the paragraphs proven behind it."""

    @pytest.mark.parametrize(
        "text",
        [
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
        ],
    )
    def test_stays_spine(self, text: str) -> None:
        """Each shape the third review scored as complement is spine now."""
        assert si.classify(text)[0] == "spine"

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("**Phase B3** (only when `--measured`): three steps.", id="paren-only-when"),
            pytest.param("**Phase B3**: only with `--top=N`; three steps.", id="flag-with-value"),
            pytest.param("**Phase B3**: three steps, only with --measured.", id="unquoted-flag"),
            pytest.param("**Phase B3**: three steps, only if `--measured` is set.", id="only-if"),
            pytest.param("**Phase B3**: three steps, only when `--measured` is passed.", id="only-when"),
            pytest.param("**Worktree isolation (opt-in `--worktree`)** — run first.", id="opt-in-flag-in-label"),
        ],
    )
    def test_gate_shapes_are_gated(self, text: str) -> None:
        """Every documented gate shape reads as complement, gated."""
        assert si.classify(text)[::2] == ("complement", True)

    def test_paragraphs_governing_other_runs_are_not_counted(self, tmp_path: Path) -> None:
        """Plain paragraphs after a gated lead and fence stay spine, so a small proven part is below the floor."""
        text = (
            "**Worktree isolation (opt-in `--worktree`)** — run first.\n\n```bash\nls\n```\n\n"
            f"*Skip only when the mode is report. {pad(500)}*\n\nWhen skipping, drop both tasks. {pad(500)}\n"
        )
        assert run(tmp_path, {"s.md": (text, "lazy")})["findings"] == []

    def test_substep_paragraphs_count_and_the_unrelated_tail_does_not(self, tmp_path: Path) -> None:
        """Sub-steps named after the gated label stay in the span; the closing paragraph with no gate does not."""
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

    @pytest.mark.parametrize(
        "text",
        [
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
            pytest.param("**Why**: you must run `make sync` after every push.", id="why-with-must"),
            pytest.param("**Example**: Don't pass `--force` to the publish helper.", id="example-dont"),
            pytest.param("**Example**: first, run the linter; then the tests.", id="example-first-run"),
            pytest.param("**Why**: please re-run the scan whenever the cache version changes.", id="why-please"),
            pytest.param(
                '**Worked example**: `{ "status": "done", "file": "<path>" }` is the reply.', id="spaced-json"
            ),
        ],
    )
    def test_round_four_shapes_stay_spine(self, text: str) -> None:
        """Each shape the fourth review scored as complement is spine now."""
        assert si.classify(text)[0] == "spine"

    @pytest.mark.parametrize(
        "text",
        [
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
    def test_gate_must_be_the_blocks_own_condition(self, text: str) -> None:
        """A gate phrase that modifies a sub-clause, or sits behind an every-run statement, does not gate the block."""
        assert si.classify(text)[0] == "spine"

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("**Phase M**: only with `--measured`, reads the missing transcripts.", id="missing-as-noun"),
            pytest.param("**Phase D**: only with `--deep` (off by default), runs the long pass.", id="off-by-default"),
            pytest.param("**Phase T**: only with `--team`, always spawning three teammates.", id="always-spawning"),
            pytest.param("**Step 8**: only when `--reply`, never in batch mode.", id="extra-condition"),
        ],
    )
    def test_unrelated_words_after_the_gate_do_not_drop_it(self, text: str) -> None:
        """Polarity is read from phrases about the flag, not from any negative word nearby."""
        assert si.classify(text)[::2] == ("complement", True)

    def test_run_scope_phrases_only_end_the_gated_lead(self) -> None:
        """A sentence that merely uses "every" or "always" inside the gated step stays counted."""
        text = (
            f"**Phase M**: only with `--measured`. Reads every session transcript. Always write the ledger. {pad(300)}"
        )
        assert si.gated_tokens(text) > 300

    def test_a_gated_heading_counts_its_whole_section(self, tmp_path: Path) -> None:
        """A section under a flag-only heading is conditional as a whole, so a large one is a move candidate."""
        text = (
            f"## Step 7: Draft the reply (only when --reply)\n\nWrite it. {pad(300)}\n\nReuse the report. {pad(300)}\n"
        )
        found = run(tmp_path, {"skills/x/SKILL.md": (text, "lazy")})["findings"]
        assert [(f["action"], f["tokens"] > 550) for f in found] == [("move", True)]

    def test_a_dominant_gated_block_in_a_loaded_rule_is_still_flagged(self, tmp_path: Path) -> None:
        """Own-gate is decided by the load mechanism, so an always-loaded rule never counts as the gated unit."""
        text = f"## Rule\n\n**Rule A**: never skip it. {pad(300)}\n\n**Phase B3**: only with `--x`. {pad(1200)}\n"
        found = run(tmp_path, {"rules/r.md": (text, "always")})["findings"]
        assert [(f["action"], f["severity"]) for f in found] == [("move", "medium")]

    def test_a_skill_body_is_never_the_gated_unit(self, tmp_path: Path) -> None:
        """A SKILL.md that is mostly one gated block is still scanned."""
        text = f"**Phase A**: never skip it. {pad(200)}\n\n**Phase B3**: only with `--x`. {pad(1200)}\n"
        found = run(tmp_path, {"skills/x/SKILL.md": (text, "lazy")})["findings"]
        assert [f["action"] for f in found] == ["move"]

    def test_nouns_that_start_with_re_are_not_commands(self) -> None:
        """ "Recall dropped" and "Reset cost" are explanations, not instructions."""
        assert si.classify("**Why**: Recall dropped when the cache was cold, so the run reread it.")[0] == "complement"

    def test_unconditional_sentence_ends_the_gated_lead(self) -> None:
        """A sentence in the lead paragraph that applies on every run is not counted behind the gate."""
        text = f"**Phase B3**: only with `--measured`. {pad(200)}. Every run, regardless of flags, must write index.md. {pad(600)}"
        assert si.gated_tokens(text) < 260

    def test_fence_straight_after_the_lead_is_not_counted(self) -> None:
        """The loader or guard fence under a gated lead stays behind when the rest moves, so it is not counted."""
        text = f"**Phase B3**: only with `--measured`.\n\n```bash\n{pad(600)}\n```\n"
        assert si.gated_tokens(text) == si.estimate_tokens("**Phase B3**: only with `--measured`.")

    def test_flag_loaded_file_stays_the_gated_unit_whatever_its_paragraphs_say(self, tmp_path: Path) -> None:
        """A flag-only file with a long closing paragraph that names no flag is still not flagged."""
        text = (
            "**Phase B3 — Measured evidence**: three steps, only with `--measured`.\n\n"
            f"B3a — list: {pad(300)}\n\n```bash\nls\n```\n\n"
            f"B3b — ask: {pad(300)}\n\n"
            f"It records every skill: {pad(450)}\n"
        )
        assert run(tmp_path, {"skills/x/modes/m.md": (text, "lazy")})["findings"] == []

    def test_gated_rows_do_not_set_the_trim_median(self, tmp_path: Path) -> None:
        """Only ungated complement blocks set the lazy median, so gated rows cannot lower the trim bar."""
        gated = "\n\n".join(f"**Step {i}**: only with `--x{i}`. {pad(30)}" for i in range(6))
        example = f"**Worked example**: {pad(450)}"
        report = run(tmp_path, {"n.md": (f"## Notes\n\n{gated}\n\n{example}\n", "lazy")})
        assert report["medians"] == {"lazy": float(si.estimate_tokens(example))}

    def test_unknown_load_class_in_an_external_inventory_does_not_crash_the_merge(self, tmp_path: Path) -> None:
        """Two byte-identical copies under a load class outside always, scoped and lazy are merged, not a KeyError."""
        text = (
            f"## Mode\n\n**Phase A**: audit, never skip it. {pad(900)}\n\n**Phase B3**: only with `--x`. {pad(600)}\n"
        )
        found = run(tmp_path, {"a.md": (text, "lazy"), "b.md": (text, "on-demand")})["findings"]
        assert [f["copies"] for f in found] == [2]

    def test_equal_copies_name_the_most_read_load(self, tmp_path: Path) -> None:
        """Of two medium copies the row names the always-loaded one, whatever the file order."""
        text = f"## Rule\n\n**Rationale**: {pad(600)}\n\n**Step**: do it. {pad(900)}\n"
        found = run(tmp_path, {"a.md": (text, "scoped"), "b.md": (text, "always")})["findings"]
        assert [(f["file"], f["load"], f["copies"]) for f in found] == [("b.md", "always", 2)]

    @pytest.mark.parametrize("name", ["efficiency.md", "efficiency-measured.md"])
    def test_shipped_efficiency_mode_files_flag_nothing(self, tmp_path: Path, name: str) -> None:
        """Real case: the trimmed mode file and its flag-only companion produce no finding."""
        text = (MODES / name).read_text(encoding="utf-8")
        assert run(tmp_path, {f"skills/audit/modes/{name}": (text, "lazy")})["findings"] == []


class TestRound6Scope:
    """Covers round 6: negations inside a label, heading sections, run-scope phrases and the own-gate path."""

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
        """A heading that says the step always runs, or that the flag only changes what it does, is not a gate."""
        assert si.classify(text)[0] == "spine"

    @pytest.mark.parametrize(
        "text",
        [
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
    def test_run_scope_words_about_the_step_do_not_drop_the_gate(self, text: str) -> None:
        """Only phrases that say the step runs on every run cancel a gate, not words about what the step does."""
        assert si.classify(text)[::2] == ("complement", True)

    def test_gated_heading_section_stops_at_a_default_path_subsection_and_skips_the_loader(self) -> None:
        """A level-5 heading ends the section, and a unit that loads a mode file stays behind."""
        text = (
            f"## Step 7: Draft reply (only when --reply)\n\nWrite it. {pad(200)}\n\n> loads: modes/team.md\n\n"
            f"##### Default path\n\nWithout the flag print the table. {pad(400)}\n"
        )
        assert 150 < si.gated_tokens(text) < 300

    def test_a_skill_folder_named_modes_is_not_a_mode_file(self, tmp_path: Path) -> None:
        """A SKILL.md under a skill folder called `modes` is still scanned."""
        text = f"**Phase A**: never skip it. {pad(200)}\n\n**Phase B3**: only with `--x`. {pad(1200)}\n"
        found = run(tmp_path, {"skills/modes/SKILL.md": (text, "lazy")})["findings"]
        assert [f["action"] for f in found] == ["move"]

    def test_a_shared_file_is_the_gated_unit(self, tmp_path: Path) -> None:
        """A lazy file under skills/_shared made mostly of one gated block is not flagged."""
        text = f"**Phase A**: never skip it. {pad(200)}\n\n**Phase B3**: only with `--x`. {pad(1200)}\n"
        assert run(tmp_path, {"skills/_shared/x.md": (text, "lazy")})["findings"] == []
