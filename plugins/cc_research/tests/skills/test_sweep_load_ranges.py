"""Pin the partial skill-file loads sweep runs instead of ``cat``-ing whole owner files.

Sweep inlines plan steps P-P1..P-P3, the judge steps J1-J6 and the run steps R0-R7 by running a ``sed`` command over
each owner's SKILL.md. The commands are anchored on headings, not line numbers, so they survive edits above the anchor.
These tests execute the exact command text found in sweep/SKILL.md against the real owner files and check three
properties: every step sweep names is still loaded, nothing sweep executes sits in the dropped part, and a renamed
anchor prints nothing, which sweep's stop line treats as a failed load. The real ``sed`` call is the contract, so it is
never mocked.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_needs_sed = pytest.mark.skipif(shutil.which("sed") is None, reason="sed is not on PATH")

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_SWEEP = _SKILLS / "sweep" / "SKILL.md"
_STEP_HEADING = re.compile(r"^#{2,3} Step (\S+?):")
_CONSTANT_KEY = re.compile(r"^([A-Z][A-Z_]+):")
_STEP_REFERENCE = re.compile(r"\b(?:J[1-6][ab]?|R[0-7])\b")
_SENTINEL_WRITE = re.compile(r'>\s*"\$\{TMPDIR')
_JUDGE_HEAD_MARKERS = ("name: judge", "<objective>", "## Agent Resolution")


def _owner(skill: str) -> Path:
    """Return the owner SKILL.md path of ``skill``."""
    return _SKILLS / skill / "SKILL.md"


def _load_arguments(skill: str) -> list[str]:
    """Return the sweep load command for ``skill`` as an argv list without the trailing owner file path."""
    owner_ref = f'"$_RESEARCH_SKILLS/{skill}/SKILL.md"'
    lines = _SWEEP.read_text(encoding="utf-8").splitlines()
    commands = [line for line in lines if line.startswith("sed ") and line.endswith(owner_ref)]
    assert len(commands) == 1, f"sweep must hold exactly one sed load of {skill}/SKILL.md, found {len(commands)}"
    arguments = shlex.split(commands[0])
    assert arguments[-1] == f"$_RESEARCH_SKILLS/{skill}/SKILL.md"
    return arguments[:-1]


def _load(skill: str, source: Path | None = None) -> str:
    """Run sweep's load command for ``skill`` against its owner file, or against ``source`` when given."""
    target = source or _owner(skill)
    done = subprocess.run(
        [*_load_arguments(skill), str(target)], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return done.stdout


def _step_labels(text: str) -> set[str]:
    """Return the ``<label>`` of every ``Step <label>:`` heading in ``text``."""
    return {match[1] for line in text.splitlines() if (match := _STEP_HEADING.match(line))}


def _constant_keys(text: str) -> set[str]:
    """Return the keys of the yaml block inside the ``<constants>`` element of ``text``."""
    constants = text.split("<constants>", 1)[1].split("</constants>", 1)[0]
    return {match[1] for line in constants.splitlines() if (match := _CONSTANT_KEY.match(line))}


@_needs_sed
class TestLoadedRange:
    """Sweep's sed loads keep exactly the steps sweep runs from each owner skill."""

    def test_plan_keeps_p1_to_p3_only(self) -> None:
        """The plan load runs P-P1 through P-P3 (including P-P2b) and drops P-P0 and P-P4.

        Sweep needs the plan steps that write the program, not the preamble or the closing handoff, so the load is cut
        at the P-P1 and P-P3 headings.
        """
        assert _step_labels(_load("plan")) == {"P-P1", "P-P2", "P-P2b", "P-P3"}

    def test_judge_keeps_every_step_and_notes(self) -> None:
        """The judge load keeps J1-J6 and the notes, and drops only the head of the file.

        Sweep runs the whole judge procedure, so everything from the judge-mode heading to the end of the file must
        survive; the dropped head holds only front matter, objective and agent resolution.
        """
        loaded = _load("judge")
        assert _step_labels(loaded) == {"J1", "J2", "J3", "J4", "J5a", "J5b", "J6"}
        assert "<notes>" in loaded
        assert "Verdict deterministic" in loaded
        assert not any(marker in loaded for marker in _JUDGE_HEAD_MARKERS)
        assert _owner("judge").read_text(encoding="utf-8").endswith(loaded)

    def test_run_keeps_constants_agent_resolution_steps_notes(self) -> None:
        """The run load keeps constants, Agent Resolution (its sentinel feeds every mode load), R0-R7, modes, notes.

        Dropping Agent Resolution would leave the run-skill-dir sentinel unset for the mode loads that follow, so only
        the objective block may fall away.
        """
        loaded = _load("run")
        assert _step_labels(loaded) == {f"R{n}" for n in range(8)}
        required = ("<constants>", "</constants>", "## Agent Resolution", "research-run-skill-dir")
        assert all(token in loaded for token in required)
        assert all(token in loaded for token in ("## Resume Mode", "## Mode: colab", "<notes>"))
        assert "<objective>" not in loaded
        assert _owner("run").read_text(encoding="utf-8").endswith(loaded)

    def test_run_constants_defined_in_loaded_text(self) -> None:
        """Each constant the run steps name resolves to a key of the ``<constants>`` block the load keeps.

        A constant referenced by an R-step but defined only in the dropped part would be undefined when sweep runs.
        """
        loaded = _load("run")
        owner_keys = _constant_keys(_owner("run").read_text(encoding="utf-8"))
        loaded_keys = _constant_keys(loaded)
        referenced = set(re.findall(r"\b[A-Z]{3,}(?:_[A-Z]+)+\b", loaded.split("</constants>", 1)[1])) & owner_keys
        assert loaded_keys == owner_keys
        assert referenced <= loaded_keys

    @pytest.mark.parametrize("skill", ["judge", "run"])
    def test_dropped_head_leaves_no_unset_state(self, skill: str) -> None:
        """Nothing in the dropped head of judge/run writes state the kept steps read, unless sweep writes it itself.

        The head is skipped, so a sentinel it wrote or a ``--persist`` key it set would be missing at runtime unless
        sweep sets the same keys.
        """
        owner_text = _owner(skill).read_text(encoding="utf-8")
        dropped = owner_text[: len(owner_text) - len(_load(skill))]
        persisted_by_head = set(re.findall(r"--persist (\S+)", dropped))
        persisted_by_sweep = set(re.findall(r"--persist (\S+)", _SWEEP.read_text(encoding="utf-8")))
        assert not _SENTINEL_WRITE.search(dropped)
        assert persisted_by_head <= persisted_by_sweep

    def test_every_named_step_is_loaded(self) -> None:
        """Each J/R step label mentioned in sweep resolves to a heading in the matching loaded range.

        A step label that sweep cites but whose heading fell outside both loads would send the model to a step it never
        received.
        """
        mentioned = set(_STEP_REFERENCE.findall(_SWEEP.read_text(encoding="utf-8")))
        loaded = _step_labels(_load("judge")) | _step_labels(_load("run"))
        assert mentioned
        assert mentioned <= loaded


@_needs_sed
class TestAnchorDrift:
    """A drifted anchor must surface as an empty load, not as a silently shortened one."""

    @pytest.mark.parametrize(
        ("skill", "anchor", "renamed"),
        [
            pytest.param("plan", "### Step P-P1:", "### Step P-P1 (renamed):", id="plan"),
            pytest.param("judge", "## Judge Mode", "## Judge Section", id="judge"),
            pytest.param("run", "</objective>", "</objective >", id="run"),
        ],
    )
    def test_renamed_anchor_loads_nothing(self, skill: str, anchor: str, renamed: str, tmp_path: Path) -> None:
        """A renamed anchor yields an empty load, which sweep's ``Load prints nothing`` stop line catches.

        Each owner is copied to a temporary file with its anchor edited, so the real owner file is never touched. An
        empty load is the signal sweep stops on; a partial one would run with missing steps.
        """
        text = _owner(skill).read_text(encoding="utf-8")
        drifted = tmp_path / "SKILL.md"
        drifted.write_text(text.replace(anchor, renamed), encoding="utf-8")
        assert anchor in text
        assert _load(skill, drifted) == ""


class TestSweepInstructions:
    """Sweep's own prose carries the instructions the partial loads depend on."""

    def test_agent_resolution_runs_first(self) -> None:
        """Sweep tells the model to execute run's Agent Resolution block, whose sentinel the mode loads need.

        The run load keeps Agent Resolution but a model only runs it when sweep says so, before any mode load reads the
        ``research-run-skill-dir`` sentinel.
        """
        text = _SWEEP.read_text(encoding="utf-8")
        assert "Execute run's Agent Resolution block first" in text
        assert "research-run-skill-dir" in text

    @pytest.mark.parametrize("skill", ["plan", "judge", "run"])
    def test_stop_line_for_failed_load(self, skill: str) -> None:
        """Every partial load keeps a stop message for an empty or failed load.

        Without it a load that prints nothing, such as one hit by a renamed anchor, would be treated as success.
        """
        assert f"! sweep: cannot load {skill}/SKILL.md" in _SWEEP.read_text(encoding="utf-8")
