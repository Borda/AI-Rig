#!/usr/bin/env python3
"""Validate release evidence and record explicitly requested local demo executions.

## Purpose

Reject passing release communication results whose local evidence receipt cannot prove a bounded release contract.

## Scope

This module validates the release-specific Git range, already-published patch subtraction, claims, attribution, output
bytes, changelog preservation, and demo execution receipt associated with a versioned release result. It deliberately
checks exact local records and source bytes; it does not claim to decide general prose truth or publish a release.

## Usage

``validate_release_evidence(metadata, out_dir, requested_artifacts)`` is called from ``validate-artifacts.py`` after
the common result and gate checks. The caller supplies parsed result metadata, the retained run directory, and selected
deliverable names. Validation only reads local Git and files. The separate, explicit CLI
``python release_evidence.py record-demo --script demo.py --cwd . --environment 'project environment' --out run``
executes the script with the selected Python interpreter, retaining output and before/after digests in a new directory.
It never installs dependencies or grants network access; the owning workflow must authorize execution first.

## Outputs

A receipt satisfying every binding returns normally. A missing, stale, incomplete, or internally inconsistent record
raises ``SystemExit`` with a stable ``release-evidence-*`` reason that makes the result ineligible for promotion.

## Failure

Examples include a non-ancestor baseline, unaccounted candidate, incomplete credit or exclusion, path/digest drift,
lost historical changelog bytes, placeholder summary, or a demo receipt whose recorded output cannot be bound. Ambiguous
patch equivalence fails closed rather than being inferred from subjects, titles, or net tree state.

## Used by

``shared/validate-artifacts.py`` invokes this helper for release contract version one. ``skills/release`` documents the
receipt producer contract. ``tests/test_release_artifacts.py`` uses real temporary Git histories to exercise the public
validator entrypoint.
"""

from __future__ import annotations

import ast
import argparse
from collections import Counter
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from run_gates import terminate_process


_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_DISPOSITIONS = {"included", "already-released", "internal-only", "reverted", "superseded", "merge-only"}
_EXCLUSION_BASES = {"legal-restriction", "privacy-request", "verified-duplicate"}


def _fail(reason: str) -> None:
    """Stop validation with a stable release-evidence failure reason."""
    raise SystemExit(f"release-evidence-{reason}")


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one regular evidence file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repository: Path, *args: str) -> str:
    """Run one local Git read command and reject a nonzero result."""
    completed = subprocess.run(["git", *args], cwd=repository, capture_output=True, text=True, check=False)
    if completed.returncode:
        _fail("git:" + args[0])
    return completed.stdout.strip()


def _is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    """Return whether one verified Git object is reachable from another."""
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    _fail("git:merge-base")


def _patch_id(repository: Path, sha: str) -> str:
    """Return a nonempty stable patch ID for a local nonempty commit patch."""
    patch = subprocess.run(
        ["git", "show", "--format=", "--no-ext-diff", sha],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if patch.returncode:
        _fail("git:show")
    stable = subprocess.run(
        ["git", "patch-id", "--stable"], input=patch.stdout, capture_output=True, text=True, check=False
    )
    if stable.returncode or not stable.stdout.strip():
        _fail("patch-id")
    return stable.stdout.split()[0]


def _blob_sha256(repository: Path, sha: str, path: str) -> str | None:
    """Return one tree blob digest, or ``None`` when the path is absent from that tree."""
    completed = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=repository, capture_output=True, check=False)
    if completed.returncode == 0:
        return hashlib.sha256(completed.stdout).hexdigest()
    if completed.returncode == 128:
        return None
    _fail("git:show")


def _tree_entry(repository: Path, sha: str, path: str) -> tuple[str, str, str] | None:
    """Return the exact literal-path mode, type, and object identity, including gitlinks."""
    record = _git(repository, "--literal-pathspecs", "ls-tree", "-z", sha, "--", path)
    if not record:
        return None
    fields = record.split("\t", 1)[0].split()
    if len(fields) != 3:
        _fail("git:ls-tree")
    return fields[0], fields[1], fields[2]


def _changed_paths(repository: Path, sha: str) -> set[str]:
    """Return the exact paths changed by one candidate patch, including root commits."""
    paths = (
        _git(repository, "diff-tree", "--root", "--no-commit-id", "-r", "--name-only", "-z", sha)
        .rstrip("\0")
        .split("\0")
    )
    if not paths or paths == [""]:
        _fail("released-paths")
    return set(paths)


def _nonempty_section(text: str, heading: str) -> bool:
    """Return whether an exact Markdown heading has content before the next peer heading."""
    match = re.search(rf"(?m)^{re.escape(heading)}\s*$", text)
    if match is None:
        return False
    following = re.search(r"(?m)^##\s+", text[match.end() :])
    body = text[match.end() : match.end() + following.start() if following else len(text)]
    return bool(body.strip())


def _identity_hash(name: str, email: str) -> str:
    """Produce the internal, normalized identity binding for one person."""
    return hashlib.sha256(f"{name}\0{email.casefold()}".encode()).hexdigest()


def _eligible_identities(repository: Path, candidates: set[str]) -> dict[str, str]:
    """Derive every author/coauthor identity before separating human and bot credits."""
    identities: dict[str, str] = {}
    for candidate in candidates:
        record = _git(
            repository, "show", "-s", "--format=%aN%x00%aE%x00%(trailers:key=Co-authored-by,valueonly)", candidate
        )
        author_name, author_email, trailers = record.split("\x00", maxsplit=2)
        if author_name and author_email:
            identities[_identity_hash(author_name, author_email)] = author_name
        for name, email in re.findall(r"(.*?)\s*<([^>]+)>", trailers):
            identities[_identity_hash(name.strip(), email.strip())] = name.strip()
    return identities


def _require_receipt(metadata: dict[str, Any]) -> dict[str, Any]:
    """Extract the supported version-one receipt from result metadata."""
    receipt = metadata.get("release_evidence")
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        _fail("receipt")
    return receipt


def _validate_scope(receipt: dict[str, Any], metadata: dict[str, Any]) -> tuple[Path, set[str], set[str]]:
    """Bind a pinned repository, ancestor range, and complete candidate inventory."""
    raw_repository = receipt.get("repository")
    if not isinstance(raw_repository, str) or not Path(raw_repository).is_absolute():
        _fail("repository")
    repository = Path(raw_repository)
    if not repository.is_dir() or _git(repository, "rev-parse", "--is-inside-work-tree") != "true":
        _fail("repository")
    head = metadata.get("release_head")
    if not isinstance(head, str) or not _SHA.fullmatch(head) or _git(repository, "rev-parse", "HEAD") != head:
        _fail("head")
    final_tree = receipt.get("final_tree")
    if not isinstance(final_tree, str) or _git(repository, "rev-parse", f"{head}^{{tree}}") != final_tree:
        _fail("final-tree")
    baseline = receipt.get("baseline")
    if baseline is not None and (not isinstance(baseline, str) or not _SHA.fullmatch(baseline)):
        _fail("baseline")
    if baseline is not None and not _is_ancestor(repository, baseline, head):
        _fail("baseline-ancestry")
    expected = set(_git(repository, "rev-list", f"{baseline}..{head}" if baseline else head).splitlines())
    rows = receipt.get("candidates")
    if not isinstance(rows, list) or (not rows and expected):
        _fail("candidates")
    recorded: set[str] = set()
    included: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            _fail("candidate-row")
        sha, disposition, reason = row.get("sha"), row.get("disposition"), row.get("reason")
        if not isinstance(sha, str) or sha not in expected or sha in recorded:
            _fail("candidate-set")
        if disposition not in _DISPOSITIONS or not isinstance(reason, str) or not reason.strip():
            _fail("candidate-disposition")
        recorded.add(sha)
        if disposition == "included":
            included.add(sha)
        if disposition == "already-released":
            released_head = receipt.get("released_head")
            released_sha, comparison = row.get("released_sha"), row.get("comparison")
            if (
                not isinstance(released_head, str)
                or not _SHA.fullmatch(released_head)
                or not isinstance(released_sha, str)
                or not _SHA.fullmatch(released_sha)
                or comparison not in {"identity", "stable-patch"}
            ):
                _fail("released-binding")
            if not _is_ancestor(repository, released_sha, released_head):
                _fail("released-reachability")
            if comparison == "identity" and released_sha != sha:
                _fail("released-identity")
            if comparison == "stable-patch" and _patch_id(repository, released_sha) != _patch_id(repository, sha):
                _fail("released-patch")
            published_tree = row.get("published_tree")
            path_rows = row.get("published_paths")
            if (
                not isinstance(published_tree, str)
                or _git(repository, "rev-parse", f"{released_head}^{{tree}}") != published_tree
                or not isinstance(path_rows, list)
            ):
                _fail("released-tree")
            paths: dict[str, str | None] = {}
            for path_row in path_rows:
                if (
                    not isinstance(path_row, dict)
                    or not isinstance(path_row.get("path"), str)
                    or path_row["path"] in paths
                    or path_row.get("sha256") is not None
                    and (
                        not isinstance(path_row["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", path_row["sha256"])
                    )
                    or path_row.get("mode") is not None
                    and (not isinstance(path_row["mode"], str) or not re.fullmatch(r"[0-7]{6}", path_row["mode"]))
                    or path_row.get("object_type") is not None
                    and path_row["object_type"] not in {"blob", "commit"}
                ):
                    _fail("released-path-row")
                paths[path_row["path"]] = path_row.get("sha256")
            if set(paths) != _changed_paths(repository, sha):
                _fail("released-paths")
            for path, digest in paths.items():
                row = next(item for item in path_rows if item["path"] == path)
                candidate_entry = _tree_entry(repository, sha, path)
                published_entry = _tree_entry(repository, released_head, path)
                if (
                    digest != _blob_sha256(repository, sha, path)
                    or digest != _blob_sha256(repository, released_head, path)
                    or candidate_entry != published_entry
                    or (candidate_entry[:2] if candidate_entry else (None, None))
                    != (row.get("mode"), row.get("object_type"))
                ):
                    _fail("released-tree-path")
    if recorded != expected:
        _fail("candidate-set")
    return repository, expected, included


def _validate_claims(receipt: dict[str, Any], out_dir: Path, included: set[str]) -> None:
    """Bind every included candidate to a nonempty retained-artifact claim excerpt."""
    claims = receipt.get("claims")
    if not isinstance(claims, list):
        _fail("claims")
    claimed: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            _fail("claim-row")
        artifact, excerpt, sha, candidates = (
            claim.get("artifact"),
            claim.get("excerpt"),
            claim.get("sha256"),
            claim.get("candidate_shas"),
        )
        deliverables = (out_dir / "deliverables").resolve()
        path = deliverables / str(artifact)
        if (
            not isinstance(artifact, str)
            or Path(artifact).name != artifact
            or not isinstance(excerpt, str)
            or not excerpt.strip()
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or not isinstance(candidates, list)
            or not candidates
            or any(not isinstance(item, str) for item in candidates)
            or len(candidates) != len(set(candidates))
        ):
            _fail("claim-row")
        if (
            not path.is_file()
            or path.resolve().parent != deliverables
            or _sha256(path) != sha
            or excerpt not in path.read_text(encoding="utf-8")
        ):
            _fail("claim-binding")
        if not all(isinstance(item, str) and item in included for item in candidates):
            _fail("claim-candidate")
        claimed.update(candidates)
    if claimed != included:
        _fail("claim-coverage")


def _validate_scope_record(out_dir: Path, receipt: dict[str, Any], head: str, candidates: set[str]) -> None:
    """Require scope prose to visibly retain receipt coordinates rather than headings alone."""
    scope = out_dir / "release-scope.md"
    text = scope.read_text(encoding="utf-8")
    required = {head, receipt["final_tree"], *candidates}
    baseline = receipt.get("baseline")
    if baseline is not None:
        required.add(baseline)
    released_head = receipt.get("released_head")
    if isinstance(released_head, str):
        required.add(released_head)
    if not all(value in text for value in required):
        _fail("scope-record")


def _validate_artifacts(receipt: dict[str, Any], out_dir: Path, requested: set[str]) -> dict[str, Path]:
    """Bind each selected retained artifact to its declared final destination bytes."""
    rows = receipt.get("artifacts")
    if not isinstance(rows, list):
        _fail("artifacts")
    paths: dict[str, Path] = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail("artifact-row")
        name, destination, digest = row.get("name"), row.get("destination"), row.get("sha256")
        if (
            not isinstance(name, str)
            or not isinstance(destination, str)
            or not Path(destination).is_absolute()
            or not isinstance(digest, str)
        ):
            _fail("artifact-row")
        retained = out_dir / "deliverables" / name
        final = Path(destination)
        if (
            name in paths
            or not retained.is_file()
            or not final.is_file()
            or _sha256(retained) != digest
            or _sha256(final) != digest
        ):
            _fail("artifact-binding")
        paths[name] = final
    if set(paths) != requested:
        _fail("artifact-set")
    for name, path in paths.items():
        text = path.read_text(encoding="utf-8")
        if name == "SUMMARY.md":
            if not _nonempty_section(text, "## Overview") or not _nonempty_section(text, "## Benefits"):
                _fail("summary-role")
        if name == "MIGRATION.md" and not (
            _nonempty_section(text, "## Actions") or _nonempty_section(text, "## No migration required")
        ):
            _fail("migration-role")
        if name == "CHANGELOG.md" and not re.search(r"^#+\s+.+", text, re.MULTILINE):
            _fail("changelog-role")
    return paths


def _pr_author_identities(receipt: dict[str, Any], candidates: set[str]) -> dict[str, str]:
    """Collect receipt-recorded PR author identities associated with candidate commits."""
    rows = receipt.get("pr_authors", [])
    if not isinstance(rows, list):
        _fail("pr-authors")
    identities: dict[str, str] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("sha") not in candidates
            or not isinstance(row.get("identity_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["identity_hash"])
            or not isinstance(row.get("display_name"), str)
            or not row["display_name"].strip()
            or ("is_bot" in row and not isinstance(row["is_bot"], bool))
        ):
            _fail("pr-author-row")
        identities[row["identity_hash"]] = row["display_name"].strip()
    return identities


def _validate_bots(
    receipt: dict[str, Any], eligible: dict[str, str], out_dir: Path, artifacts: dict[str, Path]
) -> set[str]:
    """Bind classified automation identities to one aggregate credit, never human entries."""
    rows = receipt.get("bots", [])
    if not isinstance(rows, list):
        _fail("bot-inventory")
    identities: set[str] = set()
    labels: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            _fail("bot-row")
        identity, name, label = row.get("identity_hash"), row.get("display_name"), row.get("label")
        if (
            not isinstance(identity, str)
            or identity not in eligible
            or identity in identities
            or name != eligible[identity]
            or not isinstance(label, str)
            or not label.strip()
            or any(character in label for character in "\r\n,*")
        ):
            _fail("bot-row")
        proof = row.get("classification_evidence")
        if not isinstance(proof, dict) or not isinstance(proof.get("path"), str):
            _fail("bot-classification-proof")
        proof_path = out_dir / proof["path"]
        if (
            not proof_path.is_file()
            or not proof_path.resolve().is_relative_to(out_dir.resolve())
            or proof.get("sha256") != _sha256(proof_path)
            or not isinstance(proof.get("excerpt"), str)
            or not proof["excerpt"].strip()
            or proof["excerpt"] not in proof_path.read_text(encoding="utf-8")
        ):
            _fail("bot-classification-proof")
        identities.add(identity)
        labels.add(label)
    required = {identity for identity, name in eligible.items() if "[bot]" in name.casefold()}
    required.update(
        row["identity_hash"]
        for row in receipt.get("pr_authors", [])
        if row.get("is_bot") is True and row["identity_hash"] in eligible
    )
    if not required <= identities:
        _fail("bot-coverage")
    # Only the draft owns public contributor credits; audit/demo record them privately.
    target = artifacts.get("DRAFT.md", out_dir / "contributors.md")
    text = target.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("*Automated contributions:")]
    expected = "*Automated contributions: " + ", ".join(sorted(labels)) + "*"
    if lines != ([expected] if identities else []):
        _fail("bot-aggregate-credit")
    if any(re.search(rf"(?m)^\s*[-*]\s+\*\*{re.escape(eligible[identity])}\*\*", text) for identity in identities):
        _fail("bot-individual-credit")
    return identities


def _validate_contributors(
    receipt: dict[str, Any], repository: Path, out_dir: Path, candidates: set[str], artifacts: dict[str, Path]
) -> None:
    """Reconcile human credit/exclusion and separate aggregate automation accounting."""
    rows = receipt.get("contributors")
    if not isinstance(rows, list):
        _fail("contributors")
    recorded: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("identity_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["identity_hash"])
            or not isinstance(row.get("display_name"), str)
            or not row["display_name"].strip()
        ):
            _fail("contributor-row")
        identity = row["identity_hash"]
        disposition = row.get("disposition")
        if identity in recorded or disposition not in {"credited", "excluded"}:
            _fail("contributor-row")
        if disposition == "credited" and (
            not isinstance(row.get("credit_excerpt"), str)
            or f"**{row['display_name'].strip()}**" not in row["credit_excerpt"]
        ):
            _fail("contributor-credit")
        if disposition == "excluded" and (
            row.get("exclusion_basis") not in _EXCLUSION_BASES
            or not isinstance(row.get("exclusion"), str)
            or not row["exclusion"].strip()
        ):
            _fail("contributor-exclusion")
        recorded.add(identity)
    new_candidates = {row["sha"] for row in receipt["candidates"] if row["disposition"] != "already-released"}
    eligible = _eligible_identities(repository, new_candidates)
    for identity, display_name in _pr_author_identities(receipt, candidates).items():
        if not any(
            row["identity_hash"] == identity and row["sha"] in new_candidates for row in receipt.get("pr_authors", [])
        ):
            continue
        if identity in eligible and eligible[identity] != display_name:
            _fail("pr-author-alias")
        eligible[identity] = display_name
    bots = _validate_bots(receipt, eligible, out_dir, artifacts)
    if recorded & bots:
        _fail("bot-human-overlap")
    if recorded != set(eligible) - bots:
        _fail("contributor-coverage")
    public = "\n".join(path.read_text(encoding="utf-8") for name, path in artifacts.items() if name != "demo.py")
    exclusions = (out_dir / "contributors.md").read_text(encoding="utf-8")
    credit_text = public if public else exclusions
    for row in rows:
        if row["display_name"].strip() != eligible[row["identity_hash"]]:
            _fail("contributor-display-name")
        if row["disposition"] == "credited" and row["credit_excerpt"] not in credit_text:
            _fail("contributor-credit")
        if row["disposition"] == "excluded" and row["exclusion"] not in exclusions:
            _fail("contributor-exclusion")
        if row["disposition"] == "excluded":
            proof = row.get("exclusion_evidence")
            if not isinstance(proof, dict):
                _fail("contributor-exclusion-proof")
            proof_path = out_dir / str(proof.get("path", ""))
            if (
                not proof_path.is_file()
                or not proof_path.resolve().is_relative_to(out_dir.resolve())
                or proof.get("sha256") != _sha256(proof_path)
                or not isinstance(proof.get("excerpt"), str)
                or not proof["excerpt"].strip()
                or proof["excerpt"] not in proof_path.read_text(encoding="utf-8")
            ):
                _fail("contributor-exclusion-proof")
    if re.search(r"[\w.+-]+@[\w.-]+", public):
        _fail("public-email")


def _changelog_sections(data: bytes) -> dict[bytes, bytes]:
    """Split canonical level-two version sections without normalizing their bytes."""
    starts = list(re.finditer(rb"(?m)^##[ \t]+[^\r\n]+", data))
    sections: dict[bytes, bytes] = {}
    for index, start in enumerate(starts):
        heading = start.group()
        if heading in sections:
            _fail("changelog-duplicate-heading")
        end = starts[index + 1].start() if index + 1 < len(starts) else len(data)
        section = data[start.start() : end]
        # Reference definitions are validated separately and included in the release excerpt below.
        lines = section.splitlines(keepends=True)
        while lines and (not lines[-1].strip() or re.match(rb"^\[[^]\r\n]+\]:[ \t]+", lines[-1])):
            lines.pop()
        sections[heading] = b"".join(lines)
    return sections


def _validate_changelog(receipt: dict[str, Any], artifacts: dict[str, Path]) -> None:
    """Require exact historical sections and reference definitions to survive changelog editing."""
    changelog = receipt.get("changelog")
    if not isinstance(changelog, dict):
        _fail("changelog")
    if changelog.get("destination") is None:
        if (
            changelog.get("backup") is not None
            or not isinstance(changelog.get("absent_reason"), str)
            or not changelog["absent_reason"].strip()
        ):
            _fail("changelog-absence")
        if "CHANGELOG.md" in artifacts:
            text = artifacts["CHANGELOG.md"].read_text(encoding="utf-8")
            heading = changelog.get("target_heading")
            if not isinstance(heading, str) or not heading.startswith("## ") or not _nonempty_section(text, heading):
                _fail("changelog-target")
        return
    for key in ("backup", "destination"):
        value = changelog.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
            _fail("changelog-path")
    backup, final = Path(changelog["backup"]), Path(changelog["destination"])
    if changelog.get("backup_sha256") != _sha256(backup) or changelog.get("final_sha256") != _sha256(final):
        _fail("changelog-digest")
    old, current = backup.read_bytes(), final.read_bytes()
    heading = changelog.get("target_heading")
    if not isinstance(heading, str) or not heading.startswith("## "):
        _fail("changelog-target")
    target = heading.encode("utf-8")
    before, after = _changelog_sections(old), _changelog_sections(current)
    if target not in after or not _nonempty_section(after[target].decode("utf-8"), heading):
        _fail("changelog-target")
    mutable = {target, b"## Unreleased", b"## [Unreleased]"}
    old_detail = Counter(
        line
        for key, section in before.items()
        if key in mutable
        for line in section.splitlines(keepends=True)[1:]
        if line.strip()
    )
    new_detail = Counter(
        line
        for key, section in after.items()
        if key in mutable
        for line in section.splitlines(keepends=True)[1:]
        if line.strip()
    )
    if old_detail - new_detail:
        _fail("changelog-target-detail")
    for key, section in before.items():
        if key not in mutable and (key not in after or section.rstrip(b"\r\n") not in after[key]):
            _fail("changelog-history")
    references = re.findall(rb"(?m)^\[[^]\r\n]+\]:[ \t]+[^\r\n]+", old)
    if any(reference not in current for reference in references):
        _fail("changelog-history")
    if "CHANGELOG.md" in artifacts:
        all_references = re.findall(rb"(?m)^\[[^]\r\n]+\]:[ \t]+[^\r\n]+", current)
        excerpt = after[target].rstrip(b"\r\n") + b"\n"
        all_references = [reference for reference in all_references if reference not in excerpt.splitlines()]
        if all_references:
            excerpt += b"\n" + b"\n".join(all_references) + b"\n"
        if artifacts["CHANGELOG.md"].read_bytes() != excerpt:
            _fail("changelog-excerpt")


def _validate_demo(receipt: dict[str, Any], artifacts: dict[str, Path]) -> None:
    """Bind a selected demo to a successful helper-produced execution receipt and output."""
    if "demo.py" not in artifacts:
        return
    demo = receipt.get("demo")
    if not isinstance(demo, dict) or demo.get("sha256") != _sha256(artifacts["demo.py"]):
        _fail("demo-receipt")
    command = [demo.get("interpreter"), str(artifacts["demo.py"].resolve())]
    if (
        type(demo.get("exit_code")) is not int
        or demo["exit_code"] != 0
        or demo.get("command") != command
        or not all(
            isinstance(demo.get(key), str) and demo[key]
            for key in ("interpreter", "environment", "cwd", "output", "execution_receipt")
        )
    ):
        _fail("demo-execution")
    output = Path(demo["output"])
    receipt = Path(demo.get("execution_receipt", ""))
    if (
        not output.is_file()
        or not isinstance(demo.get("output_sha256"), str)
        or demo["output_sha256"] != _sha256(output)
        or not receipt.is_file()
        or not isinstance(demo.get("execution_receipt_sha256"), str)
        or demo["execution_receipt_sha256"] != _sha256(receipt)
    ):
        _fail("demo-output")
    try:
        execution = json.loads(receipt.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail("demo-execution-receipt")
    if not isinstance(execution, dict) or execution != {
        "command": demo["command"],
        "cwd": demo["cwd"],
        "environment": demo["environment"],
        "exit_code": 0,
        "interpreter": demo["interpreter"],
        "output_sha256": demo["output_sha256"],
        "script_sha256": demo["sha256"],
        "script_sha256_after": demo["sha256"],
    }:
        _fail("demo-execution-receipt")
    try:
        ast.parse(artifacts["demo.py"].read_text(encoding="utf-8"))
    except SyntaxError:
        _fail("demo-python")


def validate_release_evidence(metadata: dict[str, Any], out_dir: Path, requested_artifacts: set[str]) -> None:
    """Validate the release-specific receipt required for a passing contract-v1 result."""
    receipt = _require_receipt(metadata)
    repository, candidates, included = _validate_scope(receipt, metadata)
    _validate_scope_record(out_dir, receipt, metadata["release_head"], candidates)
    if not requested_artifacts:
        _validate_contributors(receipt, repository, out_dir, candidates, {})
        _validate_changelog(receipt, {})
        return
    artifacts = _validate_artifacts(receipt, out_dir, requested_artifacts)
    _validate_claims(receipt, out_dir, included)
    _validate_contributors(receipt, repository, out_dir, candidates, artifacts)
    _validate_changelog(receipt, artifacts)
    _validate_demo(receipt, artifacts)


def record_demo(
    script: Path, interpreter: Path, cwd: Path, environment: str, out: Path, timeout: int = 600
) -> dict[str, Any]:
    """Execute one explicitly authorized local Python demo and retain digest-bound evidence.

    Output uses a new directory; existing evidence is never overwritten. Execution inherits the caller's environment
    and permissions, with no dependency installation or network grant. A timeout or nonzero exit is retained as failed
    evidence. Validation never calls this execution entrypoint implicitly.
    """
    script, interpreter, cwd, out = script.resolve(), interpreter.absolute(), cwd.resolve(), out.resolve()
    if (
        not script.is_file()
        or not interpreter.is_file()
        or not cwd.is_dir()
        or not environment.strip()
        or not 0 < timeout <= 600
    ):
        _fail("demo-input")
    digest = _sha256(script)
    out.mkdir(parents=True, exist_ok=False)
    output = out / "output.txt"
    command = [str(interpreter), str(script)]
    with output.open("xb") as stream:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name != "nt"
        )
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process(process, sys.platform)
            exit_code = 124
    execution = {
        "command": command,
        "cwd": str(cwd),
        "environment": environment,
        "exit_code": exit_code,
        "interpreter": str(interpreter),
        "output_sha256": _sha256(output),
        "script_sha256": digest,
        "script_sha256_after": _sha256(script),
    }
    execution_path = out / "execution.json"
    execution_path.write_text(json.dumps(execution, indent=2) + "\n", encoding="utf-8", newline="\n")
    receipt = {
        **{
            key: execution[key]
            for key in ("command", "cwd", "environment", "exit_code", "interpreter", "output_sha256")
        },
        "sha256": digest,
        "output": str(output),
        "execution_receipt": str(execution_path),
        "execution_receipt_sha256": _sha256(execution_path),
    }
    (out / "demo.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
    return receipt


def main() -> int:
    """Record a single authorized demo; report failure without retrying or promoting it."""
    parser = argparse.ArgumentParser(description="Record an explicitly authorized local demo execution.")
    parser.add_argument("action", choices=["record-demo"])
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    receipt = record_demo(args.script, args.python, args.cwd, args.environment, args.out, args.timeout_seconds)
    print(json.dumps(receipt))
    return 0 if receipt["exit_code"] == 0 and receipt["sha256"] == _sha256(args.script) else 1


if __name__ == "__main__":
    raise SystemExit(main())
