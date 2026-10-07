"""Guard the resolve turn-budget contracts: run-level task ceiling, no-polling agent waits, one-call state checks.

Measured resolve runs spent ~27 task calls, ~24 ad-hoc git calls and ~7 improvised ``ScheduleWakeup``/``ListAgents``
calls each, every one a full-context turn. These tests pin the instructions that remove them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_RESOLVE = Path(__file__).resolve().parents[2] / "skills/resolve"
_REVIEW = _RESOLVE.parent / "review"
_PLUGIN = _RESOLVE.parents[1]
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="resolve blocks are Bash")


#: Every spawn site and the agent-watch batch file it must arm.
_SPAWN_SITES = [
    pytest.param("modes/pr-intelligence.md", "agent-watch-intel.tsv", id="intel"),
    pytest.param("modes/conflict-resolution.md", "agent-watch-conflict.tsv", id="conflict"),
    pytest.param("modes/action-item-dispatch.md", "agent-watch-challenge.tsv", id="challenge"),
    pytest.param("modes/action-item-dispatch.md", "agent-watch-impl.tsv", id="impl"),
    pytest.param("modes/lint-qa-gate.md", "agent-watch-qa.tsv", id="qa"),
]


def _read(relative: str) -> str:
    """Read one resolve skill file."""
    return (_RESOLVE / relative).read_text(encoding="utf-8")


def _bash_block_after(text: str, heading: str) -> str:
    """Return the first Bash block after a unique heading or phrase."""
    start = text.index("```bash", text.index(heading)) + len("```bash\n")
    return text[start : text.index("```", start)]


def _bash_path(path: Path) -> str:
    """Return the fixture path in Git Bash syntax on native Windows."""
    if sys.platform != "win32":
        return str(path)
    return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()


def _env(tmp_path: Path, session: str) -> dict[str, str]:
    """Build the environment a resolve Bash block runs in."""
    return os.environ | {
        "CLAUDE_CODE_SESSION_ID": session,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp_path),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }


#: Every step-level transition the baseline flow made; each must still happen so progress stays visible.
_STEP_TRANSITIONS = [
    ("TASK_GATHER", "in_progress"),
    ("TASK_GATHER", "completed"),
    ("TASK_SELECT", "in_progress"),
    ("TASK_SELECT", "completed"),
    ("TASK_CHECKOUT", "in_progress"),
    ("TASK_CHECKOUT", "completed"),
    ("TASK_CHECKOUT", "deleted"),
    ("TASK_CONFLICT", "in_progress"),
    ("TASK_CONFLICT", "completed"),
    ("TASK_CONFLICT", "deleted"),
    ("TASK_IMPL", "in_progress"),
    ("TASK_IMPL", "completed"),
    ("TASK_IMPL", "deleted"),
    ("TASK_LINT", "in_progress"),
    ("TASK_LINT", "completed"),
    ("TASK_CLOSE", "in_progress"),
    ("TASK_CLOSE", "completed"),
    ("TASK_CLOSE", "deleted"),
]


class TestTaskBudget:
    @pytest.mark.parametrize(
        "subject",
        [
            "Step 2: Gather action items",
            "Step 3: Select action items",
            "Step 4: Checkout PR branch [if pr mode]",
            "Steps 5–7: Conflict resolution [if pr mode]",
            "Step 8: Implement selected items [if items selected]",
            "Step 9: Lint and QA gate",
            "Steps 10–11: Push and final report [if pr mode]",
        ],
    )
    def test_step_level_tasks_stay_visible(self, subject: str) -> None:
        """Every baseline step task is still created, so the user sees the same step-level progress."""
        assert f'TaskCreate(subject="{subject}' in _read("SKILL.md")

    @pytest.mark.parametrize(("task", "status"), [pytest.param(t, s, id=f"{t}-{s}") for t, s in _STEP_TRANSITIONS])
    def test_every_step_transition_still_happens_once(self, task: str, status: str) -> None:
        """Each baseline transition appears exactly once — removed transitions hide progress, duplicates double-fire."""
        assert _read("SKILL.md").count(f'TaskUpdate(task_id={task}, status="{status}")') == 1

    @pytest.mark.parametrize(
        ("relative", "rule"),
        [
            pytest.param(
                "SKILL.md",
                "no response may consist only of `TaskCreate`/`TaskUpdate`/`TaskList` calls, per-item and per-conflict tasks included",
                id="skill-budget",
            ),
            pytest.param(
                "SKILL.md",
                "the only standalone one is `TASK_CLOSE` → `completed` immediately before the long Step 11 report",
                id="single-exception",
            ),
            pytest.param(
                "SKILL.md",
                "the same response as the Step 7b join's first tool call, so it is never a bookkeeping-only turn",
                id="per-item-creates",
            ),
            pytest.param("modes/action-item-dispatch.md", "never a response of task calls alone", id="step-8-updates"),
            pytest.param("modes/conflict-resolution.md", "riding with the next real tool call", id="conflict-creates"),
            pytest.param(
                "modes/conflict-resolution.md",
                "in one response, riding with the merge commit call",
                id="conflict-closes",
            ),
        ],
    )
    def test_no_bookkeeping_only_turns(self, relative: str, rule: str) -> None:
        """Every task call site rides with real work; only the completion before the long report stands alone.

        Standalone bookkeeping turns are the measured cost (each re-reads the whole context); the task count is not.
        """
        assert rule in _read(relative)

    def test_selection_gate_keeps_spinner_honest(self) -> None:
        """Step 3d still switches gather → select before the user's idle window, riding with the contract block."""
        skill = _read("SKILL.md")
        selection = skill[skill.index("## Step 3d") : skill.index("**Cap mechanics")]
        assert (
            '`TaskUpdate(task_id=TASK_GATHER, status="completed")` and `TaskUpdate(task_id=TASK_SELECT, status="in_progress")` both ride'
            in selection
        )

    def test_per_item_map_is_one_write_and_one_check(self) -> None:
        """The item→task map is written once and validated once, never appended one Bash call per item."""
        skill = _read("SKILL.md")
        step = skill[skill.index("## Step 3e") : skill.index("## Step 4")]
        assert "write the whole map in one Write tool call" in step
        assert '>> "$IMPL_DIR/item-tasks.tsv"' not in step
        assert "Issue every item's `TaskCreate` in **one response**" in step


class TestNoPolling:
    def test_resolve_forbids_polling_tools(self) -> None:
        """The wait rule names every improvised polling tool and the deadline mechanism replacing it."""
        skill = _read("SKILL.md")
        rule = skill[skill.index("## Agent wait discipline") : skill.index("## State checks")]
        assert "**Never** call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop" in rule
        assert "agent_watch.py" in rule
        assert "→ ⏱ `timed_out` now, never wait for it further" in rule

    @pytest.mark.parametrize(("relative", "batch_file"), _SPAWN_SITES)
    def test_every_spawn_site_arms_its_deadline(self, relative: str, batch_file: str) -> None:
        """Each spawn site writes its own watch batch and points at the shared wait rule."""
        text = _read(relative)
        assert batch_file in text
        assert "Agent wait discipline" in text

    @pytest.mark.parametrize("pattern", ["poll every", "polling interval", "CHALLENGE_POLL_S", "liveness probe"])
    def test_no_polling_cadence_remains(self, pattern: str) -> None:
        """No resolve or review instruction may still describe a poll cadence the model would act on."""
        files = [*_RESOLVE.rglob("*.md"), *_REVIEW.rglob("*.md")]
        assert [path.name for path in files if pattern in path.read_text(encoding="utf-8")] == []

    def test_review_shares_the_rule(self) -> None:
        """Oss:review spawns the same way, so it carries the same prohibition and immediate ⏱ on a missing file."""
        review = (_REVIEW / "SKILL.md").read_text(encoding="utf-8")
        assert "never call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop" in review
        assert "is ⏱ `timed_out` at once" in review

    @_skip_no_bash
    def test_watch_block_reports_batches(self, tmp_path: Path) -> None:
        """The documented check block reads IMPL_DIR from its sentinel and prints one JSON report."""
        session = "resolve-watch-block"
        impl_dir = tmp_path / "impl"
        impl_dir.mkdir()
        (impl_dir / "agent-watch-intel.tsv").write_text("intel\t-\t300\n", encoding="utf-8", newline="\n")
        (tmp_path / f"resolve-impl-dir-{session}").write_text(
            f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n"
        )
        block = _bash_block_after(_read("SKILL.md"), "## Agent wait discipline")
        result = subprocess.run(
            [_BASH, "-c", block], cwd=tmp_path, env=_env(tmp_path, session), capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["agents"][0]["status"] == "awaiting-envelope"


class TestStateChecks:
    @pytest.mark.skipif(_BASH is None or shutil.which("git") is None, reason="needs Bash and git")
    def test_state_check_block_prints_one_snapshot(self, tmp_path: Path) -> None:
        """The documented one-call state check returns the snapshot JSON resolve reads instead of separate git calls."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        block = _bash_block_after(_read("SKILL.md"), "## State checks — one call")
        result = subprocess.run(
            [_BASH, "-c", block],
            cwd=repo,
            env=_env(tmp_path, "resolve-state-check"),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["branch"] == "main"

    def test_rule_keeps_writes_and_guard_fences(self) -> None:
        """The snapshot replaces ad-hoc reads only; guard fences and git writes keep their own commands."""
        skill = _read("SKILL.md")
        rule = skill[skill.index("## State checks — one call") : skill.index("## Step 1")]
        assert "Writes (`add`, `commit`, `push`, `merge`) are never replaced." in rule
        assert "The guard fences inside steps keep their own git commands" in rule

    def test_step_6_context_is_one_call_with_reloaded_refs(self) -> None:
        """Steps 6a and 6b share one call that reloads its refs, instead of two calls reading unbound variables."""
        conflicts = _read("modes/conflict-resolution.md")
        step6 = conflicts[conflicts.index("## Step 6") : conflicts.index("## Step 7")]
        assert step6.count("```bash") == 1
        assert "resolve-base-ref-${CSID}" in step6
        assert "resolve-head-ref-${CSID}" in step6
