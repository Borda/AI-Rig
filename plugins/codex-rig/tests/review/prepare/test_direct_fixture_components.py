"""Verify bounded fixture admission through real direct-review producers.

A synthetic secret-looking fixture may be admitted per verified component instead of attesting a whole input. These
tests drive the real discovery, binding-export and plan-producing steps (and, where it matters, a real git repository
and the native assembler) to show that approval is bound to the exact component, review context and source comparison.
"""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import shutil
import subprocess
from collections.abc import Callable
from itertools import accumulate
from pathlib import Path
from typing import Any

import pytest
import test_review_direct as direct
from _git_fixtures import git_bytes
from test_review_prepare import HELPER, _build_review_inputs, _prepare

COMPONENT_SEPARATOR = "\n--- UTF-8 component ---\n"
KNOWN_COMPONENTS = {"direct-plan:file:1", "direct-diff:section:0", "direct-source:1"}
SENSITIVE = "review-direct-sensitive-material"


def _producer() -> dict[str, Any]:
    """Load the direct-review producer's namespace the way the script runs it."""
    return runpy.run_path(str(HELPER))


def _has_matches(item: dict[str, Any]) -> bool:
    """Select discovery entries that contain a fixture match."""
    return bool(item["matches"])


def _add_stable_source(run: Path, producer: dict[str, Any]) -> None:
    """Make the challenger brief cover the unchanged `stable.py` caller and resynchronize routing."""
    briefs_path = run / "review-briefs.json"
    briefs = json.loads(briefs_path.read_bytes())
    briefs["challenger"]["source_paths"].append("stable.py")
    briefs_path.write_bytes(json.dumps(briefs).encode())
    producer["review_routing"].synchronize_routing(run)


def _discover_and_export(
    run: Path,
    fixture: str,
    evidence: str,
    selection_name: str,
    *,
    keep: Callable[[dict[str, Any]], bool] = _has_matches,
    **prepare_kwargs: Any,
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    """Discover fixture matches, verify the kept components and export their bindings; return both processes."""
    discovered = _prepare(run, paged=False, discover_fixtures=True, **prepare_kwargs)
    assert discovered.returncode == 0, discovered.stderr
    selections = {
        "schema_version": 1,
        "inputs": [
            {
                "input_id": item["input_id"],
                "complete_input_verified": True,
                "verification_evidence": evidence,
                "matches": [
                    {**match, "match_sha256": hashlib.sha256(fixture.encode()).hexdigest()} for match in item["matches"]
                ],
            }
            for item in json.loads(discovered.stdout)["inputs"]
            if keep(item)
        ],
    }
    selection_path = run / selection_name
    selection_path.write_bytes(json.dumps(selections).encode())
    exported = _prepare(run, paged=False, export_fixture_bindings=selection_path, **prepare_kwargs)
    assert exported.returncode == 0, exported.stderr
    return discovered, exported


def _admit_bindings(run: Path, exported_stdout: str, approval_evidence: str, rationale: str) -> None:
    """Record the exported bindings as inert-fixture admissions in the run's routing file."""
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing["inert_fixture_admissions"] = [
        {**binding, "approval_evidence": approval_evidence, "rationale": rationale}
        for item in json.loads(exported_stdout)["inputs"]
        for binding in item["bindings"]
    ]
    routing_path.write_bytes(json.dumps(routing).encode())


def _direct_manifest(plan: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    """Build the direct-review manifest that binds the plan file by digest."""
    return {
        "manifest_kind": "direct-review",
        "parent_thread_id": "parent",
        "review_run_id": plan["review_run_id"],
        "review_input_sha256": plan["review_input_sha256"],
        "direct_execution": {
            "plan_path": "direct-plan.json",
            "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
    }


def _commit(root: Path, message: str, *paths: str) -> str:
    """Stage the named paths, commit them as a fixed author, and return the new HEAD."""
    git_bytes(root, "add", *paths)
    git_bytes(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", message)
    return git_bytes(root, "rev-parse", "HEAD").decode().strip()


def _export_for_base(run: Path, root: Path, head: str, base: str, fixture: str) -> subprocess.CompletedProcess[str]:
    """Point the run at one verified PR base, discover the fixture and export its component bindings."""
    local_checkout = {"worktree": root.as_posix(), "expected_head": head, "diff_base_oid": base, "diff_head_oid": head}
    (run / "local-checkout.json").write_bytes(json.dumps(local_checkout).encode())
    (run / "diff.patch").write_bytes(git_bytes(root, "diff", "--binary", f"{base}...{head}", "--"))
    (run / "files.txt").write_bytes(git_bytes(root, "diff", "--name-only", f"{base}...{head}", "--"))
    _, exported = _discover_and_export(
        run,
        fixture,
        "Parent inspected the complete known fixture and this verified source comparison.",
        "verified-comparison.json",
        source_root=root,
    )
    return exported


def _write_role_cards(root: Path, roles: tuple[str, ...]) -> None:
    """Write a minimal role card for each named reviewer role under the run's `role-cards` directory."""
    for role in roles:
        card = root / "role-cards" / role / "ROLE.md"
        card.parent.mkdir(parents=True)
        card.write_bytes(b"Role.")


def _replace_string(field: str) -> Callable[[dict[str, Any], Path], Path]:
    """Build a context change that swaps one plan string for an equal-length different one."""

    def change(plan: dict[str, Any], artifact_root: Path) -> Path:
        plan[field] = "b" * len(plan[field])
        return artifact_root

    return change


def _change_context_role(plan: dict[str, Any], artifact_root: Path) -> Path:
    """Rename the first reviewer context's role."""
    plan["contexts"][0]["role_id"] = "other"
    return artifact_root


def _change_model(plan: dict[str, Any], artifact_root: Path) -> Path:
    """Switch the selected review model."""
    plan["model_selection"]["model"] = "other"
    return artifact_root


def _change_brief_evidence(plan: dict[str, Any], artifact_root: Path) -> Path:
    """Replace the digest of the recorded brief evidence."""
    plan["brief_evidence"]["a.md"] = "b" * 64
    return artifact_root


def _change_snapshot_revision(plan: dict[str, Any], artifact_root: Path) -> Path:
    """Move the source snapshot to another equal-length revision."""
    plan["source_snapshot"]["revision"] = "b" * 40
    return artifact_root


def _move_artifact_root(plan: dict[str, Any], artifact_root: Path) -> Path:
    """Copy the whole artifact tree to a sibling directory and return the new root."""
    moved = artifact_root.with_name(artifact_root.name + "-moved")
    shutil.copytree(artifact_root, moved)
    return moved


#: Every equal-length review-context change that must invalidate an exact component approval.
CONTEXT_CHANGES = {
    "review_run_id": _replace_string("review_run_id"),
    "parent_thread_id": _replace_string("parent_thread_id"),
    "contexts": _change_context_role,
    "model_selection": _change_model,
    "briefs_sha256": _replace_string("briefs_sha256"),
    "brief_evidence": _change_brief_evidence,
    "expected_head": _replace_string("expected_head"),
    "expected_diff_base": _replace_string("expected_diff_base"),
    "scope_path": _replace_string("scope_path"),
    "snapshot_revision": _change_snapshot_revision,
    "artifact_root": _move_artifact_root,
}


@pytest.mark.integration
def test_direct_component_admission_excludes_large_unrelated_source(tmp_path: Path) -> None:
    """A small fixture must not require attesting a ten-megabyte unrelated source input.

    A synthetic key sits in a small source file while an unrelated caller is huge. Discovery, binding export and plan
    production run for real, and only the small components may be selected, bound and approved; the plan stays huge but
    the reviewer inputs carrying the fixture stay small and tamper-evident.
    """
    fixture = "sk-" + "j" * 24
    run = _build_review_inputs(
        tmp_path,
        fixture_source=f"example = '{fixture}'\n",
        unchanged_caller=True,
        unchanged_caller_content="ordinary = 1\n" * 800000,
    )
    producer = _producer()
    _add_stable_source(run, producer)

    discovered, exported = _discover_and_export(
        run, fixture, "Parent inspected this complete synthetic file component and its identity.", "selections.json"
    )

    assert fixture not in discovered.stdout + discovered.stderr
    assert re.search(r"[0-9a-f]{64}", discovered.stdout) is None
    selected = [item for item in json.loads(discovered.stdout)["inputs"] if item["matches"]]
    assert {item["input_id"] for item in selected} == KNOWN_COMPONENTS
    assert not (run / "direct-plan.json").exists()
    unapproved = _prepare(run, paged=False)
    assert unapproved.returncode != 0
    assert SENSITIVE in unapproved.stderr
    _admit_bindings(
        run,
        exported.stdout,
        "Explicit approval recorded for this exact inert synthetic component.",
        "Synthetic material with no credential authority.",
    )
    prepared = _prepare(run, paged=False)
    assert prepared.returncode == 0, prepared.stderr
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    assert plan["schema_version"] == 3
    assert len(plan_path.read_bytes()) > 10000000
    inputs = producer["_direct_review_inputs"](run, plan)
    assert sum(len(inputs[item["input_id"]].encode()) for item in selected) < 12000
    roles = set(json.loads((run / "review-routing.json").read_bytes())["triggered_roles"])
    validator = producer["validator"]
    manifest = _direct_manifest(plan, plan_path)
    with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
        validator._validate_direct_review(run, manifest, [], roles, tmp_path, "parent")
    plan["source_snapshot"]["files"][1]["content"] += "body changed\n"
    plan_path.write_bytes(producer["_json_bytes"](plan))
    with pytest.raises(SystemExit, match=SENSITIVE):
        validator._validate_direct_review(run, _direct_manifest(plan, plan_path), [], roles, tmp_path, "parent")


@pytest.mark.parametrize("kind", ["direct-plan", "direct-diff"])
def test_components_preserve_every_original_utf8_byte(tmp_path: Path, kind: str) -> None:
    """Partition boundaries conserve Unicode, CRLF, delimiters and source-embedded structural lookalikes.

    A plan or diff is split into UTF-8 components; the concatenated bodies must equal the original bytes exactly and
    each header's byte range must match its body, even when the content imitates the plan's own JSON structure.
    """
    producer = _producer()
    plan = {
        "schema_version": 3,
        "source_root": "C:/source/root",
        "source_snapshot": {
            "files": [
                {"path": "a.py", "content": 'café\r\n  "source_snapshot": {\n    "files": [\n'},
                {"path": "b.py", "content": "second\n"},
            ]
        },
    }
    original = (
        producer["_json_bytes"](plan).decode()
        if kind == "direct-plan"
        else "unparsed café preamble\r\ndiff --git a/a b/a\r\n+a\r\ndiff --git a/b b/b\n+b\n"
    )

    components = producer["_direct_component_inputs"](tmp_path, plan, kind, original)

    parts = [text.split(COMPONENT_SEPARATOR, 1) for text in components.values()]
    records = [json.loads(header) for header, _ in parts]
    bodies = [body.encode() for _, body in parts]
    offsets = [0, *accumulate(len(body) for body in bodies)]
    original_bytes = original.encode()
    assert [record["byte_start"] for record in records] == offsets[:-1]
    assert [record["byte_end"] for record in records] == offsets[1:]
    assert all(
        original_bytes[record["byte_start"] : record["byte_end"]] == body for record, body in zip(records, bodies)
    )
    assert all(record["source_root"] == "C:/source/root" for record in records)
    assert b"".join(bodies) == original_bytes


def test_cross_section_sensitive_match_is_not_split_or_exported(tmp_path: Path) -> None:
    """The aggregate strict scan must reject a match lost at a component boundary.

    A bearer-token line ends one diff section and its value would start the next; partitioning must refuse instead of
    exporting two halves that each look harmless.
    """
    producer = _producer()
    text = "diff --git a/a b/a\n+Authorization: Bearer \ndiff --git a/b b/b\n+b\n"

    with pytest.raises(ValueError, match="review-direct-component-boundary-sensitive"):
        producer["_direct_component_inputs"](tmp_path, {"source_root": tmp_path.as_posix()}, "direct-diff", text)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("version", "removed_fields"),
    [pytest.param(1, {"model_selection"}, id="schema-1"), pytest.param(2, set(), id="schema-2")],
)
def test_historical_direct_plan_inputs_keep_aggregate_contract(
    tmp_path: Path, version: int, removed_fields: set[str]
) -> None:
    """Historical plans retain original input digests and pass the unchanged source admission gate.

    A schema-1 or schema-2 plan is rebuilt from a real current plan; its inputs must stay whole (no per-component split)
    and reproduce the original plan bytes, and the validator must still gate on the role set.
    """
    run = _build_review_inputs(tmp_path)
    prepared = _prepare(run, paged=False)
    assert prepared.returncode == 0, prepared.stderr
    producer = _producer()
    plan_path = run / "direct-plan.json"
    current = json.loads(plan_path.read_bytes())
    plan = {key: value for key, value in current.items() if key not in removed_fields} | {"schema_version": version}
    plan_path.write_bytes(producer["_json_bytes"](plan))

    inputs = producer["_direct_review_inputs"](run, plan)

    assert "direct-plan" in inputs
    assert "direct-diff" in inputs
    assert not any(":file:" in key or ":section:" in key or ":remainder:" in key for key in inputs)
    unfixtured = {
        key: value for key, value in plan.items() if key not in {"inert_fixture_admissions", "routing_sha256"}
    }
    assert inputs["direct-plan"] == producer["_json_bytes"](unfixtured).decode()
    with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
        producer["validator"]._validate_direct_review(
            run, _direct_manifest(plan, plan_path), [], {"challenger"}, tmp_path, "parent"
        )


@pytest.mark.integration
def test_exporting_one_component_does_not_admit_unknown_sibling(tmp_path: Path) -> None:
    """Verified small-component export must not authorize an unselected source record or raw source.

    Two secret-looking values exist, but only the known one is verified and approved; the unknown sibling in `stable.py`
    must still make plan production fail, with no dispatch or plan written and no value echoed.
    """
    fixture = "sk-" + "k" * 24
    unknown = "sk-" + "m" * 24
    run = _build_review_inputs(
        tmp_path,
        fixture_source=f"example = '{fixture}'\n",
        unchanged_caller=True,
        unchanged_caller_content=f"unknown = '{unknown}'\n",
    )
    _add_stable_source(run, _producer())
    discovered, exported = _discover_and_export(
        run,
        fixture,
        "Parent inspected only the complete known synthetic component.",
        "verified-only-known.json",
        keep=lambda item: item["input_id"] in KNOWN_COMPONENTS,
    )
    _admit_bindings(
        run,
        exported.stdout,
        "Parent approved only the inspected synthetic component.",
        "Known inert material without credential authority.",
    )

    refused = _prepare(run, paged=False)

    assert refused.returncode != 0
    assert SENSITIVE in refused.stderr
    assert not (run / "dispatch.json").exists()
    assert not (run / "direct-plan.json").exists()
    assert all(value not in discovered.stdout + exported.stdout + refused.stderr for value in (fixture, unknown))


@pytest.mark.integration
def test_component_approved_source_survives_full_direct_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Component approval works with the actual producer, ordinary source reads and native evidence assembly.

    The fixture is discovered, verified, exported and approved inside the native rollout fixture's prepare step; the
    assembled direct evidence must succeed without echoing the fixture and report reviewer-reported consumption.
    """
    fixture = "sk-" + "n" * 24

    def prepare_approved(run: Path) -> subprocess.CompletedProcess[str]:
        """Exercise discovery and binding export before recording fixture authority in routing."""
        producer = _producer()
        producer["review_routing"].synchronize_routing(run)
        _, exported = _discover_and_export(
            run, fixture, "Parent inspected this complete inert synthetic component.", "verified-components.json"
        )
        _admit_bindings(
            run,
            exported.stdout,
            "Explicit approval of the exact synthetic component.",
            "Inert material without credential authority.",
        )
        return _prepare(run, paged=False)

    monkeypatch.setattr(
        direct,
        "_review_inputs",
        lambda path, **kwargs: _build_review_inputs(path, fixture_source=f"example = '{fixture}'\n", **kwargs),
    )
    monkeypatch.setattr(direct, "_direct_prepare", prepare_approved)
    run, home = direct._direct_evidence(tmp_path)

    assembled = direct._assemble_direct(run, home)

    assert assembled.returncode == 0, assembled.stderr
    assert fixture not in assembled.stdout + assembled.stderr
    assert json.loads(assembled.stdout)["source_consumption"] == "reviewer-reported"


@pytest.mark.parametrize("change", [pytest.param(change, id=field) for field, change in CONTEXT_CHANGES.items()])
@pytest.mark.parametrize(
    "identity",
    [
        pytest.param("direct-plan:file:0", id="direct-plan"),
        pytest.param("direct-diff:section:0", id="direct-diff"),
        pytest.param("direct-source:0", id="direct-source"),
    ],
)
def test_component_approval_rejects_changed_review_context(
    tmp_path: Path, identity: str, change: Callable[[dict[str, Any], Path], Path]
) -> None:
    """Equal-length context changes at the same artifact root must invalidate exact fixture approval.

    An approval is minted for one component under one review context. Changing any bound context field (run id, thread,
    roles, model, brief digests, heads, scope, revision) or moving the artifact root, without changing the component's
    length, must make the same fixture count as unapproved sensitive material again.
    """
    producer = _producer()
    fixture = "sk-" + "p" * 24
    plan = {
        "schema_version": 3,
        "source_root": tmp_path.as_posix(),
        "review_run_id": "first",
        "parent_thread_id": "first",
        "contexts": [
            {"role_id": "first", "source_paths": ["a.py"], "context_path": "task.md", "context_sha256": "a" * 64}
        ],
        "model_selection": {"mode": "explicit", "model": "first"},
        "briefs_sha256": "a" * 64,
        "brief_evidence": {"a.md": "a" * 64},
        "expected_head": "a" * 40,
        "expected_diff_base": "a" * 40,
        "scope_path": "first",
        "source_snapshot": {
            "revision": "a" * 40,
            "files": [{"path": "a.py", "content": fixture, "kind": "file", "encoding": "utf-8"}],
        },
    }
    (tmp_path / "diff.patch").write_bytes(f"diff --git a/a.py b/a.py\n+{fixture}\n".encode())
    (tmp_path / "task.md").write_bytes(b"Task.")
    (tmp_path / "brief.md").write_bytes(b"Brief.")
    (tmp_path / "review-briefs.json").write_bytes(
        json.dumps({role: {"evidence_path": "brief.md"} for role in ("first", "other")}).encode()
    )
    _write_role_cards(tmp_path, ("first", "other"))
    selected = producer["_direct_review_inputs"](tmp_path, plan)[identity]
    start = selected.index(fixture)
    admissions = [
        {
            "input_sha256": hashlib.sha256(selected.encode()).hexdigest(),
            "match_start": start,
            "match_end": start + len(fixture),
            "match_sha256": hashlib.sha256(fixture.encode()).hexdigest(),
            "approval_evidence": "Explicit approval for this complete synthetic component and review context.",
            "rationale": "Inert synthetic test data.",
        }
    ]
    validator = producer["validator"]
    assert not validator.review_input_has_sensitive_material(selected, admissions)
    artifact_root = change(plan, tmp_path)

    after = producer["_direct_review_inputs"](artifact_root, plan)[identity]

    assert validator.review_input_has_sensitive_material(after, admissions)


@pytest.mark.integration
def test_receipt_backed_pr_base_change_requires_fresh_component_approval(tmp_path: Path) -> None:
    """An unchanged fixture section at one tip cannot reuse approvals from another verified PR base.

    A real git repository gets two bases under the same reviewed tip. Bindings exported for the first base must leave
    plan production refused once the run points at the second base, and only bindings exported for the second base admit
    it, with the plan recording that verified base as its comparison base.
    """
    fixture = "sk-" + "r" * 24
    run = _build_review_inputs(tmp_path, source_name="a.py", second_file=True)
    local_receipt = run / "local-source/review-worktree.json"
    root = Path(json.loads(local_receipt.read_bytes())["review_worktree"])
    base_a = git_bytes(root, "rev-parse", "HEAD").decode().strip()
    (root / "a.py").write_bytes(b"value = 1\n")
    base_b = _commit(root, "Second base", "other.py")
    (root / "a.py").write_bytes(f"value = 2\nexample = '{fixture}'\n".encode())
    (root / "other.py").write_bytes(b"other = 3\n")
    head = _commit(root, "Reviewed tip", "a.py", "other.py")
    assert not git_bytes(root, "status", "--porcelain")
    local_receipt.unlink()
    briefs_path = run / "review-briefs.json"
    briefs = json.loads(briefs_path.read_bytes())
    briefs_path.write_bytes(
        json.dumps({role: {**brief, "source_paths": ["a.py", "other.py"]} for role, brief in briefs.items()}).encode()
    )
    _producer()["review_routing"].synchronize_routing(run)
    exports = [_export_for_base(run, root, head, base, fixture) for base in (base_a, base_b)]
    bindings = [{item["input_id"]: item["bindings"] for item in json.loads(e.stdout)["inputs"]} for e in exports]
    expected_inputs = {"direct-plan:file:0", "direct-diff:section:0", "direct-source:0"}
    assert set(bindings[0]) == expected_inputs
    assert set(bindings[1]) == expected_inputs
    assert all(bindings[0][i][0]["input_sha256"] != bindings[1][i][0]["input_sha256"] for i in expected_inputs)
    approval = "Explicit approval for the complete synthetic component in this comparison."
    _admit_bindings(run, exports[0].stdout, approval, "Known inert fixture.")
    stale = _prepare(run, source_root=root, paged=False)
    assert stale.returncode != 0
    assert SENSITIVE in stale.stderr
    _admit_bindings(run, exports[1].stdout, approval, "Known inert fixture.")

    fresh = _prepare(run, source_root=root, paged=False)

    assert fresh.returncode == 0, fresh.stderr
    plan = json.loads((run / "direct-plan.json").read_bytes())
    assert plan["expected_diff_base"] is None
    assert plan["source_comparison_base"] == base_b


@pytest.mark.integration
def test_direct_consumer_rederives_frozen_source_comparison_base(tmp_path: Path) -> None:
    """Rehashing a plan cannot make a claimed comparison base replace the verified actual base.

    The complete native evidence run is tampered by writing a bogus `source_comparison_base` back into the plan; the
    final consumer must re-derive the real base from provenance and refuse the changed one.
    """
    run, home = direct._direct_evidence(tmp_path)
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    assert plan["source_comparison_base"] == plan["source_snapshot"]["revision"]
    plan["source_comparison_base"] = "0" * 40
    plan_path.write_bytes((json.dumps(plan, indent=2, sort_keys=True) + "\n").encode())

    result = direct._assemble_direct(run, home)

    assert result.returncode != 0
    assert "review-direct-source-comparison-changed" in result.stderr
