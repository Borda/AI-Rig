"""Contract tests for the live wiring of the reviewer environment, author claims and the Codex confidence codes.

The provisioner, the claim reproducer and the bridge's confidence fields existed, but no review step loaded or read
them: a missing package was still a bare gap, PR performance claims were only listed, and an unscored Codex lane was
named with free text. These tests pin the load sites and the reader rules, line by line, in the shipped review files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"


def _lines(relative: str) -> list[str]:
    """Return the lines of one shipped review file."""
    return (_REVIEW / relative).read_text(encoding="utf-8").splitlines()


def _line_starting(relative: str, prefix: str) -> str:
    """Return the one line of a review file that starts with ``prefix``."""
    matches = [line for line in _lines(relative) if line.startswith(prefix)]
    assert len(matches) == 1, f"{relative}: expected one line starting {prefix!r}, found {len(matches)}"
    return matches[0]


class TestLoadSites:
    """Step 2 loads both modes before the PR-code gate, so the gate can bind the provisioned interpreter."""

    def test_agent_launch_declares_and_renders_both_modes(self) -> None:
        """Both basenames are declared as loads and rendered by a fixed block after the Step 2 prelude."""
        lines = _lines("modes/agent-launch.md")
        assert "<!-- loads: provision-env.md, claim-repro.md -->" in lines
        prelude_end = lines.index("Follow the handoff protocol above. File absent → warn and continue without it.")
        renders = [
            i
            for i, line in enumerate(lines)
            if 'render_plugin_root.py" "$REVIEW_SKILL_DIR/modes/' in line
            and ("provision-env" in line or "claim-repro" in line)
        ]
        assert len(renders) == 2
        assert all(i > prelude_end for i in renders)
        provision, claims = renders
        assert "provision-env.md" in lines[provision]
        assert "claim-repro.md" in lines[claims]

    def test_provisioning_precedes_the_gate_plan(self) -> None:
        """The load paragraph says why provisioning comes first: the gate binds an argv naming ``env_python``."""
        rule = _line_starting("modes/agent-launch.md", "**Reviewer environment and author claims")
        assert "before the PR-code gate plans its command" in rule
        assert "`env_python` from `$RUN_DIR/review-env.json`" in rule


class TestAgentPrompts:
    """Agents read the provisioning record before raising a package gap, and the gate plans with its interpreter."""

    def test_gate_plans_with_the_provisioned_interpreter(self) -> None:
        """A ready or partial environment's interpreter is the command's first argument."""
        rule = _line_starting("templates/agent-prompts.md", "**Auto — a clean scan of this exact head")
        assert "the first argument is its `env_python`" in rule
        assert "never `.venv/bin/python`" in rule
        assert "or the file is absent, keep the project interpreter" in rule

    def test_guard_environment_follows_the_reconcile_block(self) -> None:
        """The launch-environment rule sits after the clause reconcile block, so 'the block below' stays adjacent."""
        lines = _lines("templates/agent-prompts.md")
        clause = next(i for i, line in enumerate(lines) if line.startswith("**PR-code clause — before dispatch.**"))
        guard_env = next(i for i, line in enumerate(lines) if line.startswith("**Guard launch environment.**"))
        record = next(i for i, line in enumerate(lines) if line.startswith("**Code-safety record"))
        assert lines[clause + 2] == "```bash"
        assert clause < guard_env < record
        assert "`VIRTUAL_ENV=<env_dir>`" in lines[guard_env]

    @pytest.mark.parametrize(
        "statement",
        [
            pytest.param(
                "a package in `installed[]` is present in `env_python`; use it and record no gap", id="installed"
            ),
            pytest.param("keeps the gap with that `cause` and the first `error_tail` line", id="failed"),
            pytest.param("is a contribution finding `<pkg>: gated (<gate_reason>)` and is never fetched", id="gated"),
            pytest.param("their per-command permission prompt stays that fallback's approval", id="overlay-approval"),
        ],
    )
    def test_missing_package_reads_the_record_first(self, statement: str) -> None:
        """The procedure opens with the provisioning record, and the overlay stays a prompted fallback."""
        rule = _line_starting("templates/agent-prompts.md", "**Missing-package evidence gap")
        assert statement in rule
        assert rule.index("review-env.json") < rule.index("claim needs a run")

    def test_host_class_is_a_fact_not_a_gap(self) -> None:
        """Every agent records the host class as a per-run fact and names only a class the host lacks."""
        rule = _line_starting("templates/agent-prompts.md", "**Confidence gaps — every agent:**")
        assert "recorded once per run" in rule
        assert "`not reproducible on this host: requires <CLASS>`" in rule
        assert "one macOS host" not in rule

    def test_perf_agent_writes_the_claim_benchmark(self) -> None:
        """The perf agent writes the benchmark and its claim list but never runs them."""
        rule = _line_starting("templates/agent-prompts.md", "**Agent 3 — foundry:perf-optimizer**")
        for token in ("`$RUN_DIR/claims.json`", "`$RUN_DIR/claim-bench.py`", "`$RUN_DIR/claim-bench-claims.txt`"):
            assert token in rule
        assert "never run them yourself" in rule


class TestConsolidator:
    """The consolidator reads the environment, the claim results and the bridge's own confidence codes."""

    def test_codex_lane_uses_the_bridge_codes(self) -> None:
        """A scored reply carries its gap lines; an unscored one is named by its closed-set code."""
        rule = _line_starting("templates/consolidator-prompt.md", "**Confidence parsing:**")
        assert "currently carries none" not in rule
        assert "`Codex co-review <status>: <confidence_null_reason>`" in rule
        assert "Carry each `confidence_gaps` line" in rule
        assert "`Codex co-review not run: peer-unavailable`" in rule
        assert "`Codex co-review not run: quota-exhausted`" in rule

    def test_bridge_absent_writes_the_peer_unavailable_record(self) -> None:
        """Step 2 leaves a one-line record when the bridge is absent, which the consolidator rule reads."""
        rule = _line_starting("modes/agent-launch.md", "**Bridge absent.**")
        assert "`⊘ codex not run: peer-unavailable — bridge@borda-ai-rig is <status>`" in rule
        consolidator = _line_starting("templates/consolidator-prompt.md", "**Confidence parsing:**")
        assert "`⊘ codex not run: peer-unavailable …`" in consolidator

    @pytest.mark.parametrize(
        "statement",
        [
            pytest.param("write one `**Environment**:` line", id="environment-line"),
            pytest.param("The host class is a fact of this run, never a gap or a confidence deduction", id="host-fact"),
            pytest.param("File absent on a code PR → `**Environment**: not provisioned", id="record-absent"),
        ],
    )
    def test_environment_rule(self, statement: str) -> None:
        """The environment record becomes one report line, and its absence is stated rather than skipped."""
        assert statement in _line_starting("templates/consolidator-prompt.md", "**Reviewer environment:**")

    @pytest.mark.parametrize(
        "statement",
        [
            pytest.param("`not-run` claims are execution gaps with their reason, never `not-reproduced`", id="not-run"),
            pytest.param("names exactly the class this run's host lacks", id="hardware-narrow"),
            pytest.param("`claims-result.json` is absent → every claim is `not-run`", id="results-absent"),
            pytest.param("Never restate the author's absolute numbers as reproduced", id="relative-only"),
        ],
    )
    def test_author_claims_rule(self, statement: str) -> None:
        """Every extracted claim reaches the report, including when the reproduction never ran."""
        assert statement in _line_starting("templates/consolidator-prompt.md", "**Author claims:**")

    def test_provisioning_failure_feeds_the_capability_kind(self) -> None:
        """A failed or gated package from the record keeps a package candidate UNVERIFIED with its cause."""
        item = _line_starting("templates/consolidator-prompt.md", "6. Every UNVERIFIED candidate")
        assert "`$RUN_DIR/review-env.json` lists in `failed[]` or `skipped_gated[]` is kind `package`" in item
        assert "`gated: <gate_reason>`" in item

    def test_report_template_has_environment_and_claim_lines(self) -> None:
        """The report template carries the two new evidence lines and a Codex row that names the null code."""
        lines = _lines("templates/review-report.md")
        assert sum(line.startswith("**Environment**:") for line in lines) == 1
        assert sum(line.startswith("**Author claim**:") for line in lines) == 1
        codex = next(line for line in lines if line.startswith("| Codex co-reviewer |"))
        assert "confidence_null_reason" in codex
        assert "confidence_gaps" in codex
