"""Accept equivalent reviewer presentation without losing structural or factual checks."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest
import test_review_batches as batches
import test_review_prepare as preparation


@pytest.mark.parametrize("main", [False, True])
@pytest.mark.parametrize("variant", ["hard-breaks", "tabs", "blank-lines", "heading", "wrapped-rationale"])
def test_current_assessment_preserves_meaning_across_presentation(tmp_path: Path, main: bool, variant: str) -> None:
    """A complete judgment must not require another reviewer turn for equivalent Markdown."""
    validator = runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))
    heading = "Main Reviewer Assessment" if main else "Reviewer Assessment"
    body = (
        f"## {heading}\n\nSource rating version: 1\nRating: 2\n"
        "Verification readiness: complete\nRationale: One source-backed obligation remains.\n"
    )
    if variant == "hard-breaks":
        body = body.replace("\n", "  \n")
    elif variant == "tabs":
        body = body.replace(": ", ":\t").replace("\n", "\t\r\n")
    elif variant == "blank-lines":
        body = body.replace("\n", "\n\n")
    elif variant == "heading":
        body = body.replace(f"## {heading}", f"  ##\t{heading} ###")
    else:
        body = body.replace("obligation remains.", "obligation\nremains.")
    response = tmp_path / "response.md"
    response.write_bytes(body.encode())
    assessment = validator["_retained_reviewer_rating"](
        response,
        local_reviewer_wave=False,
        main=main,
        role="challenger",
        source_rating_version=1,
        retain_assessment=True,
    )
    assert assessment["rating"] == 2
    assert assessment["source_rating_version"] == 1
    assert assessment["verification_readiness"] == "complete"
    assert " ".join(assessment["rationale"].split()) == "One source-backed obligation remains."
    assert response.read_bytes() == body.encode()


@pytest.mark.parametrize("damage", ["duplicate-field", "mixed-heading", "fenced-assessment", "missing", "conflict"])
def test_presentation_tolerance_preserves_substantive_rejections(tmp_path: Path, damage: str) -> None:
    """Equivalent presentation cannot hide ambiguity, absence or contradictory readiness."""
    validator = runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))
    body = (
        "## Reviewer Assessment\nSource rating version: 1  \nRating: 2  \n"
        "Verification readiness: complete  \nRationale: One obligation remains.\n"
    )
    if damage == "duplicate-field":
        body += "Rating: 1\n"
    elif damage == "mixed-heading":
        body += "##\tReviewer Assessment ##\nRating: 1\nRationale: Conflicting judgment.\n"
    elif damage == "fenced-assessment":
        body = "```text\n" + body + "```\n"
    elif damage == "missing":
        body = body[: body.index("Rationale:")]
    else:
        body = body.replace("Rating: 2", "Rating: null")
    response = tmp_path / "response.md"
    response.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit, match="review-assessment-"):
        validator["_retained_reviewer_rating"](
            response,
            local_reviewer_wave=False,
            main=False,
            role="challenger",
            source_rating_version=1,
        )


@pytest.mark.integration
def test_equivalent_batch_markdown_survives_assembly_and_aggregate(tmp_path: Path) -> None:
    """Admission and the next consumer retain all judgments and original response bytes."""
    run = batches._batch_inputs(tmp_path)
    batches._in_process_step(run, "prepare", source_only=True)
    home = tmp_path / "codex-home"
    record = batches._batch_finding("F_ONE", "low", "Document the returned value.")
    body = batches._batch_output([record], 2)
    body = body.replace("## Reviewer ", "##\tReviewer ").replace("\n```json\n", "\n\n~~~json  \n")
    body = body.replace("\n```", "\n~~~~  ").replace("version: 1\n", "version: 1  \n\n")
    body = body.replace("Rating: 2\n", "Rating:\t2  \n").replace("readiness: complete\n", "readiness: complete  \n")
    body = body[: body.index("Rationale:")] + "Rationale: Rating: describes the scoped judgment.\n"
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
        for item in manifest["passes"]:
            raw = directory / item["attempts"][0]["raw_output_path"]
            assert raw.read_bytes() == body.encode()
            raw_paths.append(raw)
    result = batches._batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    assert manifest["source_findings"]
    for item in manifest["passes"]:
        for part in item["source_parts"]:
            assert part["rating"] == 2
            assert part["verification_readiness"] == "complete"
            assert part["confidence"]["score"] == 0.95
            assert part["rationale"] == "Rating: describes the scoped judgment."
    assert all(path.read_bytes() == body.encode() for path in raw_paths)


def test_shape_repair_preserves_cosmetic_bytes_around_json(tmp_path: Path) -> None:
    """An eligible data-shape correction operates on original spans, not rewritten presentation."""
    validator = runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))
    record = batches._batch_finding("A", "low", "Preserve the source obligation.")
    body = batches._batch_output([record], 2).replace("## Reviewer ", "##\tReviewer ")
    body = body.replace("```json\n", "~~~json  \n").replace("\n```", "\n~~~~")
    malformed = body.replace(json.dumps(record["closure_evidence"]), json.dumps([record["closure_evidence"]]))
    snapshot = {"files": [{"path": "widget.py", "kind": "text", "content": "value = 2\n", "sha256": "a" * 64}]}
    assert validator["_closure_shape_repair"](malformed, snapshot, "challenger") == body


@pytest.mark.parametrize("mode", ["ordinary", "main", "batch"])
@pytest.mark.parametrize(
    "label",
    [
        pytest.param("Rating", id="rating-label"),
        pytest.param("Rationale", id="rationale-label"),
        pytest.param("Source rating version", id="version-label"),
        pytest.param("Verification readiness", id="readiness-label"),
    ],
)
def test_inline_rationale_label_is_value_but_later_declaration_is_rejected(
    tmp_path: Path, mode: str, label: str
) -> None:
    """Keep the explicit rationale value boundary without admitting a second field declaration."""
    validator = runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))
    body = batches._batch_output([], 2)
    if mode != "batch":
        body = body[body.index("## Reviewer Assessment") :]
    if mode == "main":
        body = body.replace("## Reviewer Assessment", "## Main Reviewer Assessment")
    rationale = f"{label}: is the visible field name."
    body = body[: body.index("Rationale:")] + f"Rationale: {rationale}\n"
    response = tmp_path / "response.md"
    response.write_bytes(body.encode())
    assessment = validator["_retained_reviewer_rating"](
        response,
        local_reviewer_wave=False,
        main=mode == "main",
        role="challenger",
        source_rating_version=1,
        batch_response=mode == "batch",
        retain_assessment=True,
    )
    assert assessment["rating"] == 2
    assert assessment["rationale"] == rationale
    if mode == "batch":
        assert validator["_batch_reviewer_findings"](response, {"files": []}, "challenger") == []
    assert response.read_bytes() == body.encode()
    response.write_text(body + f"{label}: conflicting declaration\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="review-assessment-"):
        validator["_retained_reviewer_rating"](
            response,
            local_reviewer_wave=False,
            main=mode == "main",
            role="challenger",
            source_rating_version=1,
            batch_response=mode == "batch",
        )
    if mode == "batch":
        with pytest.raises(SystemExit, match="review-batch-individual-findings-format"):
            validator["_batch_reviewer_findings"](response, {"files": []}, "challenger")
