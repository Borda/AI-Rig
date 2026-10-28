#!/usr/bin/env python3
"""Partition a changed-file scope into complete, measurable review inputs.

## Purpose:
Keep a large challenge review moving when one source snapshot and diff exceed a reviewer route's input budget. Each
chunk preserves full current source bytes for whole files, including deleted and non-ignored untracked files, with the
supporting tracked Git patch. A single oversized file remains visible instead of silently disappearing.

## Scope:
Inventory changed files or a complete selected scope under optional literal repository-relative paths. Pack them in
stable order and verify that their disjoint union still equals the current requested scope. Clean final checking binds
independent results for every pair decision and declared larger interaction to complete member-chunk source. A separate
stopped check binds supplied partial results and identifies unfinished review slots.
The schema-two scoped plan records exact whole-file change units and assigned interaction endpoints. A pinned-base,
origin-bound, first-run text profile admits review and checks authenticated component and independent audit results;
unsupported profiles fail closed. The helper does not launch agents, approve paid routes, change source, or certify
comprehension.

## Usage:
Run ``chunk_diff.py plan --repository <root> --out <new-directory> --goal <goal> --specification <criteria>
--done-when <condition>`` with optional ``--scope-path`` and ``--scope-mode changed|all``. Set
``--budget-bytes`` from the selected review route after reserving room for its role card and instructions.
For a continuation, pass ``--caller-run``, that run's ``loop-ledger.json`` via ``--prior-ledger``, and
``--prior-coverage`` with one source-path and assigned-review entry per final-round signature. The planner pins a copy
of the ledger header and its ``loop-rounds.jsonl`` round log, records their ``ledger_digest``, and checks that digest
against the declared caller run on each check. This does not authenticate the
historical reviewer after the original source changes. It appends the block
to the specification so every reviewer receives the earlier counterexamples.
Run ``chunk_diff.py check --manifest <out>/chunks.json`` before dispatch and final acceptance; replan after edits.
Pass ``--results <out>/results.json --summary`` to emit a validated clean child-backed JSON summary, or use
``check-stopped --manifest <out>/chunks.json --results <out>/results.json --summary`` for partial stopped evidence.
For scoped planning, pass a full commit OID as ``--diff-base`` and the invoking checkout as ``--origin-repository``
while ``--repository`` names its detached review mirror. Then run ``scoped-plan --manifest <out>/chunks.json
--assignments <file> --out <out>/scoped-chunks.json`` to record source units, interactions, and independent audits.
Check that manifest before dispatch, then check it with its results before acceptance. Scoped continuations, oversized
files, binary or symlink source, hidden index flags, and base-only deletions remain unsupported.

## Outputs:
Write ``chunks.json`` and one exact source JSON and diff patch per chunk. The manifest records scope inventory, sizes,
digests, and oversize files for the owning challenge workflow and its next reviewer. The separate results map records
one independently assessed decision for each chunk pair and each declared larger interaction group.
Each summary names validated runs and own score series without a combined score. Stopped output also names pending
chunks, interactions, declared groups, and prior signatures lacking independently verified current assessment.

## Failure:
Reject missing changes, unsafe output paths, stale source or index state, overlap, omitted files, modified artifacts,
incomplete interaction decisions, or malformed manifests with a nonzero exit. A size ceiling affects only the selected
route and never proves that every independent review route is unavailable.

## Used by:
The challenge-resolve planner and acceptance check; reviewers consume its frozen per-chunk evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
from adversarial_loop import ledger_digest, load_ledger, rounds_path, validate_ledger
from collect_diff import (
    canonical_diff_arguments,
    canonical_diff_environment,
    capture_source_snapshot,
    reject_gitlink_changes,
)

#: Default per-chunk size budget, in bytes, for the --budget-bytes planner option.
DEFAULT_BUDGET_BYTES = 1_000_000


def _git(repository: Path, *arguments: str) -> bytes:
    """Read one local Git result and fail when source inspection is incomplete."""
    result = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "-C", os.fspath(repository), "--literal-pathspecs", *canonical_diff_arguments(arguments)],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
        capture_output=True,
        check=False,
        env=canonical_diff_environment() if arguments and arguments[0] == "diff" else None,
    )
    if result.returncode:
        raise ValueError(f"git-inspection-failed:{arguments[0]}")
    return result.stdout


def _inventory(repository: Path, scopes: list[str], scope_mode: str, base: str = "HEAD") -> list[str]:
    """List changed files or all selected HEAD, index, and non-ignored worktree files."""
    reject_gitlink_changes(repository, (base, "--", *scopes))
    if scope_mode == "changed":
        tracked = _git(repository, "diff", base, "--no-renames", "--name-only", "-z", "--", *scopes)
        untracked = _git(repository, "ls-files", "--others", "--exclude-standard", "-z", "--", *scopes)
    elif scope_mode == "all":
        tracked = _git(repository, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", *scopes)
        tracked += _git(repository, "ls-files", "--cached", "-z", "--", *scopes)
        if base != "HEAD":
            tracked += _git(repository, "diff", base, "--no-renames", "--name-only", "-z", "--", *scopes)
        untracked = _git(repository, "ls-files", "--others", "--exclude-standard", "-z", "--", *scopes)
    else:
        raise ValueError("scope-mode-invalid")
    paths = sorted({os.fsdecode(item) for item in (tracked + untracked).split(b"\0") if item})
    if not paths:
        raise ValueError("empty-changed-scope")
    return paths


def _material(repository: Path, paths: list[str], base: str = "HEAD") -> tuple[bytes, bytes]:
    """Capture the exact source serializer and patch that a reviewer must receive."""
    snapshot = capture_source_snapshot(repository, paths)
    source = (json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    diff = _git(repository, "diff", base, "--binary", "--no-renames", "--", *paths)
    return source, diff


def _safe_output(repository: Path, output: Path) -> Path:
    """Keep evidence outside reviewable source or inside an ignored artifact directory."""
    destination = output.resolve()
    try:
        relative = destination.relative_to(repository.resolve()).as_posix()
    except ValueError:
        return destination
    ignored = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "-C", os.fspath(repository), "check-ignore", "-q", "--no-index", "--", relative],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
        capture_output=True,
        check=False,
    )
    if ignored.returncode != 0:
        raise ValueError("output-must-be-ignored-or-outside-repository")
    return destination


def _artifact(root: Path, raw: object) -> Path:
    """Resolve a manifest path without accepting traversal or links outside the run."""
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute() or ".." in Path(raw).parts:
        raise ValueError("chunk-artifact-path-invalid")
    path = (root / raw).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("chunk-artifact-path-invalid")
    return path


def _prior_ledger_artifact(root: Path, raw: object) -> Path:
    """Resolve a pinned prior ledger header whose sibling round log must also stay inside the run."""
    header = _artifact(root, raw)
    if not isinstance(raw, str):
        raise TypeError(f"raw must be str, got {type(raw).__name__}")
    _artifact(root, rounds_path(Path(raw)).as_posix())
    return header


def _literal_source_path(value: object) -> bool:
    """Accept a repository-relative source coordinate on every supported host."""
    return (
        isinstance(value, str)
        and bool(value)
        and "\x00" not in value
        and not PurePosixPath(value).is_absolute()
        and not PureWindowsPath(value).is_absolute()
        and not PureWindowsPath(value).drive
        and ".." not in PurePosixPath(value).parts
        and ".." not in PureWindowsPath(value).parts
    )


def _prior_request_block(ledger: dict[str, object], coverage: list[dict[str, object]]) -> str:
    """Render prior counterexamples and required source paths for exact request delivery."""
    by_signature = {item["signature"]: item for item in coverage}
    findings = [
        {
            "signature": finding["signature"],
            "evidence": finding["evidence"],
            "source_paths": by_signature[finding["signature"]]["source_paths"],
        }
        | {"tier": finding["tier"], "structural": finding["structural"]}
        for finding in ledger["rounds"][-1]["findings"]
    ]
    return "Prior findings to reassess:\n" + json.dumps(
        sorted(findings, key=lambda item: item["signature"]), sort_keys=True, ensure_ascii=True
    )


def _scoped_units(repository: Path, chunks: list[dict[str, object]], base: str = "HEAD") -> list[dict[str, object]]:
    """Derive conservative whole-file change units from captured bytes and each exact patch."""
    units = []
    for chunk in chunks:
        for path in chunk["scope_paths"]:
            source, diff = _material(repository, [path], base)
            record = json.loads(source)["files"][0]
            if base != "HEAD" and record.get("encoding") == "utf-8" and "\x00" in record["content"]:
                raise ValueError("scoped-unsupported-source-unreviewed")
            diff_lines = diff.splitlines()
            identity = {
                "path": path,
                "chunk_id": chunk["id"],
                "kind": record["kind"],
                "encoding": record.get("encoding"),
                "record_sha256": hashlib.sha256(
                    json.dumps(record, sort_keys=True, ensure_ascii=True).encode("utf-8")
                ).hexdigest(),
                "diff_sha256": hashlib.sha256(diff).hexdigest(),
                "binary_diff": b"GIT binary patch" in diff_lines
                or any(line.startswith(b"Binary files ") and line.endswith(b" differ") for line in diff_lines),
                "symlink_diff": any(
                    line
                    in {b"new file mode 120000", b"deleted file mode 120000", b"old mode 120000", b"new mode 120000"}
                    or (line.startswith(b"index ") and line.endswith(b" 120000"))
                    for line in diff_lines
                ),
            }
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
            units.append({"id": digest, **identity})
    return sorted(units, key=lambda item: item["path"])


def _scoped_supporting_units(manifest: dict[str, object]) -> list[dict[str, object]]:
    """Bind unchanged interaction and pinned-continuation source bytes independently of Git status."""
    paths = set()
    for assignment in manifest["interaction_assignments"]:
        paths.update(assignment["requirement"]["source_paths"])
    prior_paths: set[str] = set()
    if manifest.get("diff_base_oid"):
        prior = manifest.get("prior_findings")
        coverage = prior.get("coverage") if isinstance(prior, dict) else None
        if not isinstance(coverage, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("source_paths"), list)
            or any(not _literal_source_path(path) for path in item["source_paths"])
            for item in coverage
        ):
            raise ValueError("prior-coverage-invalid")
        for item in coverage:
            prior_paths.update(item["source_paths"])
        paths.update(prior_paths)
    paths.difference_update(manifest["inventory"])
    prior_paths.difference_update(manifest["inventory"])
    if not paths:
        return []
    records = capture_source_snapshot(Path(manifest["repository"]), sorted(paths))["files"]
    if manifest.get("diff_base_oid") and any(
        record["kind"] == "symlink"
        or record.get("encoding") == "base64"
        or (record.get("encoding") == "utf-8" and "\x00" in record["content"])
        for record in records
    ):
        raise ValueError("scoped-supporting-source-unsupported")
    present = {record["path"] for record in records if record["kind"] != "missing"}
    if not prior_paths.issubset(present):
        raise ValueError("scoped-prior-supporting-source-missing")
    return [
        {
            "path": record["path"],
            "kind": record["kind"],
            "encoding": record.get("encoding"),
            "record_sha256": hashlib.sha256(
                json.dumps(record, sort_keys=True, ensure_ascii=True).encode("utf-8")
            ).hexdigest(),
        }
        for record in records
    ]


def _validate_scoped_assignments(manifest: dict[str, object]) -> None:
    """Reject ambiguous assignments and require real captured records for interaction endpoints."""
    assignments = manifest["interaction_assignments"]
    audits = manifest["audit_assignments"]
    chunks = {chunk["id"]: chunk for chunk in manifest["chunks"]}
    if not isinstance(assignments, list) or not isinstance(audits, list) or not audits:
        raise ValueError("scoped-assignment-invalid")
    ids = []
    repository = Path(manifest["repository"])
    audit_ids = []
    audited_chunks = []
    for audit in audits:
        if not isinstance(audit, dict) or set(audit) != {"id", "chunk_ids"}:
            raise ValueError("scoped-audit-assignment-invalid")
        identity, chunk_ids = audit["id"], audit["chunk_ids"]
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or not isinstance(chunk_ids, list)
            or not chunk_ids
            or any(not isinstance(chunk_id, str) or chunk_id not in chunks for chunk_id in chunk_ids)
            or chunk_ids != sorted(set(chunk_ids))
        ):
            raise ValueError("scoped-audit-assignment-invalid")
        audit_ids.append(identity)
        audited_chunks.extend(chunk_ids)
    if audit_ids != sorted(set(audit_ids)) or sorted(audited_chunks) != sorted(chunks):
        raise ValueError("scoped-audit-assignment-invalid")
    for assignment in assignments:
        if not isinstance(assignment, dict) or set(assignment) != {"chunk_id", "requirement"}:
            raise ValueError("scoped-assignment-invalid")
        chunk_id = assignment["chunk_id"]
        requirement = assignment["requirement"]
        if not isinstance(chunk_id, str) or chunk_id not in chunks or not isinstance(requirement, dict):
            raise ValueError("scoped-assignment-invalid")
        if set(requirement) != {"id", "source_paths", "claim"}:
            raise ValueError("scoped-assignment-invalid")
        identity, paths, claim = requirement["id"], requirement["source_paths"], requirement["claim"]
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or identity == "interaction-inventory-audit"
            or not isinstance(paths, list)
            or not paths
            or any(not _literal_source_path(path) for path in paths)
            or paths != sorted(set(paths))
            or not set(paths).intersection(chunks[chunk_id]["scope_paths"])
            or not isinstance(claim, str)
            or not claim.strip()
        ):
            raise ValueError("scoped-assignment-invalid")
        records = capture_source_snapshot(repository, paths)["files"]
        if len(records) != len(paths) or any(
            record["kind"] == "missing" and record["path"] not in manifest["inventory"] for record in records
        ):
            raise ValueError("scoped-endpoint-missing")
        ids.append(identity)
    if ids != sorted(set(ids)):
        raise ValueError("scoped-assignment-invalid")


def scoped_plan(manifest_path: Path, assignments_path: Path, output: Path) -> Path:
    """Create an additive source-derived scoped inventory without changing its schema-one plan."""
    check(manifest_path, allow_draft=True)
    manifest = json.loads(manifest_path.read_bytes())
    assignments = json.loads(assignments_path.read_bytes())
    if not isinstance(assignments, dict) or set(assignments) != {"interactions", "audit_assignments"}:
        raise ValueError("scoped-assignment-invalid")
    destination = output.resolve()
    if destination.parent != manifest_path.resolve().parent or destination == manifest_path.resolve():
        raise ValueError("scoped-output-invalid")
    if "diff_base_oid" in manifest and "origin_repository" not in manifest:
        raise ValueError("chunk-origin-binding-required")
    manifest.pop("draft", None)
    manifest.update(
        schema_version=2,
        changed_units=_scoped_units(
            Path(manifest["repository"]), manifest["chunks"], manifest.get("diff_base_oid", "HEAD")
        ),
        interaction_assignments=assignments["interactions"],
        audit_assignments=assignments["audit_assignments"],
    )
    _validate_scoped_assignments(manifest)
    manifest["supporting_units"] = _scoped_supporting_units(manifest)
    if manifest.get("diff_base_oid") and manifest["supporting_units"]:
        support_paths = [unit["path"] for unit in manifest["supporting_units"]]
        origin_support = capture_source_snapshot(Path(manifest["origin_repository"]), support_paths)
        mirror_support = capture_source_snapshot(Path(manifest["repository"]), support_paths)
        if (
            origin_support["revision"] != mirror_support["revision"]
            or origin_support["files"] != mirror_support["files"]
            or len(origin_support["files"]) != len(support_paths)
        ):
            raise ValueError("chunk-origin-supporting-source-mismatch")
        support_bytes = (json.dumps(origin_support, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
        support_path = destination.with_name(destination.stem + "-origin-supporting-source.json")
        with support_path.open("xb") as stream:
            stream.write(support_bytes)
        manifest.update(
            origin_supporting_snapshot_path=support_path.name,
            origin_supporting_snapshot_sha256=hashlib.sha256(support_bytes).hexdigest(),
        )
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return destination


def plan(
    repository: Path,
    output: Path,
    scopes: list[str],
    budget_bytes: int,
    scope_mode: str,
    request: dict[str, str],
    prior_ledger: Path | None = None,
    prior_coverage: Path | None = None,
    caller_run: Path | None = None,
    diff_base_oid: str | None = None,
    origin_repository: Path | None = None,
) -> Path:
    """Freeze disjoint chunks and declare whether this review resumes a caller run."""
    if budget_bytes <= 0:
        raise ValueError("budget-must-be-positive")
    if (
        not isinstance(request, dict)
        or set(request) != {"goal", "specification", "done_when"}
        or any(not isinstance(value, str) or not value.strip() for value in request.values())
    ):
        raise ValueError("chunk-request-invalid")
    # Resolve the real directory name before Git's relative prefix; Windows normalizes trailing spaces.
    repository = repository.resolve(strict=True)
    prefix = os.fsdecode(_git(repository, "rev-parse", "--show-prefix").rstrip(b"\r\n"))
    # Git for Windows can omit a trailing space from --show-toplevel; climb from the supplied path instead.
    for _ in filter(None, prefix.split("/")):
        repository = repository.parent
    scopes = scopes or ["."]
    base = "HEAD"
    if diff_base_oid is not None:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", diff_base_oid):
            raise ValueError("chunk-diff-base-invalid")
        try:
            resolved = _git(repository, "rev-parse", "--verify", diff_base_oid + "^{commit}").decode("ascii").strip()
        except ValueError as exc:
            raise ValueError("chunk-diff-base-invalid") from exc
        if resolved != diff_base_oid:
            raise ValueError("chunk-diff-base-invalid")
        base = diff_base_oid
    inventory = _inventory(repository, scopes, scope_mode, base)
    # A base-only deletion has no current source record; fail before publishing partial coverage.
    if base != "HEAD":
        captured = {record["path"] for record in capture_source_snapshot(repository, inventory)["files"]}
        if not set(inventory).issubset(captured):
            raise ValueError("chunk-base-only-deletion-unsupported")
    output = _safe_output(repository, output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("chunk-output-already-populated")
    if (prior_ledger is None) != (prior_coverage is None):
        raise ValueError("prior-coverage-invalid")
    if caller_run is not None and prior_ledger is None:
        raise ValueError("prior-ledger-required-for-continuation")
    if prior_ledger is not None and caller_run is None:
        raise ValueError("caller-run-required-for-prior-ledger")
    if caller_run is not None:
        caller_run = caller_run.resolve()
    if caller_run is not None and prior_ledger.resolve() != (caller_run / "loop-ledger.json").resolve():
        raise ValueError("prior-ledger-caller-run-mismatch")
    prior = load_ledger(prior_ledger) if prior_ledger is not None else None
    if prior is not None and (validate_ledger(prior) or not prior["rounds"]):
        raise ValueError("prior-ledger-invalid")
    coverage = json.loads(prior_coverage.read_bytes()) if prior_coverage is not None else []

    groups: list[list[str]] = []
    current: list[str] = []
    for path in inventory:
        candidate = [*current, path]
        source, diff = _material(repository, candidate, base)
        if current and len(source) + len(diff) > budget_bytes:
            groups.append(current)
            current = [path]
        else:
            current = candidate
    if current:
        groups.append(current)

    chunk_ids = {path: f"chunk-{index:03d}" for index, paths in enumerate(groups, 1) for path in paths}
    signatures = {finding["signature"] for finding in prior["rounds"][-1]["findings"]} if prior else set()
    if not isinstance(coverage, list) or any(not isinstance(item, dict) for item in coverage):
        raise ValueError("prior-coverage-invalid")
    if any(not isinstance(item.get("signature"), str) for item in coverage):
        raise ValueError("prior-coverage-invalid")
    if {item["signature"] for item in coverage} != signatures:
        raise ValueError("prior-coverage-invalid")
    if len(coverage) != len(signatures):
        raise ValueError("prior-coverage-invalid")
    for item in coverage:
        if (
            not isinstance(item, dict)
            or set(item) != {"signature", "source_paths", "review_kind", "chunk_ids"}
            or not isinstance(item["source_paths"], list)
            or not item["source_paths"]
            or any(not _literal_source_path(path) for path in item["source_paths"])
            or item["source_paths"] != sorted(set(item["source_paths"]))
            or item["review_kind"] not in {"chunk", "interaction", "group"}
            or not isinstance(item["chunk_ids"], list)
            or len(item["chunk_ids"])
            != {"chunk": 1, "interaction": 2, "group": len(item["chunk_ids"])}[item["review_kind"]]
            or item["chunk_ids"] != sorted(set(item["chunk_ids"]))
            or any(chunk_id not in set(chunk_ids.values()) for chunk_id in item["chunk_ids"])
        ):
            raise ValueError("prior-coverage-invalid")
        if item["review_kind"] == "group" and len(item["chunk_ids"]) < 3:
            raise ValueError("prior-coverage-invalid")
    if prior:
        block = "\n" + _prior_request_block(prior, coverage)
        specification = request["specification"]
        if not specification.endswith(block):
            if "Prior findings to reassess:\n" in specification:
                raise ValueError("prior-request-block-conflict")
            request = request | {"specification": specification + block}

    output.mkdir(parents=True, exist_ok=True)
    prior_digest = None
    if prior_ledger is not None:
        pinned = output / "prior-loop-ledger.json"
        pinned.write_bytes(prior_ledger.read_bytes())
        if rounds_path(prior_ledger).is_file():
            rounds_path(pinned).write_bytes(rounds_path(prior_ledger).read_bytes())
        prior_digest = ledger_digest(pinned)
        if prior_digest != ledger_digest(prior_ledger):
            raise ValueError("prior-ledger-changed")
    chunks = []
    for index, paths in enumerate(groups, 1):
        source, diff = _material(repository, paths, base)
        name = f"chunk-{index:03d}"
        source_path = f"{name}-source.json"
        diff_path = f"{name}.diff"
        (output / source_path).write_bytes(source)
        (output / diff_path).write_bytes(diff)
        chunks.append(
            {
                "id": name,
                "scope_paths": paths,
                "source_path": source_path,
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "diff_path": diff_path,
                "diff_sha256": hashlib.sha256(diff).hexdigest(),
                "input_bytes": len(source) + len(diff),
                "oversize": len(source) + len(diff) > budget_bytes,
            }
        )
    manifest = {
        "schema_version": 1,
        "origin": {
            "kind": "continuation" if caller_run is not None else "first-run",
            "caller_run": str(caller_run) if caller_run is not None else None,
        },
        "repository": repository.as_posix(),
        "request": request,
        "requested_scope_paths": scopes,
        "scope_mode": scope_mode,
        "inventory": inventory,
        "budget_bytes": budget_bytes,
        "revision": _git(repository, "rev-parse", "HEAD").decode("ascii").strip(),
        "chunks": chunks,
        "prior_findings": {
            "ledger_path": "prior-loop-ledger.json" if prior_digest is not None else None,
            "ledger_sha256": prior_digest,
            "coverage": coverage,
        },
    }
    if diff_base_oid is not None:
        manifest.update(schema_version=2, diff_base_oid=diff_base_oid, draft=True)
        if origin_repository is not None:
            origin = origin_repository.resolve(strict=True)
            if (
                origin == repository
                or _git(repository, "branch", "--show-current").strip()
                or (origin / os.fsdecode(_git(origin, "rev-parse", "--git-common-dir").strip())).resolve()
                != (repository / os.fsdecode(_git(repository, "rev-parse", "--git-common-dir").strip())).resolve()
            ):
                raise ValueError("chunk-origin-repository-invalid")
            original = capture_source_snapshot(origin, inventory)
            mirror = capture_source_snapshot(repository, inventory)
            if original["repository"] != origin.as_posix() or mirror["repository"] != repository.as_posix():
                raise ValueError("chunk-origin-repository-invalid")
            if original["revision"] != mirror["revision"] or original["files"] != mirror["files"]:
                raise ValueError("chunk-origin-source-mismatch")
            if _inventory(origin, scopes, scope_mode, base) != inventory:
                raise ValueError("chunk-origin-inventory-mismatch")
            if _git(origin, "diff", base, "--binary", "--no-renames", "--", *inventory) != _git(
                repository, "diff", base, "--binary", "--no-renames", "--", *inventory
            ):
                raise ValueError("chunk-origin-diff-mismatch")
            original_bytes = (json.dumps(original, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
            (output / "origin-source.json").write_bytes(original_bytes)
            manifest.update(
                origin_repository=origin.as_posix(),
                origin_snapshot_path="origin-source.json",
                origin_snapshot_sha256=hashlib.sha256(original_bytes).hexdigest(),
                origin_index_sha256=hashlib.sha256(
                    _git(origin, "ls-files", "--stage", "-z", "--", *scopes)
                ).hexdigest(),
            )
    manifest_path = output / "chunks.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    check(manifest_path, allow_draft=diff_base_oid is not None)
    return manifest_path


def check(
    manifest_path: Path, results_path: Path | None = None, *, allow_draft: bool = False
) -> dict[str, object] | None:
    """Reject stale chunk evidence and summarize validated result references when supplied."""
    root = manifest_path.resolve().parent
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    base_keys = {
        "schema_version",
        "repository",
        "request",
        "requested_scope_paths",
        "scope_mode",
        "inventory",
        "budget_bytes",
        "revision",
        "chunks",
    }
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] not in {1, 2}
        or set(manifest)
        != base_keys
        | {"prior_findings", "origin"}
        | ({"diff_base_oid"} if "diff_base_oid" in manifest else set())
        | (
            {"origin_repository", "origin_snapshot_path", "origin_snapshot_sha256", "origin_index_sha256"}
            if "origin_repository" in manifest
            else set()
        )
        | (
            {"origin_supporting_snapshot_path", "origin_supporting_snapshot_sha256"}
            if "origin_supporting_snapshot_path" in manifest
            else set()
        )
        | (
            {"draft"}
            if manifest.get("draft") is True
            else {"changed_units", "interaction_assignments", "audit_assignments", "supporting_units"}
            if manifest["schema_version"] == 2
            else set()
        )
    ):
        raise ValueError("chunk-manifest-invalid")
    if "diff_base_oid" in manifest:
        base = manifest["diff_base_oid"]
        if (
            manifest["schema_version"] != 2
            or not isinstance(base, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base)
        ):
            raise ValueError("chunk-diff-base-invalid")
        if (
            _git(Path(manifest["repository"]), "rev-parse", "--verify", base + "^{commit}").decode("ascii").strip()
            != base
        ):
            raise ValueError("chunk-diff-base-invalid")
    else:
        base = "HEAD"
    if ("origin_repository" in manifest or manifest.get("draft") is True) and "diff_base_oid" not in manifest:
        raise ValueError("chunk-origin-binding-invalid")
    if manifest.get("draft") is True and not allow_draft:
        raise ValueError("scoped-comparison-profile-unverified")
    if manifest.get("diff_base_oid") is not None and not manifest.get("draft") and "origin_repository" not in manifest:
        raise ValueError("chunk-origin-binding-required")
    if "origin_repository" in manifest:
        source_repo = Path(manifest["origin_repository"])
        if not source_repo.is_absolute() or source_repo.resolve().as_posix() != manifest["origin_repository"]:
            raise ValueError("chunk-origin-repository-invalid")
        mirror_repo = Path(manifest["repository"])
        if (
            source_repo == mirror_repo
            or _git(mirror_repo, "branch", "--show-current").strip()
            or (source_repo / os.fsdecode(_git(source_repo, "rev-parse", "--git-common-dir").strip())).resolve()
            != (mirror_repo / os.fsdecode(_git(mirror_repo, "rev-parse", "--git-common-dir").strip())).resolve()
        ):
            raise ValueError("chunk-origin-repository-invalid")
        origin_bytes = _artifact(root, manifest["origin_snapshot_path"]).read_bytes()
        if hashlib.sha256(origin_bytes).hexdigest() != manifest["origin_snapshot_sha256"]:
            raise ValueError("chunk-origin-source-changed")
        saved = json.loads(origin_bytes)
        current = capture_source_snapshot(source_repo, manifest["inventory"])
        if current.get("repository") != source_repo.as_posix():
            raise ValueError("chunk-origin-repository-invalid")
        if (
            saved != current
            or hashlib.sha256(
                _git(source_repo, "ls-files", "--stage", "-z", "--", *manifest["requested_scope_paths"])
            ).hexdigest()
            != manifest["origin_index_sha256"]
            or _inventory(source_repo, manifest["requested_scope_paths"], manifest["scope_mode"], base)
            != manifest["inventory"]
        ):
            raise ValueError("chunk-origin-source-changed")
        if (
            saved != capture_source_snapshot(source_repo, manifest["inventory"])
            or hashlib.sha256(
                _git(source_repo, "ls-files", "--stage", "-z", "--", *manifest["requested_scope_paths"])
            ).hexdigest()
            != manifest["origin_index_sha256"]
        ):
            raise ValueError("chunk-origin-source-changed")
    origin = manifest["origin"]
    if not isinstance(origin, dict) or set(origin) != {"kind", "caller_run"}:
        raise ValueError("chunk-origin-invalid")
    prior = manifest["prior_findings"]
    if not isinstance(prior, dict):
        raise ValueError("prior-coverage-invalid")
    if origin["kind"] == "continuation":
        if (
            not isinstance(origin["caller_run"], str)
            or not origin["caller_run"].strip()
            or "\x00" in origin["caller_run"]
            or not Path(origin["caller_run"]).is_absolute()
        ):
            raise ValueError("chunk-origin-invalid")
        if prior.get("ledger_path") is None:
            raise ValueError("prior-ledger-required-for-continuation")
    elif origin != {"kind": "first-run", "caller_run": None}:
        raise ValueError("chunk-origin-invalid")
    elif prior.get("ledger_path") is not None:
        raise ValueError("chunk-origin-prior-ledger-mismatch")
    repository = Path(manifest["repository"])
    request = manifest["request"]
    scopes = manifest["requested_scope_paths"]
    scope_mode = manifest["scope_mode"]
    inventory = manifest["inventory"]
    chunks = manifest["chunks"]
    if (
        not repository.is_absolute()
        or not isinstance(request, dict)
        or set(request) != {"goal", "specification", "done_when"}
        or any(not isinstance(value, str) or not value.strip() for value in request.values())
        or not isinstance(scopes, list)
        or not scopes
        or not all(isinstance(path, str) for path in scopes)
        or scope_mode not in {"changed", "all"}
        or not isinstance(inventory, list)
        or not all(isinstance(path, str) for path in inventory)
        or not isinstance(chunks, list)
        or not chunks
    ):
        raise ValueError("chunk-manifest-invalid")
    if (
        _inventory(repository, scopes, scope_mode, base) != inventory
        or _git(repository, "rev-parse", "HEAD").decode("ascii").strip() != manifest["revision"]
    ):
        raise ValueError("chunk-inventory-changed")
    covered: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        if (
            not isinstance(chunk, dict)
            or set(chunk)
            != {
                "id",
                "scope_paths",
                "source_path",
                "source_sha256",
                "diff_path",
                "diff_sha256",
                "input_bytes",
                "oversize",
            }
            or chunk["id"] != f"chunk-{index:03d}"
        ):
            raise ValueError("chunk-manifest-invalid")
        paths = chunk["scope_paths"]
        if not isinstance(paths, list) or not paths or paths != sorted(set(paths)):
            raise ValueError("chunk-scope-invalid")
        covered.extend(paths)
        source, diff = _material(repository, paths, base)
        if (
            _artifact(root, chunk["source_path"]).read_bytes() != source
            or hashlib.sha256(source).hexdigest() != chunk["source_sha256"]
        ):
            raise ValueError("chunk-source-changed")
        if (
            _artifact(root, chunk["diff_path"]).read_bytes() != diff
            or hashlib.sha256(diff).hexdigest() != chunk["diff_sha256"]
        ):
            raise ValueError("chunk-diff-changed")
        total = len(source) + len(diff)
        if chunk["input_bytes"] != total or chunk["oversize"] is not (total > manifest["budget_bytes"]):
            raise ValueError("chunk-size-invalid")
    if sorted(covered) != inventory or len(covered) != len(set(covered)):
        raise ValueError("chunk-coverage-invalid")
    if manifest["schema_version"] == 2 and not manifest.get("draft"):
        if manifest["changed_units"] != _scoped_units(repository, chunks, base):
            raise ValueError("scoped-units-changed")
        _validate_scoped_assignments(manifest)
        if manifest["supporting_units"] != _scoped_supporting_units(manifest):
            raise ValueError("scoped-supporting-source-changed")
        if manifest.get("diff_base_oid") and manifest["supporting_units"]:
            if "origin_supporting_snapshot_path" not in manifest:
                raise ValueError("chunk-origin-supporting-source-unbound")
            support_bytes = _artifact(root, manifest["origin_supporting_snapshot_path"]).read_bytes()
            support_paths = [unit["path"] for unit in manifest["supporting_units"]]
            saved_support = json.loads(support_bytes)
            original_support = capture_source_snapshot(Path(manifest["origin_repository"]), support_paths)
            mirror_support = capture_source_snapshot(repository, support_paths)
            if (
                hashlib.sha256(support_bytes).hexdigest() != manifest["origin_supporting_snapshot_sha256"]
                or saved_support != original_support
                or original_support["revision"] != mirror_support["revision"]
                or original_support["files"] != mirror_support["files"]
            ):
                raise ValueError("chunk-origin-supporting-source-changed")
        elif "origin_supporting_snapshot_path" in manifest:
            raise ValueError("chunk-origin-supporting-source-invalid")
    _check_prior_manifest(root, manifest)
    if manifest["origin"]["kind"] == "continuation":
        caller_ledger = Path(manifest["origin"]["caller_run"]) / "loop-ledger.json"
        try:
            if caller_ledger.is_symlink() or not caller_ledger.is_file() or rounds_path(caller_ledger).is_symlink():
                raise ValueError("caller-ledger-unavailable")
            caller_digest = ledger_digest(caller_ledger)
        except (OSError, ValueError) as error:
            raise ValueError("caller-ledger-unavailable") from error
        copied = _prior_ledger_artifact(root, manifest["prior_findings"]["ledger_path"])
        if caller_digest != ledger_digest(copied):
            raise ValueError("caller-ledger-changed")
    if manifest.get("draft") is True and allow_draft:
        return None
    if manifest["schema_version"] == 2:
        # Only a source-bound, first-run text profile can reach independent review; other routes keep their gates.
        if not manifest.get("diff_base_oid") or not manifest.get("origin_repository") or origin["kind"] != "first-run":
            raise ValueError("scoped-comparison-profile-unverified")
        for checkout in (Path(manifest["origin_repository"]), repository):
            index_entries = _git(checkout, "ls-files", "-v", "-z", "--", *scopes).split(b"\0")
            if any(entry and (entry[:1] == b"S" or entry[:1].islower()) for entry in index_entries):
                raise ValueError("chunk-origin-index-flags-unsupported")
        if any(chunk["oversize"] for chunk in chunks):
            raise ValueError("scoped-oversize-source-unreviewed")
        if any(
            unit["kind"] != "file" or unit["encoding"] != "utf-8" or unit["binary_diff"] or unit["symlink_diff"]
            for unit in manifest["changed_units"]
        ):
            raise ValueError("scoped-unsupported-source-unreviewed")
        if any(unit["kind"] != "file" or unit["encoding"] != "utf-8" for unit in manifest["supporting_units"]):
            raise ValueError("scoped-supporting-source-unsupported")
        if results_path is None:
            return None
        return _check_scoped_results(root, manifest, manifest_bytes, results_path)
    if results_path is not None:
        runs, results_digest = _check_results(root, manifest, results_path)
        _check_prior_results(root, manifest, results_path)
        return {
            "schema_version": 1,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "results_sha256": results_digest,
            "runs": runs,
        }
    return None


def _check_prior_manifest(root: Path, manifest: dict[str, object]) -> None:
    """Pin every earlier signature and its declared source and reviewer assignment."""
    prior = manifest["prior_findings"]
    if not isinstance(prior, dict) or set(prior) != {"ledger_path", "ledger_sha256", "coverage"}:
        raise ValueError("prior-coverage-invalid")
    ledger_path = prior["ledger_path"]
    coverage = prior["coverage"]
    if ledger_path is None:
        if prior["ledger_sha256"] is not None or coverage != []:
            raise ValueError("prior-coverage-invalid")
        return
    ledger_file = _prior_ledger_artifact(root, ledger_path)
    if ledger_digest(ledger_file) != prior["ledger_sha256"]:
        raise ValueError("prior-ledger-changed")
    ledger = load_ledger(ledger_file)
    if validate_ledger(ledger) or not ledger["rounds"]:
        raise ValueError("prior-ledger-invalid")
    signatures = {finding["signature"] for finding in ledger["rounds"][-1]["findings"]}
    if not isinstance(coverage, list) or len(coverage) != len(signatures):
        raise ValueError("prior-coverage-invalid")
    by_id = {chunk["id"]: chunk for chunk in manifest["chunks"]}
    seen = set()
    for item in coverage:
        if not isinstance(item, dict) or set(item) != {"signature", "source_paths", "review_kind", "chunk_ids"}:
            raise ValueError("prior-coverage-invalid")
        signature = item["signature"]
        paths = item["source_paths"]
        ids = item["chunk_ids"]
        kind = item["review_kind"]
        if (
            not isinstance(signature, str)
            or signature not in signatures
            or signature in seen
            or not isinstance(paths, list)
            or not paths
            or paths != sorted(set(paths))
            or any(not _literal_source_path(path) for path in paths)
            or kind not in {"chunk", "interaction", "group"}
            or not isinstance(ids, list)
            or ids != sorted(set(ids))
            or any(chunk_id not in by_id for chunk_id in ids)
            or len(ids) != {"chunk": 1, "interaction": 2, "group": len(ids)}[kind]
            or (kind == "group" and len(ids) < 3)
        ):
            raise ValueError("prior-coverage-invalid")
        seen.add(signature)
    if not manifest["request"]["specification"].endswith("\n" + _prior_request_block(ledger, coverage)):
        raise ValueError("prior-request-block-missing")


def _check_prior_results(root: Path, manifest: dict[str, object], results_path: Path) -> None:
    """Require the assigned current reviewer to assess each earlier signature."""
    results = json.loads(results_path.read_bytes())
    prior_path = manifest["prior_findings"]["ledger_path"]
    if prior_path is None:
        return
    prior = load_ledger(_prior_ledger_artifact(root, prior_path))
    expected = {finding["signature"]: finding for finding in prior["rounds"][-1]["findings"]}
    for item in manifest["prior_findings"]["coverage"]:
        issue = _prior_result_issue(root, results, item, expected[item["signature"]])
        if issue is not None:
            raise ValueError(f"{issue}:{item['signature']}")


def _prior_result_issue(
    root: Path,
    results: dict[str, object],
    item: dict[str, object],
    expected: dict[str, object],
    *,
    require_current_review: bool = False,
) -> str | None:
    """Return why one assigned reviewer has not verified a prior signature on the required source."""
    entries = {
        "chunk": {(entry["id"],): entry for entry in results["chunks"]},
        "interaction": {tuple(entry["chunk_ids"]): entry for entry in results["interactions"]},
        "group": {tuple(entry["chunk_ids"]): entry for entry in results["groups"]},
    }
    entry = entries[item["review_kind"]].get(tuple(item["chunk_ids"]))
    if entry is None or entry["result_path"] is None:
        return "prior-review-assignment-missing"
    result_path = _artifact(root, entry["result_path"])
    run = result_path.parent
    evidence = json.loads((run / "loop-evidence.json").read_bytes())
    visible: set[str] = set()
    for source_path in (evidence.get("current_source_path"), evidence.get("current_supporting_source_path")):
        if source_path is None:
            continue
        snapshot = json.loads(_artifact(run, source_path).read_bytes())
        visible.update(record["path"] for record in snapshot["files"])
    if not set(item["source_paths"]).issubset(visible):
        return "prior-source-coverage-incomplete"
    ledger_path = run / "loop-ledger.json"
    if not ledger_path.is_file():
        return "prior-finding-not-resolved"
    ledger = load_ledger(ledger_path)
    if not ledger["rounds"]:
        return "prior-finding-not-resolved"
    if require_current_review:
        # A stopped child may retain a real earlier review while its source has changed since that round.
        final_round = ledger["rounds"][-1]
        evidence_rounds = evidence.get("rounds")
        if not isinstance(evidence_rounds, list) or not evidence_rounds:
            return "prior-finding-review-stale"
        final_evidence = evidence_rounds[-1]
        try:
            review_source = _artifact(run, final_evidence["source_path"]).read_bytes()
            current_source = _artifact(run, evidence["current_source_path"]).read_bytes()
            review_diff = (run / f"round-{final_round['index']}.diff").read_bytes()
            current_diff = (run / "current.diff").read_bytes()
            current_supporting = evidence.get("current_supporting_source_path")
            reviewed_supporting = final_evidence.get("supporting_source_path")
            support_current = current_supporting is None and reviewed_supporting is None
            if current_supporting is not None and reviewed_supporting is not None:
                support_current = (
                    _artifact(run, current_supporting).read_bytes() == _artifact(run, reviewed_supporting).read_bytes()
                )
        except (KeyError, OSError, ValueError, TypeError):
            return "prior-finding-review-stale"
        if review_source != current_source or review_diff != current_diff or not support_current:
            return "prior-finding-review-stale"
    findings = ledger["rounds"][-1]["findings"]
    matched = [finding for finding in findings if finding["signature"] == item["signature"]]
    if len(matched) != 1 or matched[0]["disposition"] not in {"verified-fixed", "rejected"}:
        return "prior-finding-not-resolved"
    if any(matched[0][key] != expected[key] for key in ("tier", "structural")):
        return "prior-finding-attribution-mismatch"
    return None


def check_stopped(manifest_path: Path, results_path: Path) -> dict[str, object]:
    """Summarize validated partial child results and the review slots still pending."""
    check(manifest_path)
    root = manifest_path.resolve().parent
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    results_bytes = results_path.read_bytes()
    results = json.loads(results_bytes)
    if (
        not isinstance(results, dict)
        or set(results) != {"schema_version", "chunks", "interactions", "groups"}
        or type(results["schema_version"]) is not int
        or results["schema_version"] != 1
        or any(not isinstance(results[key], list) for key in ("chunks", "interactions", "groups"))
    ):
        raise ValueError("chunk-stopped-results-invalid")
    chunks = manifest["chunks"]
    by_id = {chunk["id"]: chunk for chunk in chunks}
    order = list(by_id)
    expected_pairs = list(itertools.combinations(order, 2))
    runs: list[dict[str, object]] = []
    result_refs: set[str] = set()
    supplied_chunks: set[str] = set()
    for entry in results["chunks"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"id", "result_path"}
            or not isinstance(entry["id"], str)
            or entry["id"] not in by_id
            or entry["id"] in supplied_chunks
            or not isinstance(entry["result_path"], str)
        ):
            raise ValueError("chunk-stopped-chunks-invalid")
        supplied_chunks.add(entry["id"])
        chunk = by_id[entry["id"]]
        result_ref = entry["result_path"]
        if result_ref in result_refs:
            raise ValueError("chunk-result-path-reused")
        result_refs.add(result_ref)
        runs.append(
            _check_clean_run(
                root,
                manifest["repository"],
                chunk["scope_paths"],
                _artifact(root, chunk["source_path"]).read_bytes(),
                _artifact(root, chunk["diff_path"]).read_bytes(),
                result_ref,
                chunk["id"],
                manifest["request"],
                require_clean=False,
            )
        )

    reviewed_runs: dict[str, set[str]] = {}
    run_decisions: dict[str, list[dict[str, object]]] = {}
    run_labels: dict[str, str] = {}
    supplied_pairs: set[tuple[str, str]] = set()
    declared_pairs: set[tuple[str, str]] = set()
    for entry in results["interactions"]:
        if not isinstance(entry, dict) or set(entry) != {"chunk_ids", "decision", "evidence", "result_path"}:
            raise ValueError("chunk-stopped-interactions-invalid")
        pair = entry["chunk_ids"]
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(chunk_id, str) for chunk_id in pair)
            or tuple(pair) not in expected_pairs
            or tuple(pair) in declared_pairs
            or entry["decision"] not in {"reviewed", "no-interaction"}
            or not isinstance(entry["evidence"], str)
            or not entry["evidence"].strip()
            or (entry["result_path"] is not None and not isinstance(entry["result_path"], str))
        ):
            raise ValueError("chunk-stopped-interactions-invalid")
        declared_pairs.add(tuple(pair))
        result_ref = entry["result_path"]
        if result_ref is None:
            continue
        supplied_pairs.add(tuple(pair))
        reviewed_runs.setdefault(result_ref, set()).update(
            path for chunk_id in pair for path in by_id[chunk_id]["scope_paths"]
        )
        run_decisions.setdefault(result_ref, []).append(
            {"chunk_ids": pair, "decision": entry["decision"], "evidence": entry["evidence"]}
        )
        run_labels.setdefault(result_ref, "+".join(pair))

    supplied_groups: set[tuple[str, ...]] = set()
    pending_groups: list[list[str]] = []
    for entry in results["groups"]:
        if not isinstance(entry, dict) or set(entry) != {"chunk_ids", "evidence", "result_path"}:
            raise ValueError("chunk-stopped-groups-invalid")
        ids = entry["chunk_ids"]
        if (
            not isinstance(ids, list)
            or len(ids) < 3
            or not all(isinstance(chunk_id, str) and chunk_id in by_id for chunk_id in ids)
            or ids != [chunk_id for chunk_id in order if chunk_id in ids]
            or len(ids) != len(set(ids))
            or tuple(ids) in supplied_groups
            or not isinstance(entry["evidence"], str)
            or not entry["evidence"].strip()
            or (entry["result_path"] is not None and not isinstance(entry["result_path"], str))
        ):
            raise ValueError("chunk-stopped-groups-invalid")
        supplied_groups.add(tuple(ids))
        result_ref = entry["result_path"]
        if result_ref is None:
            pending_groups.append(ids)
            continue
        reviewed_runs.setdefault(result_ref, set()).update(
            path for chunk_id in ids for path in by_id[chunk_id]["scope_paths"]
        )
        run_decisions.setdefault(result_ref, []).append(
            {"chunk_ids": ids, "decision": "reviewed", "evidence": entry["evidence"]}
        )
        run_labels.setdefault(result_ref, "+".join(ids))

    for result_ref, involved_paths in reviewed_runs.items():
        if result_ref in result_refs:
            raise ValueError("chunk-result-path-reused")
        result_refs.add(result_ref)
        paths = sorted(involved_paths)
        source, diff = _material(Path(manifest["repository"]), paths)
        runs.append(
            _check_clean_run(
                root,
                manifest["repository"],
                paths,
                source,
                diff,
                result_ref,
                run_labels[result_ref],
                manifest["request"],
                run_decisions[result_ref],
                require_clean=False,
            )
        )
    if not runs:
        raise ValueError("chunk-stopped-results-empty")
    pending_chunks = [chunk_id for chunk_id in order if chunk_id not in supplied_chunks]
    pending_interactions = [list(pair) for pair in expected_pairs if pair not in supplied_pairs]
    prior_path = manifest["prior_findings"]["ledger_path"]
    expected = {}
    if prior_path is not None:
        prior = load_ledger(_prior_ledger_artifact(root, prior_path))
        expected = {finding["signature"]: finding for finding in prior["rounds"][-1]["findings"]}
    pending_prior_signatures = [
        item["signature"]
        for item in manifest["prior_findings"]["coverage"]
        if _prior_result_issue(root, results, item, expected[item["signature"]], require_current_review=True)
        is not None
    ]
    if not (
        pending_chunks
        or pending_interactions
        or pending_groups
        or pending_prior_signatures
        or any(run["reason"] != "clean" for run in runs)
    ):
        raise ValueError("chunk-stopped-results-clean")
    return {
        "schema_version": 1,
        "status": "stopped",
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "results_sha256": hashlib.sha256(results_bytes).hexdigest(),
        "runs": runs,
        "pending_chunks": pending_chunks,
        "pending_interactions": pending_interactions,
        "pending_groups": sorted(pending_groups),
        "pending_prior_signatures": pending_prior_signatures,
    }


def _check_results(root: Path, manifest: dict[str, object], results_path: Path) -> tuple[list[dict[str, object]], str]:
    """Require clean chunks and return their ordered validated score references."""
    results_bytes = results_path.read_bytes()
    results = json.loads(results_bytes)
    chunks = manifest["chunks"]
    if (
        not isinstance(results, dict)
        or set(results) != {"schema_version", "chunks", "interactions", "groups"}
        or type(results["schema_version"]) is not int
        or results["schema_version"] != 1
        or not isinstance(results["chunks"], list)
        or not isinstance(results["interactions"], list)
        or not isinstance(results["groups"], list)
        or not isinstance(chunks, list)
        or len(results["chunks"]) != len(chunks)
    ):
        raise ValueError("chunk-results-invalid")
    expected_pairs = list(itertools.combinations((chunk["id"] for chunk in chunks), 2))
    interactions = results["interactions"]
    if len(interactions) != len(expected_pairs):
        raise ValueError("chunk-interaction-coverage-invalid")
    for chunk, entry in zip(chunks, results["chunks"], strict=True):
        if not isinstance(entry, dict) or set(entry) != {"id", "result_path"}:
            raise ValueError("chunk-results-invalid")
        if entry["id"] != chunk["id"]:
            raise ValueError("chunk-results-coverage-invalid")
    by_id = {chunk["id"]: chunk for chunk in chunks}
    reviewed_runs: dict[str, set[str]] = {}
    run_decisions: dict[str, list[dict[str, object]]] = {}
    run_labels: dict[str, str] = {}
    for pair, entry in zip(expected_pairs, interactions, strict=True):
        if not isinstance(entry, dict) or entry.get("chunk_ids") != list(pair):
            raise ValueError("chunk-interaction-coverage-invalid")
        if (
            set(entry) != {"chunk_ids", "decision", "evidence", "result_path"}
            or entry.get("decision") not in {"reviewed", "no-interaction"}
            or not isinstance(entry.get("evidence"), str)
            or not entry["evidence"].strip()
            or not isinstance(entry.get("result_path"), str)
        ):
            raise ValueError("chunk-interaction-decision-invalid")
        result_ref = entry["result_path"]
        reviewed_runs.setdefault(result_ref, set()).update(
            path for chunk_id in pair for path in by_id[chunk_id]["scope_paths"]
        )
        run_decisions.setdefault(result_ref, []).append(
            {"chunk_ids": list(pair), "decision": entry["decision"], "evidence": entry["evidence"]}
        )
        run_labels.setdefault(result_ref, "+".join(pair))
    previous_group: tuple[str, ...] = ()
    chunk_order = list(by_id)
    for entry in results["groups"]:
        if not isinstance(entry, dict):
            raise ValueError("chunk-interaction-group-invalid")
        ids = entry.get("chunk_ids")
        if (
            set(entry) != {"chunk_ids", "evidence", "result_path"}
            or not isinstance(ids, list)
            or len(ids) < 3
            or not all(isinstance(chunk_id, str) and chunk_id in by_id for chunk_id in ids)
            or ids != [chunk_id for chunk_id in chunk_order if chunk_id in ids]
            or len(ids) != len(set(ids))
            or tuple(ids) <= previous_group
            or not isinstance(entry.get("evidence"), str)
            or not entry["evidence"].strip()
            or not isinstance(entry.get("result_path"), str)
        ):
            raise ValueError("chunk-interaction-group-invalid")
        previous_group = tuple(ids)
        result_ref = entry["result_path"]
        reviewed_runs.setdefault(result_ref, set()).update(
            path for chunk_id in ids for path in by_id[chunk_id]["scope_paths"]
        )
        run_decisions.setdefault(result_ref, []).append(
            {"chunk_ids": ids, "decision": "reviewed", "evidence": entry["evidence"]}
        )
        run_labels.setdefault(result_ref, "+".join(ids))
    result_paths = [entry["result_path"] for entry in results["chunks"]]
    if not all(isinstance(path, str) for path in result_paths):
        raise ValueError("chunk-result-path-invalid")
    if len(result_paths) != len(set(result_paths)) or set(result_paths).intersection(reviewed_runs):
        raise ValueError("chunk-result-path-reused")

    runs = []
    for chunk, entry in zip(chunks, results["chunks"], strict=True):
        runs.append(
            _check_clean_run(
                root,
                manifest["repository"],
                chunk["scope_paths"],
                _artifact(root, chunk["source_path"]).read_bytes(),
                _artifact(root, chunk["diff_path"]).read_bytes(),
                entry["result_path"],
                chunk["id"],
                manifest["request"],
            )
        )
    for result_ref, involved_paths in reviewed_runs.items():
        paths = sorted(involved_paths)
        source, diff = _material(Path(manifest["repository"]), paths)
        runs.append(
            _check_clean_run(
                root,
                manifest["repository"],
                paths,
                source,
                diff,
                result_ref,
                run_labels[result_ref],
                manifest["request"],
                run_decisions[result_ref],
            )
        )
    return runs, hashlib.sha256(results_bytes).hexdigest()


def _scoped_audit_requirements(manifest: dict[str, object], audit: dict[str, object]) -> list[dict[str, object]]:
    """Bind an independent auditor to its source units and the complete interaction inventory."""
    inventory = {
        "changed_units": manifest["changed_units"],
        "supporting_units": manifest["supporting_units"],
        "interaction_assignments": manifest["interaction_assignments"],
    }
    units = [unit for unit in manifest["changed_units"] if unit["chunk_id"] in audit["chunk_ids"]]
    audit_requirement = {
        "id": "interaction-inventory-audit",
        "source_paths": sorted(unit["path"] for unit in units),
        "claim": "Assess this complete source-derived inventory and declared interactions: "
        + json.dumps(inventory, sort_keys=True, ensure_ascii=True),
        "unit_ids": sorted(unit["id"] for unit in units),
    }
    material = [
        assignment["requirement"]
        for assignment in manifest["interaction_assignments"]
        if assignment["chunk_id"] in audit["chunk_ids"]
    ]
    return sorted([audit_requirement, *material], key=lambda item: item["id"])


def _scoped_review_identities(root: Path, result_path: str) -> set[str]:
    """Read every reviewer identity from an already authenticated child result."""
    run = _artifact(root, result_path).parent
    ledger = load_ledger(run / "loop-ledger.json")
    identities = [round_["reviewer"]["identity"] for round_ in ledger["rounds"]]
    if not identities or any(not isinstance(identity, str) or not identity.strip() for identity in identities):
        raise ValueError("scoped-reviewer-identity-invalid")
    return set(identities)


def _scoped_delivered_paths(root: Path, result_path: str) -> set[str]:
    """Return records from current snapshots bound by the child's evidence validator."""
    run = _artifact(root, result_path).parent
    evidence = json.loads((run / "loop-evidence.json").read_bytes())
    visible = set()
    for field in ("current_source_path", "current_supporting_source_path"):
        source = evidence.get(field)
        if source is not None:
            snapshot = json.loads(_artifact(run, source).read_bytes())
            visible.update(record["path"] for record in snapshot["files"])
    return visible


def _check_scoped_results(
    root: Path, manifest: dict[str, object], manifest_bytes: bytes, results_path: Path
) -> dict[str, object]:
    """Require every scoped child and independently sourced audits of all change units."""
    results_bytes = results_path.read_bytes()
    results = json.loads(results_bytes)
    chunks = manifest["chunks"]
    if (
        not isinstance(results, dict)
        or set(results) != {"schema_version", "chunks", "audits"}
        or type(results.get("schema_version")) is not int
        or results["schema_version"] != 2
        or not isinstance(results["chunks"], list)
        or len(results["chunks"]) != len(chunks)
        or not isinstance(results["audits"], list)
        or len(results["audits"]) != len(manifest["audit_assignments"])
    ):
        raise ValueError("scoped-results-invalid")
    if any(chunk["oversize"] for chunk in chunks):
        raise ValueError("scoped-oversize-source-unreviewed")
    if any(
        unit["kind"] == "symlink" or unit["encoding"] == "base64" or unit["binary_diff"] or unit["symlink_diff"]
        for unit in manifest["changed_units"]
    ):
        raise ValueError("scoped-unsupported-source-unreviewed")
    if any(unit["kind"] == "symlink" or unit["encoding"] == "base64" for unit in manifest["supporting_units"]):
        raise ValueError("scoped-supporting-source-unsupported")
    result_paths = []
    for chunk, result in zip(chunks, results["chunks"], strict=True):
        if (
            not isinstance(result, dict)
            or set(result) != {"id", "result_path"}
            or result["id"] != chunk["id"]
            or not isinstance(result["result_path"], str)
        ):
            raise ValueError("scoped-results-coverage-invalid")
        result_paths.append(result["result_path"])
    for audit, result in zip(manifest["audit_assignments"], results["audits"], strict=True):
        if (
            not isinstance(result, dict)
            or set(result) != {"id", "result_path"}
            or result["id"] != audit["id"]
            or not isinstance(result["result_path"], str)
        ):
            raise ValueError("scoped-audit-results-coverage-invalid")
        result_paths.append(result["result_path"])
    if len(result_paths) != len(set(result_paths)):
        raise ValueError("scoped-result-path-reused")

    repository = Path(manifest["repository"])
    runs = []
    reviewers = {}
    child_paths = {}
    for chunk, result in zip(chunks, results["chunks"], strict=True):
        path = result["result_path"]
        requirements = [
            assignment["requirement"]
            for assignment in manifest["interaction_assignments"]
            if assignment["chunk_id"] == chunk["id"]
        ]
        runs.append(
            _check_clean_run(
                root,
                manifest["repository"],
                chunk["scope_paths"],
                _artifact(root, chunk["source_path"]).read_bytes(),
                _artifact(root, chunk["diff_path"]).read_bytes(),
                path,
                chunk["id"],
                manifest["request"],
                interaction_requirements=requirements,
                expected_base=manifest.get("diff_base_oid"),
            )
        )
        delivered = _scoped_delivered_paths(root, path)
        if any(not set(requirement["source_paths"]).issubset(delivered) for requirement in requirements):
            raise ValueError(f"scoped-interaction-endpoint-missing:{chunk['id']}")
        reviewers[chunk["id"]] = _scoped_review_identities(root, path)
        child_paths[chunk["id"]] = path

    for audit, result in zip(manifest["audit_assignments"], results["audits"], strict=True):
        audit_paths = sorted(
            unit["path"] for unit in manifest["changed_units"] if unit["chunk_id"] in audit["chunk_ids"]
        )
        audit_source, audit_diff = _material(repository, audit_paths, manifest.get("diff_base_oid", "HEAD"))
        requirements = _scoped_audit_requirements(manifest, audit)
        path = result["result_path"]
        runs.append(
            _check_clean_run(
                root,
                manifest["repository"],
                audit_paths,
                audit_source,
                audit_diff,
                path,
                audit["id"],
                manifest["request"],
                interaction_requirements=requirements,
                expected_base=manifest.get("diff_base_oid"),
            )
        )
        delivered = _scoped_delivered_paths(root, path)
        if any(not set(requirement["source_paths"]).issubset(delivered) for requirement in requirements):
            raise ValueError(f"scoped-audit-endpoint-missing:{audit['id']}")
        audited_reviewers = set().union(*(reviewers[chunk_id] for chunk_id in audit["chunk_ids"]))
        if _scoped_review_identities(root, path).intersection(audited_reviewers):
            raise ValueError("scoped-auditor-not-independent")

    prior_path = manifest["prior_findings"]["ledger_path"]
    if prior_path is not None:
        prior = load_ledger(_prior_ledger_artifact(root, prior_path))
        expected = {finding["signature"]: finding for finding in prior["rounds"][-1]["findings"]}
        prior_map = {
            "chunks": [{"id": key, "result_path": value} for key, value in child_paths.items()],
            "interactions": [],
            "groups": [],
        }
        for item in manifest["prior_findings"]["coverage"]:
            if item["review_kind"] != "chunk":
                key = "interactions" if item["review_kind"] == "interaction" else "groups"
                prior_map[key].append(
                    {"chunk_ids": item["chunk_ids"], "result_path": child_paths[item["chunk_ids"][0]]}
                )
            issue = _prior_result_issue(root, prior_map, item, expected[item["signature"]])
            if issue is not None:
                raise ValueError(f"{issue}:{item['signature']}")
    return {
        "schema_version": 2,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "results_sha256": hashlib.sha256(results_bytes).hexdigest(),
        "runs": runs,
        "coverage_limit": "Independent source-aware review cannot prove every semantic interaction was discovered.",
    }


def _check_clean_run(
    root: Path,
    repository: str,
    paths: list[str],
    source: bytes,
    diff: bytes,
    result_ref: str,
    label: str,
    coordinator_request: dict[str, str],
    decisions: list[dict[str, object]] | None = None,
    *,
    require_clean: bool = True,
    interaction_requirements: list[dict[str, object]] | None = None,
    expected_base: str | None = None,
) -> dict[str, object]:
    """Bind one validated loop to its request and source, requiring clean when requested."""
    result_path = _artifact(root, result_ref)
    if result_path.name != "result.json":
        raise ValueError("chunk-result-path-invalid")
    run = result_path.parent
    result = json.loads(result_path.read_text(encoding="utf-8"))
    loop = result.get("metadata", {}).get("adversarial_loop", {})
    if require_clean and (result.get("status") != "pass" or loop.get("reason") != "clean"):
        raise ValueError(f"chunk-result-not-clean:{label}")
    if not require_clean and (result.get("status") not in {"pass", "fail"} or not isinstance(loop.get("reason"), str)):
        raise ValueError(f"chunk-result-status-invalid:{label}")
    scores = loop.get("scores")
    rounds = loop.get("rounds")
    if not require_clean and scores == [] and rounds == []:
        raise ValueError(f"chunk-result-review-missing:{label}")
    if (
        not isinstance(scores, list)
        or (require_clean and not scores)
        or any(type(score) is not int or score < 0 for score in scores)
        or (require_clean and scores[-1] != 0)
        or not isinstance(rounds, list)
        or len(rounds) != len(scores)
        or any(
            not isinstance(round_record, dict) or not isinstance(round_record.get("decision"), str)
            for round_record in rounds
        )
        or (require_clean and rounds[-1]["decision"] != "clean")
    ):
        raise ValueError(f"chunk-result-score-invalid:{label}")
    evidence = json.loads((run / "loop-evidence.json").read_text(encoding="utf-8"))
    if evidence.get("diff_base_oid") != expected_base:
        raise ValueError(f"chunk-result-diff-base-mismatch:{label}")
    if evidence.get("repository") != repository or evidence.get("scope_paths") != paths:
        raise ValueError(f"chunk-result-scope-mismatch:{label}")
    request = evidence.get("request")
    if (
        evidence.get("schema_version") not in {2, 3}
        or not isinstance(request, dict)
        or set(request) != {"goal", "specification", "done_when"}
        or any(not isinstance(value, str) or not value.strip() for value in request.values())
    ):
        raise ValueError(f"chunk-result-request-missing:{label}")
    if decisions is not None and interaction_requirements is not None:
        raise ValueError("chunk-assessment-contract-conflict")
    if interaction_requirements is not None:
        assessment = "Interaction assessment:\n" + json.dumps(
            interaction_requirements, sort_keys=True, ensure_ascii=True
        )
        expected_request = coordinator_request | {
            "specification": coordinator_request["specification"] + "\n" + assessment
        }
        if evidence.get("schema_version") != 3 or evidence.get("interaction_requirements") != interaction_requirements:
            raise ValueError(f"scoped-interaction-requirements-mismatch:{label}")
    elif decisions is not None:
        assessment = "Interaction assessment:\n" + json.dumps(decisions, sort_keys=True, ensure_ascii=True)
        if assessment not in request["specification"]:
            raise ValueError(f"chunk-interaction-assessment-missing:{label}")
        expected_request = coordinator_request | {
            "specification": coordinator_request["specification"] + "\n" + assessment
        }
    else:
        expected_request = coordinator_request
    if request != expected_request:
        raise ValueError(f"chunk-result-request-mismatch:{label}")
    if _artifact(run, evidence.get("current_source_path")).read_bytes() != source:
        raise ValueError(f"chunk-result-source-mismatch:{label}")
    if (run / "current.diff").read_bytes() != diff:
        raise ValueError(f"chunk-result-diff-mismatch:{label}")
    validator = Path(__file__).resolve().parents[2] / "shared" / "validate-artifacts.py"
    validated = subprocess.run(  # noqa: S603 - argv list, no shell
        [
            sys.executable,
            str(validator),
            "--skill",
            "challenge-resolve",
            "--out",
            str(run),
            "--result",
            str(result_path),
        ],
        capture_output=True,
        check=False,
    )
    if validated.returncode:
        raise ValueError(f"chunk-result-validation-failed:{label}")
    summary = {"result_path": result_ref, "scores": scores, "decision": rounds[-1]["decision"] if rounds else "not-run"}
    if not require_clean:
        summary["reason"] = loop["reason"]
    return summary


def main() -> int:
    """Expose planning and freshness checks without running a reviewer."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    planner = commands.add_parser("plan")
    planner.add_argument("--repository", type=Path, required=True)
    planner.add_argument("--out", type=Path, required=True)
    planner.add_argument("--scope-path", action="append", default=[])
    planner.add_argument("--scope-mode", choices=("changed", "all"), default="changed")
    planner.add_argument("--diff-base", help="Full immutable comparison commit ID")
    planner.add_argument("--origin-repository", type=Path, help="Original checkout backing the detached review mirror")
    planner.add_argument("--budget-bytes", type=int, default=DEFAULT_BUDGET_BYTES)
    planner.add_argument("--goal", required=True)
    planner.add_argument("--specification", required=True)
    planner.add_argument("--done-when", required=True)
    planner.add_argument(
        "--prior-ledger", type=Path, help="Parent-authenticated earlier loop-ledger.json for continuation"
    )
    planner.add_argument("--prior-coverage", type=Path, help="JSON assignments for every prior final-round signature")
    planner.add_argument("--caller-run", type=Path, help="Existing run whose reviewed findings this plan continues")
    scoped = commands.add_parser("scoped-plan")
    scoped.add_argument("--manifest", type=Path, required=True)
    scoped.add_argument("--assignments", type=Path, required=True)
    scoped.add_argument("--out", type=Path, required=True)
    checker = commands.add_parser("check")
    checker.add_argument("--manifest", type=Path, required=True)
    checker.add_argument("--results", type=Path, help="Ordered chunk result map for final coverage validation")
    checker.add_argument("--summary", action="store_true", help="Print validated child-backed JSON; requires --results")
    stopped = commands.add_parser("check-stopped")
    stopped.add_argument("--manifest", type=Path, required=True)
    stopped.add_argument("--results", type=Path, required=True)
    stopped.add_argument("--summary", action="store_true", help="Print validated partial child-backed JSON")
    arguments = parser.parse_args()
    try:
        if arguments.command == "plan":
            print(
                plan(
                    arguments.repository,
                    arguments.out,
                    arguments.scope_path,
                    arguments.budget_bytes,
                    arguments.scope_mode,
                    {
                        "goal": arguments.goal,
                        "specification": arguments.specification,
                        "done_when": arguments.done_when,
                    },
                    arguments.prior_ledger,
                    arguments.prior_coverage,
                    arguments.caller_run,
                    arguments.diff_base,
                    arguments.origin_repository,
                )
            )
        elif arguments.command == "scoped-plan":
            print(scoped_plan(arguments.manifest, arguments.assignments, arguments.out))
        elif arguments.command == "check-stopped":
            summary = check_stopped(arguments.manifest, arguments.results)
            if arguments.summary:
                print(json.dumps(summary, sort_keys=True, ensure_ascii=True))
            else:
                print("stopped chunk evidence current; pending review coverage listed in --summary")
        else:
            if arguments.summary and arguments.results is None:
                raise ValueError("chunk-summary-results-required")
            summary = check(arguments.manifest, arguments.results)
            if arguments.summary:
                print(json.dumps(summary, sort_keys=True, ensure_ascii=True))
            elif arguments.results is None:
                print("chunk source inventory current")
            else:
                print("declared interaction evidence current; undeclared interactions unverified")
    except (OSError, ValueError, RuntimeError, UnicodeError, json.JSONDecodeError) as error:
        print(f"chunk-diff-error:{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
