"""Pin the partial skill-file loads sweep runs instead of ``cat``-ing whole owner files.

Sweep inlines plan steps P-P1..P-P3, the judge steps J1-J6 and the run steps R0-R7 by running a ``sed`` command over
each owner's SKILL.md. The commands are anchored on headings, not line numbers, so they survive edits above the anchor.
These tests execute the exact command text found in sweep/SKILL.md against the real owner files and check three
properties: every step sweep names is still loaded, nothing sweep executes sits in the dropped part, and a renamed
anchor prints nothing, which sweep's stop line treats as a failed load.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_needs_sed = pytest.mark.skipif(shutil.which("sed") is None, reason="sed is not on PATH")

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_SWEEP = _SKILLS / "sweep" / "SKILL.md"
_LOAD_LINE = re.compile(r'^(?P<command>sed\b.*"\$_RESEARCH_SKILLS/(?P<skill>plan|judge|run)/SKILL\.md")$', re.MULTILINE)
_STEP_REFERENCE = re.compile(r"\b(?:J[1-6][ab]?|R[0-7])\b")
_SENTINEL_WRITE = re.compile(r'>\s*"\$\{TMPDIR')
_JUDGE_HEAD_MARKERS = ("name: judge", "<objective>", "## Agent Resolution")


def _owner(skill: str) -> Path:
    """Return the owner SKILL.md path of ``skill``."""
    return _SKILLS / skill / "SKILL.md"


def _load_arguments(skill: str) -> list[str]:
    """Return the sweep load command for ``skill`` as an argv list without the trailing owner file path."""
    matches = [m for m in _LOAD_LINE.finditer(_SWEEP.read_text(encoding="utf-8")) if m["skill"] == skill]
    assert len(matches) == 1, f"sweep must hold exactly one sed load of {skill}/SKILL.md, found {len(matches)}"
    arguments = shlex.split(matches[0]["command"])
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
    return set(re.findall(r"^#{2,3} Step (\S+?):", text, re.MULTILINE))


def _constant_keys(text: str) -> set[str]:
    """Return the keys of the yaml block inside the ``<constants>`` element of ``text``."""
    constants = text.split("<constants>", 1)[1].split("</constants>", 1)[0]
    return set(re.findall(r"^([A-Z][A-Z_]+):", constants, re.MULTILINE))


@_needs_sed
def test_plan_load_keeps_p1_to_p3_only() -> None:
    """The plan load runs P-P1 through P-P3 (incl.

    P-P2b) and drops P-P0 and P-P4.
    """
    assert _step_labels(_load("plan")) == {"P-P1", "P-P2", "P-P2b", "P-P3"}


@_needs_sed
def test_judge_load_keeps_every_step_and_the_notes() -> None:
    """The judge load keeps J1-J6 and the notes, and drops only the head of the file."""
    loaded = _load("judge")
    assert _step_labels(loaded) == {"J1", "J2", "J3", "J4", "J5a", "J5b", "J6"}
    assert "<notes>" in loaded
    assert "Verdict deterministic" in loaded
    assert not any(marker in loaded for marker in _JUDGE_HEAD_MARKERS)
    assert _owner("judge").read_text(encoding="utf-8").endswith(loaded)


@_needs_sed
def test_run_load_keeps_constants_agent_resolution_steps_and_notes() -> None:
    """The run load keeps constants, Agent Resolution (its sentinel feeds every mode load), R0-R7, modes and notes."""
    loaded = _load("run")
    assert _step_labels(loaded) == {f"R{n}" for n in range(8)}
    required = ("<constants>", "</constants>", "## Agent Resolution", "research-run-skill-dir")
    assert all(token in loaded for token in required)
    assert all(token in loaded for token in ("## Resume Mode", "## Mode: colab", "<notes>"))
    assert "<objective>" not in loaded
    assert _owner("run").read_text(encoding="utf-8").endswith(loaded)


@_needs_sed
def test_every_run_constant_is_defined_in_the_loaded_text() -> None:
    """Each constant the run steps name resolves to a key of the ``<constants>`` block the load keeps."""
    loaded = _load("run")
    owner_keys = _constant_keys(_owner("run").read_text(encoding="utf-8"))
    loaded_keys = _constant_keys(loaded)
    referenced = set(re.findall(r"\b[A-Z]{3,}(?:_[A-Z]+)+\b", loaded.split("</constants>", 1)[1])) & owner_keys
    assert loaded_keys == owner_keys
    assert referenced <= loaded_keys


@_needs_sed
@pytest.mark.parametrize("skill", [pytest.param("judge", id="judge"), pytest.param("run", id="run")])
def test_dropped_head_sets_no_state_sweep_leaves_unset(skill: str) -> None:
    """Nothing in the dropped head of judge/run writes state the kept steps read, unless sweep writes it itself."""
    owner_text = _owner(skill).read_text(encoding="utf-8")
    dropped = owner_text[: len(owner_text) - len(_load(skill))]
    persisted_by_head = set(re.findall(r"--persist (\S+)", dropped))
    persisted_by_sweep = set(re.findall(r"--persist (\S+)", _SWEEP.read_text(encoding="utf-8")))
    assert not _SENTINEL_WRITE.search(dropped)
    assert persisted_by_head <= persisted_by_sweep


@_needs_sed
def test_every_step_sweep_names_is_loaded() -> None:
    """Each J/R step label mentioned in sweep resolves to a heading in the matching loaded range."""
    mentioned = set(_STEP_REFERENCE.findall(_SWEEP.read_text(encoding="utf-8")))
    loaded = _step_labels(_load("judge")) | _step_labels(_load("run"))
    assert mentioned
    assert mentioned <= loaded


def test_sweep_runs_run_agent_resolution_first() -> None:
    """Sweep tells the model to execute run's Agent Resolution block, whose sentinel the mode loads need."""
    text = _SWEEP.read_text(encoding="utf-8")
    assert "Execute run's Agent Resolution block first" in text
    assert "research-run-skill-dir" in text


@_needs_sed
@pytest.mark.parametrize(
    ("skill", "anchor", "renamed"),
    [
        pytest.param("plan", "### Step P-P1:", "### Step P-P1 (renamed):", id="plan"),
        pytest.param("judge", "## Judge Mode", "## Judge Section", id="judge"),
        pytest.param("run", "</objective>", "</objective >", id="run"),
    ],
)
def test_renamed_anchor_prints_nothing(skill: str, anchor: str, renamed: str, tmp_path: Path) -> None:
    """A renamed anchor yields an empty load, which sweep's ``Load prints nothing`` stop line catches."""
    text = _owner(skill).read_text(encoding="utf-8")
    drifted = tmp_path / "SKILL.md"
    drifted.write_text(text.replace(anchor, renamed), encoding="utf-8")
    assert anchor in text
    assert _load(skill, drifted) == ""


@pytest.mark.parametrize(
    "skill", [pytest.param("plan", id="plan"), pytest.param("judge", id="judge"), pytest.param("run", id="run")]
)
def test_each_load_has_a_stop_line(skill: str) -> None:
    """Every partial load keeps a stop message for an empty or failed load."""
    assert f"! sweep: cannot load {skill}/SKILL.md" in _SWEEP.read_text(encoding="utf-8")
