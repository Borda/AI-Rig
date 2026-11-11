"""Guard the oss:review efficiency changes without losing any user ask the committed baseline flow had.

Measured review runs spent ~8 polling-style calls (``ScheduleWakeup``, checkpoint ``touch``/``find`` probes, ``true``
no-ops), ~10 task calls and several one-file ``cat`` loads per run. These tests pin the replacements and the full set of
baseline asks, mapped by id, so a future removal fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest
from _text_io import bash_path

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"
_SHARED = _REVIEW.parent / "_shared"
_PLUGIN = _REVIEW.parents[1]
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="the wake-up check is Bash")


@cache
def _skill() -> str:
    """Read the review skill; the shipped text is immutable during a run."""
    return (_REVIEW / "SKILL.md").read_text(encoding="utf-8") + (_REVIEW / "modes/post-agent.md").read_text(
        encoding="utf-8"
    )


#: (section start, section end, marker that only exists while the baseline question is still asked there)
_ASKS = [
    pytest.param("### Existing-report guard", "## Step 1", "Re-run the full fan-out?", id="prior-report-kept"),
    pytest.param(
        "### Direct report fast-path",
        "### Agent 0",
        "A report path was passed without `--reply`",
        id="report-without-reply-kept",
    ),
    pytest.param(
        "### 7a — Follow-up gate",
        "### 7b — Confidence block",
        "invoke `AskUserQuestion` tool directly",
        id="follow-up-gate-kept",
    ),
]


@pytest.mark.parametrize(("section_start", "section_end", "marker"), _ASKS)
def test_baseline_ask_still_happens(section_start: str, section_end: str, marker: str) -> None:
    """Every question the baseline review asked is still asked in its section, with the tool named beside it.

    Efficiency work removed polling and task calls; this pins that none of the three user-facing asks (existing-report
    guard, direct report fast-path, follow-up gate) was dropped with them.
    """
    text = _skill()
    start = text.index(section_start)
    section = text[start : text.index(section_end, start)]
    assert marker in section
    assert "AskUserQuestion" in section


def test_codemap_gate_asks_only_for_the_lazy_preference() -> None:
    """Gate A prepares by default and retains a confirm-first question only when requested.

    The skill loads the shared gates document; that document must keep the automatic default, the explicit lazy
    preference, the autobuild opt-out and the force-off detection command.
    """
    assert 'cat "$_OSS_SHARED/codemap-gates.md"' in _skill()
    gates = (_SHARED / "codemap-gates.md").read_text(encoding="utf-8")
    assert "Gate A prepares automatically by default" in gates
    assert "`LAZY_CODEMAP` is the explicit confirm-first preference" in gates
    assert "SCAN_NO_AUTOBUILD=1" in gates
    assert "detect_codemap.py --prefix <current-skill> --force-off" in gates


def test_review_preflight_reloads_gate_state_and_freezes_stale_queries() -> None:
    """The review query block sees persisted gate decisions before running any batch.

    Every codemap query line must carry the no-heal guard, and the block must reload the enabled and currency sentinels
    instead of assuming an earlier shell still holds them.
    """
    context = (_REVIEW / "modes" / "codemap-context.md").read_text(encoding="utf-8")
    assert 'IFS= read -r CODEMAP_ENABLED  2>/dev/null < "${TMPDIR:-/tmp}/review-codemap-enabled-${CSID}"' in context
    assert 'IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/review-codemap-currency-${CSID}"' in context
    assert '[ "$CODEMAP_ENABLED" = "true" ] && command -v codemap-py' in context
    assert '[ -f "${_IDX}/${PROJ}.json" ]' not in context
    query_lines = [
        line for line in context.splitlines() if "codemap-py query" in line and not line.lstrip().startswith("#")
    ]
    assert query_lines
    assert all("$_CODEMAP_NO_HEAL" in line for line in query_lines)


class TestNoPolling:
    """Waiting for agents is event-driven: per-agent deadlines replace the silence probe and polling loops."""

    @pytest.mark.parametrize(
        "retired",
        [
            "HARD_CUTOFF",
            "EXTENSION=300",
            "REVIEW_CHECKPOINT",
            'find "$RUN_DIR" -newer',
            "POLL_START",
        ],
    )
    def test_silence_probe_is_gone(self, retired: str) -> None:
        """The checkpoint/find silence probe and its constants are replaced by per-agent deadlines."""
        assert retired not in _skill()

    @pytest.mark.parametrize("batch", ["review", "verify", "consolidate", "reply"])
    def test_every_spawn_arms_a_deadline(self, batch: str) -> None:
        """Every spawn site names its watch batch, and the rule forbids every waiting tool."""
        skill = _skill()
        assert f"`{batch}`" in skill[: skill.index("</constants>")]
        assert "never call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop" in skill

    @_skip_no_bash
    def test_wake_up_check_reports_agents(self, tmp_path: Path) -> None:
        """The documented wake-up block reads the run dir from its sentinel and prints one deadline report.

        Runs the shipped block in a real Bash against a run dir holding one pending agent deadline, so it proves the
        sentinel read, the helper call and the JSON report shape end to end.
        """
        skill = _skill()
        anchor = skill.index("On each wake-up — a completion or idle notification")
        start = skill.index("```bash", anchor) + len("```bash\n")
        block = skill[start : skill.index("```", start)]
        session = "review-watch"
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "agent-watch-review.tsv").write_text(
            f"foundry:qa-specialist\t{bash_path(run_dir)}/foundry--qa-specialist.md\t1800\n",
            encoding="utf-8",
            newline="\n",
        )
        (tmp_path / f"oss-review-run-dir-{session}").write_text(
            f"{bash_path(run_dir)}\n", encoding="utf-8", newline="\n"
        )
        result = subprocess.run(
            [_BASH, "-c", block],
            cwd=tmp_path,
            env=os.environ
            | {
                "CLAUDE_CODE_SESSION_ID": session,
                "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
                "TMPDIR": str(tmp_path),
                "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["agents"][0]["status"] == "pending"


class TestBookkeeping:
    """Task bookkeeping rides with real work; no turn is spent on bookkeeping alone."""

    def test_zero_bookkeeping_only_turns(self) -> None:
        """Task calls ride with real work; only the completion before a long output stands alone.

        The measured cost of a bookkeeping-only turn is a full context re-read, so the skill must keep the rule stated.
        """
        assert "**Zero bookkeeping-only turns**" in _skill()

    def test_cross_validation_task_stays_visible(self) -> None:
        """The Step 4 task is created upfront and never deleted: parent validation runs on every review.

        Only the verifier spawns depend on critical/blocking findings, so a review without them still completes the
        task.
        """
        skill = _skill()
        assert (
            "Never deleted: parent validation runs on every review, only the verifier spawns are conditional" in skill
        )


def test_step_2_prelude_is_one_call() -> None:
    """Handoff protocol, Codex availability and prompt templates load in one Bash call instead of three.

    Each separate call re-reads the whole context; the prelude block must name all three loads together.
    """
    skill = _skill()
    prelude = skill[skill.index("**Step 2 prelude — one call**") :]
    block = prelude[prelude.index("```bash") : prelude.index("```", prelude.index("```bash") + 7)]
    assert all(part in block for part in ("file-handoff-protocol.md", "check_bridge.py", "templates/agent-prompts.md"))


def test_small_diff_never_reviewed_inline() -> None:
    """The spawn-count gate never lets diff size replace the specialist fan-out with an inline orchestrator review.

    A 158-line fix PR was once reviewed by the orchestrator alone because the gate said work under ~73 calls should be
    done inline. That skipped the pinned qa-specialist and the challenger. A small change can carry the highest risk, so
    the gate must keep its "never zero" floor and must not bring back the inline escape.
    """
    skill = _skill()
    gate = skill[skill.index("**Spawn-count gate") : skill.index("**Dimension-gated codemap supplement**")]
    assert "do it inline, spawn nothing" not in gate
    assert "**never zero**" in gate
    assert "**Diff size never licenses inline review.**" in gate


def test_fix_scope_trim_is_gated_on_impact_tier() -> None:
    """A FIX drops perf and architecture review only when the change stays off the main code path.

    Diff size alone once decided depth, so a small fix inside a public entry point got the lighter lineup. The skip must
    stay tied to the codemap impact tier, with unknown impact defaulting to FULL.
    """
    skill = _skill()
    assert "skip Agent 3 (perf-optimizer), Agent 6 (solution-architect) **only when `IMPACT_TIER=LIGHT`**" in skill
    assert 'echo "FULL · impact unknown: codemap unavailable"' in skill
    assert 'IFS= read -r IMPACT_LINE < "${TMPDIR:-/tmp}/oss-review-impact-' in skill
    assert "review_impact_tier.py" in (_REVIEW / "modes" / "codemap-context.md").read_text(encoding="utf-8")


@_skip_no_bash
def test_worktree_entry_precedes_codemap_preparation(tmp_path: Path) -> None:
    """Executing preparation fences in document order detects codemap inside the reviewed checkout."""
    head = "a" * 40
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "pr-meta.json").write_text(json.dumps({"headRefOid": head}), encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-pr-tag-order").write_text("7\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-snap-dir-order").write_text(bash_path(snapshot) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-flags-order").write_text(
        "DIRECT_PATH_MODE=false\nWT_ENABLED=true\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "review-codemap-forced-off-order").write_text("true\n", encoding="utf-8", newline="\n")
    script = """
git() {
  case "$*" in
    'rev-parse --show-toplevel') echo "$TMPDIR" ;;
    'rev-parse HEAD') [ ! -f reviewed-head ] || { echo "$HEAD_SHA"; return 0; }; echo base ;;
    'status --porcelain') return 0 ;;
    'worktree add --detach '*) touch "$4/reviewed-head" ;;
    *) return 91 ;;
  esac
}
python() { printf '%s\\n' "$*" > "$TMPDIR/detected"; }
"""
    section = _skill().split("## Step 1:", 1)[1].split("### File scope detection", 1)[0]
    script += _preparation_fences(section)
    result = subprocess.run(
        [_BASH, "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {
            "TMPDIR": bash_path(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "order",
            "HEAD_SHA": head,
            "ARGUMENTS": "7 --worktree",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--arguments 7 --worktree" in (tmp_path / "detected").read_text()


def _preparation_fences(section: str) -> str:
    """Compose preparation recipes in source order, emulating the host's worktree entry after each call."""
    scripts = []
    active: list[str] | None = None
    for line in section.splitlines():
        if line == "```bash":
            active = []
        elif line == "```" and active is not None:
            block = "\n".join(active)
            if "_DETECT_CODEMAP=" in block or "git worktree add --detach" in block:
                scripts.append(
                    block
                    + '\nif [ -f "$TMPDIR/oss-review-wt-order" ]; then IFS= read -r WT < "$TMPDIR/oss-review-wt-order"; cd "$WT" || exit 1; fi\n'
                )
            active = None
        elif active is not None:
            active.append(line)
    return "\n".join(scripts)
