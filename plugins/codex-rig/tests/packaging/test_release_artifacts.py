"""Exercise release communication completion through the public artifact validator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def bind_handoff(
    directory: Path,
    result: dict[str, Any],
    *,
    title: str = "warning-only",
    only_table: str | None = None,
    presentation_version: int = 8,
) -> None:
    """Render real release handoff evidence so tests exercise schema-v2 completion end to end."""
    spec = importlib.util.spec_from_file_location("release_finalizer", PLUGIN_ROOT / "shared/final_handoff.py")
    assert spec is not None
    assert spec.loader is not None
    finalizer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(finalizer)
    lines = (directory / "release-readiness.md").read_text(encoding="utf-8").splitlines()
    rows = [line.strip("| ").split(" | ") for line in lines if line.startswith("| ")][2:]
    records = [{"id": "change", "evidence": "change-table.md"}]
    records.extend({"id": f"check-{index}", "evidence": "release-readiness.md"} for index in range(len(rows)))
    gates = json.loads((directory / "gates.json").read_text(encoding="utf-8"))
    handoff = {
        "schema_version": 1,
        "presentation_version": presentation_version,
        "skill": "release",
        "branch": "standard",
        "outcome": {"title": title, "summary": "Release communication has been assessed."},
        "tables": [
            {
                "heading": "Changes",
                "columns": ["Change", "SemVer impact", "Status / blocker", "Evidence"],
                "rows": [
                    {
                        "id": "change",
                        "cells": ["Parsing fix", "patch", "Drafted", "change-table.md"],
                        "source_ids": ["change"],
                    }
                ],
            },
            {
                "heading": "Readiness",
                "columns": ["Check", "Status", "Evidence", "Blocker / next action"],
                "rows": [
                    {"id": f"check-{index}", "cells": cells, "source_ids": [f"check-{index}"]}
                    for index, cells in enumerate(rows)
                ],
            },
        ],
        "source_records": records,
        "source_coverage": {
            "source_records_total": len(records),
            "represented_source_records_total": len(records),
            "omitted_source_records_total": 0,
        },
        "verification": [
            {"check": check["id"], "status": check["status"], "evidence": check["stdout"]} for check in gates["checks"]
        ],
        "remaining": [],
        "next_steps": [],
        "confidence": {"score": 1.0, "band": "fair", "limits": [], "gaps": []},
        "artifacts": [{"label": "Result", "path": result["artifact_path"]}],
        "caller_contract": None,
    }
    if only_table is not None:
        handoff["tables"] = [table for table in handoff["tables"] if table["heading"] == only_table]
        source_ids = {source for table in handoff["tables"] for row in table["rows"] for source in row["source_ids"]}
        handoff["source_records"] = [record for record in records if record["id"] in source_ids]
        handoff["source_coverage"].update(
            source_records_total=len(source_ids), represented_source_records_total=len(source_ids)
        )
    source = directory / "final-handoff.json"
    rendered = directory / "final.md"
    receipt = directory / "final-handoff.validation.json"
    source.write_text(json.dumps(handoff), encoding="utf-8")
    validation = finalizer.render_files(source, rendered, receipt)
    result["metadata"]["final_handoff"] = {
        "schema_version": 1,
        "handoff_path": str(source),
        "rendered_path": str(rendered),
        "validation_path": str(receipt),
        "handoff_sha256": validation["handoff_sha256"],
        "rendered_sha256": validation["rendered_sha256"],
        "branch": "standard",
    }


@pytest.fixture
def validator() -> ModuleType:
    """Load the shipped validator without mocking its release or shared gates."""
    spec = importlib.util.spec_from_file_location(
        "release_artifact_validator", PLUGIN_ROOT / "shared/validate-artifacts.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="release_source_template", scope="module")
def _release_source_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    """Build the one-commit release source repository once per module and return it with its head and tree.

    Git writes the repository bytes itself, so they never depend on the host newline default the release fixture varies.
    Automatic maintenance is disabled before committing so Git cannot mutate the template while it is copied. Every case
    copies this read-only template instead of spawning the same Git commands again; tests that add commits mutate only
    their private copy.
    """
    repository = tmp_path_factory.mktemp("release-source-template") / "source"
    repository.mkdir()
    for arguments in (
        ["init", "-q"],
        ["config", "maintenance.auto", "false"],
        ["config", "user.name", "Release Test"],
        ["config", "user.email", "release@example.invalid"],
        ["commit", "--allow-empty", "-m", "fixture\n\nCo-authored-by: Codex <codex@openai.com>"],
    ):
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    head, final_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD", "HEAD^{tree}"], cwd=repository, text=True
    ).split()
    return repository, head, final_tree


@pytest.fixture
def release_run(
    tmp_path: Path, text_newline_default: None, release_source_template: tuple[Path, str, str]
) -> tuple[Path, dict[str, object]]:
    """Create complete release evidence with real gate logs and a structured draft."""
    checks = []
    for gate in ("lint", "format", "types", "tests", "review"):
        log = tmp_path / f"{gate}.txt"
        log.write_text("Passed local acceptance.\n", encoding="utf-8")
        checks.append(
            {
                "id": gate,
                "status": "pass",
                "exit_code": 0,
                "duration_seconds": 0,
                "command_path": log.name,
                "stdout": log.name,
                "stderr": log.name,
                "source": {
                    "expected_head": "a" * 40,
                    "before": {"head": "a" * 40, "status": ""},
                    "after": {"head": "a" * 40, "status": ""},
                },
            }
        )
    (tmp_path / "gates.json").write_text(
        json.dumps({"checks": checks, "checks_failed": [], "status": "pass"}), encoding="utf-8"
    )
    evidence = {
        "change-table.md": "Candidate fix mapped to source and public notes.",
        "release-readiness.md": "# Readiness\n## SemVer\nPatch.\n## Migration\nNone.\n## Checks\n"
        "| Check | Status | Evidence | Blocker / next action |\n| --- | --- | --- | --- |\n"
        + "".join(
            f"| {name} | pass | Reviewed source | None |\n"
            for name in (
                "SemVer",
                "Migration",
                "Release scope",
                "Contributors",
                "Changelog",
                "Documentation",
                "Verification",
                "Artifacts",
            )
        )
        + "\n## Blockers\nNone.\n",
        "release-scope.md": "Head and baseline\nReleased comparison\nCandidate accounting\nLimits",
        "contributors.md": "Coverage\nCredits\n**Release Test**\n**Codex**\nExclusions\nUnresolved",
        "changelog-audit.md": "Scope\nPreservation\nAdded and flagged\nLimits",
        "draft-review.md": "Claims\nContributors\nChangelog preservation\nExamples\nDeliverables\nLimits",
    }
    for name, text in evidence.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    outputs = tmp_path / "deliverables"
    outputs.mkdir()
    (outputs / "DRAFT.md").write_text(
        "# v1.2.1: Accurate parsing\n\n## Summary\nParsing accepts valid empty input.\n\n"
        "- **Empty input** returns an empty result instead of raising.\n\nDrop-in upgrade, no code changes.\n"
        "## Highlights\nEmpty input now returns an empty result instead of raising.\n"
        "## Migration guide\nNo migration required.\n## Notable changes\n"
        "- Fixed empty input ([commit](https://example.org/commit/abc)).\n"
        "## Contributors\n- **Release Test** — parsing fix.\n- **Codex** — coauthored release evidence.\n\n"
        "**Full changelog**: https://example.org/compare/v1.2.0...abc\n",
        encoding="utf-8",
    )
    template, head, final_tree = release_source_template
    repository = tmp_path / "source"
    shutil.copytree(template, repository)
    draft = outputs / "DRAFT.md"
    changelog_backup = tmp_path / "changelog-before.md"
    changelog_backup.write_text("# Changelog\n\n## v1.0\n- Existing detail.\n", encoding="utf-8", newline="\n")
    changelog_final = tmp_path / "CHANGELOG.md"
    changelog_final.write_text(
        changelog_backup.read_text(encoding="utf-8") + "\n## v1.2.1\n- Parsing fix.\n", encoding="utf-8", newline="\n"
    )
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    identities = [
        hashlib.sha256(b"Release Test\0release@example.invalid").hexdigest(),
        hashlib.sha256(b"Codex\0codex@openai.com").hexdigest(),
    ]
    result = {
        "schema_version": 2,
        "status": "pass",
        "checks_run": [check["id"] for check in checks],
        "checks_failed": [],
        "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "confidence": 1.0,
        "artifact_path": str(tmp_path / "result.json"),
        "metadata": {
            "release_contract_version": 1,
            "mode": "notes",
            "release_head": head,
            "requested_artifacts": ["DRAFT.md"],
            "confidence_gaps": [],
            "confidence_gap_closures": [],
            "confidence_recovery": {
                "initial_confidence": 1.0,
                "final_confidence": 1.0,
                "status": "fair",
                "evidence": ["Fixture evidence"],
                "recovery_actions": ["Inspected complete fixture"],
                "remaining_limits": [],
            },
            "release_evidence": {
                "schema_version": 1,
                "repository": str(repository),
                "baseline": None,
                "final_tree": final_tree,
                "candidates": [{"sha": head, "disposition": "included", "reason": "release change"}],
                "claims": [
                    {"artifact": "DRAFT.md", "sha256": digest, "excerpt": "Parsing accepts", "candidate_shas": [head]}
                ],
                "contributors": [
                    {
                        "identity_hash": identities[0],
                        "display_name": "Release Test",
                        "disposition": "credited",
                        "credit_excerpt": "**Release Test**",
                    },
                    {
                        "identity_hash": identities[1],
                        "display_name": "Codex",
                        "disposition": "credited",
                        "credit_excerpt": "**Codex**",
                    },
                ],
                "artifacts": [{"name": "DRAFT.md", "destination": str(draft), "sha256": digest}],
                "changelog": {
                    "target_heading": "## v1.2.1",
                    "backup": str(changelog_backup),
                    "destination": str(changelog_final),
                    "backup_sha256": hashlib.sha256(changelog_backup.read_bytes()).hexdigest(),
                    "final_sha256": hashlib.sha256(changelog_final.read_bytes()).hexdigest(),
                },
            },
        },
    }
    (tmp_path / "release-scope.md").write_text(
        (tmp_path / "release-scope.md").read_text(encoding="utf-8") + f"\n{head}\n{final_tree}\n",
        encoding="utf-8",
    )
    for check in checks:
        check["source"] = {
            "expected_head": head,
            "before": {"head": head, "status": ""},
            "after": {"head": head, "status": ""},
        }
    (tmp_path / "gates.json").write_text(
        json.dumps({"checks": checks, "checks_failed": [], "status": "pass"}), encoding="utf-8"
    )
    bind_handoff(tmp_path, result)
    return tmp_path, result


def validate_run(validator: ModuleType, run: tuple[Path, dict[str, object]]) -> None:
    """Persist a candidate and invoke the same public entrypoint as the CLI."""
    directory, result = run
    candidate = directory / "result.candidate.json"
    candidate.write_text(json.dumps(result), encoding="utf-8")
    validator.validate("release", directory, candidate)


@pytest.mark.integration
def test_release_historical_presentation_cannot_certify_a_fresh_candidate(
    validator: ModuleType, release_run: tuple
) -> None:
    """Keep historical reports readable without admitting old rendering for a new release result."""
    directory, result = release_run
    bind_handoff(directory, result, presentation_version=2)
    canonical = directory / "result.json"
    canonical.write_text(json.dumps(result), encoding="utf-8")
    validator.validate("release", directory, canonical)
    with pytest.raises(SystemExit, match="current-handoff-presentation-v8-required"):
        validate_run(validator, release_run)


@pytest.mark.parametrize(
    "variant",
    [
        "human-underscores",
        "human-plain",
        "human-link",
        "human-reference",
        "human-collapsed",
        "human-shortcut",
        "human-partial-emphasis",
        "human-linked-partial-emphasis",
        "human-handle",
        "human-linked-handle",
        "draft-heading",
        "draft-setext",
        "summary-heading",
        "summary-setext",
        "migration-setext",
    ],
)
@pytest.mark.integration
def test_release_accepts_equivalent_markdown_with_rebound_receipts(
    validator: ModuleType, release_run: tuple, variant: str
) -> None:
    """Keep verified identities and required sections independent of emphasis and ATX spelling."""
    directory, result = release_run
    receipt = result["metadata"]["release_evidence"]
    draft = directory / "deliverables/DRAFT.md"
    text = draft.read_text(encoding="utf-8")
    if variant.startswith("human-"):
        credit = receipt["contributors"][0]
        replacement = {
            "human-underscores": "__Release Test__",
            "human-plain": "Release Test",
            "human-link": "[Release Test](https://example.org/person)",
            "human-reference": "[Release Test][person]",
            "human-collapsed": "[Release Test][]",
            "human-shortcut": "[Release Test]",
            "human-partial-emphasis": "Release **Test**",
            "human-linked-partial-emphasis": "[Release **Test**](https://example.org/person)",
            "human-handle": "**Release Test** (@release-test)",
            "human-linked-handle": "[Release **Test**](https://example.org/person) ([@release-test](https://example.org/profile))",
        }[variant]
        text = text.replace(credit["credit_excerpt"], replacement)
        credit["credit_excerpt"] = replacement
        if variant in {"human-reference", "human-collapsed", "human-shortcut"}:
            reference = "person" if variant == "human-reference" else "Release Test"
            text += f"\n[{reference}]: https://example.org/person\n"
    elif variant == "draft-heading":
        text = text.replace("## Summary\n", "  ##\tSummary ##\n")
    elif variant == "draft-setext":
        text = text.replace("## Summary\n", "Summary\n-------\n")
    elif variant == "migration-setext":
        select_artifact(
            release_run,
            "MIGRATION.md",
            "No migration required\n---------------------\nExisting input keeps its behavior.\n",
        )
    else:
        summary = directory / "deliverables/SUMMARY.md"
        summary_text = "## Overview ##\nParsing accepts empty input.\n## Benefits ###\nNo recovery needed.\n"
        if variant == "summary-setext":
            summary_text = (
                "Overview\n--------\nParsing accepts empty input.\n\nBenefits\n--------\nNo recovery needed.\n"
            )
        summary.write_text(summary_text, encoding="utf-8")
        result["metadata"]["requested_artifacts"].append("SUMMARY.md")
        receipt["artifacts"].append(
            {
                "name": "SUMMARY.md",
                "destination": str(summary),
                "sha256": hashlib.sha256(summary.read_bytes()).hexdigest(),
            }
        )
    draft.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    receipt["artifacts"][0]["sha256"] = digest
    receipt["claims"][0]["sha256"] = digest
    bind_handoff(directory, result)
    validate_run(validator, release_run)


@pytest.mark.parametrize(
    ("definition", "accepted"),
    [
        pytest.param("[person]: https://example.org/person", True, id="bare-destination"),
        pytest.param('[person]: <https://example.org/person> "Profile"', True, id="angle-and-title"),
        pytest.param("[person]: https://example.org/person 'Profile'", True, id="single-quoted-title"),
        pytest.param("[person]: https://example.org/person (Profile)", True, id="parenthesized-title"),
        pytest.param("[person]: https://example.org/a(b)c", True, id="balanced-destination"),
        pytest.param(r"[person]: https://example.org/a\(b\)", True, id="escaped-destination"),
        pytest.param("[person]:\n  https://example.org/person", True, id="next-line-destination"),
        pytest.param('[person]: https://example.org/person\n  "Profile"', True, id="next-line-title"),
        pytest.param(
            '[other]: https://example.org/other\n  "Other profile"\n[person]: https://example.org/person',
            True,
            id="definition-after-continuation-title",
        ),
        pytest.param('[person]: https://example.org/person "Public\nprofile"', True, id="multiline-title"),
        pytest.param("[person]: <>", True, id="empty-angle-destination"),
        pytest.param("[person]: https://example.org/person unexpected words", False, id="unquoted-trailing-words"),
        pytest.param('[person]: https://example.org/person "Profile', False, id="unclosed-title"),
        pytest.param('[person]: https://example.org/person "Profile" extra', False, id="trailing-title-junk"),
        pytest.param("[person]: <https://example.org/person", False, id="unclosed-angle-destination"),
        pytest.param("[person]: <https://example.org/<person>>", False, id="nested-angle-destination"),
        pytest.param("[person]: https://example.org/a(b", False, id="unclosed-destination-parenthesis"),
        pytest.param('[person]: https://example.org/person "Public\n\nprofile"', False, id="blank-title-line"),
        pytest.param(
            "A preceding paragraph.\n[person]: https://example.org/person", False, id="paragraph-continuation"
        ),
    ],
)
@pytest.mark.integration
def test_release_reference_credit_requires_complete_definition(
    validator: ModuleType, release_run: tuple, definition: str, accepted: bool
) -> None:
    """Resolve actual reference definitions, never a prefix of malformed or ordinary paragraph text."""
    directory, result = release_run
    receipt = result["metadata"]["release_evidence"]
    draft = directory / "deliverables/DRAFT.md"
    label = "[Release Test][person]"
    text = draft.read_text(encoding="utf-8").replace("**Release Test**", label)
    draft.write_text(text + "\n" + definition + "\n", encoding="utf-8")
    receipt["contributors"][0]["credit_excerpt"] = label
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    receipt["artifacts"][0]["sha256"] = digest
    receipt["claims"][0]["sha256"] = digest
    bind_handoff(directory, result)
    if accepted:
        validate_run(validator, release_run)
    else:
        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)


@pytest.mark.parametrize(
    ("label", "accepted"),
    [
        pytest.param("[**Release *Test***](https://example.org/person)", True, id="strong-around-emphasis"),
        pytest.param("[*Release **Test***](https://example.org/person)", True, id="emphasis-around-strong"),
        pytest.param("[__Release _Test___](https://example.org/person)", True, id="nested-underscores"),
        pytest.param("[***Release Test***](https://example.org/person)", True, id="shared-triple-runs"),
        pytest.param("**Release *Test***", True, id="nested-unlinked"),
        pytest.param("__Release *Test*__", True, id="mixed-nested-markers"),
        pytest.param("**Release\u00a0Test**", True, id="unicode-visible-name-space"),
        pytest.param("**Release\u2028Test**", True, id="unicode-visible-name-separator"),
        pytest.param("**Release [*Test*](https://example.org/person)**", True, id="link-within-emphasis"),
        pytest.param('[Release Test](https://example.org/person "Profile")', True, id="inline-title"),
        pytest.param("[Release Test](https://example.org/a(b)c)", True, id="inline-balanced-destination"),
        pytest.param("[**Release *Test*** Extra](https://example.org/person)", False, id="longer-nested-label"),
        pytest.param("[**Release *Tester***](https://example.org/person)", False, id="wrong-nested-label"),
        pytest.param("[**Release *Test**](https://example.org/person)", False, id="unbalanced-nested-label"),
        pytest.param(r"[**Release \*Test***](https://example.org/person)", False, id="escaped-literal-marker"),
        pytest.param("[Release Test](https://example.org/person unexpected words)", False, id="inline-trailing-words"),
        pytest.param('[Release Test](https://example.org/person "Profile" extra)', False, id="inline-title-junk"),
    ],
)
@pytest.mark.integration
def test_release_credit_normalizes_complete_balanced_label(
    validator: ModuleType, release_run: tuple, label: str, accepted: bool
) -> None:
    """Read nested complete names without discarding literal markers, longer names or invalid links."""
    directory, result = release_run
    receipt = result["metadata"]["release_evidence"]
    draft = directory / "deliverables/DRAFT.md"
    draft.write_text(draft.read_text(encoding="utf-8").replace("**Release Test**", label), encoding="utf-8")
    receipt["contributors"][0]["credit_excerpt"] = label
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    receipt["artifacts"][0]["sha256"] = digest
    receipt["claims"][0]["sha256"] = digest
    bind_handoff(directory, result)
    if accepted:
        validate_run(validator, release_run)
    else:
        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["inline", "reference"])
@pytest.mark.parametrize("boundary", ["leading", "before-title", "after-title", "after-destination", "title-content"])
@pytest.mark.parametrize(
    ("separator", "syntax_space", "destination_character"),
    [
        pytest.param(" ", True, False, id="ascii-space"),
        pytest.param("\t", True, False, id="ascii-tab"),
        pytest.param("\x00", False, True, id="nul-replacement-destination"),
        pytest.param("\x0b", False, False, id="vertical-tab"),
        pytest.param("\x0c", False, False, id="form-feed"),
        pytest.param("\x1c", False, False, id="file-separator"),
        pytest.param("\x1f", False, False, id="unit-separator"),
        pytest.param("\x7f", False, False, id="delete-control"),
        pytest.param("\u00a0", False, True, id="nonbreaking-space"),
        pytest.param("\u0085", False, True, id="next-line"),
        pytest.param("\u2003", False, True, id="em-space"),
        pytest.param("\u2028", False, True, id="line-separator"),
        pytest.param("\u2029", False, True, id="paragraph-separator"),
        pytest.param("\u3000", False, True, id="ideographic-space"),
    ],
)
def test_release_link_whitespace_respects_syntax_boundaries(
    validator: ModuleType,
    release_run: tuple,
    kind: str,
    boundary: str,
    separator: str,
    syntax_space: bool,
    destination_character: bool,
) -> None:
    """Distinguish syntax separators from valid Unicode destination or quoted-title content."""
    directory, result = release_run
    receipt = result["metadata"]["release_evidence"]
    uri = "https://example.org/person"
    content = {
        "leading": separator + uri,
        "before-title": uri + separator + '"Public profile"',
        "after-title": uri + ' "Public profile"' + separator,
        "after-destination": f"<{uri}>" + separator,
        "title-content": uri + ' "Public' + separator + 'profile"',
    }[boundary]
    label = f"[Release Test]({content})" if kind == "inline" else "[Release Test][person]"
    draft = directory / "deliverables/DRAFT.md"
    text = draft.read_text(encoding="utf-8").replace("**Release Test**", label)
    if kind == "reference":
        text += f"\n[person]: {content}\n"
    draft.write_text(text, encoding="utf-8")
    receipt["contributors"][0]["credit_excerpt"] = label
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    receipt["artifacts"][0]["sha256"] = digest
    receipt["claims"][0]["sha256"] = digest
    bind_handoff(directory, result)
    accepted = syntax_space or boundary == "title-content" or (boundary == "leading" and destination_character)
    if accepted:
        validate_run(validator, release_run)
    else:
        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)


@pytest.mark.parametrize(
    "separator",
    [
        pytest.param(" ", id="ascii-space"),
        pytest.param("\t", id="ascii-tab"),
        pytest.param("\x0b", id="vertical-tab"),
        pytest.param("\x0c", id="form-feed"),
        pytest.param("\x1c", id="file-separator"),
        pytest.param("\x1f", id="unit-separator"),
        pytest.param("\u00a0", id="nonbreaking-space"),
        pytest.param("\u0085", id="next-line"),
        pytest.param("\u2028", id="line-separator"),
        pytest.param("\u2029", id="paragraph-separator"),
    ],
)
@pytest.mark.parametrize("context", ["paragraph", "fence-ending"])
@pytest.mark.integration
def test_release_reference_boundary_uses_markdown_blank_lines(
    validator: ModuleType, release_run: tuple, separator: str, context: str
) -> None:
    """Unicode whitespace or control text cannot invent a reference-definition paragraph boundary."""
    directory, result = release_run
    receipt = result["metadata"]["release_evidence"]
    label = "[Release Test][person]"
    draft = directory / "deliverables/DRAFT.md"
    text = draft.read_text(encoding="utf-8").replace("**Release Test**", label)
    ending = separator if context == "paragraph" else f"```text\nExample.\n```{separator}"
    draft.write_text(text + f"\n{ending}\n[person]: https://example.org/person\n", encoding="utf-8")
    receipt["contributors"][0]["credit_excerpt"] = label
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    receipt["artifacts"][0]["sha256"] = digest
    receipt["claims"][0]["sha256"] = digest
    bind_handoff(directory, result)
    if separator in " \t":
        validate_run(validator, release_run)
    else:
        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)


@pytest.mark.parametrize("destination", ["bare", "angle"])
@pytest.mark.parametrize(
    "character",
    [
        pytest.param("\u00a0", id="nonbreaking-space"),
        pytest.param("\u0085", id="next-line"),
        pytest.param("\u2003", id="em-space"),
        pytest.param("\u2028", id="line-separator"),
        pytest.param("\u2029", id="paragraph-separator"),
        pytest.param("\u3000", id="ideographic-space"),
        pytest.param("é", id="accented-latin"),
        pytest.param("路径", id="cjk"),
        pytest.param("😀", id="emoji"),
    ],
)
@pytest.mark.integration
def test_release_keeps_valid_unicode_destination_characters(
    validator: ModuleType, release_run: tuple, destination: str, character: str
) -> None:
    """Unicode URL characters remain destination data rather than syntax whitespace or line endings."""
    directory, result = release_run
    receipt = result["metadata"]["release_evidence"]
    uri = f"https://example.org/a{character}b"
    if destination == "angle":
        uri = f"<{uri}>"
    label = f"[Release Test]({uri})"
    draft = directory / "deliverables/DRAFT.md"
    draft.write_text(draft.read_text(encoding="utf-8").replace("**Release Test**", label), encoding="utf-8")
    receipt["contributors"][0]["credit_excerpt"] = label
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    receipt["artifacts"][0]["sha256"] = digest
    receipt["claims"][0]["sha256"] = digest
    bind_handoff(directory, result)
    validate_run(validator, release_run)


@pytest.mark.integration
@pytest.mark.parametrize("outer", ["inline", "reference"])
@pytest.mark.parametrize("inner", ["inline", "reference", "collapsed", "shortcut"])
def test_release_rejects_nested_credit_links(validator: ModuleType, release_run: tuple, outer: str, inner: str) -> None:
    """Keep a deactivated outer link's literal syntax in the complete visible identity."""
    directory, result = release_run
    receipt = result["metadata"]["release_evidence"]
    nested = {
        "inline": "[Test](https://inner.example)",
        "reference": "[Test][inner]",
        "collapsed": "[Test][]",
        "shortcut": "[Test]",
    }[inner]
    label = f"[Release {nested}](https://outer.example)" if outer == "inline" else f"[Release {nested}][outer]"
    draft = directory / "deliverables/DRAFT.md"
    text = draft.read_text(encoding="utf-8").replace("**Release Test**", label)
    if inner != "inline":
        key = "inner" if inner == "reference" else "Test"
        text += f"\n[{key}]: https://inner.example\n"
    if outer == "reference":
        text += "\n[outer]: https://outer.example\n"
    draft.write_text(text, encoding="utf-8")
    receipt["contributors"][0]["credit_excerpt"] = label
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    receipt["artifacts"][0]["sha256"] = digest
    receipt["claims"][0]["sha256"] = digest
    bind_handoff(directory, result)
    with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
        validate_run(validator, release_run)


@pytest.mark.parametrize(
    ("name", "label"),
    [
        pytest.param("Release [Test]", "Release [Test]", id="plain-literal-brackets"),
        pytest.param("Release [Test]", "[Release [Test]](https://example.org/person)", id="linked-literal-brackets"),
        pytest.param("Release [Test]", r"[Release \[Test\]](https://example.org/person)", id="linked-escaped-brackets"),
        pytest.param(
            "[Release Test](https://outer.example)",
            "[Release [Test](https://inner.example)](https://outer.example)",
            id="deactivated-outer-syntax-is-name-text",
        ),
    ],
)
@pytest.mark.integration
def test_release_preserves_literal_credit_brackets(
    validator: ModuleType, release_run: tuple, name: str, label: str
) -> None:
    """Keep literal brackets in a retained PR author's exact visible name, linked or unlinked."""
    directory, result = release_run
    receipt = result["metadata"]["release_evidence"]
    receipt["pr_authors"] = [
        {"sha": result["metadata"]["release_head"], "identity_hash": "f" * 64, "display_name": name}
    ]
    receipt["contributors"].append(
        {"identity_hash": "f" * 64, "display_name": name, "disposition": "credited", "credit_excerpt": label}
    )
    draft = directory / "deliverables/DRAFT.md"
    draft.write_text(draft.read_text(encoding="utf-8") + f"\n- {label} — reviewed contribution.\n", encoding="utf-8")
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    receipt["artifacts"][0]["sha256"] = digest
    receipt["claims"][0]["sha256"] = digest
    bind_handoff(directory, result)
    validate_run(validator, release_run)


@pytest.mark.integration
def test_real_gate_source_receipts_validate_end_to_end(validator: ModuleType, release_run: tuple) -> None:
    """Accept actual runner evidence from a clean pinned source through the release consumer."""
    directory, result = release_run
    repository = directory / "source"
    # The fixture copy is already initialized with this identity; one more commit gives the release a baseline.
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "fixture\n\nCo-authored-by: Codex <codex@openai.com>"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    head, final_tree, baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD", "HEAD^{tree}", "HEAD^"], cwd=repository, text=True
    ).split()
    arguments = [
        sys.executable,
        str(PLUGIN_ROOT / "shared/run_gates.py"),
        "--out",
        str(directory),
        "--expected-head",
        head,
    ]
    for gate in ("lint", "format", "types", "tests", "review"):
        arguments.extend([f"--{gate}", "exit 0"])
    completed = subprocess.run(arguments, cwd=repository, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    gates = json.loads((directory / "gates.json").read_text(encoding="utf-8"))
    assert all(check["source"]["after"] == {"head": head, "status": ""} for check in gates["checks"])
    result["metadata"]["release_head"] = head
    receipt = result["metadata"]["release_evidence"]
    receipt.update(
        repository=str(repository),
        baseline=baseline,
        final_tree=final_tree,
        candidates=[{"sha": head, "disposition": "included", "reason": "release change"}],
    )
    (directory / "release-scope.md").write_text(
        (directory / "release-scope.md").read_text(encoding="utf-8") + f"\n{baseline}\n{head}\n{final_tree}\n",
        encoding="utf-8",
    )
    receipt["claims"][0]["candidate_shas"] = [head]
    bind_handoff(directory, result)
    validate_run(validator, release_run)


@pytest.mark.parametrize("missing", ["DRAFT.md", "SUMMARY.md", "MIGRATION.md", "CHANGELOG.md"])
@pytest.mark.integration
def test_selected_release_output_cannot_be_missing(validator: ModuleType, release_run: tuple, missing: str) -> None:
    """Reject passing readiness artifacts that omit a requested communication output."""
    directory, result = release_run
    result["metadata"]["requested_artifacts"] = list(dict.fromkeys(["DRAFT.md", missing]))
    if missing == "DRAFT.md":
        (directory / "deliverables" / missing).unlink()
    with pytest.raises(SystemExit, match="release-missing-deliverable"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("section", ["Summary", "Highlights", "Migration guide", "Notable changes", "Contributors"])
@pytest.mark.integration
def test_draft_requires_nonempty_sections(validator: ModuleType, release_run: tuple, section: str) -> None:
    """A section name elsewhere in prose cannot substitute for actual draft content."""
    directory, _ = release_run
    draft = directory / "deliverables/DRAFT.md"
    draft.write_text(
        draft.read_text(encoding="utf-8").replace(f"## {section}", f"Mention of {section}"), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="release-draft-section"):
        validate_run(validator, release_run)


@pytest.mark.parametrize(
    "residue", ["[Release hook]", "[User-facing win]", "> Summary guidance:", "[Full changelog URL]"]
)
@pytest.mark.integration
def test_draft_rejects_template_residue(validator: ModuleType, release_run: tuple, residue: str) -> None:
    """A draft that still carries template placeholders or writing notes is not a finished release draft."""
    directory, _ = release_run
    draft = directory / "deliverables/DRAFT.md"
    draft.write_text(draft.read_text(encoding="utf-8") + f"\n{residue}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="release-draft-template-residue"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("missing", ["release-scope.md", "contributors.md", "changelog-audit.md", "draft-review.md"])
@pytest.mark.integration
def test_release_requires_coverage_and_preservation_evidence(
    validator: ModuleType, release_run: tuple, missing: str
) -> None:
    """Passing checks cannot replace release-set, credit, or preservation evidence."""
    directory, _ = release_run
    (directory / missing).unlink()
    with pytest.raises(SystemExit, match="missing-artifact"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("mode", ["notes", "prepare", "demo"])
@pytest.mark.integration
def test_mode_minimum_cannot_be_omitted_from_selected_outputs(
    validator: ModuleType, release_run: tuple, mode: str
) -> None:
    """Prevent deleting the requested-artifact list to make an incomplete mode pass."""
    _, result = release_run
    result["metadata"].update(mode=mode, requested_artifacts=[])
    with pytest.raises(SystemExit, match="release-required-deliverables"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("bad", [None, True, 0, 2, "1"])
@pytest.mark.integration
def test_unknown_release_contract_rejected(validator: ModuleType, release_run: tuple, bad: object) -> None:
    """Malformed version markers cannot silently enter the legacy compatibility path."""
    _, result = release_run
    result["metadata"]["release_contract_version"] = bad
    with pytest.raises(SystemExit, match="release-contract-version"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("status", ["pass", "fail"])
@pytest.mark.integration
def test_complete_notes_and_honestly_blocked_prepare_are_reportable(
    validator: ModuleType, release_run: tuple, status: str
) -> None:
    """Allow completed notes and failed preparation without manufacturing unwritten artifacts."""
    _, result = release_run
    result["status"] = status
    if status == "fail":
        result["checks_failed"] = ["release-scope-unresolved"]
        result["metadata"].update(
            mode="prepare", requested_artifacts=["DRAFT.md", "CHANGELOG.md", "SUMMARY.md", "MIGRATION.md"]
        )
        bind_handoff(release_run[0], result, title="blocked")
    validate_run(validator, release_run)


@pytest.mark.integration
def test_audit_and_historical_report_need_no_new_draft(validator: ModuleType, release_run: tuple) -> None:
    """Read-only audits and retained pre-contract results remain valid without a draft."""
    directory, result = release_run
    (directory / "deliverables/DRAFT.md").unlink()
    result["metadata"].update(mode="audit", requested_artifacts=[])
    bind_handoff(directory, result, title="release-ready")
    validate_run(validator, release_run)
    del result["metadata"]["release_contract_version"]
    del result["metadata"]["final_handoff"]
    del result["schema_version"]
    for name in ("release-scope.md", "contributors.md", "changelog-audit.md", "draft-review.md"):
        (directory / name).unlink()
    validate_run(validator, release_run)


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        pytest.param("## Checks", "Checks in prose", "release-readiness-table-invalid", id="table-heading-missing"),
        pytest.param(
            "| Contributors | pass | Reviewed source | None |\n",
            "",
            "release-readiness-checks-missing",
            id="credits-check-omitted",
        ),
        pytest.param(
            "| SemVer | pass |",
            "| SemVer | unavailable |",
            "release-pass-with-readiness-blocker",
            id="unavailable-is-not-pass",
        ),
        pytest.param(
            "| Changelog | pass |",
            "| Changelog | fail |",
            "release-pass-with-readiness-blocker",
            id="failed-preservation",
        ),
    ],
)
@pytest.mark.integration
def test_readiness_cannot_be_prose_only_or_hide_blockers(
    validator: ModuleType, release_run: tuple, old: str, new: str, error: str
) -> None:
    """Require a complete readiness table and reject green status with failed evidence."""
    directory, _ = release_run
    readiness = directory / "release-readiness.md"
    readiness.write_text(readiness.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    with pytest.raises(SystemExit, match=error):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_new_release_contract_cannot_use_legacy_handoff_bypass(validator: ModuleType, release_run: tuple) -> None:
    """Do not treat a newly versioned release contract as a pre-handoff historical result."""
    _, result = release_run
    result.pop("schema_version")
    result["metadata"].pop("final_handoff")
    with pytest.raises(SystemExit, match="release-contract-requires-schema-v2"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("mode", ["notes", "audit"])
@pytest.mark.integration
def test_release_ready_must_match_mode_and_readiness(validator: ModuleType, release_run: tuple, mode: str) -> None:
    """Prevent release-ready claims for notes-only scope or uncleared readiness warnings."""
    directory, result = release_run
    result["metadata"]["mode"] = mode
    if mode == "audit":
        result["metadata"]["requested_artifacts"] = []
        path = directory / "release-readiness.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("| SemVer | pass |", "| SemVer | warning |"), encoding="utf-8"
        )
    bind_handoff(directory, result, title="release-ready")
    with pytest.raises(SystemExit, match="release-readiness-verdict-mismatch"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_final_readiness_rows_must_match_saved_report(validator: ModuleType, release_run: tuple) -> None:
    """Reject a validly rendered handoff when the report evidence has changed underneath it."""
    directory, _ = release_run
    path = directory / "release-readiness.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Reviewed source", "Different source"), encoding="utf-8")
    with pytest.raises(SystemExit, match="release-readiness-handoff-mismatch"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_new_release_requires_changes_table_beside_readiness(validator: ModuleType, release_run: tuple) -> None:
    """Reject a freshly rendered, internally consistent handoff that silently drops all changes."""
    directory, result = release_run
    bind_handoff(directory, result, only_table="Readiness")
    with pytest.raises(SystemExit, match="release-changes-table-missing"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_historical_release_keeps_single_changes_table(validator: ModuleType, release_run: tuple) -> None:
    """Do not retroactively impose new release tables or source receipts on retained handoffs."""
    directory, result = release_run
    del result["metadata"]["release_contract_version"]
    del result["metadata"]["release_head"]
    bind_handoff(directory, result, only_table="Changes")
    validate_run(validator, release_run)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "wrong-expected",
        "wrong-before",
        "wrong-after",
        "dirty-before",
        "dirty-after",
        "git-error",
        "missing-after",
    ],
)
@pytest.mark.integration
def test_release_rejects_unbound_gate_source(validator: ModuleType, release_run: tuple, mutation: str) -> None:
    """A passing command from another revision or dirty checkout cannot certify pinned release source."""
    directory, _ = release_run
    path = directory / "gates.json"
    gates = json.loads(path.read_text(encoding="utf-8"))
    check = next(check for check in gates["checks"] if check["id"] == "tests")
    source = check["source"]
    if mutation == "missing":
        del check["source"]
    elif mutation == "wrong-expected":
        source["expected_head"] = "b" * 40
    elif mutation.startswith("wrong-"):
        source[mutation.removeprefix("wrong-")]["head"] = "b" * 40
    elif mutation.startswith("dirty-"):
        source[mutation.removeprefix("dirty-")]["status"] = " M source.py\n"
    elif mutation == "git-error":
        source["before"]["error"] = "Git inspection failed"
    else:
        source["after"] = None
    path.write_text(json.dumps(gates), encoding="utf-8")
    with pytest.raises(SystemExit, match="release-gate-source"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("head", [None, "", "main", "abc", "a" * 41])
@pytest.mark.integration
def test_passing_release_requires_pinned_full_head(validator: ModuleType, release_run: tuple, head: object) -> None:
    """Require an immutable Git coordinate before trusting source-bound gate receipts."""
    _, result = release_run
    result["metadata"]["release_head"] = head
    with pytest.raises(SystemExit, match="release-head-invalid"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_blocked_release_can_report_missing_source_evidence(validator: ModuleType, release_run: tuple) -> None:
    """Retain an honest blocked result when release source cannot be selected or verified."""
    directory, result = release_run
    result["status"] = "fail"
    result["checks_failed"] = ["release-source-unavailable"]
    del result["metadata"]["release_head"]
    path = directory / "gates.json"
    gates = json.loads(path.read_text(encoding="utf-8"))
    for check in gates["checks"]:
        check.pop("source")
    path.write_text(json.dumps(gates), encoding="utf-8")
    bind_handoff(directory, result, title="blocked")
    validate_run(validator, release_run)


@pytest.mark.integration
def test_passing_release_requires_machine_receipt(validator: ModuleType, release_run: tuple) -> None:
    """Versioned release prose cannot replace source-bound receipt evidence."""
    _, result = release_run
    del result["metadata"]["release_evidence"]
    with pytest.raises(SystemExit, match="release-evidence-receipt"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_passing_release_rejects_unaccounted_candidate(validator: ModuleType, release_run: tuple) -> None:
    """Every pinned Git candidate needs an explicit disposition and reason."""
    _, result = release_run
    result["metadata"]["release_evidence"]["candidates"] = []
    with pytest.raises(SystemExit, match="release-evidence-candidates"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_passing_release_rejects_stale_final_tree(validator: ModuleType, release_run: tuple) -> None:
    """Candidate accounting cannot reuse a receipt from a different source tree."""
    _, result = release_run
    result["metadata"]["release_evidence"]["final_tree"] = "0" * 40
    with pytest.raises(SystemExit, match="release-evidence-final-tree"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_passing_release_rejects_heading_only_scope_record(validator: ModuleType, release_run: tuple) -> None:
    """Scope headings cannot replace pinned source coordinates and candidate accounting."""
    directory, _ = release_run
    (directory / "release-scope.md").write_text(
        "Head and baseline\nReleased comparison\nCandidate accounting\nLimits\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="release-evidence-scope-record"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_passing_release_rejects_placeholder_summary(validator: ModuleType, release_run: tuple) -> None:
    """A role keyword alone cannot make a selected summary substantive."""
    directory, result = release_run
    summary = directory / "deliverables/SUMMARY.md"
    summary.write_text("# Summary\n\nRelease\n", encoding="utf-8")
    digest = hashlib.sha256(summary.read_bytes()).hexdigest()
    result["metadata"]["requested_artifacts"].append("SUMMARY.md")
    result["metadata"]["release_evidence"]["artifacts"].append(
        {"name": "SUMMARY.md", "destination": str(summary), "sha256": digest}
    )
    with pytest.raises(SystemExit, match="release-evidence-summary-role"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_passing_release_requires_credit_for_known_pr_author(validator: ModuleType, release_run: tuple) -> None:
    """A retained PR author joins the same complete credit accounting as Git identities."""
    _, result = release_run
    receipt = result["metadata"]["release_evidence"]
    receipt["pr_authors"] = [
        {"sha": result["metadata"]["release_head"], "identity_hash": "f" * 64, "display_name": "Pat"}
    ]
    with pytest.raises(SystemExit, match="release-evidence-contributor-coverage"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("bot_name", ["automation[bot]", "renovate"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing-inventory",
        "missing-line",
        "duplicate-line",
        "individual-credit",
        "individual-underscores",
        "individual-link",
        "individual-reference",
        "individual-shortcut",
        "individual-partial-emphasis",
        "individual-nested-emphasis",
        "individual-unicode-destination",
        "stale-proof",
        "valid",
        "underscore-line",
        "plain-line",
    ],
)
@pytest.mark.integration
def test_bot_pr_authors_require_one_aggregate_credit(
    validator: ModuleType, release_run: tuple, bot_name: str, fault: str
) -> None:
    """Keep known PR bots out of human credits and require their one visible aggregate line."""
    directory, result = release_run
    receipt = result["metadata"]["release_evidence"]
    receipt["pr_authors"] = [
        {"sha": result["metadata"]["release_head"], "identity_hash": "f" * 64, "display_name": bot_name, "is_bot": True}
    ]
    proof = directory / "bot-account.json"
    proof.write_text(json.dumps({"login": bot_name, "type": "Bot"}), encoding="utf-8")
    receipt["bots"] = [
        {
            "identity_hash": "f" * 64,
            "display_name": bot_name,
            "label": "@" + bot_name,
            "classification_evidence": {
                "path": proof.name,
                "sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
                "excerpt": proof.read_text(encoding="utf-8"),
            },
        }
    ]
    aggregate = f"*Automated contributions: @{bot_name}*"
    if fault == "underscore-line":
        aggregate = f"_Automated contributions: @{bot_name}_"
    elif fault == "plain-line":
        aggregate = f"Automated contributions: @{bot_name}"
    draft = directory / "deliverables/DRAFT.md"
    text = draft.read_text(encoding="utf-8")
    if fault == "missing-inventory":
        receipt.pop("bots")
    if fault != "missing-line":
        text += "\n" + aggregate + "\n"
    if fault == "duplicate-line":
        text += aggregate + "\n"
    if fault == "individual-credit":
        text += f"- **{bot_name}** — automation.\n"
    if fault == "individual-underscores":
        text += f"- __{bot_name}__ — automation.\n"
    if fault == "individual-link":
        text += f"- [{bot_name}](https://example.org/bot) — automation.\n"
    if fault == "individual-reference":
        text += f"- [{bot_name}][account] — automation.\n\n[account]: https://example.org/bot\n"
    if fault == "individual-shortcut":
        reference_label = bot_name.replace("[", r"\[").replace("]", r"\]")
        text += f"- [{reference_label}] — automation.\n\n[{reference_label}]: https://example.org/bot\n"
    if fault == "individual-partial-emphasis":
        text += f"- **{bot_name[:3]}**{bot_name[3:]} — automation.\n"
    if fault == "individual-nested-emphasis":
        text += f"- [**{bot_name[:3]}*{bot_name[3:]}***](https://example.org/bot) — automation.\n"
    if fault == "individual-unicode-destination":
        text += f"- [{bot_name}](https://example.org/a\u2028b) — automation.\n"
    if fault == "stale-proof":
        proof.write_text("changed evidence", encoding="utf-8")
    draft.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    receipt["artifacts"][0]["sha256"] = digest
    receipt["claims"][0]["sha256"] = digest
    bind_handoff(directory, result)

    if fault in {"valid", "underscore-line", "plain-line"}:
        validate_run(validator, release_run)
    else:
        with pytest.raises(SystemExit, match="release-evidence-bot-"):
            validate_run(validator, release_run)


@pytest.mark.parametrize("second_author", ["Second Human", "automation[bot]"])
@pytest.mark.integration
def test_passing_release_rejects_uncredited_second_coauthor(
    validator: ModuleType, release_run: tuple, second_author: str
) -> None:
    """All human and bot coauthor trailers require credit accounting, not silent filtering."""
    directory, result = release_run
    repository = directory / "source"
    # The fixture pins its release head to the copied repository's current HEAD.
    baseline = result["metadata"]["release_head"]
    subprocess.run(
        [
            "git",
            "commit",
            "--allow-empty",
            "-m",
            f"release change\n\nCo-authored-by: First Human <first@example.invalid>\nCo-authored-by: {second_author} <second@example.invalid>",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    head, final_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD", "HEAD^{tree}"], cwd=repository, text=True
    ).split()
    receipt = result["metadata"]["release_evidence"]
    result["metadata"]["release_head"] = head
    receipt.update(
        baseline=baseline,
        final_tree=final_tree,
        candidates=[{"sha": head, "disposition": "included", "reason": "release change"}],
        contributors=[
            {
                "identity_hash": hashlib.sha256(b"Release Test\0release@example.invalid").hexdigest(),
                "display_name": "Release Test",
                "disposition": "credited",
                "credit_excerpt": "**Release Test**",
            },
        ],
    )
    (directory / "release-scope.md").write_text(
        (directory / "release-scope.md").read_text(encoding="utf-8")
        + f"\n{baseline}\n{head}\n{receipt['final_tree']}\n",
        encoding="utf-8",
    )
    receipt["claims"][0]["candidate_shas"] = [head]
    gates = json.loads((directory / "gates.json").read_text(encoding="utf-8"))
    for check in gates["checks"]:
        check["source"] = {
            "expected_head": head,
            "before": {"head": head, "status": ""},
            "after": {"head": head, "status": ""},
        }
    (directory / "gates.json").write_text(json.dumps(gates), encoding="utf-8")
    bind_handoff(directory, result)
    reason = "bot-coverage" if "[bot]" in second_author else "contributor-coverage"
    with pytest.raises(SystemExit, match=f"release-evidence-{reason}"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("published_state", ["active", "reverted", "different-mode"])
@pytest.mark.parametrize("filename", ["feature.txt", "space ü [x].txt"])
@pytest.mark.parametrize("autocrlf", ["false", "input", "true"])
@pytest.mark.integration
def test_released_patch_subtraction_rejects_introduced_then_reverted_behavior(
    validator: ModuleType,
    release_run: tuple,
    published_state: str,
    filename: str,
    autocrlf: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching historical patch cannot subtract behavior absent from the published release tree."""
    directory, result = release_run
    repository = directory / "source"
    subprocess.run(["git", "config", "core.autocrlf", autocrlf], cwd=repository, check=True, capture_output=True)
    # The fixture pins its release head to the copied repository's current HEAD.
    root = result["metadata"]["release_head"]
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=repository, text=True).strip()
    feature = repository / filename
    # Undefined CP1252 byte 0x9D must survive patch comparison without text decoding.
    feature_bytes = "enabled \u081d\n".encode("utf-8")
    feature.write_bytes(feature_bytes)
    subprocess.run(["git", "add", "--", filename], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "enable feature"], cwd=repository, check=True, capture_output=True)
    introduced = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert subprocess.check_output(["git", "show", f"{introduced}:{filename}"], cwd=repository) == feature_bytes
    subprocess.run(["git", "checkout", "-q", "-b", "published", root], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "merge", "--ff-only", introduced], cwd=repository, check=True, capture_output=True)
    if published_state == "reverted":
        subprocess.run(["git", "revert", "--no-edit", introduced], cwd=repository, check=True, capture_output=True)
    elif published_state == "different-mode":
        subprocess.run(["git", "config", "core.filemode", "false"], cwd=repository, check=True, capture_output=True)
        subprocess.run(
            ["git", "update-index", "--chmod=+x", "--", filename], cwd=repository, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "change executable mode"], cwd=repository, check=True, capture_output=True
        )
    published_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    subprocess.run(["git", "checkout", "-q", "-b", "candidate", root], cwd=repository, check=True, capture_output=True)
    feature.write_bytes(feature_bytes)
    subprocess.run(["git", "add", "--", filename], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "backport feature"], cwd=repository, check=True, capture_output=True)
    candidate, final_tree, published_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD", "HEAD^{tree}", f"{published_head}^{{tree}}"], cwd=repository, text=True
    ).split()
    assert candidate != introduced
    assert subprocess.check_output(["git", "show", f"{candidate}:{filename}"], cwd=repository) == feature_bytes
    receipt = result["metadata"]["release_evidence"]
    result["metadata"]["release_head"] = candidate
    receipt.update(
        baseline=root,
        final_tree=final_tree,
        released_head=published_head,
        candidates=[
            {
                "sha": candidate,
                "disposition": "already-released",
                "reason": "candidate patch was evaluated against the published line",
                "released_sha": introduced,
                "comparison": "stable-patch",
                "published_tree": published_tree,
                "published_paths": [
                    {
                        "path": filename,
                        "sha256": hashlib.sha256(feature_bytes).hexdigest(),
                        "mode": "100644",
                        "object_type": "blob",
                    }
                ],
            }
        ],
        claims=[],
        contributors=[],
    )
    (directory / "release-scope.md").write_text(
        (directory / "release-scope.md").read_text(encoding="utf-8")
        + f"\n{root}\n{candidate}\n{receipt['final_tree']}\n{published_head}\n",
        encoding="utf-8",
    )
    gates = json.loads((directory / "gates.json").read_text(encoding="utf-8"))
    for check in gates["checks"]:
        check["source"] = {
            "expected_head": candidate,
            "before": {"head": candidate, "status": ""},
            "after": {"head": candidate, "status": ""},
        }
    (directory / "gates.json").write_text(json.dumps(gates), encoding="utf-8")
    bind_handoff(directory, result)
    real_run = subprocess.run

    def run_with_cp1252_default(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        """Exercise real Git with a Windows legacy decoder when encoding is unspecified."""
        if kwargs.get("text") and kwargs.get("encoding") is None:
            kwargs["encoding"] = "cp1252"
        return real_run(*args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(subprocess, "run", run_with_cp1252_default)
        if published_state == "active":
            validate_run(validator, release_run)
        else:
            with pytest.raises(SystemExit, match="release-evidence-released-tree-path"):
                validate_run(validator, release_run)
    subprocess.run(["git", "checkout", "-q", branch], cwd=repository, check=True, capture_output=True)


def select_artifact(run: tuple, name: str, text: str) -> Path:
    """Add one requested retained artifact and bind its actual destination for public validation."""
    directory, result = run
    artifact = directory / "deliverables" / name
    artifact.write_text(text, encoding="utf-8", newline="\n")
    result["metadata"]["requested_artifacts"].append(name)
    result["metadata"]["release_evidence"]["artifacts"].append(
        {"name": name, "destination": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    )
    return artifact


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        pytest.param("excerpt", "", "claim-row", id="empty-excerpt"),
        pytest.param("candidate_shas", [], "claim-row", id="empty-candidate-group"),
        pytest.param("artifact", "../contributors.md", "claim-row", id="outside-deliverables"),
    ],
)
@pytest.mark.integration
def test_claim_bindings_cannot_be_empty_or_escape(
    validator: ModuleType, release_run: tuple, field: str, value: object, reason: str
) -> None:
    """A valid receipt cannot replace a real claim anchor with an empty or escaped binding."""
    release_run[1]["metadata"]["release_evidence"]["claims"][0][field] = value
    with pytest.raises(SystemExit, match=f"release-evidence-{reason}"):
        validate_run(validator, release_run)


@pytest.mark.parametrize(
    ("name", "text", "reason"),
    [
        pytest.param("SUMMARY.md", "release overview benefits placeholder", "summary-role", id="summary-keywords"),
        pytest.param("MIGRATION.md", "upgrade change placeholder", "migration-role", id="migration-keywords"),
        pytest.param("CHANGELOG.md", "## v1.2.1\n- placeholder\n", "changelog-excerpt", id="incomplete-changelog"),
    ],
)
@pytest.mark.integration
def test_secondary_artifact_placeholders_rejected(
    validator: ModuleType, release_run: tuple, name: str, text: str, reason: str
) -> None:
    """Nonempty secondary files with matching digests still need their actual content contract."""
    select_artifact(release_run, name, text)
    with pytest.raises(SystemExit, match=f"release-evidence-{reason}"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_complete_prepare_validates_actual_secondary_outputs(validator: ModuleType, release_run: tuple) -> None:
    """Accept the complete selected release set, including the exact canonical target excerpt."""
    directory, result = release_run
    select_artifact(
        release_run,
        "SUMMARY.md",
        "# Summary\n## Overview\nEmpty input is accepted.\n## Benefits\nCallers need no empty-input guard.\n",
    )
    select_artifact(
        release_run, "MIGRATION.md", "# Migration\n## No migration required\nExisting valid input keeps its behavior.\n"
    )
    select_artifact(release_run, "CHANGELOG.md", "## v1.2.1\n- Parsing fix.\n")
    result["metadata"]["mode"] = "prepare"
    bind_handoff(directory, result, title="release-ready")
    validate_run(validator, release_run)


@pytest.mark.integration
def test_actual_artifact_destination_is_rechecked(validator: ModuleType, release_run: tuple) -> None:
    """A retained valid summary cannot conceal different bytes at the user-facing destination."""
    directory, result = release_run
    select_artifact(release_run, "SUMMARY.md", "## Overview\nParsing fix.\n## Benefits\nEmpty input supported.\n")
    destination = directory / "user-summary.md"
    destination.write_text("stale user-facing summary", encoding="utf-8")
    result["metadata"]["release_evidence"]["artifacts"][-1]["destination"] = str(destination)
    with pytest.raises(SystemExit, match="release-evidence-artifact-binding"):
        validate_run(validator, release_run)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-person",
        "partial-name",
        "plain-substring",
        "wrong-linked-name",
        "longer-emphasized-link",
        "longer-emphasized-reference",
        "missing-reference",
        "cross-document-reference",
        "definition-only-credit",
        "definition-only-unicode-destination",
        "fenced-credit-only",
        "missing-anchor",
        "unproved-exclusion",
    ],
)
@pytest.mark.integration
def test_human_credit_requires_identity_and_visible_evidence(
    validator: ModuleType, release_run: tuple, mutation: str
) -> None:
    """Hash accounting alone cannot omit humans or assign their credit to someone else."""
    directory, result = release_run
    credit = result["metadata"]["release_evidence"]["contributors"][0]
    if mutation == "wrong-person":
        credit.update(display_name="Codex", credit_excerpt="**Codex**")
        reason = "contributor-display-name"
    elif mutation in {
        "partial-name",
        "plain-substring",
        "wrong-linked-name",
        "longer-emphasized-link",
        "longer-emphasized-reference",
    }:
        draft = directory / "deliverables/DRAFT.md"
        replacement = (
            "[Release Tester](https://example.org/person)" if mutation == "wrong-linked-name" else "__Release Tester__"
        )
        if mutation == "longer-emphasized-link":
            replacement = "[**Release Test** Extra](https://example.org/person)"
        elif mutation == "longer-emphasized-reference":
            replacement = "[**Release Test** Extra][person]"
        draft.write_text(draft.read_text(encoding="utf-8").replace("**Release Test**", replacement), encoding="utf-8")
        if mutation == "longer-emphasized-reference":
            draft.write_text(
                draft.read_text(encoding="utf-8") + "\n[person]: https://example.org/person\n", encoding="utf-8"
            )
        credit["credit_excerpt"] = "Release Test" if mutation == "plain-substring" else replacement
        digest = hashlib.sha256(draft.read_bytes()).hexdigest()
        result["metadata"]["release_evidence"]["artifacts"][0]["sha256"] = digest
        result["metadata"]["release_evidence"]["claims"][0]["sha256"] = digest
        reason = "contributor-credit"
    elif mutation == "missing-anchor":
        credit["credit_excerpt"] = "**Release Test** — credit not actually present"
        reason = "contributor-credit"
    elif mutation in {
        "missing-reference",
        "cross-document-reference",
        "definition-only-credit",
        "definition-only-unicode-destination",
        "fenced-credit-only",
    }:
        draft = directory / "deliverables/DRAFT.md"
        text = draft.read_text(encoding="utf-8")
        replacement = "[Release Test][person]"
        if mutation in {"definition-only-credit", "definition-only-unicode-destination"}:
            replacement = "A different contributor"
            prefix = "\u00a0" if mutation == "definition-only-unicode-destination" else ""
            text += f"\n[Release Test]: {prefix}https://example.org/person\n"
            credit["credit_excerpt"] = "[Release Test]"
        elif mutation == "fenced-credit-only":
            replacement = "A different contributor"
            text += "\n```markdown\n- **Release Test** — example only.\n```\n"
            credit["credit_excerpt"] = "**Release Test**"
        else:
            credit["credit_excerpt"] = replacement
        draft.write_text(
            text.replace("- **Release Test** — parsing fix.", f"- {replacement} — parsing fix."), encoding="utf-8"
        )
        if mutation == "cross-document-reference":
            select_artifact(
                release_run,
                "SUMMARY.md",
                "## Overview\nParsing improved.\n## Benefits\nInput supported.\n\n[person]: https://example.org/person\n",
            )
        digest = hashlib.sha256(draft.read_bytes()).hexdigest()
        result["metadata"]["release_evidence"]["artifacts"][0]["sha256"] = digest
        result["metadata"]["release_evidence"]["claims"][0]["sha256"] = digest
        reason = "contributor-credit"
    else:
        credit.update(disposition="excluded", exclusion_basis="privacy-request", exclusion="Remove this credit")
        (directory / "contributors.md").write_text(
            "Coverage Credits Exclusions Unresolved\nRemove this credit", encoding="utf-8"
        )
        reason = "contributor-exclusion-proof"
    with pytest.raises(SystemExit, match=f"release-evidence-{reason}"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("mutation", ["history-detail", "history-reference", "target-detail"])
@pytest.mark.integration
def test_changelog_content_cannot_be_pruned(validator: ModuleType, release_run: tuple, mutation: str) -> None:
    """Rehashed final bytes cannot conceal removed historical or preexisting target information."""
    _, result = release_run
    record = result["metadata"]["release_evidence"]["changelog"]
    backup, destination = Path(record["backup"]), Path(record["destination"])
    if mutation == "history-detail":
        destination.write_bytes(destination.read_bytes().replace(b"Existing detail.", b"Short."))
        reason = "changelog-history"
    elif mutation == "history-reference":
        backup.write_bytes(backup.read_bytes() + b"\n[old]: https://example.org/history\n")
        reason = "changelog-history"
    else:
        backup.write_bytes(backup.read_bytes() + b"\n## v1.2.1\n- Detailed API condition.\n")
        reason = "changelog-target-detail"
    record.update(
        backup_sha256=hashlib.sha256(backup.read_bytes()).hexdigest(),
        final_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
    )
    with pytest.raises(SystemExit, match=f"release-evidence-{reason}"):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_changelog_target_additions_preserve_existing_detail(validator: ModuleType, release_run: tuple) -> None:
    """Allow ordinary target-section additions while retaining older exact bytes and target entries."""
    _, result = release_run
    record = result["metadata"]["release_evidence"]["changelog"]
    backup, destination = Path(record["backup"]), Path(record["destination"])
    backup.write_bytes(destination.read_bytes())
    destination.write_bytes(destination.read_bytes() + b"- Additional condition documented.\n")
    record.update(
        backup_sha256=hashlib.sha256(backup.read_bytes()).hexdigest(),
        final_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
    )
    validate_run(validator, release_run)


@pytest.mark.parametrize("selector_style", ["atx", "setext"])
@pytest.mark.integration
def test_setext_changelog_retains_exact_unicode_crlf_history_and_excerpt(
    validator: ModuleType, release_run: tuple, selector_style: str
) -> None:
    """Semantic version lookup must preserve original historical and selected excerpt bytes."""
    _, result = release_run
    record = result["metadata"]["release_evidence"]["changelog"]
    if selector_style == "setext":
        record["target_heading"] = "v1.2.1\n------"
    backup, destination = Path(record["backup"]), Path(record["destination"])
    historical = "Changelog\r\n=========\r\n\r\nv1.0\r\n----\r\n- Café behavior.\r\n".encode()
    target = b"v1.2.1\r\n------\r\n- Parsing fix.\r\n"
    backup.write_bytes(historical + b"\r\nv1.2.1\r\n------\r\n- Existing target detail.\r\n")
    destination.write_bytes(historical + b"\r\n" + target + b"- Existing target detail.\r\n")
    record.update(
        backup_sha256=hashlib.sha256(backup.read_bytes()).hexdigest(),
        final_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
    )
    excerpt = select_artifact(release_run, "CHANGELOG.md", "placeholder")
    expected = target + b"- Existing target detail.\n"
    excerpt.write_bytes(expected)
    result["metadata"]["release_evidence"]["artifacts"][-1]["sha256"] = hashlib.sha256(excerpt.read_bytes()).hexdigest()
    validate_run(validator, release_run)
    assert destination.read_bytes().startswith(historical)
    assert excerpt.read_bytes() == expected
    destination.write_bytes(destination.read_bytes().replace("Café".encode(), b"Short"))
    record["final_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="release-evidence-changelog-history"):
        validate_run(validator, release_run)


@pytest.mark.parametrize("name", ["DRAFT.md", "SUMMARY.md", "MIGRATION.md"])
@pytest.mark.integration
def test_release_required_sections_cannot_be_supplied_by_fenced_examples(
    validator: ModuleType, release_run: tuple, name: str
) -> None:
    """An example's headings cannot supply the actual selected document's required sections."""
    directory, result = release_run
    if name == "DRAFT.md":
        artifact = directory / "deliverables/DRAFT.md"
        artifact.write_text("```markdown\n" + artifact.read_text(encoding="utf-8") + "\n```\n", encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        result["metadata"]["release_evidence"]["artifacts"][0]["sha256"] = digest
        result["metadata"]["release_evidence"]["claims"][0]["sha256"] = digest
        reason = "release-draft-section"
    else:
        body = (
            "Overview\n--------\nParsing fix.\n\nBenefits\n--------\nEmpty input supported.\n"
            if name == "SUMMARY.md"
            else "No migration required\n---------------------\nInputs unchanged.\n"
        )
        select_artifact(release_run, name, "```markdown\n" + body + "```\n")
        reason = "release-evidence-summary-role" if name == "SUMMARY.md" else "release-evidence-migration-role"
    with pytest.raises(SystemExit, match=reason):
        validate_run(validator, release_run)


@pytest.mark.integration
def test_release_without_canonical_changelog_records_explicit_absence(
    validator: ModuleType, release_run: tuple
) -> None:
    """Local-only notes can disclose a project without a canonical changelog instead of inventing one."""
    release_run[1]["metadata"]["release_evidence"]["changelog"] = {
        "destination": None,
        "backup": None,
        "absent_reason": "Project has no canonical changelog.",
    }
    validate_run(validator, release_run)


@pytest.mark.integration
@pytest.mark.parametrize("stdout", ["", "not JSON\n"])
def test_demo_execution_reports_recorder_crashes(
    validator: ModuleType, release_run: tuple, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    """Keep a recorder crash visible when its exit code matches an expected demo failure."""
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write({stdout!r}); raise RuntimeError('recorder cleanup failed')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    def failed_recorder(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Supply the real crash result at the recorder subprocess boundary."""
        assert command[1] == str(PLUGIN_ROOT / "shared/release_evidence.py")
        assert command[2] == "record-demo"
        return crashed

    monkeypatch.setattr(subprocess, "run", failed_recorder)
    with pytest.raises(pytest.fail.Exception, match="Demo recorder did not produce a JSON receipt") as raised:
        test_demo_execution_receipt_is_produced_and_verified(validator, release_run, "timeout")

    failure = str(raised.value)
    assert "returncode=1" in failure
    assert f"stdout={stdout!r}" in failure
    assert "RuntimeError: recorder cleanup failed" in failure


@pytest.mark.parametrize(
    "scenario", ["success", "silent-success", "failure", "missing", "output-drift", "script-drift", "timeout"]
)
@pytest.mark.integration
def test_demo_execution_receipt_is_produced_and_verified(
    validator: ModuleType, release_run: tuple, scenario: str
) -> None:
    """Exercise the public recorder and validator with actual successful, failed, stale, and absent runs."""
    directory, result = release_run
    source = "print('demo successful')\n"
    if scenario == "failure":
        source = "raise RuntimeError('broken demo')\n"
    elif scenario == "timeout":
        source = "import time\ntime.sleep(10)\n"
    elif scenario == "silent-success":
        source = "answer = 42\n"
    demo = select_artifact(release_run, "demo.py", source)
    result["metadata"].update(mode="demo", requested_artifacts=["demo.py"])
    receipt = result["metadata"]["release_evidence"]
    receipt["artifacts"] = receipt["artifacts"][-1:]
    receipt["claims"] = [
        {
            "artifact": "demo.py",
            "sha256": hashlib.sha256(demo.read_bytes()).hexdigest(),
            "excerpt": source.strip(),
            "candidate_shas": [result["metadata"]["release_head"]],
        }
    ]
    (directory / "contributors.md").write_text(
        "Coverage Credits Exclusions Unresolved\n**Release Test**\n**Codex**\n", encoding="utf-8"
    )
    if scenario != "missing":
        completed = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "shared/release_evidence.py"),
                "record-demo",
                "--script",
                str(demo),
                "--cwd",
                str(directory),
                "--environment",
                "test environment",
                "--out",
                str(directory / "demo-execution"),
                "--timeout-seconds",
                # Only the timeout scenario needs the short budget; a loaded runner can take over 1 s to start Python
                "1" if scenario == "timeout" else "60",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        diagnostics = f"returncode={completed.returncode}\nstdout={completed.stdout!r}\nstderr={completed.stderr}"
        assert completed.returncode == (1 if scenario in {"failure", "timeout"} else 0), diagnostics
        try:
            receipt["demo"] = json.loads(completed.stdout)
        except json.JSONDecodeError:
            pytest.fail(f"Demo recorder did not produce a JSON receipt:\n{diagnostics}")
        if scenario == "output-drift":
            Path(receipt["demo"]["output"]).write_bytes(b"forged output")
        elif scenario == "script-drift":
            demo.write_text("print('different script')\n", encoding="utf-8")
            digest = hashlib.sha256(demo.read_bytes()).hexdigest()
            receipt["artifacts"][0]["sha256"] = digest
            receipt["claims"][0].update(sha256=digest, excerpt="print('different script')")
    if scenario in {"success", "silent-success"}:
        validate_run(validator, release_run)
    else:
        reason = (
            "demo-execution"
            if scenario in {"failure", "timeout"}
            else "demo-output"
            if scenario == "output-drift"
            else "demo-receipt"
        )
        with pytest.raises(SystemExit, match=f"release-evidence-{reason}"):
            validate_run(validator, release_run)
