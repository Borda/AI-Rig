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
from pathlib import Path

import pytest

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"
_SHARED = _REVIEW.parent / "_shared"
_PLUGIN = _REVIEW.parents[1]
_BASH = shutil.which("bash")


def _skill() -> str:
    """Read the review skill."""
    return (_REVIEW / "SKILL.md").read_text(encoding="utf-8")


def _bash_path(path: Path) -> str:
    """Return the fixture path in Git Bash syntax on native Windows."""
    if sys.platform != "win32":
        return str(path)
    return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()


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
    """Every question the baseline review asked is still asked in its section, with the tool named beside it."""
    text = _skill()
    start = text.index(section_start)
    section = text[start : text.index(section_end, start)]
    assert marker in section
    assert "AskUserQuestion" in section


def test_codemap_gate_still_asks() -> None:
    """The codemap index gate is delegated to the shared contract, which must still ask before building."""
    assert 'cat "$_OSS_SHARED/codemap-gates.md"' in _skill()
    assert "Gate A always asks (`AskUserQuestion`)" in (_SHARED / "codemap-gates.md").read_text(encoding="utf-8")


class TestNoPolling:
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
        assert "never `ScheduleWakeup`, `ListAgents` or a `Monitor` loop" in skill

    @pytest.mark.skipif(_BASH is None, reason="the wake-up check is Bash")
    def test_wake_up_check_reports_agents(self, tmp_path: Path) -> None:
        """The documented wake-up block reads the run dir from its sentinel and prints one deadline report."""
        skill = _skill()
        anchor = skill.index("On each wake-up — a completion or idle notification")
        start = skill.index("```bash", anchor) + len("```bash\n")
        block = skill[start : skill.index("```", start)]
        session = "review-watch"
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "agent-watch-review.tsv").write_text(
            f"foundry:qa-specialist\t{_bash_path(run_dir)}/foundry--qa-specialist.md\t1800\n",
            encoding="utf-8",
            newline="\n",
        )
        (tmp_path / f"oss-review-run-dir-{session}").write_text(
            f"{_bash_path(run_dir)}\n", encoding="utf-8", newline="\n"
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
    def test_zero_bookkeeping_only_turns(self) -> None:
        """Task calls ride with real work; only the completion before a long output stands alone."""
        assert "**Zero bookkeeping-only turns**" in _skill()

    def test_cross_validation_task_stays_visible(self) -> None:
        """The Step 4 task is still created upfront and deleted when unused, so the step list matches the baseline."""
        skill = _skill()
        assert "always created upfront, so the step stays visible; the delete rides with Step 5's first call" in skill


def test_step_2_prelude_is_one_call() -> None:
    """Handoff protocol, Codex availability and prompt templates load in one Bash call instead of three."""
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
