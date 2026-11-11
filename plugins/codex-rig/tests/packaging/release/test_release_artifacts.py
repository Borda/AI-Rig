"""Exercise release communication completion through the public artifact validator.

Every case builds a complete, valid release run on disk (gate logs, readiness report, draft, receipt, rendered handoff,
and a real one-commit Git repository), breaks exactly one thing, and validates through the same entrypoint the CLI uses.
Nothing in the shipped validator, release evidence helper, or gate runner is mocked.
"""

from __future__ import annotations

import functools
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]

Run = tuple[Path, dict[str, Any]]
_REAL_RUN = subprocess.run
_PERSON_URI = "https://example.org/person"
_GATES = ("lint", "format", "types", "tests", "review")
_BOTS = ["automation[bot]", "renovate"]


@functools.cache
def load_finalizer() -> ModuleType:
    """Load the shipped handoff renderer once; it keeps no module-level state between renders."""
    return load_module("release_finalizer", PLUGIN_ROOT / "shared/final_handoff.py")


def bind_handoff(
    directory: Path,
    result: dict[str, Any],
    *,
    title: str = "warning-only",
    only_table: str | None = None,
    presentation_version: int = 8,
) -> None:
    """Render real release handoff evidence so tests exercise schema-v2 completion end to end."""
    finalizer = load_finalizer()
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


def gate_source(head: str) -> dict[str, Any]:
    """Return the clean source receipt a gate records when it ran on exactly ``head``."""
    return {"expected_head": head, "before": {"head": head, "status": ""}, "after": {"head": head, "status": ""}}


@pytest.fixture(scope="module")
def validator() -> ModuleType:
    """Load the shipped validator once per module without mocking its release or shared gates.

    The validator keeps no module-level state; every run validates its own directory and candidate.
    """
    return load_module("release_artifact_validator", PLUGIN_ROOT / "shared/validate-artifacts.py")


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
def release_run(tmp_path: Path, text_newline_default: None, release_source_template: tuple[Path, str, str]) -> Run:
    """Create complete release evidence with real gate logs and a structured draft."""
    template, head, final_tree = release_source_template
    checks = []
    for gate in _GATES:
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
                "source": gate_source(head),
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
        "release-scope.md": f"Head and baseline\nReleased comparison\nCandidate accounting\nLimits\n{head}\n{final_tree}\n",
        "contributors.md": "Coverage\nCredits\n**Release Test**\n**Codex**\nExclusions\nUnresolved",
        "changelog-audit.md": "Scope\nPreservation\nAdded and flagged\nLimits",
        "draft-review.md": "Claims\nContributors\nChangelog preservation\nExamples\nDeliverables\nLimits",
    }
    for name, text in evidence.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    outputs = tmp_path / "deliverables"
    outputs.mkdir()
    draft = outputs / "DRAFT.md"
    draft.write_text(
        "# v1.2.1: Accurate parsing\n\n## Summary\nParsing accepts valid empty input.\n\n"
        "- **Empty input** returns an empty result instead of raising.\n\nDrop-in upgrade, no code changes.\n"
        "## Highlights\nEmpty input now returns an empty result instead of raising.\n"
        "## Migration guide\nNo migration required.\n## Notable changes\n"
        "- Fixed empty input ([commit](https://example.org/commit/abc)).\n"
        "## Contributors\n- **Release Test** — parsing fix.\n- **Codex** — coauthored release evidence.\n\n"
        "**Full changelog**: https://example.org/compare/v1.2.0...abc\n",
        encoding="utf-8",
    )
    repository = tmp_path / "source"
    shutil.copytree(template, repository)
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
    bind_handoff(tmp_path, result)
    return tmp_path, result


def validate_run(validator: ModuleType, run: Run) -> None:
    """Persist a candidate and invoke the same public entrypoint as the CLI."""
    directory, result = run
    candidate = directory / "result.candidate.json"
    candidate.write_text(json.dumps(result), encoding="utf-8")
    validator.validate("release", directory, candidate)


def receipt_of(run: Run) -> dict[str, Any]:
    """Return the run's mutable release evidence receipt."""
    return run[1]["metadata"]["release_evidence"]


def read_draft(run: Run) -> str:
    """Return the current text of the run's retained draft."""
    return (run[0] / "deliverables/DRAFT.md").read_text(encoding="utf-8")


def rebind_draft(run: Run, text: str) -> None:
    """Write the draft, point the receipt's artifact and claim digests at it, and render the handoff again."""
    directory, result = run
    draft = directory / "deliverables/DRAFT.md"
    draft.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    receipt = receipt_of(run)
    receipt["artifacts"][0]["sha256"] = digest
    receipt["claims"][0]["sha256"] = digest
    bind_handoff(directory, result)


def credit_with_label(run: Run, label: str, appendix: str = "", *, excerpt: str | None = None) -> None:
    """Replace the first contributor's credit by ``label``, append ``appendix`` to the draft, and rebind the receipt.

    The receipt's credit excerpt is ``label`` unless a different ``excerpt`` is claimed.
    """
    receipt_of(run)["contributors"][0]["credit_excerpt"] = label if excerpt is None else excerpt
    rebind_draft(run, read_draft(run).replace("**Release Test**", label) + appendix)


def select_artifact(run: Run, name: str, text: str, *, newline: str | None = "\n") -> Path:
    """Add one requested retained artifact and bind its actual destination for public validation."""
    directory, result = run
    artifact = directory / "deliverables" / name
    artifact.write_text(text, encoding="utf-8", newline=newline)
    result["metadata"]["requested_artifacts"].append(name)
    receipt_of(run)["artifacts"].append(
        {"name": name, "destination": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    )
    return artifact


def append_scope(directory: Path, *coordinates: str) -> None:
    """Append pinned Git coordinates to the scope record, one per line."""
    scope = directory / "release-scope.md"
    scope.write_text(
        scope.read_text(encoding="utf-8") + "".join(f"\n{item}" for item in coordinates) + "\n", encoding="utf-8"
    )


def pin_gate_sources(directory: Path, head: str) -> None:
    """Make every recorded gate claim a clean run on exactly ``head``."""
    path = directory / "gates.json"
    gates = json.loads(path.read_text(encoding="utf-8"))
    for check in gates["checks"]:
        check["source"] = gate_source(head)
    path.write_text(json.dumps(gates), encoding="utf-8")


def rehash_changelog(record: dict[str, Any]) -> None:
    """Refresh the changelog record's digests from the current backup and destination bytes."""
    record.update(
        backup_sha256=hashlib.sha256(Path(record["backup"]).read_bytes()).hexdigest(),
        final_sha256=hashlib.sha256(Path(record["destination"]).read_bytes()).hexdigest(),
    )


def add_pr_author(run: Run, name: str, **extra: Any) -> None:
    """Record ``name`` as the retained PR author of the release head."""
    receipt_of(run)["pr_authors"] = [
        {"sha": run[1]["metadata"]["release_head"], "identity_hash": "f" * 64, "display_name": name, **extra}
    ]


def git(repository: Path, *arguments: str) -> str:
    """Run one Git command in the private repository copy and return its decoded, trimmed output."""
    completed = subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    return completed.stdout.decode(errors="replace").strip()


def link_markup(kind: str, content: str) -> tuple[str, str]:
    """Return the visible credit label and the appendix that carries ``content`` as an inline or reference link."""
    if kind == "inline":
        return f"[Release Test]({content})", ""
    return "[Release Test][person]", f"\n[person]: {content}\n"


_LINK_BOUNDARIES = {
    "leading": "{sep}{uri}",
    "before-title": '{uri}{sep}"Public profile"',
    "after-title": '{uri} "Public profile"{sep}',
    "after-destination": "<{uri}>{sep}",
    "title-content": '{uri} "Public{sep}profile"',
}

# (id, separator, is a Markdown syntax space, is valid destination content)
_LINK_SEPARATORS = [
    ("ascii-space", " ", True, False),
    ("ascii-tab", "\t", True, False),
    ("nul-replacement-destination", "\x00", False, True),
    ("vertical-tab", "\x0b", False, False),
    ("form-feed", "\x0c", False, False),
    ("file-separator", "\x1c", False, False),
    ("unit-separator", "\x1f", False, False),
    ("delete-control", "\x7f", False, False),
    ("nonbreaking-space", "\u00a0", False, True),
    ("next-line", "\u0085", False, True),
    ("em-space", "\u2003", False, True),
    ("line-separator", "\u2028", False, True),
    ("paragraph-separator", "\u2029", False, True),
    ("ideographic-space", "\u3000", False, True),
]


def link_whitespace_cases(*, accepted: bool) -> list[Any]:
    """Build the (kind, boundary, separator) cases whose Markdown verdict equals ``accepted``."""
    return [
        pytest.param(kind, boundary, separator, id=f"{kind}-{boundary}-{name}")
        for kind in ("inline", "reference")
        for boundary in _LINK_BOUNDARIES
        for name, separator, syntax_space, destination_character in _LINK_SEPARATORS
        if (syntax_space or boundary == "title-content" or (boundary == "leading" and destination_character))
        is accepted
    ]


_BLANK_LINE_CONTEXTS = [
    pytest.param("{sep}", id="paragraph"),
    pytest.param("```text\nExample.\n```{sep}", id="fence-ending"),
]

_BOT_AGGREGATE = "*Automated contributions: @{bot}*"
_BOT_REJECTED_APPENDIX = [
    pytest.param("", id="missing-line"),
    pytest.param(f"\n{_BOT_AGGREGATE}\n{_BOT_AGGREGATE}\n", id="duplicate-line"),
    pytest.param(f"\n{_BOT_AGGREGATE}\n- **{{bot}}** — automation.\n", id="individual-credit"),
    pytest.param(f"\n{_BOT_AGGREGATE}\n- __{{bot}}__ — automation.\n", id="individual-underscores"),
    pytest.param(f"\n{_BOT_AGGREGATE}\n- [{{bot}}](https://example.org/bot) — automation.\n", id="individual-link"),
    pytest.param(
        f"\n{_BOT_AGGREGATE}\n- [{{bot}}][account] — automation.\n\n[account]: https://example.org/bot\n",
        id="individual-reference",
    ),
    pytest.param(
        f"\n{_BOT_AGGREGATE}\n- [{{ref}}] — automation.\n\n[{{ref}}]: https://example.org/bot\n",
        id="individual-shortcut",
    ),
    pytest.param(f"\n{_BOT_AGGREGATE}\n- **{{head}}**{{tail}} — automation.\n", id="individual-partial-emphasis"),
    pytest.param(
        f"\n{_BOT_AGGREGATE}\n- [**{{head}}*{{tail}}***](https://example.org/bot) — automation.\n",
        id="individual-nested-emphasis",
    ),
    pytest.param(
        f"\n{_BOT_AGGREGATE}\n- [{{bot}}](https://example.org/a\u2028b) — automation.\n",
        id="individual-unicode-destination",
    ),
]


def arrange_bot(run: Run, bot_name: str) -> Path:
    """Record ``bot_name`` as a PR author with classification proof and return the proof file."""
    directory, _ = run
    add_pr_author(run, bot_name, is_bot=True)
    proof = directory / "bot-account.json"
    proof.write_text(json.dumps({"login": bot_name, "type": "Bot"}), encoding="utf-8")
    receipt_of(run)["bots"] = [
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
    return proof


def append_bot_text(run: Run, template: str, bot_name: str) -> None:
    """Append the template, filled in for ``bot_name``, to the draft and rebind the receipt."""
    text = template.format(
        bot=bot_name,
        head=bot_name[:3],
        tail=bot_name[3:],
        ref=bot_name.replace("[", r"\[").replace("]", r"\]"),
    )
    rebind_draft(run, read_draft(run) + text)


def keep_published(repository: Path, introduced: str, filename: str) -> None:
    """Leave the published line holding the introduced behavior."""


def revert_published(repository: Path, introduced: str, filename: str) -> None:
    """Revert the introduced behavior on the published line."""
    git(repository, "revert", "--no-edit", introduced)


def change_published_mode(repository: Path, introduced: str, filename: str) -> None:
    """Keep the introduced bytes on the published line but change the file's executable mode."""
    git(repository, "config", "core.filemode", "false")
    git(repository, "update-index", "--chmod=+x", "--", filename)
    git(repository, "commit", "-m", "change executable mode")


def run_with_cp1252_default(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """Run real Git with a Windows legacy decoder when text decoding is left unspecified."""
    if kwargs.get("text") and kwargs.get("encoding") is None:
        kwargs["encoding"] = "cp1252"
    return _REAL_RUN(*args, **kwargs)


def arrange_backported_feature(
    run: Run, filename: str, autocrlf: str, publish: Callable[[Path, str, str], None]
) -> None:
    """Build a candidate that backports a feature the published line introduced, then reverted or kept.

    ``publish`` decides what the published line holds afterwards, and the receipt claims the candidate as already
    released by a stable-patch comparison against it.
    """
    directory, result = run
    repository = directory / "source"
    git(repository, "config", "core.autocrlf", autocrlf)
    # The fixture pins its release head to the copied repository's current HEAD.
    root = result["metadata"]["release_head"]
    feature = repository / filename
    # Undefined CP1252 byte 0x9D must survive patch comparison without text decoding.
    feature_bytes = "enabled \u081d\n".encode()
    feature.write_bytes(feature_bytes)
    git(repository, "add", "--", filename)
    git(repository, "commit", "-m", "enable feature")
    introduced = git(repository, "rev-parse", "HEAD")
    assert subprocess.check_output(["git", "show", f"{introduced}:{filename}"], cwd=repository) == feature_bytes
    git(repository, "checkout", "-q", "-b", "published", root)
    git(repository, "merge", "--ff-only", introduced)
    publish(repository, introduced, filename)
    published_head = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "-q", "-b", "candidate", root)
    feature.write_bytes(feature_bytes)
    git(repository, "add", "--", filename)
    git(repository, "commit", "-m", "backport feature")
    candidate, final_tree, published_tree = git(
        repository, "rev-parse", "HEAD", "HEAD^{tree}", f"{published_head}^{{tree}}"
    ).split()
    assert candidate != introduced
    assert subprocess.check_output(["git", "show", f"{candidate}:{filename}"], cwd=repository) == feature_bytes
    result["metadata"]["release_head"] = candidate
    receipt_of(run).update(
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
    append_scope(directory, root, candidate, final_tree, published_head)
    pin_gate_sources(directory, candidate)
    bind_handoff(directory, result)


def arrange_setext_changelog(run: Run, selector_style: str) -> SimpleNamespace:
    """Give the run a CRLF setext changelog with Unicode history and its selected excerpt."""
    record = receipt_of(run)["changelog"]
    if selector_style == "setext":
        record["target_heading"] = "v1.2.1\n------"
    backup, destination = Path(record["backup"]), Path(record["destination"])
    historical = "Changelog\r\n=========\r\n\r\nv1.0\r\n----\r\n- Café behavior.\r\n".encode()
    target = b"v1.2.1\r\n------\r\n- Parsing fix.\r\n"
    backup.write_bytes(historical + b"\r\nv1.2.1\r\n------\r\n- Existing target detail.\r\n")
    destination.write_bytes(historical + b"\r\n" + target + b"- Existing target detail.\r\n")
    rehash_changelog(record)
    excerpt = select_artifact(run, "CHANGELOG.md", "placeholder")
    expected = target + b"- Existing target detail.\n"
    excerpt.write_bytes(expected)
    receipt_of(run)["artifacts"][-1]["sha256"] = hashlib.sha256(excerpt.read_bytes()).hexdigest()
    return SimpleNamespace(
        historical=historical, expected=expected, destination=destination, excerpt=excerpt, record=record
    )


def prepare_demo(run: Run, source: str) -> Path:
    """Select ``source`` as the only demo deliverable and claim it, returning the retained script."""
    directory, result = run
    demo = select_artifact(run, "demo.py", source)
    result["metadata"].update(mode="demo", requested_artifacts=["demo.py"])
    receipt = receipt_of(run)
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
    return demo


def record_demo(run: Run, demo: Path, *, timeout_seconds: int, returncode: int) -> None:
    """Run the public demo recorder and store its JSON receipt, failing with full diagnostics when it crashes."""
    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/release_evidence.py"),
            "record-demo",
            "--script",
            str(demo),
            "--cwd",
            str(run[0]),
            "--environment",
            "test environment",
            "--out",
            str(run[0] / "demo-execution"),
            "--timeout-seconds",
            str(timeout_seconds),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    diagnostics = f"returncode={completed.returncode}\nstdout={completed.stdout!r}\nstderr={completed.stderr}"
    assert completed.returncode == returncode, diagnostics
    try:
        receipt_of(run)["demo"] = json.loads(completed.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"Demo recorder did not produce a JSON receipt:\n{diagnostics}")


@pytest.mark.integration
class TestReadinessAndHandoff:
    """Readiness reports, final handoff rendering, and the verdict they must agree on."""

    def test_historical_presentation_stays_readable(self, validator: ModuleType, release_run: Run) -> None:
        """A retained result rendered with an older presentation version still validates as canonical.

        Old reports on disk were rendered before the current presentation existed; they must remain readable.
        """
        directory, result = release_run
        bind_handoff(directory, result, presentation_version=2)
        canonical = directory / "result.json"
        canonical.write_text(json.dumps(result), encoding="utf-8")

        validator.validate("release", directory, canonical)

    def test_historical_presentation_cannot_certify_fresh_candidate(
        self, validator: ModuleType, release_run: Run
    ) -> None:
        """Old rendering is not admitted for a new release result.

        The same older presentation that stays readable as a retained report must fail as a fresh candidate.
        """
        directory, result = release_run
        bind_handoff(directory, result, presentation_version=2)

        with pytest.raises(SystemExit, match="current-handoff-presentation-v8-required"):
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
    def test_readiness_needs_full_table_and_cannot_hide_blockers(
        self, validator: ModuleType, release_run: Run, old: str, new: str, error: str
    ) -> None:
        """Require a complete readiness table and reject green status with failed evidence.

        A prose-only readiness report or a missing credits row cannot pass, and neither can a pass verdict that sits
        beside an unavailable or failed check.
        """
        readiness = release_run[0] / "release-readiness.md"
        readiness.write_text(readiness.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

        with pytest.raises(SystemExit, match=error):
            validate_run(validator, release_run)

    def test_new_contract_cannot_use_legacy_handoff_bypass(self, validator: ModuleType, release_run: Run) -> None:
        """A newly versioned release contract is never treated as a pre-handoff historical result.

        Dropping the schema version and the final handoff from a versioned result must not select the legacy path.
        """
        _, result = release_run
        result.pop("schema_version")
        result["metadata"].pop("final_handoff")

        with pytest.raises(SystemExit, match="release-contract-requires-schema-v2"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("mode", "requested", "status"),
        [
            pytest.param("notes", ["DRAFT.md"], "pass", id="notes-only-scope"),
            pytest.param("audit", [], "warning", id="uncleared-warning"),
        ],
    )
    def test_release_ready_must_match_mode_and_readiness(
        self, validator: ModuleType, release_run: Run, mode: str, requested: list[str], status: str
    ) -> None:
        """A release-ready title is rejected for a notes-only scope or for uncleared readiness warnings.

        Notes mode never certifies readiness, and an audit with a warning row is not ready either.
        """
        directory, result = release_run
        result["metadata"].update(mode=mode, requested_artifacts=requested)
        readiness = directory / "release-readiness.md"
        readiness.write_text(
            readiness.read_text(encoding="utf-8").replace("| SemVer | pass |", f"| SemVer | {status} |"),
            encoding="utf-8",
        )
        bind_handoff(directory, result, title="release-ready")

        with pytest.raises(SystemExit, match="release-readiness-verdict-mismatch"):
            validate_run(validator, release_run)

    def test_readiness_rows_must_match_saved_report(self, validator: ModuleType, release_run: Run) -> None:
        """A validly rendered handoff is rejected when the saved readiness evidence changed underneath it.

        The handoff rows were rendered from the original report; editing the report afterwards leaves them stale.
        """
        readiness = release_run[0] / "release-readiness.md"
        readiness.write_text(
            readiness.read_text(encoding="utf-8").replace("Reviewed source", "Different source"), encoding="utf-8"
        )

        with pytest.raises(SystemExit, match="release-readiness-handoff-mismatch"):
            validate_run(validator, release_run)

    def test_new_release_requires_changes_table_beside_readiness(self, validator: ModuleType, release_run: Run) -> None:
        """A freshly rendered, internally consistent handoff that drops the changes table is rejected."""
        directory, result = release_run
        bind_handoff(directory, result, only_table="Readiness")

        with pytest.raises(SystemExit, match="release-changes-table-missing"):
            validate_run(validator, release_run)

    def test_historical_release_keeps_single_changes_table(self, validator: ModuleType, release_run: Run) -> None:
        """Retained handoffs from before the readiness table existed are not retroactively held to it.

        With the contract version and release head removed, a handoff that carries only the changes table passes.
        """
        directory, result = release_run
        del result["metadata"]["release_contract_version"]
        del result["metadata"]["release_head"]
        bind_handoff(directory, result, only_table="Changes")

        validate_run(validator, release_run)

    def test_complete_notes_are_reportable(self, validator: ModuleType, release_run: Run) -> None:
        """A completed notes run with its draft on disk validates as passing."""
        validate_run(validator, release_run)

    def test_blocked_prepare_is_reportable_without_artifacts(self, validator: ModuleType, release_run: Run) -> None:
        """An honestly failed preparation is accepted without manufacturing the artifacts it could not write.

        The run requests every prepare artifact yet writes none of them, which is allowed because the result reports
        itself blocked.
        """
        directory, result = release_run
        result["status"] = "fail"
        result["checks_failed"] = ["release-scope-unresolved"]
        result["metadata"].update(
            mode="prepare", requested_artifacts=["DRAFT.md", "CHANGELOG.md", "SUMMARY.md", "MIGRATION.md"]
        )
        bind_handoff(directory, result, title="blocked")

        validate_run(validator, release_run)

    @pytest.fixture
    def audit_run(self, release_run: Run) -> Run:
        """Turn the run into a read-only audit that retains no draft and reports release-ready."""
        directory, result = release_run
        (directory / "deliverables/DRAFT.md").unlink()
        result["metadata"].update(mode="audit", requested_artifacts=[])
        bind_handoff(directory, result, title="release-ready")
        return release_run

    def test_audit_needs_no_new_draft(self, validator: ModuleType, audit_run: Run) -> None:
        """A read-only audit validates without any retained draft."""
        validate_run(validator, audit_run)

    def test_historical_report_needs_no_new_draft(self, validator: ModuleType, audit_run: Run) -> None:
        """A retained pre-contract result validates with no draft, schema version, handoff, or release evidence files.

        Results written before the release contract existed carry none of the newer metadata and must stay valid.
        """
        directory, result = audit_run
        del result["metadata"]["release_contract_version"]
        del result["metadata"]["final_handoff"]
        del result["schema_version"]
        (directory / "release-scope.md").unlink()
        (directory / "contributors.md").unlink()
        (directory / "changelog-audit.md").unlink()
        (directory / "draft-review.md").unlink()

        validate_run(validator, audit_run)


@pytest.mark.integration
class TestContributorCredit:
    """Human and bot credits must name the verified identity completely, in visible draft text."""

    @pytest.mark.parametrize(
        ("label", "definition"),
        [
            pytest.param("__Release Test__", "", id="underscores"),
            pytest.param("Release Test", "", id="plain"),
            pytest.param("[Release Test](https://example.org/person)", "", id="link"),
            pytest.param("[Release Test][person]", "\n[person]: https://example.org/person\n", id="reference"),
            pytest.param("[Release Test][]", "\n[Release Test]: https://example.org/person\n", id="collapsed"),
            pytest.param("[Release Test]", "\n[Release Test]: https://example.org/person\n", id="shortcut"),
            pytest.param("Release **Test**", "", id="partial-emphasis"),
            pytest.param("[Release **Test**](https://example.org/person)", "", id="linked-partial-emphasis"),
            pytest.param("**Release Test** (@release-test)", "", id="handle"),
            pytest.param(
                "[Release **Test**](https://example.org/person) ([@release-test](https://example.org/profile))",
                "",
                id="linked-handle",
            ),
        ],
    )
    def test_equivalent_markup_is_accepted(
        self, validator: ModuleType, release_run: Run, label: str, definition: str
    ) -> None:
        """Verified identities are credited whatever emphasis, link, or handle spelling the draft uses.

        The receipt is rebound to each spelling so only the visible name matching decides the verdict.
        """
        credit_with_label(release_run, label, definition)

        validate_run(validator, release_run)

    @pytest.mark.parametrize(
        "definition",
        [
            pytest.param("[person]: https://example.org/person", id="bare-destination"),
            pytest.param('[person]: <https://example.org/person> "Profile"', id="angle-and-title"),
            pytest.param("[person]: https://example.org/person 'Profile'", id="single-quoted-title"),
            pytest.param("[person]: https://example.org/person (Profile)", id="parenthesized-title"),
            pytest.param("[person]: https://example.org/a(b)c", id="balanced-destination"),
            pytest.param(r"[person]: https://example.org/a\(b\)", id="escaped-destination"),
            pytest.param("[person]:\n  https://example.org/person", id="next-line-destination"),
            pytest.param('[person]: https://example.org/person\n  "Profile"', id="next-line-title"),
            pytest.param(
                '[other]: https://example.org/other\n  "Other profile"\n[person]: https://example.org/person',
                id="definition-after-continuation-title",
            ),
            pytest.param('[person]: https://example.org/person "Public\nprofile"', id="multiline-title"),
            pytest.param("[person]: <>", id="empty-angle-destination"),
        ],
    )
    def test_complete_reference_definition_is_accepted(
        self, validator: ModuleType, release_run: Run, definition: str
    ) -> None:
        """A reference credit resolves against every complete Markdown definition form.

        Titles, angle destinations, balanced parentheses, and continuation lines all still define the reference.
        """
        credit_with_label(release_run, "[Release Test][person]", f"\n{definition}\n")

        validate_run(validator, release_run)

    @pytest.mark.parametrize(
        "definition",
        [
            pytest.param("[person]: https://example.org/person unexpected words", id="unquoted-trailing-words"),
            pytest.param('[person]: https://example.org/person "Profile', id="unclosed-title"),
            pytest.param('[person]: https://example.org/person "Profile" extra', id="trailing-title-junk"),
            pytest.param("[person]: <https://example.org/person", id="unclosed-angle-destination"),
            pytest.param("[person]: <https://example.org/<person>>", id="nested-angle-destination"),
            pytest.param("[person]: https://example.org/a(b", id="unclosed-destination-parenthesis"),
            pytest.param('[person]: https://example.org/person "Public\n\nprofile"', id="blank-title-line"),
            pytest.param("A preceding paragraph.\n[person]: https://example.org/person", id="paragraph-continuation"),
        ],
    )
    def test_malformed_reference_definition_is_rejected(
        self, validator: ModuleType, release_run: Run, definition: str
    ) -> None:
        """A reference credit never resolves against a prefix of malformed or ordinary paragraph text.

        Only an actual definition makes the label a link, so trailing junk, unclosed quoting, and a definition that
        continues a paragraph all leave the credit unresolved.
        """
        credit_with_label(release_run, "[Release Test][person]", f"\n{definition}\n")

        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        "label",
        [
            pytest.param("[**Release *Test***](https://example.org/person)", id="strong-around-emphasis"),
            pytest.param("[*Release **Test***](https://example.org/person)", id="emphasis-around-strong"),
            pytest.param("[__Release _Test___](https://example.org/person)", id="nested-underscores"),
            pytest.param("[***Release Test***](https://example.org/person)", id="shared-triple-runs"),
            pytest.param("**Release *Test***", id="nested-unlinked"),
            pytest.param("__Release *Test*__", id="mixed-nested-markers"),
            pytest.param("**Release\u00a0Test**", id="unicode-visible-name-space"),
            pytest.param("**Release\u2028Test**", id="unicode-visible-name-separator"),
            pytest.param("**Release [*Test*](https://example.org/person)**", id="link-within-emphasis"),
            pytest.param('[Release Test](https://example.org/person "Profile")', id="inline-title"),
            pytest.param("[Release Test](https://example.org/a(b)c)", id="inline-balanced-destination"),
        ],
    )
    def test_complete_balanced_label_is_accepted(self, validator: ModuleType, release_run: Run, label: str) -> None:
        """Nested complete names are read as the visible identity whatever their emphasis nesting.

        Balanced emphasis, links inside emphasis, and Unicode spaces inside the name all still spell the contributor.
        """
        credit_with_label(release_run, label)

        validate_run(validator, release_run)

    @pytest.mark.parametrize(
        "label",
        [
            pytest.param("[**Release *Test*** Extra](https://example.org/person)", id="longer-nested-label"),
            pytest.param("[**Release *Tester***](https://example.org/person)", id="wrong-nested-label"),
            pytest.param("[**Release *Test**](https://example.org/person)", id="unbalanced-nested-label"),
            pytest.param(r"[**Release \*Test***](https://example.org/person)", id="escaped-literal-marker"),
            pytest.param("[Release Test](https://example.org/person unexpected words)", id="inline-trailing-words"),
            pytest.param('[Release Test](https://example.org/person "Profile" extra)', id="inline-title-junk"),
        ],
    )
    def test_incomplete_or_wrong_label_is_rejected(self, validator: ModuleType, release_run: Run, label: str) -> None:
        """Literal markers, longer names, wrong names, and invalid links never count as the contributor's credit.

        Normalizing the label must not discard an escaped marker or accept text beyond the identity.
        """
        credit_with_label(release_run, label)

        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(("kind", "boundary", "separator"), link_whitespace_cases(accepted=True))
    def test_link_whitespace_accepted_where_syntax_allows(
        self, validator: ModuleType, release_run: Run, kind: str, boundary: str, separator: str
    ) -> None:
        """Syntax spaces, and Unicode whitespace inside destinations or quoted titles, leave the link valid.

        Every separator is placed at every boundary of an inline and a reference link; only the combinations the
        Markdown grammar allows are listed here.
        """
        content = _LINK_BOUNDARIES[boundary].format(sep=separator, uri=_PERSON_URI)

        credit_with_label(release_run, *link_markup(kind, content))

        validate_run(validator, release_run)

    @pytest.mark.parametrize(("kind", "boundary", "separator"), link_whitespace_cases(accepted=False))
    def test_link_whitespace_rejected_where_syntax_forbids(
        self, validator: ModuleType, release_run: Run, kind: str, boundary: str, separator: str
    ) -> None:
        """Control characters and Unicode spaces that are not Markdown syntax invalidate the link.

        The counterpart of the accepted cases: the same boundaries with separators the grammar does not allow there.
        """
        content = _LINK_BOUNDARIES[boundary].format(sep=separator, uri=_PERSON_URI)

        credit_with_label(release_run, *link_markup(kind, content))

        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize("context", _BLANK_LINE_CONTEXTS)
    @pytest.mark.parametrize("separator", [pytest.param(" ", id="ascii-space"), pytest.param("\t", id="ascii-tab")])
    def test_blank_line_boundary_accepts_space_and_tab(
        self, validator: ModuleType, release_run: Run, separator: str, context: str
    ) -> None:
        """A line holding only spaces or tabs is blank, so the definition after it starts a new paragraph."""
        ending = context.format(sep=separator)

        credit_with_label(release_run, "[Release Test][person]", f"\n{ending}\n[person]: {_PERSON_URI}\n")

        validate_run(validator, release_run)

    @pytest.mark.parametrize("context", _BLANK_LINE_CONTEXTS)
    @pytest.mark.parametrize(
        "separator",
        [
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
    def test_blank_line_boundary_rejects_unicode_whitespace(
        self, validator: ModuleType, release_run: Run, separator: str, context: str
    ) -> None:
        """Unicode whitespace or control text cannot invent a reference-definition paragraph boundary.

        The definition stays a continuation of the preceding text, so the reference credit does not resolve.
        """
        ending = context.format(sep=separator)

        credit_with_label(release_run, "[Release Test][person]", f"\n{ending}\n[person]: {_PERSON_URI}\n")

        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize("wrapper", [pytest.param("{}", id="bare"), pytest.param("<{}>", id="angle")])
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
    def test_valid_unicode_destination_characters_stay_data(
        self, validator: ModuleType, release_run: Run, wrapper: str, character: str
    ) -> None:
        """Unicode URL characters remain destination data rather than syntax whitespace or line endings."""
        destination = wrapper.format(f"https://example.org/a{character}b")

        credit_with_label(release_run, f"[Release Test]({destination})")

        validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("outer", "outer_definition"),
        [
            pytest.param("[Release {}](https://outer.example)", "", id="inline-outer"),
            pytest.param("[Release {}][outer]", "\n[outer]: https://outer.example\n", id="reference-outer"),
        ],
    )
    @pytest.mark.parametrize(
        ("nested", "nested_definition"),
        [
            pytest.param("[Test](https://inner.example)", "", id="inline-inner"),
            pytest.param("[Test][inner]", "\n[inner]: https://inner.example\n", id="reference-inner"),
            pytest.param("[Test][]", "\n[Test]: https://inner.example\n", id="collapsed-inner"),
            pytest.param("[Test]", "\n[Test]: https://inner.example\n", id="shortcut-inner"),
        ],
    )
    def test_nested_credit_links_are_rejected(
        self,
        validator: ModuleType,
        release_run: Run,
        outer: str,
        outer_definition: str,
        nested: str,
        nested_definition: str,
    ) -> None:
        """A deactivated outer link's literal syntax stays part of the complete visible identity.

        Markdown cannot nest links, so the inner link wins and the outer brackets become name text that no longer
        matches the contributor.
        """
        credit_with_label(release_run, outer.format(nested), nested_definition + outer_definition)

        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("name", "label"),
        [
            pytest.param("Release [Test]", "Release [Test]", id="plain-literal-brackets"),
            pytest.param(
                "Release [Test]", "[Release [Test]](https://example.org/person)", id="linked-literal-brackets"
            ),
            pytest.param(
                "Release [Test]", r"[Release \[Test\]](https://example.org/person)", id="linked-escaped-brackets"
            ),
            pytest.param(
                "[Release Test](https://outer.example)",
                "[Release [Test](https://inner.example)](https://outer.example)",
                id="deactivated-outer-syntax-is-name-text",
            ),
        ],
    )
    def test_literal_brackets_in_retained_author_name_are_preserved(
        self, validator: ModuleType, release_run: Run, name: str, label: str
    ) -> None:
        """A retained PR author's exact visible name keeps its literal brackets, linked or unlinked."""
        receipt = receipt_of(release_run)
        add_pr_author(release_run, name)
        receipt["contributors"].append(
            {"identity_hash": "f" * 64, "display_name": name, "disposition": "credited", "credit_excerpt": label}
        )

        rebind_draft(release_run, read_draft(release_run) + f"\n- {label} — reviewed contribution.\n")

        validate_run(validator, release_run)

    def test_display_name_must_match_credited_identity(self, validator: ModuleType, release_run: Run) -> None:
        """Hash accounting alone cannot assign one person's credit to someone else."""
        receipt_of(release_run)["contributors"][0].update(display_name="Codex", credit_excerpt="**Codex**")

        with pytest.raises(SystemExit, match="release-evidence-contributor-display-name"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("replacement", "excerpt", "appendix"),
        [
            pytest.param("__Release Tester__", "__Release Tester__", "", id="partial-name"),
            pytest.param("__Release Tester__", "Release Test", "", id="plain-substring"),
            pytest.param(
                "[Release Tester](https://example.org/person)",
                "[Release Tester](https://example.org/person)",
                "",
                id="wrong-linked-name",
            ),
            pytest.param(
                "[**Release Test** Extra](https://example.org/person)",
                "[**Release Test** Extra](https://example.org/person)",
                "",
                id="longer-emphasized-link",
            ),
            pytest.param(
                "[**Release Test** Extra][person]",
                "[**Release Test** Extra][person]",
                "\n[person]: https://example.org/person\n",
                id="longer-emphasized-reference",
            ),
        ],
    )
    def test_credit_must_name_the_whole_identity(
        self, validator: ModuleType, release_run: Run, replacement: str, excerpt: str, appendix: str
    ) -> None:
        """A credit for a longer, shorter, or different visible name is not credit for the contributor.

        Hash accounting alone cannot let a near-miss name stand in for the verified human.
        """
        credit_with_label(release_run, replacement, appendix, excerpt=excerpt)

        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)

    def test_credit_excerpt_must_appear_in_draft(self, validator: ModuleType, release_run: Run) -> None:
        """A claimed credit excerpt that the draft does not actually contain is rejected."""
        receipt_of(release_run)["contributors"][0]["credit_excerpt"] = "**Release Test** — credit not actually present"

        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("replacement", "appendix", "excerpt"),
        [
            pytest.param("[Release Test][person]", "", "[Release Test][person]", id="missing-reference"),
            pytest.param(
                "A different contributor",
                "\n[Release Test]: https://example.org/person\n",
                "[Release Test]",
                id="definition-only-credit",
            ),
            pytest.param(
                "A different contributor",
                "\n[Release Test]: \u00a0https://example.org/person\n",
                "[Release Test]",
                id="definition-only-unicode-destination",
            ),
            pytest.param(
                "A different contributor",
                "\n```markdown\n- **Release Test** — example only.\n```\n",
                "**Release Test**",
                id="fenced-credit-only",
            ),
        ],
    )
    def test_credit_needs_visible_evidence_in_draft(
        self, validator: ModuleType, release_run: Run, replacement: str, appendix: str, excerpt: str
    ) -> None:
        """A definition, an unresolved reference, or a fenced example is not a visible credit.

        The contributors list names someone else, so the verified human is credited nowhere a reader would see it.
        """
        receipt_of(release_run)["contributors"][0]["credit_excerpt"] = excerpt
        text = (read_draft(release_run) + appendix).replace(
            "- **Release Test** — parsing fix.", f"- {replacement} — parsing fix."
        )

        rebind_draft(release_run, text)

        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)

    def test_reference_definition_in_another_document_is_not_credit(
        self, validator: ModuleType, release_run: Run
    ) -> None:
        """A reference label whose definition lives in a different deliverable does not resolve in the draft."""
        label = "[Release Test][person]"
        receipt_of(release_run)["contributors"][0]["credit_excerpt"] = label
        select_artifact(
            release_run,
            "SUMMARY.md",
            "## Overview\nParsing improved.\n## Benefits\nInput supported.\n\n[person]: https://example.org/person\n",
        )

        rebind_draft(release_run, read_draft(release_run).replace("**Release Test**", label))

        with pytest.raises(SystemExit, match="release-evidence-contributor-credit"):
            validate_run(validator, release_run)

    def test_exclusion_needs_proof(self, validator: ModuleType, release_run: Run) -> None:
        """Excluding a credit requires evidence of the request in the contributors record, not just a claim."""
        receipt_of(release_run)["contributors"][0].update(
            disposition="excluded", exclusion_basis="privacy-request", exclusion="Remove this credit"
        )
        (release_run[0] / "contributors.md").write_text(
            "Coverage Credits Exclusions Unresolved\nRemove this credit", encoding="utf-8"
        )

        with pytest.raises(SystemExit, match="release-evidence-contributor-exclusion-proof"):
            validate_run(validator, release_run)

    def test_known_pr_author_requires_credit(self, validator: ModuleType, release_run: Run) -> None:
        """A retained PR author joins the same complete credit accounting as Git identities."""
        add_pr_author(release_run, "Pat")

        with pytest.raises(SystemExit, match="release-evidence-contributor-coverage"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("second_author", "reason"),
        [
            pytest.param("Second Human", "contributor-coverage", id="human"),
            pytest.param("automation[bot]", "bot-coverage", id="bot"),
        ],
    )
    def test_uncredited_second_coauthor_is_rejected(
        self, validator: ModuleType, release_run: Run, second_author: str, reason: str
    ) -> None:
        """All human and bot coauthor trailers need credit accounting, not silent filtering.

        A new release commit names two coauthors but the receipt credits only the committer, so the second coauthor must
        be reported as uncovered, as a bot or as a human.
        """
        directory, result = release_run
        repository = directory / "source"
        # The fixture pins its release head to the copied repository's current HEAD.
        baseline = result["metadata"]["release_head"]
        coauthors = f"Co-authored-by: First Human <first@example.invalid>\nCo-authored-by: {second_author} <second@example.invalid>"
        git(repository, "commit", "--allow-empty", "-m", f"release change\n\n{coauthors}")
        head, final_tree = git(repository, "rev-parse", "HEAD", "HEAD^{tree}").split()
        result["metadata"]["release_head"] = head
        receipt = receipt_of(release_run)
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
                }
            ],
        )
        receipt["claims"][0]["candidate_shas"] = [head]
        append_scope(directory, baseline, head, final_tree)
        pin_gate_sources(directory, head)
        bind_handoff(directory, result)

        with pytest.raises(SystemExit, match=f"release-evidence-{reason}"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize("bot_name", _BOTS)
    @pytest.mark.parametrize(
        "appendix",
        [
            pytest.param(f"\n{_BOT_AGGREGATE}\n", id="valid"),
            pytest.param("\n_Automated contributions: @{bot}_\n", id="underscore-line"),
            pytest.param("\nAutomated contributions: @{bot}\n", id="plain-line"),
        ],
    )
    def test_bot_pr_authors_are_credited_by_one_aggregate_line(
        self, validator: ModuleType, release_run: Run, bot_name: str, appendix: str
    ) -> None:
        """Known PR bots stay out of human credits and appear once in the visible aggregate line.

        Emphasis style or its absence on the aggregate line does not change the verdict.
        """
        arrange_bot(release_run, bot_name)
        append_bot_text(release_run, appendix, bot_name)

        validate_run(validator, release_run)

    @pytest.mark.parametrize("bot_name", _BOTS)
    @pytest.mark.parametrize("appendix", _BOT_REJECTED_APPENDIX)
    def test_bot_credit_faults_are_rejected(
        self, validator: ModuleType, release_run: Run, bot_name: str, appendix: str
    ) -> None:
        """A missing or duplicated aggregate line, or any individual credit for a bot, is rejected.

        Bots get one aggregate line and nothing else: bold, underscored, linked, referenced, partially emphasized,
        nested, or Unicode-destination individual credits all count as crediting the bot like a human.
        """
        arrange_bot(release_run, bot_name)
        append_bot_text(release_run, appendix, bot_name)

        with pytest.raises(SystemExit, match="release-evidence-bot-"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize("bot_name", _BOTS)
    def test_bot_without_inventory_receipt_is_rejected(
        self, validator: ModuleType, release_run: Run, bot_name: str
    ) -> None:
        """A bot PR author whose classification inventory is missing from the receipt is rejected."""
        arrange_bot(release_run, bot_name)
        receipt_of(release_run).pop("bots")
        append_bot_text(release_run, f"\n{_BOT_AGGREGATE}\n", bot_name)

        with pytest.raises(SystemExit, match="release-evidence-bot-"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize("bot_name", _BOTS)
    def test_bot_classification_proof_must_match_its_digest(
        self, validator: ModuleType, release_run: Run, bot_name: str
    ) -> None:
        """Bot classification evidence changed after hashing no longer proves the account is a bot."""
        proof = arrange_bot(release_run, bot_name)
        proof.write_text("changed evidence", encoding="utf-8")
        append_bot_text(release_run, f"\n{_BOT_AGGREGATE}\n", bot_name)

        with pytest.raises(SystemExit, match="release-evidence-bot-"):
            validate_run(validator, release_run)


@pytest.mark.integration
class TestReleaseSource:
    """Pinned Git coordinates, gate receipts, and candidate accounting bind the release to real source."""

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda check: check.pop("source"), id="missing"),
            pytest.param(lambda check: check["source"].update(expected_head="b" * 40), id="wrong-expected"),
            pytest.param(lambda check: check["source"]["before"].update(head="b" * 40), id="wrong-before"),
            pytest.param(lambda check: check["source"]["after"].update(head="b" * 40), id="wrong-after"),
            pytest.param(lambda check: check["source"]["before"].update(status=" M source.py\n"), id="dirty-before"),
            pytest.param(lambda check: check["source"]["after"].update(status=" M source.py\n"), id="dirty-after"),
            pytest.param(lambda check: check["source"]["before"].update(error="Git inspection failed"), id="git-error"),
            pytest.param(lambda check: check["source"].update(after=None), id="missing-after"),
        ],
    )
    def test_unbound_gate_source_is_rejected(
        self, validator: ModuleType, release_run: Run, mutate: Callable[[dict[str, Any]], object]
    ) -> None:
        """A passing command from another revision or dirty checkout cannot certify pinned release source."""
        path = release_run[0] / "gates.json"
        gates = json.loads(path.read_text(encoding="utf-8"))
        mutate(next(check for check in gates["checks"] if check["id"] == "tests"))
        path.write_text(json.dumps(gates), encoding="utf-8")

        with pytest.raises(SystemExit, match="release-gate-source"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize("head", [None, "", "main", "abc", "a" * 41])
    def test_passing_release_requires_pinned_full_head(
        self, validator: ModuleType, release_run: Run, head: object
    ) -> None:
        """Require an immutable full Git coordinate before trusting source-bound gate receipts."""
        release_run[1]["metadata"]["release_head"] = head

        with pytest.raises(SystemExit, match="release-head-invalid"):
            validate_run(validator, release_run)

    def test_blocked_release_can_report_missing_source_evidence(self, validator: ModuleType, release_run: Run) -> None:
        """An honest blocked result is retained when release source cannot be selected or verified."""
        directory, result = release_run
        result["status"] = "fail"
        result["checks_failed"] = ["release-source-unavailable"]
        del result["metadata"]["release_head"]
        path = directory / "gates.json"
        gates = json.loads(path.read_text(encoding="utf-8"))
        gates["checks"] = [{key: value for key, value in check.items() if key != "source"} for check in gates["checks"]]
        path.write_text(json.dumps(gates), encoding="utf-8")
        bind_handoff(directory, result, title="blocked")

        validate_run(validator, release_run)

    def test_passing_release_requires_machine_receipt(self, validator: ModuleType, release_run: Run) -> None:
        """Versioned release prose cannot replace source-bound receipt evidence."""
        del release_run[1]["metadata"]["release_evidence"]

        with pytest.raises(SystemExit, match="release-evidence-receipt"):
            validate_run(validator, release_run)

    def test_unaccounted_candidate_is_rejected(self, validator: ModuleType, release_run: Run) -> None:
        """Every pinned Git candidate needs an explicit disposition and reason."""
        receipt_of(release_run)["candidates"] = []

        with pytest.raises(SystemExit, match="release-evidence-candidates"):
            validate_run(validator, release_run)

    def test_stale_final_tree_is_rejected(self, validator: ModuleType, release_run: Run) -> None:
        """Candidate accounting cannot reuse a receipt from a different source tree."""
        receipt_of(release_run)["final_tree"] = "0" * 40

        with pytest.raises(SystemExit, match="release-evidence-final-tree"):
            validate_run(validator, release_run)

    def test_heading_only_scope_record_is_rejected(self, validator: ModuleType, release_run: Run) -> None:
        """Scope headings cannot replace pinned source coordinates and candidate accounting."""
        (release_run[0] / "release-scope.md").write_text(
            "Head and baseline\nReleased comparison\nCandidate accounting\nLimits\n", encoding="utf-8"
        )

        with pytest.raises(SystemExit, match="release-evidence-scope-record"):
            validate_run(validator, release_run)

    def test_real_gate_runner_receipts_validate_end_to_end(self, validator: ModuleType, release_run: Run) -> None:
        """Actual runner evidence from a clean pinned source is accepted by the release consumer.

        The gate runner executes for real against the private repository copy after a second commit gives the release a
        baseline, and the validator then reads the receipts it wrote.
        """
        directory, result = release_run
        repository = directory / "source"
        git(repository, "commit", "--allow-empty", "-m", "fixture\n\nCo-authored-by: Codex <codex@openai.com>")
        head, final_tree, baseline = git(repository, "rev-parse", "HEAD", "HEAD^{tree}", "HEAD^").split()
        gates = [argument for gate in _GATES for argument in (f"--{gate}", "exit 0")]
        completed = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "shared/run_gates.py"),
                "--out",
                str(directory),
                "--expected-head",
                head,
                *gates,
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        recorded = json.loads((directory / "gates.json").read_text(encoding="utf-8"))
        assert all(
            check["source"]["after"] == {"head": head, "status": "", "ignored": "[]"} for check in recorded["checks"]
        )
        result["metadata"]["release_head"] = head
        receipt = receipt_of(release_run)
        receipt.update(
            repository=str(repository),
            baseline=baseline,
            final_tree=final_tree,
            candidates=[{"sha": head, "disposition": "included", "reason": "release change"}],
        )
        append_scope(directory, baseline, head, final_tree)
        receipt["claims"][0]["candidate_shas"] = [head]
        bind_handoff(directory, result)

        validate_run(validator, release_run)

    @pytest.mark.parametrize("filename", ["feature.txt", "space ü [x].txt"])
    @pytest.mark.parametrize("autocrlf", ["false", "input", "true"])
    def test_patch_subtraction_accepts_behavior_still_published(
        self,
        validator: ModuleType,
        release_run: Run,
        monkeypatch: pytest.MonkeyPatch,
        filename: str,
        autocrlf: str,
    ) -> None:
        """A candidate whose patch matches behavior the published release tree still holds is already released.

        Real Git compares the patches byte for byte, with a legacy Windows decoder as the default text encoding, under
        every line-ending conversion setting and a file name that needs quoting.
        """
        arrange_backported_feature(release_run, filename, autocrlf, keep_published)
        monkeypatch.setattr(subprocess, "run", run_with_cp1252_default)

        validate_run(validator, release_run)

    @pytest.mark.parametrize(
        "publish",
        [pytest.param(revert_published, id="reverted"), pytest.param(change_published_mode, id="different-mode")],
    )
    @pytest.mark.parametrize("filename", ["feature.txt", "space ü [x].txt"])
    @pytest.mark.parametrize("autocrlf", ["false", "input", "true"])
    def test_patch_subtraction_rejects_introduced_then_reverted_behavior(
        self,
        validator: ModuleType,
        release_run: Run,
        monkeypatch: pytest.MonkeyPatch,
        filename: str,
        autocrlf: str,
        publish: Callable[[Path, str, str], None],
    ) -> None:
        """A matching historical patch cannot subtract behavior absent from the published release tree.

        The published line either reverted the feature or kept the bytes under a different file mode, so the candidate's
        behavior is not actually released even though an identical patch once was.
        """
        arrange_backported_feature(release_run, filename, autocrlf, publish)
        monkeypatch.setattr(subprocess, "run", run_with_cp1252_default)

        with pytest.raises(SystemExit, match="release-evidence-released-tree-path"):
            validate_run(validator, release_run)


@pytest.mark.integration
class TestDeliverables:
    """Requested outputs must exist, carry their required sections, and match their receipt bindings."""

    @pytest.mark.parametrize("missing", ["DRAFT.md", "SUMMARY.md", "MIGRATION.md", "CHANGELOG.md"])
    def test_selected_output_cannot_be_missing(self, validator: ModuleType, release_run: Run, missing: str) -> None:
        """Passing readiness artifacts that omit a requested communication output are rejected.

        When the draft itself is the requested output its file is removed; every other output was never written.
        """
        directory, result = release_run
        result["metadata"]["requested_artifacts"] = list(dict.fromkeys(["DRAFT.md", missing]))
        (directory / "deliverables" / missing).unlink(missing_ok=True)

        with pytest.raises(SystemExit, match="release-missing-deliverable"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        "missing", ["release-scope.md", "contributors.md", "changelog-audit.md", "draft-review.md"]
    )
    def test_requires_coverage_and_preservation_evidence(
        self, validator: ModuleType, release_run: Run, missing: str
    ) -> None:
        """Passing checks cannot replace release-set, credit, or preservation evidence files."""
        (release_run[0] / missing).unlink()

        with pytest.raises(SystemExit, match="missing-artifact"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize("mode", ["notes", "prepare", "demo"])
    def test_mode_minimum_cannot_be_omitted_from_selected_outputs(
        self, validator: ModuleType, release_run: Run, mode: str
    ) -> None:
        """Deleting the requested-artifact list cannot make an incomplete mode pass."""
        release_run[1]["metadata"].update(mode=mode, requested_artifacts=[])

        with pytest.raises(SystemExit, match="release-required-deliverables"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize("bad", [None, True, 0, 2, "1"])
    def test_unknown_release_contract_is_rejected(self, validator: ModuleType, release_run: Run, bad: object) -> None:
        """Malformed version markers cannot silently enter the legacy compatibility path."""
        release_run[1]["metadata"]["release_contract_version"] = bad

        with pytest.raises(SystemExit, match="release-contract-version"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize("section", ["Summary", "Highlights", "Migration guide", "Notable changes", "Contributors"])
    def test_draft_requires_nonempty_sections(self, validator: ModuleType, release_run: Run, section: str) -> None:
        """A section name elsewhere in prose cannot substitute for actual draft content."""
        draft = release_run[0] / "deliverables/DRAFT.md"
        draft.write_text(
            draft.read_text(encoding="utf-8").replace(f"## {section}", f"Mention of {section}"), encoding="utf-8"
        )

        with pytest.raises(SystemExit, match="release-draft-section"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        "residue", ["[Release hook]", "[User-facing win]", "> Summary guidance:", "[Full changelog URL]"]
    )
    def test_draft_rejects_template_residue(self, validator: ModuleType, release_run: Run, residue: str) -> None:
        """A draft that still carries template placeholders or writing notes is not a finished release draft."""
        draft = release_run[0] / "deliverables/DRAFT.md"
        draft.write_text(draft.read_text(encoding="utf-8") + f"\n{residue}\n", encoding="utf-8")

        with pytest.raises(SystemExit, match="release-draft-template-residue"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        "heading",
        [
            pytest.param("  ##\tSummary ##\n", id="atx-spacing-and-closing"),
            pytest.param("Summary\n-------\n", id="setext"),
        ],
    )
    def test_equivalent_draft_heading_is_accepted(self, validator: ModuleType, release_run: Run, heading: str) -> None:
        """The required Summary section is found whatever ATX spacing or setext spelling its heading uses."""
        rebind_draft(release_run, read_draft(release_run).replace("## Summary\n", heading))

        validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("name", "text", "newline"),
        [
            pytest.param(
                "SUMMARY.md",
                "## Overview ##\nParsing accepts empty input.\n## Benefits ###\nNo recovery needed.\n",
                None,
                id="summary-closing-hashes",
            ),
            pytest.param(
                "SUMMARY.md",
                "Overview\n--------\nParsing accepts empty input.\n\nBenefits\n--------\nNo recovery needed.\n",
                None,
                id="summary-setext",
            ),
            pytest.param(
                "MIGRATION.md",
                "No migration required\n---------------------\nExisting input keeps its behavior.\n",
                "\n",
                id="migration-setext",
            ),
        ],
    )
    def test_equivalent_secondary_heading_is_accepted(
        self, validator: ModuleType, release_run: Run, name: str, text: str, newline: str | None
    ) -> None:
        """Required secondary sections are found whatever heading spelling the selected document uses.

        The summary files are written with the host newline default so line-ending handling stays covered too.
        """
        select_artifact(release_run, name, text, newline=newline)
        bind_handoff(*release_run)

        validate_run(validator, release_run)

    def test_fenced_draft_cannot_supply_required_sections(self, validator: ModuleType, release_run: Run) -> None:
        """An example's headings, shown inside a fence, cannot supply the draft's required sections."""
        rebind_draft(release_run, "```markdown\n" + read_draft(release_run) + "\n```\n")

        with pytest.raises(SystemExit, match="release-draft-section"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("name", "body", "reason"),
        [
            pytest.param(
                "SUMMARY.md",
                "Overview\n--------\nParsing fix.\n\nBenefits\n--------\nEmpty input supported.\n",
                "release-evidence-summary-role",
                id="summary",
            ),
            pytest.param(
                "MIGRATION.md",
                "No migration required\n---------------------\nInputs unchanged.\n",
                "release-evidence-migration-role",
                id="migration",
            ),
        ],
    )
    def test_fenced_secondary_cannot_supply_required_sections(
        self, validator: ModuleType, release_run: Run, name: str, body: str, reason: str
    ) -> None:
        """An example's headings cannot supply the selected secondary document's required sections."""
        select_artifact(release_run, name, "```markdown\n" + body + "```\n")

        with pytest.raises(SystemExit, match=reason):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("excerpt", "", id="empty-excerpt"),
            pytest.param("candidate_shas", [], id="empty-candidate-group"),
            pytest.param("artifact", "../contributors.md", id="outside-deliverables"),
        ],
    )
    def test_claim_bindings_cannot_be_empty_or_escape(
        self, validator: ModuleType, release_run: Run, field: str, value: object
    ) -> None:
        """A valid receipt cannot replace a real claim anchor with an empty or escaped binding."""
        receipt_of(release_run)["claims"][0][field] = value

        with pytest.raises(SystemExit, match="release-evidence-claim-row"):
            validate_run(validator, release_run)

    def test_summary_placeholder_is_rejected(self, validator: ModuleType, release_run: Run) -> None:
        """A role keyword alone cannot make a selected summary substantive."""
        select_artifact(release_run, "SUMMARY.md", "# Summary\n\nRelease\n")

        with pytest.raises(SystemExit, match="release-evidence-summary-role"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("name", "text", "reason"),
        [
            pytest.param("SUMMARY.md", "release overview benefits placeholder", "summary-role", id="summary-keywords"),
            pytest.param("MIGRATION.md", "upgrade change placeholder", "migration-role", id="migration-keywords"),
            pytest.param("CHANGELOG.md", "## v1.2.1\n- placeholder\n", "changelog-excerpt", id="incomplete-changelog"),
        ],
    )
    def test_secondary_artifact_placeholders_are_rejected(
        self, validator: ModuleType, release_run: Run, name: str, text: str, reason: str
    ) -> None:
        """Nonempty secondary files with matching digests still need their actual content contract."""
        select_artifact(release_run, name, text)

        with pytest.raises(SystemExit, match=f"release-evidence-{reason}"):
            validate_run(validator, release_run)

    def test_complete_prepare_validates_actual_secondary_outputs(self, validator: ModuleType, release_run: Run) -> None:
        """The complete selected release set is accepted, including the exact canonical target excerpt."""
        directory, result = release_run
        select_artifact(
            release_run,
            "SUMMARY.md",
            "# Summary\n## Overview\nEmpty input is accepted.\n## Benefits\nCallers need no empty-input guard.\n",
        )
        select_artifact(
            release_run,
            "MIGRATION.md",
            "# Migration\n## No migration required\nExisting valid input keeps its behavior.\n",
        )
        select_artifact(release_run, "CHANGELOG.md", "## v1.2.1\n- Parsing fix.\n")
        result["metadata"]["mode"] = "prepare"
        bind_handoff(directory, result, title="release-ready")

        validate_run(validator, release_run)

    def test_actual_artifact_destination_is_rechecked(self, validator: ModuleType, release_run: Run) -> None:
        """A retained valid summary cannot conceal different bytes at the user-facing destination."""
        select_artifact(release_run, "SUMMARY.md", "## Overview\nParsing fix.\n## Benefits\nEmpty input supported.\n")
        destination = release_run[0] / "user-summary.md"
        destination.write_text("stale user-facing summary", encoding="utf-8")
        receipt_of(release_run)["artifacts"][-1]["destination"] = str(destination)

        with pytest.raises(SystemExit, match="release-evidence-artifact-binding"):
            validate_run(validator, release_run)


@pytest.mark.integration
class TestChangelog:
    """The changelog receipt must preserve history and the target entry byte for byte."""

    @pytest.mark.parametrize(
        ("side", "prune", "reason"),
        [
            pytest.param(
                "destination",
                lambda data: data.replace(b"Existing detail.", b"Short."),
                "changelog-history",
                id="history-detail",
            ),
            pytest.param(
                "backup",
                lambda data: data + b"\n[old]: https://example.org/history\n",
                "changelog-history",
                id="history-reference",
            ),
            pytest.param(
                "backup",
                lambda data: data + b"\n## v1.2.1\n- Detailed API condition.\n",
                "changelog-target-detail",
                id="target-detail",
            ),
        ],
    )
    def test_content_cannot_be_pruned(
        self, validator: ModuleType, release_run: Run, side: str, prune: Callable[[bytes], bytes], reason: str
    ) -> None:
        """Rehashed final bytes cannot conceal removed historical or preexisting target information."""
        record = receipt_of(release_run)["changelog"]
        path = Path(record[side])
        path.write_bytes(prune(path.read_bytes()))
        rehash_changelog(record)

        with pytest.raises(SystemExit, match=f"release-evidence-{reason}"):
            validate_run(validator, release_run)

    def test_target_additions_preserve_existing_detail(self, validator: ModuleType, release_run: Run) -> None:
        """Ordinary additions to the target section are allowed while older exact bytes and entries stay."""
        record = receipt_of(release_run)["changelog"]
        backup, destination = Path(record["backup"]), Path(record["destination"])
        backup.write_bytes(destination.read_bytes())
        destination.write_bytes(destination.read_bytes() + b"- Additional condition documented.\n")
        rehash_changelog(record)

        validate_run(validator, release_run)

    @pytest.mark.parametrize("selector_style", ["atx", "setext"])
    def test_setext_changelog_retains_exact_unicode_crlf_history_and_excerpt(
        self, validator: ModuleType, release_run: Run, selector_style: str
    ) -> None:
        """Semantic version lookup preserves the original historical and selected excerpt bytes.

        The changelog uses CRLF, setext headings, and a non-ASCII history entry; both heading styles select the same
        target entry and the validated files keep their exact bytes.
        """
        arranged = arrange_setext_changelog(release_run, selector_style)

        validate_run(validator, release_run)

        assert arranged.destination.read_bytes().startswith(arranged.historical)
        assert arranged.excerpt.read_bytes() == arranged.expected

    @pytest.mark.parametrize("selector_style", ["atx", "setext"])
    def test_setext_changelog_rejects_pruned_unicode_history(
        self, validator: ModuleType, release_run: Run, selector_style: str
    ) -> None:
        """Shortening the non-ASCII history entry of a CRLF changelog is rejected after rehashing."""
        arranged = arrange_setext_changelog(release_run, selector_style)
        arranged.destination.write_bytes(arranged.destination.read_bytes().replace("Café".encode(), b"Short"))
        arranged.record["final_sha256"] = hashlib.sha256(arranged.destination.read_bytes()).hexdigest()

        with pytest.raises(SystemExit, match="release-evidence-changelog-history"):
            validate_run(validator, release_run)

    def test_explicit_absence_of_canonical_changelog_is_recorded(self, validator: ModuleType, release_run: Run) -> None:
        """Local-only notes can disclose a project without a canonical changelog instead of inventing one."""
        receipt_of(release_run)["changelog"] = {
            "destination": None,
            "backup": None,
            "absent_reason": "Project has no canonical changelog.",
        }

        validate_run(validator, release_run)


@pytest.mark.integration
class TestDemoExecution:
    """Demo deliverables are bound to a real recorded execution receipt."""

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("print('demo successful')\n", id="success"),
            pytest.param("answer = 42\n", id="silent-success"),
        ],
    )
    def test_recorded_successful_demo_is_accepted(self, validator: ModuleType, release_run: Run, source: str) -> None:
        """A demo that really ran and exited cleanly validates, with or without output.

        The public recorder executes the script for real and the validator then verifies the receipt it wrote.
        """
        demo = prepare_demo(release_run, source)
        record_demo(release_run, demo, timeout_seconds=60, returncode=0)

        validate_run(validator, release_run)

    @pytest.mark.parametrize(
        ("source", "timeout_seconds"),
        [
            pytest.param("raise RuntimeError('broken demo')\n", 60, id="failure"),
            pytest.param("import time\ntime.sleep(10)\n", 1, id="timeout"),
        ],
    )
    def test_failed_or_timed_out_demo_is_rejected(
        self, validator: ModuleType, release_run: Run, source: str, timeout_seconds: int
    ) -> None:
        """A demo whose recorded execution crashed or ran out of time cannot back the deliverable.

        Only the timeout case needs the short budget; a loaded runner can take over a second to start Python.
        """
        demo = prepare_demo(release_run, source)
        record_demo(release_run, demo, timeout_seconds=timeout_seconds, returncode=1)

        with pytest.raises(SystemExit, match="release-evidence-demo-execution"):
            validate_run(validator, release_run)

    def test_missing_demo_receipt_is_rejected(self, validator: ModuleType, release_run: Run) -> None:
        """A demo deliverable with no recorded execution receipt is rejected."""
        prepare_demo(release_run, "print('demo successful')\n")

        with pytest.raises(SystemExit, match="release-evidence-demo-receipt"):
            validate_run(validator, release_run)

    def test_forged_demo_output_is_rejected(self, validator: ModuleType, release_run: Run) -> None:
        """Replacing the recorded output file after the run breaks the receipt's digest."""
        demo = prepare_demo(release_run, "print('demo successful')\n")
        record_demo(release_run, demo, timeout_seconds=60, returncode=0)
        Path(receipt_of(release_run)["demo"]["output"]).write_bytes(b"forged output")

        with pytest.raises(SystemExit, match="release-evidence-demo-output"):
            validate_run(validator, release_run)

    def test_demo_script_changed_after_recording_is_rejected(self, validator: ModuleType, release_run: Run) -> None:
        """A receipt recorded for one script cannot certify a different script rebound to the artifact digest."""
        demo = prepare_demo(release_run, "print('demo successful')\n")
        record_demo(release_run, demo, timeout_seconds=60, returncode=0)
        demo.write_text("print('different script')\n", encoding="utf-8")
        digest = hashlib.sha256(demo.read_bytes()).hexdigest()
        receipt = receipt_of(release_run)
        receipt["artifacts"][0]["sha256"] = digest
        receipt["claims"][0].update(sha256=digest, excerpt="print('different script')")

        with pytest.raises(SystemExit, match="release-evidence-demo-receipt"):
            validate_run(validator, release_run)

    @pytest.mark.parametrize("stdout", ["", "not JSON\n"])
    @mock.patch.object(subprocess, "run", autospec=True)
    def test_recorder_crash_stays_visible(self, recorder: mock.MagicMock, release_run: Run, stdout: str) -> None:
        """A recorder crash is reported with its exit code, output, and traceback.

        This pins the diagnostics of the demo helper above: the recorder subprocess is replaced by a crashed result so
        a failure there points at the crash instead of a bare JSON decode error.
        """
        demo = prepare_demo(release_run, "print('demo successful')\n")
        recorder.return_value = subprocess.CompletedProcess(
            [], 1, stdout=stdout, stderr="Traceback (most recent call last):\nRuntimeError: recorder cleanup failed\n"
        )

        with pytest.raises(pytest.fail.Exception, match="Demo recorder did not produce a JSON receipt") as raised:
            record_demo(release_run, demo, timeout_seconds=1, returncode=1)

        failure = str(raised.value)
        assert recorder.call_args.args[0][1:3] == [str(PLUGIN_ROOT / "shared/release_evidence.py"), "record-demo"]
        assert "returncode=1" in failure
        assert f"stdout={stdout!r}" in failure
        assert "RuntimeError: recorder cleanup failed" in failure
