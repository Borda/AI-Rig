"""Guard that every progress line, status message and artifact the baseline cc_oss skills produced is still produced.

Turn-cost work on resolve, review, release and analyse merged Bash calls, moved asks and replaced polling. None of it
may hide anything the user or tooling could see before: run-dir files, report paths, status and ⛔/⏱/! BLOCKED lines,
step-level tasks, agent liveness. ``data/observability_baseline.json`` holds what the baseline skill text emitted,
extracted from the committed baseline; each entry maps the baseline wording to the wording that now carries it (equal
unless a message was deliberately reworded). These tests fail when any entry stops appearing in the current skill text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import agent_watch as aw
import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_SKILLS = _PLUGIN / "skills"
_BASELINE = json.loads((Path(__file__).parent / "data/observability_baseline.json").read_text(encoding="utf-8"))


def _skill_text(skill: str) -> str:
    """Concatenate every Markdown file one skill ships, so an output moved between its files still counts."""
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted((_SKILLS / skill).rglob("*.md")))


def _rows(key: str) -> list:
    """Build one parametrized case per baseline entry of a kind, across all skills."""
    return [
        pytest.param(skill, entry, id=f"{skill}-{index}")
        for skill, data in _BASELINE.items()
        for index, entry in enumerate(data[key])
    ]


class TestBaselineOutputsStillProduced:
    @pytest.mark.parametrize(("skill", "artifact"), _rows("artifacts"))
    def test_run_dir_artifact(self, skill: str, artifact: str) -> None:
        """Every run-dir file the baseline skill wrote is still named in the skill, so it is still written.

        A missing artifact is a file a later step, the user, or a downstream skill looked for and no longer finds.
        """
        assert artifact in _skill_text(skill)

    @pytest.mark.parametrize(("skill", "path"), _rows("output_paths"))
    def test_report_and_output_path(self, skill: str, path: str) -> None:
        """Every report or output path the baseline skill printed or wrote is still in the skill text.

        Users and downstream skills (resolve reading review reports, release reading review notes) find work by path.
        """
        assert path in _skill_text(skill)

    @pytest.mark.parametrize(("skill", "line"), _rows("status_lines"))
    def test_status_line(self, skill: str, line: dict) -> None:
        """Every progress, warning or blocked line the baseline skill printed is still printed, reworded at most.

        Rewordings are explicit in the baseline file (``head`` differs from ``now``); a silent drop fails here.
        """
        assert line["now"] in _skill_text(skill)


class TestRestoredVisibility:
    @pytest.mark.parametrize(
        ("skill", "relative", "needle"),
        [
            pytest.param(
                "resolve", "SKILL.md", "git remote -v | grep '(fetch)' | head -10", id="resolve-step4-remotes"
            ),
            pytest.param("resolve", "SKILL.md", "git status", id="resolve-step4-status"),
            pytest.param("review", "SKILL.md", ".expected-files", id="review-expected-files"),
            pytest.param("resolve", "modes/lint-qa-gate.md", "full-suite.log", id="resolve-full-suite-log"),
            pytest.param("resolve", "modes/lint-qa-gate.md", "full-suite.rc", id="resolve-full-suite-rc"),
            pytest.param("resolve", "modes/lint-qa-gate.md", "full-suite.seconds", id="resolve-full-suite-seconds"),
            pytest.param("resolve", "templates/resolve-report.md", "## Unblock push", id="report-unblock-push"),
            pytest.param("resolve", "templates/resolve-report.md", "Push", id="report-push-status"),
            pytest.param("resolve", "templates/resolve-report.md", "Challenge Log", id="report-challenge-log"),
            pytest.param("resolve", "templates/resolve-report.md", "## Confidence", id="report-confidence"),
        ],
    )
    def test_output_kept(self, skill: str, relative: str, needle: str) -> None:
        """Outputs the turn-cost rework first dropped and then restored stay in the file that produces them.

        These were the regressions: the Step 4 git context, review's expected-file list, the full-suite run record
        (its duration is otherwise lost once the suite runs in a background call) and the final report sections.
        """
        assert needle in (_SKILLS / skill / relative).read_text(encoding="utf-8")

    def test_agent_watch_reports_liveness(self, tmp_path: Path) -> None:
        """Agent_watch shows at least what the old probe did: which agents are pending, elapsed time, who timed out.

        One agent still inside its deadline, one past it with no deliverable: the report must name both by status.
        """
        (tmp_path / "agent-watch-impl.tsv").write_text(
            "fix-a\t-\t900\nfix-b\tmissing.md\t1\n", encoding="utf-8", newline="\n"
        )
        report = aw.watch(tmp_path, now=(tmp_path / "agent-watch-impl.tsv").stat().st_mtime + 60)
        assert (report["pending"], report["timed_out"], [agent["elapsed_s"] for agent in report["agents"]]) == (
            ["fix-a"],
            ["fix-b"],
            [60, 60],
        )

    @pytest.mark.parametrize("field", ["branch", "head_subject"])
    def test_state_snapshot_covers_git_output(self, field: str) -> None:
        """The one-call snapshot still shows what the separate ``git branch``/``git log -1`` calls printed."""
        assert f'"{field}"' in (_PLUGIN / "bin/git_state_snapshot.py").read_text(encoding="utf-8")


def _task_call_counts(text: str) -> dict[str, int]:
    """Count task creates and status transitions in every call form the skills use."""
    counts: dict[str, int] = {}
    for match in _TASK_CALL.finditer(text):
        kind = match.group(1) or match.group(2) or "create"
        counts[kind] = counts.get(kind, 0) + 1
    return counts


_TASK_CALL = re.compile(
    r"TaskUpdate\((?:status=\")?(in_progress|completed|deleted)"
    r"|TaskUpdate\(task_id=\w+, status=\"(in_progress|completed|deleted)\""
    r"|TaskCreate\(subject="
)


class TestTaskVisibility:
    @pytest.mark.parametrize("skill", list(_BASELINE))
    def test_task_transitions_match_baseline(self, skill: str) -> None:
        """Each skill still creates and moves as many step tasks as the baseline did, so progress stays visible.

        Riding task calls with real work removes turns, never transitions: fewer creates or status changes here means a
        step the user used to watch in the task list now runs silently.
        """
        assert _task_call_counts(_skill_text(skill)) == _BASELINE[skill]["task_calls"]

    @pytest.mark.parametrize(("skill", "phase"), _rows("task_phases"))
    def test_named_phase_task(self, skill: str, phase: str) -> None:
        """Phase task names the baseline listed for creation are still listed (analyse vitality's seven steps)."""
        assert f'"{phase}"' in _skill_text(skill)
