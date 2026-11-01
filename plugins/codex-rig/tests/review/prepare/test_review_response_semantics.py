"""Accept equivalent reviewer presentation without losing structural or factual checks."""

from __future__ import annotations

import json
import runpy
from collections.abc import Callable
from pathlib import Path

import pytest
import test_review_batches as batches
import test_review_prepare as preparation

_ASSESSMENT = (
    "## Reviewer Assessment\n\nSource rating version: 1\nRating: 2\n"
    "Verification readiness: complete\nRationale: One source-backed obligation remains.\n"
)
_LABELS = [
    pytest.param("Rating", id="rating-label"),
    pytest.param("Rationale", id="rationale-label"),
    pytest.param("Source rating version", id="version-label"),
    pytest.param("Verification readiness", id="readiness-label"),
]


@pytest.fixture(scope="module")
def validator() -> dict[str, object]:
    """Load the code-review artifact validator once; its namespace is only read by these tests."""
    return runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))


def _rate(validator: dict[str, object], response: Path, **options: object) -> object:
    """Call the retained-rating reader as a challenger with the fixed source rating version."""
    return validator["_retained_reviewer_rating"](response, local_reviewer_wave=False, role="challenger", **options)


def _rationale_response(tmp_path: Path, label: str, shape: Callable[[str], str]) -> tuple[Path, bytes, str]:
    """Write an empty-findings response whose rationale text starts with a field label."""
    body = shape(batches._batch_output([], 2))
    rationale = f"{label}: is the visible field name."
    raw = (body[: body.index("Rationale:")] + f"Rationale: {rationale}\n").encode()
    response = tmp_path / "response.md"
    response.write_bytes(raw)
    return response, raw, rationale


def _assemble_waves(run: Path, home: Path, tmp_path: Path, body: str) -> list[Path]:
    """Assemble every batch wave with the same reviewer body and return the retained raw output paths."""
    raw_paths = []
    for wave in json.loads((run / "batch-dispatch.json").read_bytes())["waves"]:
        directory = run / wave["directory"]
        roles = [item["role_id"] for item in json.loads((directory / "inspection-plan.json").read_bytes())["contexts"]]
        preparation._assembly_evidence(
            tmp_path,
            prepared_run=directory,
            home=home,
            wave_index=wave["wave"],
            final_header="missing",
            findings={role: body for role in roles},
            blocking_counts={role: 0 for role in roles},
        )
        result = batches._batch_command(directory, "assemble-wave", home)
        assert result.returncode == 0, result.stderr
        manifest = json.loads((directory / "specialist-manifest.json").read_bytes())
        raw_paths += [directory / item["attempts"][0]["raw_output_path"] for item in manifest["passes"]]
    return raw_paths


@pytest.mark.parametrize("main", [False, True])
@pytest.mark.parametrize(
    "restyle",
    [
        pytest.param(lambda body, heading: body.replace("\n", "  \n"), id="hard-breaks"),
        pytest.param(lambda body, heading: body.replace(": ", ":\t").replace("\n", "\t\r\n"), id="tabs"),
        pytest.param(lambda body, heading: body.replace("\n", "\n\n"), id="blank-lines"),
        pytest.param(lambda body, heading: body.replace(f"## {heading}", f"  ##\t{heading} ###"), id="heading"),
        pytest.param(lambda body, heading: body.replace("obligation remains.", "obligation\nremains."), id="wrapped"),
    ],
)
def test_assessment_survives_equivalent_presentation(
    validator: dict[str, object], tmp_path: Path, main: bool, restyle: Callable[[str, str], str]
) -> None:
    """A complete judgment is read the same when its Markdown is restyled without changing meaning.

    Hard line breaks, tabs, blank lines, decorated headings and a wrapped rationale are equivalent presentation of the
    same assessment, for ordinary and main reviewers. Rejecting them would cost another reviewer turn.
    """
    heading = "Main Reviewer Assessment" if main else "Reviewer Assessment"
    body = restyle(_ASSESSMENT.replace("Reviewer Assessment", heading), heading)
    response = tmp_path / "response.md"
    response.write_bytes(body.encode())

    assessment = _rate(validator, response, main=main, source_rating_version=1, retain_assessment=True)

    assert assessment["rating"] == 2
    assert assessment["source_rating_version"] == 1
    assert assessment["verification_readiness"] == "complete"
    assert " ".join(assessment["rationale"].split()) == "One source-backed obligation remains."
    assert response.read_bytes() == body.encode()


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda body: body + "Rating: 1\n", id="duplicate-field"),
        pytest.param(
            lambda body: body + "##\tReviewer Assessment ##\nRating: 1\nRationale: Conflicting judgment.\n",
            id="mixed-heading",
        ),
        pytest.param(lambda body: "```text\n" + body + "```\n", id="fenced-assessment"),
        pytest.param(lambda body: body[: body.index("Rationale:")], id="missing-rationale"),
        pytest.param(lambda body: body.replace("Rating: 2", "Rating: null"), id="conflicting-rating"),
    ],
)
def test_presentation_tolerance_keeps_substantive_rejections(
    validator: dict[str, object], tmp_path: Path, damage: Callable[[str], str]
) -> None:
    """Equivalent presentation cannot hide ambiguity, absence or contradictory readiness.

    Each damaged assessment is padded with the same trailing-space styling the tolerant reader accepts, so the
    rejection must come from the substance: a duplicate or conflicting field, a fenced block or a missing rationale.
    """
    body = (
        "## Reviewer Assessment\nSource rating version: 1  \nRating: 2  \n"
        "Verification readiness: complete  \nRationale: One obligation remains.\n"
    )
    response = tmp_path / "response.md"
    response.write_text(damage(body), encoding="utf-8")

    with pytest.raises(SystemExit, match="review-assessment-"):
        _rate(validator, response, main=False, source_rating_version=1)


@pytest.mark.integration
def test_equivalent_batch_markdown_survives_assembly_and_aggregate(tmp_path: Path) -> None:
    """Admission and the next consumer retain all judgments and original response bytes.

    Every wave of a real batched review receives the same restyled reviewer response (tabbed headings, tilde fences,
    hard breaks, a rationale that quotes a field label). Wave assembly and the batch aggregate must keep the raw bytes
    and report the same rating, readiness, confidence and rationale for each source part.
    """
    run = batches._batch_inputs(tmp_path)
    batches._in_process_step(run, "prepare", source_only=True)
    home = tmp_path / "codex-home"
    record = batches._batch_finding("F_ONE", "low", "Document the returned value.")
    body = batches._batch_output([record], 2)
    body = body.replace("## Reviewer ", "##\tReviewer ").replace("\n```json\n", "\n\n~~~json  \n")
    body = body.replace("\n```", "\n~~~~  ").replace("version: 1\n", "version: 1  \n\n")
    body = body.replace("Rating: 2\n", "Rating:\t2  \n").replace("readiness: complete\n", "readiness: complete  \n")
    body = body[: body.index("Rationale:")] + "Rationale: Rating: describes the scoped judgment.\n"
    raw_paths = _assemble_waves(run, home, tmp_path, body)

    result = batches._batch_command(run, "assemble-batches", home)

    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    parts = [part for item in manifest["passes"] for part in item["source_parts"]]
    assert manifest["source_findings"]
    assert {part["rating"] for part in parts} == {2}
    assert {part["verification_readiness"] for part in parts} == {"complete"}
    assert {part["confidence"]["score"] for part in parts} == {0.95}
    assert {part["rationale"] for part in parts} == {"Rating: describes the scoped judgment."}
    assert all(path.read_bytes() == body.encode() for path in raw_paths)


def test_shape_repair_keeps_cosmetic_bytes_around_json(validator: dict[str, object]) -> None:
    """An eligible data-shape correction operates on original spans, not rewritten presentation.

    A batch response with tabbed headings and tilde fences carries a closure-evidence value of the wrong shape.
    Repairing it must change only that JSON value and leave every surrounding cosmetic byte untouched.
    """
    record = batches._batch_finding("A", "low", "Preserve the source obligation.")
    body = batches._batch_output([record], 2).replace("## Reviewer ", "##\tReviewer ")
    body = body.replace("```json\n", "~~~json  \n").replace("\n```", "\n~~~~")
    malformed = body.replace(json.dumps(record["closure_evidence"]), json.dumps([record["closure_evidence"]]))
    snapshot = {"files": [{"path": "widget.py", "kind": "text", "content": "value = 2\n", "sha256": "a" * 64}]}

    repaired = validator["_closure_shape_repair"](malformed, snapshot, "challenger")

    assert repaired == body


@pytest.mark.parametrize(
    ("shape", "main"),
    [
        pytest.param(lambda body: body[body.index("## Reviewer Assessment") :], False, id="ordinary"),
        pytest.param(
            lambda body: body[body.index("## Reviewer Assessment") :].replace(
                "## Reviewer Assessment", "## Main Reviewer Assessment"
            ),
            True,
            id="main",
        ),
    ],
)
@pytest.mark.parametrize("label", _LABELS)
def test_inline_rationale_label_is_value_but_later_declaration_is_rejected(
    validator: dict[str, object], tmp_path: Path, shape: Callable[[str], str], main: bool, label: str
) -> None:
    """A field label inside the rationale is plain text, while a later real declaration is a conflict.

    The rationale value ends at the end of its line, so `Rating: is the visible field name.` is rationale text. A
    separate trailing `Rating: ...` line is a second declaration of the field and must be refused.
    """
    response, raw, rationale = _rationale_response(tmp_path, label, shape)

    assessment = _rate(validator, response, main=main, source_rating_version=1, retain_assessment=True)
    response.write_text(raw.decode() + f"{label}: conflicting declaration\n", encoding="utf-8")

    assert assessment["rating"] == 2
    assert assessment["rationale"] == rationale
    with pytest.raises(SystemExit, match="review-assessment-"):
        _rate(validator, response, main=main, source_rating_version=1)


@pytest.mark.parametrize("label", _LABELS)
def test_batch_inline_rationale_label_is_value_but_later_declaration_is_rejected(
    validator: dict[str, object], tmp_path: Path, label: str
) -> None:
    """A batch response treats an in-rationale field label as text and rejects a later real declaration.

    Same boundary as the ordinary reviewer, applied to a full batch response: the findings reader must also accept the
    quoted label with no findings and refuse the conflicting second declaration.
    """
    response, raw, rationale = _rationale_response(tmp_path, label, lambda body: body)

    assessment = _rate(
        validator, response, main=False, source_rating_version=1, batch_response=True, retain_assessment=True
    )
    findings = validator["_batch_reviewer_findings"](response, {"files": []}, "challenger")
    response.write_text(raw.decode() + f"{label}: conflicting declaration\n", encoding="utf-8")

    assert assessment["rating"] == 2
    assert assessment["rationale"] == rationale
    assert findings == []
    with pytest.raises(SystemExit, match="review-assessment-"):
        _rate(validator, response, main=False, source_rating_version=1, batch_response=True)
    with pytest.raises(SystemExit, match="review-batch-individual-findings-format"):
        validator["_batch_reviewer_findings"](response, {"files": []}, "challenger")
