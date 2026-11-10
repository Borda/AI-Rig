"""Calibration problem validation and merge (``calibrate_validate_problems.py``).

Phase 1b of the calibrate pipeline must stop a bad answer key before it reaches scoring. A ground-truth answer that is
absent from the input, readable verbatim from a labelled line, contradicted by a near miss, or attached to an under-
built hard problem would score a correct reviewer as wrong. These tests pin each check in both directions, the whole-
problem rejection rule, and the merge contract (sorting, source tagging, collision prefixing, one scope problem, exit
codes).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import calibrate_validate_problems
import pytest
from calibrate_validate_problems import Floors, main, parse_floors, validate_problem

_SCRIPT = Path(calibrate_validate_problems.__file__)
#: Baseline input: two labelled header lines, then a short function whose file handle is never closed.
_INPUT = "Repository: acme/widgets\nType: issue\ndef load(path):\n    handle = open(path)\n    return handle.read()\n"


def _gt(**overrides: Any) -> dict[str, Any]:
    """Return a valid ground-truth item for ``_INPUT`` with ``overrides`` applied."""
    return {
        "claim": "file handle is never closed",
        "decisive": "open(path)",
        "role": "defect",
        "location": "load",
        "severity": "medium",
        **overrides,
    }


def _problem(**overrides: Any) -> dict[str, Any]:
    """Return a valid medium-difficulty problem with ``overrides`` applied."""
    return {
        "problem_id": "leaky-handle",
        "difficulty": "medium",
        "task_prompt": "Review this module.",
        "input": _INPUT,
        "ground_truth": [_gt()],
        "near_misses": [],
        **overrides,
    }


def _without(key: str) -> dict[str, Any]:
    """Return the baseline problem with the top-level field ``key`` removed."""
    problem = _problem()
    del problem[key]
    return problem


def _scope(problem_id: str) -> dict[str, Any]:
    """Return a valid out-of-scope problem with the given id."""
    return _problem(problem_id=problem_id, difficulty="scope", ground_truth=[], input="What is the capital of France?")


def _read(run_dir: Path, name: str) -> Any:
    """Return the parsed JSON content of one output file in ``run_dir``."""
    return json.loads((run_dir / name).read_text(encoding="utf-8"))


def _ids(run_dir: Path) -> list[tuple[str, str]]:
    """Return ``(source, problem_id)`` of every merged problem, in file order."""
    return [(record["source"], record["problem_id"]) for record in _read(run_dir, "problems.json")]


def _rejected_ids(run_dir: Path) -> list[str]:
    """Return the problem ids listed in ``problems-rejected.json``, in file order."""
    return [entry["problem_id"] for entry in _read(run_dir, "problems-rejected.json")]


def _rejected_reasons(run_dir: Path) -> str:
    """Return every rejection reason in ``problems-rejected.json`` joined into one searchable string."""
    return " | ".join(reason for entry in _read(run_dir, "problems-rejected.json") for reason in entry["reasons"])


#: Thread item ahead of its candidate pool, in the header layout of the real thread fixtures.
_ITEM = (
    "Repository: acme/widgets\n"
    "ITEM #900\n"
    "Type: issue\n"
    "State: open\n"
    "Title: Exporter crashes when the queue is empty\n"
    "Body:\n"
    "Running export on a fresh install raises IndexError in drain_queue. Possibly related to #777.\n"
    "CANDIDATE ITEMS (repository search on the title terms)\n"
)


def _row(number: int, title: str, body: str) -> str:
    """Return one candidate row in the open-pool format of the real thread fixtures."""
    return f"#{number} | state: open | title: {title} | body: {body}"


#: Canonical candidate of the duplicate: same defect, worded without any phrase of ``_ITEM``.
_CANONICAL = _row(
    801, "drain_queue fails on an empty backlog", "pop() on a list with nothing pending; length guard added."
)
#: Four unrelated candidates completing a five-row pool.
_OTHERS = (
    _row(802, "Slow export on large queues", "Throughput drops past ten thousand entries."),
    _row(803, "Partial file left after SIGTERM", "No flush before exit."),
    _row(804, "Resumable uploads", "Feature request for retrying after network loss."),
    _row(805, "Queue gauge reads zero", "Idle workers report no metrics."),
)
#: The pre-rule pool layout (``#N | kind | state | title | body``), which carries no field labels.
_UNLABELLED_ROWS = (
    "#801 | issue | closed | drain_queue fails on an empty backlog | Length guard added.",
    "#802 | issue | open | Slow export on large queues | Throughput drops.",
    "#803 | issue | open | Partial file left after SIGTERM | No flush.",
    "#804 | discussion | open | Resumable uploads | Feature request.",
    "#805 | issue | open | Queue gauge reads zero | Idle workers.",
)


def _thread(
    rows: tuple[str, ...], *, item: str = _ITEM, decisive: str = "#801", role: str = "duplicate"
) -> dict[str, Any]:
    """Return a thread problem whose input is ``item`` followed by ``rows`` and whose answer is ``decisive``."""
    answer = _gt(claim="#900 duplicates the canonical candidate", decisive=decisive, role=role, severity=None)
    return _problem(problem_id="empty-queue", input=item + "\n".join(rows) + "\n", ground_truth=[answer])


#: Axes each vitality extractor group prints, in the extractor's order.
_GROUP_AXES = {"A": ("1", "2", "5", "6"), "B": ("4", "7", "8"), "C": ("3", "9")}
#: Band-placement score range (anchor, maximum) per band, as the extractor scores clause axes.
_BAND_RANGE = {"🔴": (1, 3), "🟡": (4, 6), "🟢": (10, 10)}


def _checkpoint_axis(band: str, met: int, applicable: int) -> dict[str, Any]:
    """Return a checkpoint axis (5, 6, 7) shaped like the extractor's, with ``met`` of ``applicable`` met."""
    checkpoints = {
        str(n): {"state": "met" if n <= met else "unmet", "why": "fixture"} for n in range(1, applicable + 1)
    }
    score = met * 10 // applicable
    return {
        "band": band,
        "score": score,
        "conf": 1.0,
        "conf_degraders": [],
        "checkpoints": checkpoints,
        "met": met,
        "indeterminate": 0,
        "applicable": applicable,
        "score_strict": score,
        "score_upper": score,
    }


def _clause_axis(band: str, held: list[str], red: list[str]) -> dict[str, Any]:
    """Return a band-placement axis (1, 2, 3, 4, 8) shaped like the extractor's."""
    anchor, cap = _BAND_RANGE[band]
    score = min(cap, anchor + len(held))
    return {"band": band, "score": score, "clauses_held": held, "red_held": red, "conf": 1.0, "conf_degraders": []}


def _trajectory_axis(band: str, subs: dict[str, int | None]) -> dict[str, Any]:
    """Return a trajectory axis (9) shaped like the extractor's, from its sub-signal scores."""
    available = [score for score in subs.values() if score is not None]
    return {
        "band": band,
        "score": sum(available) / len(available),
        "sub_scores": subs,
        "conf": 1.0,
        "conf_degraders": [],
    }


#: Every axis far from a band line; bands follow the real ``vit-high-bot-inflated`` fixture except axis 3, whose 🔴
#: rests on two red conditions here instead of one.
_FAR_AXES: dict[str, dict[str, Any]] = {
    "1": _clause_axis("🟢", ["median_issue_response ≤7d", "median_pr_response ≤5d", "pct_responded_7d ≥60%"], []),
    "2": _clause_axis("🟢", ["last commit ≤14d", "commits/30d ≥5"], []),
    "3": _clause_axis("🔴", [], ["top contributor >75%", "retention <30%"]),
    "4": _clause_axis("🔴", [], ["stale >30%", "merge_rate <0.3"]),
    "5": _checkpoint_axis("🟢", 5, 5),
    "6": _checkpoint_axis("🟢", 9, 9),
    "7": _checkpoint_axis("🟢", 6, 7),
    "8": _clause_axis("🟢", ["0 critical/high alerts", "dep-config present"], []),
    "9": _trajectory_axis("🟡", {"9A": 0, "9B": 10, "9C": 0, "9D": 10}),
}
#: Two axes one step from another band: axis 5 at its 🟢 cutoff (4 of 5), axis 6 one below its 🟢 cutoff (6 of 9).
_TWO_ONE_STEP = {"5": _checkpoint_axis("🟢", 4, 5), "6": _checkpoint_axis("🟡", 6, 9)}
#: Merged-problem ids when the boundary problem passes, and when it is rejected.
_ACCEPTED = [("claude", "vit-high")]
_REJECTED: list[tuple[str, str]] = []


def _derived_files(overrides: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Return group letter → JSON text of a real-shaped extractor output, ``overrides`` applied over ``_FAR_AXES``."""
    axes = {**_FAR_AXES, **overrides}
    datasets = {"used": [], "missing": [], "not_present": [], "optional_absent": [], "partial": []}
    return {
        group: json.dumps(
            {
                "group": group,
                "analysis_now": 1791467548,
                "datasets": datasets,
                "axes": {number: axes[number] for number in numbers},
            }
        )
        for group, numbers in _GROUP_AXES.items()
    }


#: The three saved outputs of a problem with every axis far from a band line.
_FAR_FILES = _derived_files({})


def _vitality(difficulty: str = "high") -> dict[str, Any]:
    """Return a vitality problem with id ``vit-high``, long enough for every size floor, at ``difficulty``."""
    answer = _gt(claim="axis 4 is red", decisive="🔴", role="axis 4 band", location="axis 4", severity=None)
    return _problem(problem_id="vit-high", difficulty=difficulty, input=_INPUT.ljust(12000, "x"), ground_truth=[answer])


def _boundary_argv(run_dir: Path) -> list[str]:
    """Return the command line of a vitality run over ``run_dir`` with the boundary check on."""
    return ["--run-dir", str(run_dir), "--derived-gt", "--boundary"]


@pytest.fixture
def write_problem(tmp_path: Path) -> Callable[[str, str, object], Path]:
    """Return a callable writing ``problems-<source>-<stem>.json`` into the run dir ``tmp_path``."""

    def _write(source: str, stem: str, payload: object) -> Path:
        """Write one problem file and return its path."""
        path = tmp_path / f"problems-{source}-{stem}.json"
        path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
        return path

    return _write


@pytest.fixture
def write_derived(tmp_path: Path) -> Callable[[dict[str, str]], None]:
    """Return a callable writing ``derived-vit-high-<group>.json`` files, group letter → raw text, into ``tmp_path``."""

    def _write(files: dict[str, str]) -> None:
        """Write each group's saved extractor output as given, valid JSON or not."""
        for group, text in files.items():
            (tmp_path / f"derived-vit-high-{group}.json").write_text(text, encoding="utf-8", newline="\n")

    return _write


class TestParseFloors:
    """``--floors`` parsing into input-length floors."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("high=7000", Floors(high=7000, extreme=12000), id="high-only-keeps-extreme-default"),
            pytest.param("extreme=1", Floors(high=6000, extreme=1), id="extreme-only-keeps-high-default"),
            pytest.param(" high = 5 , extreme=9 ", Floors(high=5, extreme=9), id="both-with-spaces"),
            pytest.param("high=0", Floors(high=0, extreme=12000), id="zero-disables-floor"),
        ],
    )
    def test_applies_given_values(self, text: str, expected: Floors) -> None:
        """Each given pair overrides its default while an omitted difficulty keeps the default floor.

        A partial override (``high=7000`` alone) must not silently zero the other floor.
        """
        floors = parse_floors(text)

        assert floors == expected

    @pytest.mark.parametrize("text", ["", "high", "medium=10", "high=-1", "high=1.5", "high=abc", "high=1,,"])
    def test_rejects_malformed_value(self, text: str) -> None:
        """A malformed pair, an unknown difficulty, or a non-integer value raises ``ValueError``.

        Accepting it would run the size check with a floor the caller never asked for.
        """
        with pytest.raises(ValueError, match="floor"):
            parse_floors(text)


class TestSchema:
    """Check 1: problem schema, field types, and repeated ``decisive`` + ``role`` pairs."""

    @pytest.mark.parametrize(
        "problem",
        [
            pytest.param(_problem(), id="baseline"),
            pytest.param(_without("near_misses"), id="near-misses-absent"),
            pytest.param(_problem(near_misses=None), id="near-misses-null"),
            pytest.param(_problem(ground_truth=[_gt(severity=None)]), id="severity-null"),
            pytest.param(_problem(ground_truth=[_gt(location="")]), id="location-empty-string"),
            pytest.param(_problem(source="claude"), id="source-matches-file-name"),
            pytest.param(_problem(source=None), id="source-null-uses-file-name"),
            pytest.param(_problem(provenance={"repo": "acme/widgets"}), id="unknown-key-passes-through"),
            pytest.param(_scope("off-topic"), id="scope-with-empty-ground-truth"),
            pytest.param(
                _problem(ground_truth=[_gt(), _gt(role="style", claim="bare open call")]),
                id="same-decisive-different-role",
            ),
        ],
    )
    def test_accepts_valid_problem(self, problem: dict[str, Any]) -> None:
        """A problem matching schema v2, optional fields included or omitted, yields no rejection reason.

        Over-strict validation would discard good problems and starve the scoring phase.
        """
        reasons = validate_problem(problem, "claude", Floors())

        assert reasons == []

    @pytest.mark.parametrize(
        ("problem", "fragment"),
        [
            pytest.param([_problem()], "one JSON object", id="array-not-object"),
            pytest.param(_without("problem_id"), "problem_id", id="problem-id-missing"),
            pytest.param(_problem(problem_id="Leaky_Handle"), "problem_id", id="problem-id-not-kebab"),
            pytest.param(_problem(difficulty="hard"), "difficulty", id="difficulty-unknown"),
            pytest.param(_problem(difficulty=["high"]), "difficulty", id="difficulty-unhashable"),
            pytest.param(_problem(source="codex"), "disagrees with file name", id="source-mismatch"),
            pytest.param(_problem(task_prompt="  "), "task_prompt", id="task-prompt-blank"),
            pytest.param(_problem(input=""), "input must be", id="input-empty"),
            pytest.param(_problem(ground_truth="open(path)"), "ground_truth must be a list", id="ground-truth-str"),
            pytest.param(_problem(ground_truth=[]), "non-empty for a non-scope", id="non-scope-empty-ground-truth"),
            pytest.param(_scope("off-topic") | {"ground_truth": [_gt()]}, "scope problem", id="scope-with-answers"),
            pytest.param(_problem(ground_truth=["open(path)"]), "ground_truth[0] must be", id="item-not-object"),
            pytest.param(_problem(ground_truth=[_gt(decisive=" ")]), "decisive must be", id="decisive-blank"),
            pytest.param(_problem(ground_truth=[_gt(claim=None)]), "claim must be", id="claim-null"),
            pytest.param(_problem(ground_truth=[_gt(role=7)]), "role must be", id="role-not-str"),
            pytest.param(_problem(ground_truth=[_gt(location=3)]), "location must be", id="location-not-str"),
            pytest.param(_problem(ground_truth=[_gt(severity="High")]), "severity", id="severity-wrong-case"),
            pytest.param(_problem(ground_truth=[_gt(severity="blocker")]), "severity", id="severity-unknown"),
            pytest.param(
                _problem(ground_truth=[_gt(), _gt(decisive=" OPEN(path)", role="Defect", claim="other")]),
                "repeats the decisive+role pair",
                id="duplicate-pair-case-insensitive",
            ),
            pytest.param(_problem(near_misses="open(path)"), "near_misses must be a list", id="near-misses-str"),
            pytest.param(_problem(near_misses=["open"]), "near_misses[0] must be", id="near-miss-not-object"),
            pytest.param(
                _problem(near_misses=[{"ref": "handle.read()"}]), "wrong_role must be", id="near-miss-no-wrong-role"
            ),
        ],
    )
    def test_rejects_invalid_problem(self, problem: object, fragment: str) -> None:
        """A schema or type violation yields a reason naming the offending field.

        The reason text lands in ``problems-rejected.json``, so it must point the generator at what to fix.
        """
        reasons = validate_problem(problem, "claude", Floors())

        assert fragment in " | ".join(reasons)


class TestOccurrence:
    """Check 2: every answer-bearing ``decisive`` and every near-miss ``ref`` must occur in ``input``."""

    @pytest.mark.parametrize(
        "problem",
        [
            pytest.param(_problem(ground_truth=[_gt(decisive="OPEN(PATH)")]), id="decisive-other-case"),
            pytest.param(_problem(ground_truth=[_gt(decisive="  open(path)  ")]), id="decisive-padded"),
            pytest.param(_problem(near_misses=[{"ref": "Handle.Read()", "wrong_role": "defect"}]), id="ref-other-case"),
        ],
    )
    def test_accepts_text_present_in_input(self, problem: dict[str, Any]) -> None:
        """Text found in ``input`` ignoring case and surrounding whitespace passes.

        Generators vary capitalisation; a case-sensitive match would reject correct answer keys.
        """
        reasons = validate_problem(problem, "claude", Floors())

        assert reasons == []

    @pytest.mark.parametrize("marker", ["none", "None", " NONE "])
    def test_exempts_no_answer_marker(self, marker: str) -> None:
        """A ``none`` decisive marks a no-answer item and needs no matching text in ``input``.

        "No candidate is a duplicate" has nothing to point at; the baseline input never contains the word.
        """
        problem = _problem(ground_truth=[_gt(), _gt(decisive=marker, role="duplicate", severity=None)])

        reasons = validate_problem(problem, "claude", Floors())

        assert reasons == []

    @pytest.mark.parametrize(
        ("problem", "fragment"),
        [
            pytest.param(
                _problem(ground_truth=[_gt(decisive="close(path)")]),
                "ground_truth[0].decisive 'close(path)' does not occur",
                id="decisive-absent",
            ),
            pytest.param(
                _problem(near_misses=[{"ref": "os.remove", "wrong_role": "defect"}]),
                "near_misses[0].ref 'os.remove' does not occur",
                id="near-miss-ref-absent",
            ),
        ],
    )
    def test_rejects_text_absent_from_input(self, problem: dict[str, Any], fragment: str) -> None:
        """A ``decisive`` or near-miss ``ref`` missing from ``input`` is rejected.

        An answer that is not in the input cannot be found by any reviewer, so every report would score as a miss.
        """
        reasons = validate_problem(problem, "claude", Floors())

        assert fragment in " | ".join(reasons)

    @pytest.mark.parametrize(
        ("derived_gt", "expected"),
        [
            pytest.param(True, [], id="derived-gt-accepts"),
            pytest.param(
                False,
                [
                    "ground_truth[0].decisive 'declining' does not occur in input",
                    "near_misses[0].ref 'thriving' does not occur in input",
                ],
                id="default-rejects",
            ),
        ],
    )
    def test_absent_answer_accepted_only_under_derived_gt(self, derived_gt: bool, expected: list[str]) -> None:
        """A computed answer absent from ``input`` passes with ``derived_gt`` and is rejected without it.

        A band derived from a JSONL fixture by a deterministic tool never appears verbatim in the input, so the
        occurrence check, and only that check, must be switchable for such domains.
        """
        problem = _problem(
            ground_truth=[_gt(decisive="declining", role="band")],
            near_misses=[{"ref": "thriving", "wrong_role": "band"}],
        )

        reasons = validate_problem(problem, "claude", Floors(), derived_gt=derived_gt)

        assert reasons == expected

    def test_derived_gt_keeps_leak_check(self) -> None:
        """Under ``derived_gt`` a computed answer printed as a labelled input value is still a free-recall leak.

        Skipping the occurrence check must not reopen the shortcut of reading the answer off a header line.
        """
        problem = _problem(input=_INPUT + "Band: declining\n", ground_truth=[_gt(decisive="declining", role="band")])

        reasons = validate_problem(problem, "claude", Floors(), derived_gt=True)

        assert reasons == [
            "ground_truth[0].decisive 'declining' is the value of a labelled input line (free-recall leak)"
        ]


class TestFreeRecallLeak:
    """Check 3: a ``decisive`` readable verbatim as the value of a labelled input line."""

    @pytest.mark.parametrize(
        ("decisive", "text"),
        [
            pytest.param("issue", _INPUT, id="type-label-value"),
            pytest.param(" ISSUE ", _INPUT, id="other-case-padded"),
            pytest.param("acme/widgets", _INPUT, id="repository-label-value"),
            pytest.param("none", _INPUT + "Labels: none\n", id="no-answer-marker-not-exempt"),
            pytest.param("pull_request", "  Item kind :  pull_request  \n" + _INPUT, id="indented-label-spaces"),
        ],
    )
    def test_rejects_labelled_value(self, decisive: str, text: str) -> None:
        """A decisive equal to a labelled line's value is a free-recall leak and rejects the problem.

        The answer is then read off the header, so the problem measures copying rather than analysis. The ``none``
        marker is exempt only from the occurrence check: ``Labels: none`` beside a ``none`` answer still leaks.
        """
        problem = _problem(input=text, ground_truth=[_gt(), _gt(decisive=decisive, role="classification")])

        reasons = validate_problem(problem, "claude", Floors())

        assert "free-recall leak" in " | ".join(reasons)

    @pytest.mark.parametrize(
        "decisive",
        [
            pytest.param("acme", id="part-of-label-value"),
            pytest.param("handle.read()", id="unlabelled-code-line"),
        ],
    )
    def test_accepts_text_not_equal_to_label_value(self, decisive: str) -> None:
        """A decisive that is only part of a labelled value, or sits on an unlabelled line, is no leak.

        The leak rule matches whole values only; a substring rule would reject most real answers.
        """
        problem = _problem(ground_truth=[_gt(), _gt(decisive=decisive, role="classification")])

        reasons = validate_problem(problem, "claude", Floors())

        assert reasons == []


class TestSizeFloor:
    """Check 4: minimum ``input`` length for synthetic ``high`` and ``extreme`` problems."""

    @pytest.mark.parametrize(
        ("source", "difficulty", "size", "floors"),
        [
            pytest.param("claude", "high", 6000, Floors(), id="claude-high-at-default"),
            pytest.param("codex", "extreme", 12000, Floors(), id="codex-extreme-at-default"),
            pytest.param("real", "extreme", 0, Floors(), id="real-source-exempt"),
            pytest.param("claude", "medium", 0, Floors(), id="medium-has-no-floor"),
            pytest.param("codex", "high", 100, Floors(high=100), id="lowered-override"),
        ],
    )
    def test_accepts_input_meeting_floor(self, source: str, difficulty: str, size: int, floors: Floors) -> None:
        """An input at or above its floor, a ``real`` problem, or a tier without a floor passes.

        ``size`` 0 keeps the short baseline input, which is far below every default floor.
        """
        problem = _problem(difficulty=difficulty, input=_INPUT.ljust(size, "x"))

        reasons = validate_problem(problem, source, floors)

        assert reasons == []

    @pytest.mark.parametrize(
        ("source", "difficulty", "size", "floors", "fragment"),
        [
            pytest.param(
                "claude", "high", 5999, Floors(), "5999 chars, below the high floor of 6000", id="claude-high"
            ),
            pytest.param(
                "codex", "extreme", 11999, Floors(), "11999 chars, below the extreme floor of 12000", id="codex-extreme"
            ),
            pytest.param(
                "claude", "high", 6000, Floors(high=6001), "below the high floor of 6001", id="raised-override"
            ),
        ],
    )
    def test_rejects_short_input(self, source: str, difficulty: str, size: int, floors: Floors, fragment: str) -> None:
        """A synthetic hard problem whose input is shorter than its floor is rejected with both numbers.

        A short "extreme" problem is solved at a glance and inflates the score of every reviewer it calibrates.
        """
        problem = _problem(difficulty=difficulty, input=_INPUT.ljust(size, "x"))

        reasons = validate_problem(problem, source, floors)

        assert fragment in " | ".join(reasons)


class TestNearMissConflict:
    """Check 5: a near miss naming a ground-truth answer under that answer's own role."""

    @pytest.mark.parametrize(
        "near_miss",
        [
            pytest.param({"ref": "open(path)", "wrong_role": "defect"}, id="same-ref-same-role"),
            pytest.param({"ref": " OPEN(path)", "wrong_role": "Defect "}, id="case-and-space-insensitive"),
        ],
    )
    def test_rejects_contradictory_near_miss(self, near_miss: dict[str, str]) -> None:
        """A near miss whose ref and wrong role equal a ground-truth decisive and role contradicts the key.

        Reporting that item would count as both a hit and a false positive, so the problem cannot be scored.
        """
        problem = _problem(near_misses=[near_miss])

        reasons = validate_problem(problem, "claude", Floors())

        assert "contradicts a ground-truth item" in " | ".join(reasons)

    def test_accepts_same_ref_under_other_role(self) -> None:
        """A near miss naming a ground-truth decisive under a different role is a legitimate decoy.

        The same token may be correct as a defect and wrong as, for example, a style finding.
        """
        problem = _problem(near_misses=[{"ref": "open(path)", "wrong_role": "style"}])

        reasons = validate_problem(problem, "claude", Floors())

        assert reasons == []


class TestOpenShape:
    """Check 7: ``--open-shape`` requires a synthetic thread problem to render an open candidate pool."""

    @pytest.mark.parametrize(
        ("problem", "fragment"),
        [
            pytest.param(
                _thread(_UNLABELLED_ROWS),
                "open shape: only 0 candidate rows in the #<N> | state: <s> | title: <t> | body: <b> format (need 5)",
                id="pre-rule-unlabelled-rows",
            ),
            pytest.param(_thread((_CANONICAL, *_OTHERS[:3])), "only 4 candidate rows", id="four-rows"),
            pytest.param(
                _thread((_CANONICAL, *_OTHERS[:2], _row(806, "Long report", "x" * 301), _OTHERS[2])),
                "candidate bodies over 300 chars: #806 (301)",
                id="body-301-chars",
            ),
            pytest.param(
                _thread((_CANONICAL, _OTHERS[0], "```python", "drain_queue([])", "```", *_OTHERS[1:])),
                "the candidate section (#801 to #805) holds a ``` fence",
                id="fence-between-rows",
            ),
            pytest.param(
                _thread((_row(801, "drain_queue fails", "Export raises IndexError in drain_queue at once."), *_OTHERS)),
                "canonical candidate #801 shares the 4-word phrase 'raises indexerror in drain_queue' with the item",
                id="canonical-copies-item-phrase",
            ),
            pytest.param(
                _thread((_CANONICAL, *_OTHERS), decisive="#777"),
                "ground_truth[0] duplicate '#777' names no candidate row",
                id="canonical-outside-pool",
            ),
        ],
    )
    def test_rejects_closed_pool(self, problem: dict[str, Any], fragment: str) -> None:
        """A pool in another format, too small, with long bodies or a fence, or a copied canonical row is rejected.

        Each shape lets a reviewer find the duplicate by string matching instead of judging candidates, which made the
        synthetic thread problems easier than the real ones they stand in for.
        """
        reasons = validate_problem(problem, "claude", Floors(), open_shape=True)

        assert fragment in " | ".join(reasons)

    @pytest.mark.parametrize(
        ("problem", "source"),
        [
            pytest.param(_thread((_CANONICAL, *_OTHERS)), "claude", id="five-rows"),
            pytest.param(
                _thread((_CANONICAL, *_OTHERS[:3], _row(805, "Queue gauge", "y" * 300))), "codex", id="body-300-chars"
            ),
            pytest.param(
                _thread((_CANONICAL, *_OTHERS), item=_ITEM + "```python\ndrain_queue([])\n```\n"),
                "claude",
                id="fence-in-item-before-pool",
            ),
            pytest.param(
                _thread((_row(801, "Backlog bug", "IndexError in drain_queue when the backlog is empty."), *_OTHERS)),
                "claude",
                id="three-word-overlap",
            ),
            pytest.param(
                _thread((_row(801, "Exporter hangs on an empty backlog", "pop() fails; guard added."), *_OTHERS)),
                "claude",
                id="row-labels-not-counted",
            ),
            pytest.param(
                _thread((_row(801, "drain_queue fails", "raises IndexError in drain_queue"), *_OTHERS), role="related"),
                "claude",
                id="copied-phrase-outside-duplicate-role",
            ),
            pytest.param(_thread((_CANONICAL, *_OTHERS), decisive="none"), "claude", id="no-duplicate-answer"),
            pytest.param(_thread(_UNLABELLED_ROWS), "real", id="real-source-exempt"),
            pytest.param(_scope("off-topic"), "claude", id="scope-problem-exempt"),
        ],
    )
    def test_accepts_open_pool(self, problem: dict[str, Any], source: str) -> None:
        """A five-row pool within every limit passes, as do ``real`` problems and the off-domain scope probe.

        The limits are inclusive (300 chars), a fence outside the pool and a 3-word overlap are allowed, and the shared
        ``state: open | title:`` labels never count as a copied phrase, or every open item would collide with them.
        """
        reasons = validate_problem(problem, source, Floors(), open_shape=True)

        assert reasons == []

    @pytest.mark.parametrize(
        ("open_shape", "expected"),
        [
            pytest.param(False, [], id="flag-absent-accepts"),
            pytest.param(
                True,
                ["open shape: only 0 candidate rows in the #<N> | state: <s> | title: <t> | body: <b> format (need 5)"],
                id="flag-on-rejects",
            ),
        ],
    )
    def test_applies_only_with_flag(self, open_shape: bool, expected: list[str]) -> None:
        """A problem without a candidate pool passes unless ``open_shape`` is set.

        Code-review and vitality domains have no pool, so the check must stay off unless the thread run asks for it.
        """
        reasons = validate_problem(_problem(), "claude", Floors(), open_shape=open_shape)

        assert reasons == expected

    def test_cli_flag_reaches_validation(self, tmp_path: Path, write_problem: Callable) -> None:
        """``--open-shape`` on the command line rejects a pre-rule thread problem and keeps an open one.

        The flag must travel from argparse into every per-file validation, or thread runs keep the closed puzzles.
        """
        write_problem("codex", "closed", _thread(_UNLABELLED_ROWS) | {"problem_id": "closed-pool"})
        write_problem("codex", "open", _thread((_CANONICAL, *_OTHERS)))

        main(["--run-dir", str(tmp_path), "--open-shape"])

        assert (_ids(tmp_path), _rejected_ids(tmp_path)) == ([("codex", "empty-queue")], ["closed-pool"])


class TestBoundary:
    """Check 8: ``--boundary`` requires 3 axes of a hard vitality problem one step from a different band."""

    @pytest.mark.parametrize(
        ("third_axis", "expected"),
        [
            pytest.param({"7": _checkpoint_axis("🟢", 5, 7)}, _ACCEPTED, id="checkpoint-at-green-cutoff"),
            pytest.param({"7": _checkpoint_axis("🟡", 4, 7)}, _ACCEPTED, id="checkpoint-one-below-green"),
            pytest.param({"7": _checkpoint_axis("🟡", 3, 7)}, _ACCEPTED, id="checkpoint-at-yellow-cutoff"),
            pytest.param({"7": _checkpoint_axis("🔴", 2, 7)}, _ACCEPTED, id="checkpoint-one-below-yellow"),
            pytest.param({"7": _checkpoint_axis("🟢", 7, 7)}, _REJECTED, id="checkpoint-far-from-cutoff"),
            pytest.param({"4": _clause_axis("🔴", [], ["stale >30%"])}, _ACCEPTED, id="red-on-one-condition"),
            pytest.param(
                {"1": _clause_axis("🟡", ["median_issue_response ≤7d", "median_pr_response ≤5d"], [])},
                _ACCEPTED,
                id="yellow-one-clause-short",
            ),
            pytest.param(
                {"4": _clause_axis("🟡", ["stale ≤10%", "close_rate ≥0.8"], [])},
                _REJECTED,
                id="yellow-two-clauses-short",
            ),
            pytest.param(
                {"3": _clause_axis("🔴", [], ["top contributor >75%", "retention <30%"])},
                _REJECTED,
                id="red-on-two-conditions",
            ),
            pytest.param(
                {"8": _clause_axis("🟢", ["0 critical/high alerts", "dep-config present"], [])},
                _REJECTED,
                id="green-clause-axis-never-counts",
            ),
            pytest.param(
                {
                    "2": _clause_axis("🔴", [], ["last commit >60d and commits/30d = 0 (no backport upgrade)"])
                    | {"score": 0, "override": "⛔ repository archived"}
                },
                _REJECTED,
                id="abandonment-override-never-counts",
            ),
            pytest.param(
                {
                    "8": {
                        "band": "🟡",
                        "score": 6,
                        "conf": 0.4,
                        "conf_degraders": [],
                        "conf_fixed": "dependabot_alerts 403: partial scoring from secondary signals",
                    }
                },
                _REJECTED,
                id="axis-8-partial-scoring-never-counts",
            ),
            pytest.param(
                {"1": {"band": "⚪", "score": None, "conf": 0.0, "conf_degraders": [], "unavailable_reason": "x"}},
                _REJECTED,
                id="unavailable-axis-never-counts",
            ),
            pytest.param(
                {"9": _trajectory_axis("🟢", {"9A": 10, "9B": 10, "9C": 5, "9D": 5})},
                _ACCEPTED,
                id="trajectory-mean-at-green-cutoff",
            ),
            pytest.param(
                {"9": _trajectory_axis("🟡", {"9A": 10, "9B": 5, "9C": 5, "9D": None})},
                _ACCEPTED,
                id="trajectory-null-sub-signal-ignored",
            ),
            pytest.param(
                {"9": _trajectory_axis("🟡", {"9A": 0, "9B": 10, "9C": 0, "9D": 10})},
                _REJECTED,
                id="trajectory-far-from-cutoffs",
            ),
        ],
    )
    def test_counts_axis_by_its_kind(
        self,
        tmp_path: Path,
        write_problem: Callable,
        write_derived: Callable,
        third_axis: dict[str, dict[str, Any]],
        expected: list[tuple[str, str]],
    ) -> None:
        """A third axis next to two counted ones passes the problem exactly when its kind's one-step rule holds.

        Two checkpoint axes already sit one step away, so the case decides between 2 and 3 counted axes. A 🟢 clause
        axis, the abandonment override, partial axis 8 and ⚪ axes are undecidable and must never count.
        """
        write_problem("claude", "batch-1", _vitality())
        write_derived(_derived_files({**_TWO_ONE_STEP, **third_axis}))

        main(_boundary_argv(tmp_path))

        assert _ids(tmp_path) == expected

    def test_reason_names_count_axes_and_rule(
        self, tmp_path: Path, write_problem: Callable, write_derived: Callable
    ) -> None:
        """A rejection states how many axes counted, which ones, and the one-step definition it applied.

        The real ``vit-high-bot-inflated`` bands put only axis 3 (🔴 on one red condition) one step away; the generator
        can only fix the problem if the reason tells it the rule it missed.
        """
        write_problem("claude", "batch-1", _vitality())
        write_derived(_derived_files({"3": _clause_axis("🔴", ["retention ≥50%"], ["top contributor >75%"])}))

        main(_boundary_argv(tmp_path))

        reasons = _rejected_reasons(tmp_path)
        assert "boundary: only 1 of 9 axes sit one step from a different band (need 3; one step: 3)" in reasons
        assert "one step = a checkpoint axis (5, 6, 7) whose met count +-1 crosses a band cutoff" in reasons

    @pytest.mark.parametrize(
        ("difficulty", "files"),
        [
            pytest.param("high", {"A": _FAR_FILES["A"], "B": _FAR_FILES["B"]}, id="group-c-file-absent"),
            pytest.param("high", _FAR_FILES | {"C": "{"}, id="group-c-not-json"),
            pytest.param("high", _FAR_FILES | {"A": "[]"}, id="group-a-not-an-object"),
            pytest.param("high", _FAR_FILES | {"C": _FAR_FILES["B"]}, id="group-c-holds-group-b"),
            pytest.param("extreme", {}, id="extreme-without-any-file"),
        ],
    )
    def test_rejects_missing_derived_output(
        self,
        tmp_path: Path,
        write_problem: Callable,
        write_derived: Callable,
        difficulty: str,
        files: dict[str, str],
    ) -> None:
        """A hard problem whose three saved extractor outputs are not all usable is rejected as missing them.

        Without every group the boundary count is incomplete, and passing such a problem would skip the check.
        """
        write_problem("claude", "batch-1", _vitality(difficulty))
        write_derived(files)

        main(_boundary_argv(tmp_path))

        assert "derived output missing" in _rejected_reasons(tmp_path)

    @pytest.mark.parametrize(
        "drifted",
        [
            pytest.param({"5": _checkpoint_axis("🟢", 3, 5)}, id="checkpoint-band-off-cutoffs"),
            pytest.param({"1": _clause_axis("🟢", ["median_issue_response ≤7d"], [])}, id="green-missing-clauses"),
            pytest.param({"9": _trajectory_axis("🔴", {"9A": 10, "9B": 10})}, id="trajectory-band-off-mean"),
        ],
    )
    def test_reports_band_off_mirrored_rule(
        self, tmp_path: Path, write_problem: Callable, write_derived: Callable, drifted: dict[str, dict[str, Any]]
    ) -> None:
        """A printed band the mirrored extractor rule would not give is reported instead of being scored.

        The cutoffs are copied from ``vitality_extract.py``; when the extractor changes them, every hard problem must
        fail loudly rather than be judged by stale numbers.
        """
        write_problem("claude", "batch-1", _vitality())
        write_derived(_derived_files(drifted))

        main(_boundary_argv(tmp_path))

        assert "update the boundary cutoffs in calibrate_validate_problems.py" in _rejected_reasons(tmp_path)

    @pytest.mark.parametrize(
        ("difficulty", "boundary_flag"),
        [
            pytest.param("medium", ["--boundary"], id="medium-not-checked"),
            pytest.param("high", [], id="flag-absent"),
        ],
    )
    def test_skips_without_reading_outputs(
        self, tmp_path: Path, write_problem: Callable, difficulty: str, boundary_flag: list[str]
    ) -> None:
        """A problem below ``high``, or any problem without ``--boundary``, passes with no saved outputs at all.

        Easier tiers need no band lines, and runs that do not ask for the check must behave exactly as before.
        """
        write_problem("claude", "batch-1", _vitality(difficulty))

        main(["--run-dir", str(tmp_path), "--derived-gt", *boundary_flag])

        assert _ids(tmp_path) == _ACCEPTED


class TestRunDir:
    """Run-dir processing: file discovery, whole-problem rejection, merge, outputs, and exit codes."""

    def test_rejects_whole_problem_for_one_bad_item(self, tmp_path: Path, write_problem: Callable) -> None:
        """One invalid ground-truth item rejects the whole problem, keeping its valid item out of the merge too.

        Dropping only the bad item would leave its defect in the input and score a correct report as a false positive.
        """
        write_problem("claude", "mixed", _problem(problem_id="mixed", ground_truth=[_gt(), _gt(decisive="gone()")]))
        write_problem("claude", "clean", _problem(problem_id="clean"))

        code = main(["--run-dir", str(tmp_path)])

        assert code == 0
        assert _ids(tmp_path) == [("claude", "clean")]
        assert _read(tmp_path, "problems-rejected.json") == [
            {
                "file": "problems-claude-mixed.json",
                "problem_id": "mixed",
                "reasons": ["ground_truth[1].decisive 'gone()' does not occur in input"],
            }
        ]

    def test_sorts_by_source_then_problem_id(self, tmp_path: Path, write_problem: Callable) -> None:
        """Merged problems are ordered by source, then problem id, regardless of file names.

        A stable order keeps reruns diffable and makes Phase 2 spawn order deterministic.
        """
        write_problem("real", "a", _problem(problem_id="real-a", difficulty="extreme"))
        write_problem("codex", "z", _problem(problem_id="cx-a"))
        write_problem("claude", "1", _problem(problem_id="zeta"))
        write_problem("claude", "2", _problem(problem_id="beta"))

        main(["--run-dir", str(tmp_path)])

        assert _ids(tmp_path) == [("claude", "beta"), ("claude", "zeta"), ("codex", "cx-a"), ("real", "real-a")]

    def test_tags_source_and_defaults_near_misses(self, tmp_path: Path, write_problem: Callable) -> None:
        """Each merged problem gains its file's source and a ``near_misses`` list, keeping unknown keys.

        Scoring reads both fields on every problem; ``provenance`` of real problems must survive the merge.
        """
        write_problem("codex", "leaky-handle", _without("near_misses") | {"provenance": {"repo": "acme/widgets"}})

        main(["--run-dir", str(tmp_path)])

        assert _read(tmp_path, "problems.json") == [
            {**_without("near_misses"), "provenance": {"repo": "acme/widgets"}, "source": "codex", "near_misses": []}
        ]

    def test_prefixes_problem_id_shared_across_sources(self, tmp_path: Path, write_problem: Callable) -> None:
        """A problem id used by two sources is prefixed with its source on every problem carrying it.

        Prefixing both sides keeps the rename symmetric, so neither generator's problem silently owns the bare id.
        """
        write_problem("claude", "leaky-handle", _problem())
        write_problem("codex", "leaky-handle", _problem())

        main(["--run-dir", str(tmp_path)])

        assert _ids(tmp_path) == [("claude", "claude-leaky-handle"), ("codex", "codex-leaky-handle")]

    def test_rejects_problem_id_repeated_within_source(self, tmp_path: Path, write_problem: Callable) -> None:
        """A second file of one source reusing a problem id is rejected; the first file in name order is kept.

        A prefix cannot separate two problems of the same source, and silently overwriting one would lose it.
        """
        write_problem("claude", "first", _problem())
        write_problem("claude", "second", _problem())

        main(["--run-dir", str(tmp_path)])

        assert _ids(tmp_path) == [("claude", "leaky-handle")]
        assert _read(tmp_path, "problems-rejected.json")[0]["file"] == "problems-claude-second.json"

    def test_rejects_prefixed_id_clashing_with_existing_id(self, tmp_path: Path, write_problem: Callable) -> None:
        """A collision prefix that produces an id another problem already holds rejects the later problem.

        Two merged problems with one id would make the scorer conflate their results.
        """
        write_problem("claude", "a", _problem(problem_id="codex-leaky-handle"))
        write_problem("claude", "b", _problem())
        write_problem("codex", "c", _problem())

        main(["--run-dir", str(tmp_path)])

        assert _ids(tmp_path) == [("claude", "claude-leaky-handle"), ("claude", "codex-leaky-handle")]
        assert _rejected_ids(tmp_path) == ["leaky-handle"]

    def test_keeps_only_first_scope_problem(self, tmp_path: Path, write_problem: Callable) -> None:
        """Only the first scope problem in (source, problem_id) order is merged; later ones are rejected.

        One out-of-scope probe per run is the calibration contract; extras would over-weight scope discipline.
        """
        write_problem("codex", "off-a", _scope("off-a"))
        write_problem("claude", "off-z", _scope("off-z"))
        write_problem("claude", "leaky-handle", _problem())

        main(["--run-dir", str(tmp_path)])

        assert _ids(tmp_path) == [("claude", "leaky-handle"), ("claude", "off-z")]
        assert _rejected_ids(tmp_path) == ["off-a"]

    def test_invalid_scope_problem_does_not_consume_quota(self, tmp_path: Path, write_problem: Callable) -> None:
        """A scope problem rejected by a per-problem check leaves the single scope slot to a valid one.

        Otherwise one malformed probe would knock out the valid probe that sorts after it.
        """
        write_problem("claude", "off-a", _scope("off-a") | {"ground_truth": [_gt()]})
        write_problem("claude", "off-b", _scope("off-b"))

        main(["--run-dir", str(tmp_path)])

        assert _ids(tmp_path) == [("claude", "off-b")]

    def test_ignores_files_outside_naming_pattern(self, tmp_path: Path, write_problem: Callable) -> None:
        """Legacy arrays, outputs of an earlier run, and unrelated files are neither merged nor rejected.

        The run dir also holds other phase artifacts; reading them as problems would add phantom rejections.
        """
        write_problem("claude", "leaky-handle", _problem())
        (tmp_path / "problems-claude.json").write_text("[]", encoding="utf-8")
        (tmp_path / "problems-gemini-x.json").write_text("{}", encoding="utf-8")
        (tmp_path / "problems-rejected.json").write_text("[]", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("free text", encoding="utf-8")

        main(["--run-dir", str(tmp_path)])

        assert _ids(tmp_path) == [("claude", "leaky-handle")]
        assert _read(tmp_path, "problems-rejected.json") == []

    def test_rejects_unparsable_file_under_file_name_id(self, tmp_path: Path) -> None:
        """A problem file holding invalid JSON is rejected under the problem id taken from its file name.

        The generator still learns which problem failed even though its content could not be read.
        """
        (tmp_path / "problems-codex-cx-broken.json").write_text("{", encoding="utf-8")

        main(["--run-dir", str(tmp_path)])

        assert _rejected_ids(tmp_path) == ["cx-broken"]

    def test_writes_lf_line_endings(self, tmp_path: Path, write_problem: Callable) -> None:
        """Both outputs are written with LF line endings on every OS.

        Text mode would emit CRLF on Windows and break byte-level comparisons of rerun outputs.
        """
        write_problem("claude", "leaky-handle", _problem())

        main(["--run-dir", str(tmp_path)])

        assert (
            b"\r" not in (tmp_path / "problems.json").read_bytes() + (tmp_path / "problems-rejected.json").read_bytes()
        )

    def test_derived_gt_flag_reaches_validation(self, tmp_path: Path, write_problem: Callable) -> None:
        """``--derived-gt`` on the command line merges a problem whose computed answer is absent from ``input``.

        The flag must travel from argparse into every per-file validation, or the domain it exists for gets nothing.
        """
        write_problem("claude", "band", _problem(problem_id="band", ground_truth=[_gt(decisive="declining")]))

        code = main(["--run-dir", str(tmp_path), "--derived-gt"])

        assert code == 0
        assert _ids(tmp_path) == [("claude", "band")]

    def test_exits_one_when_nothing_is_valid(self, tmp_path: Path, write_problem: Callable) -> None:
        """No valid problem exits 1 and still writes an empty ``problems.json``.

        An empty merge must not leave a stale ``problems.json`` from an earlier run for scoring to pick up.
        """
        write_problem("claude", "leaky-handle", _problem(input=""))

        code = main(["--run-dir", str(tmp_path)])

        assert code == 1
        assert _read(tmp_path, "problems.json") == []

    def test_exits_one_for_missing_run_dir(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A run dir that does not exist exits 1 and reports it as unreadable rather than as zero valid problems.

        A wrong path must be distinguishable from a generator that produced nothing usable.
        """
        code = main(["--run-dir", str(tmp_path / "missing")])

        assert code == 1
        assert "run dir unreadable" in capsys.readouterr().out

    @pytest.mark.parametrize("floors", ["", "high", "low=5", "high=-3"])
    def test_exits_two_for_bad_floors(self, tmp_path: Path, floors: str) -> None:
        """A malformed ``--floors`` value is an argument error with exit code 2.

        The size check must never run with a floor the caller did not mean.
        """
        with pytest.raises(SystemExit) as excinfo:
            main(["--run-dir", str(tmp_path), "--floors", floors])

        assert excinfo.value.code == 2

    def test_cli_prints_compact_summary(self, tmp_path: Path, write_problem: Callable) -> None:
        """Run as a script, it prints one compact JSON summary line counting valid problems and exits 0.

        The pipeline parses this line to decide whether Phase 2 has problems to run.
        """
        write_problem("claude", "leaky-handle", _problem())
        write_problem("codex", "cx-bad", _problem(problem_id="cx-bad", ground_truth=[_gt(decisive="gone()")]))

        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--run-dir", str(tmp_path)],
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )

        assert result.returncode == 0
        assert result.stdout.splitlines() == [
            '{"valid":1,"rejected":1,"by_source":{"claude":1,"codex":0,"real":0},'
            '"by_difficulty":{"trivial":0,"low":0,"medium":1,"high":0,"extreme":0,"scope":0},'
            '"rejected_ids":["cx-bad"]}'
        ]
