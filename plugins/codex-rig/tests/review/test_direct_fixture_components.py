"""Verify bounded fixture admission through real direct-review producers."""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest
from test_review_prepare import HELPER, _build_review_inputs, _prepare


@pytest.mark.integration
def test_direct_component_admission_excludes_large_unrelated_source(tmp_path: Path) -> None:
    """A small fixture must not require attesting a ten-megabyte unrelated source input."""
    fixture = "sk-" + "j" * 24
    run = _build_review_inputs(
        tmp_path,
        fixture_source=f"example = '{fixture}'\n",
        unchanged_caller=True,
        unchanged_caller_content="ordinary = 1\n" * 800000,
    )
    briefs_path = run / "review-briefs.json"
    briefs = json.loads(briefs_path.read_bytes())
    briefs["challenger"]["source_paths"].append("stable.py")
    briefs_path.write_bytes(json.dumps(briefs).encode())
    producer = runpy.run_path(str(HELPER))
    producer["review_routing"].synchronize_routing(run)
    discovered = _prepare(run, paged=False, discover_fixtures=True)
    assert discovered.returncode == 0, discovered.stderr
    assert fixture not in discovered.stdout + discovered.stderr
    assert re.search(r"[0-9a-f]{64}", discovered.stdout) is None
    selected = [item for item in json.loads(discovered.stdout)["inputs"] if item["matches"]]
    assert {item["input_id"] for item in selected} == {"direct-plan:file:1", "direct-diff:section:0", "direct-source:1"}
    selections = {
        "schema_version": 1,
        "inputs": [
            {
                "input_id": item["input_id"],
                "complete_input_verified": True,
                "verification_evidence": "Parent inspected this complete synthetic file component and its identity.",
                "matches": [
                    {**match, "match_sha256": hashlib.sha256(fixture.encode()).hexdigest()} for match in item["matches"]
                ],
            }
            for item in selected
        ],
    }
    selection_path = run / "verified-selections.json"
    selection_path.write_bytes(json.dumps(selections).encode())
    exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
    assert exported.returncode == 0, exported.stderr
    assert not (run / "direct-plan.json").exists()
    unapproved = _prepare(run, paged=False)
    assert unapproved.returncode != 0
    assert "review-direct-sensitive-material" in unapproved.stderr
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing["inert_fixture_admissions"] = [
        {
            **binding,
            "approval_evidence": "Explicit approval recorded for this exact inert synthetic component.",
            "rationale": "Synthetic material with no credential authority.",
        }
        for item in json.loads(exported.stdout)["inputs"]
        for binding in item["bindings"]
    ]
    routing_path.write_bytes(json.dumps(routing).encode())
    prepared = _prepare(run, paged=False)
    assert prepared.returncode == 0, prepared.stderr
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    assert plan["schema_version"] == 3
    assert len(plan_path.read_bytes()) > 10000000
    inputs = producer["_direct_review_inputs"](run, plan)
    assert sum(len(inputs[item["input_id"]].encode()) for item in selected) < 12000
    manifest = {
        "manifest_kind": "direct-review",
        "parent_thread_id": "parent",
        "review_run_id": plan["review_run_id"],
        "review_input_sha256": plan["review_input_sha256"],
        "direct_execution": {
            "plan_path": "direct-plan.json",
            "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
    }
    validator = producer["validator"]
    with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")
    plan["source_snapshot"]["files"][1]["content"] += "body changed\n"
    plan_path.write_bytes(producer["_json_bytes"](plan))
    manifest["direct_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")


@pytest.mark.parametrize("kind", ["direct-plan", "direct-diff"])
def test_components_preserve_every_original_utf8_byte(tmp_path: Path, kind: str) -> None:
    """Partition boundaries must conserve Unicode, CRLF, delimiters and source-embedded structural lookalikes."""
    producer = runpy.run_path(str(HELPER))
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
    restored = bytearray()
    for text in components.values():
        header, body = text.split("\n--- UTF-8 component ---\n", 1)
        record = json.loads(header)
        assert record["byte_start"] == len(restored)
        restored.extend(body.encode())
        assert record["byte_end"] == len(restored)
        assert original.encode()[record["byte_start"] : record["byte_end"]] == body.encode()
        assert record["source_root"] == "C:/source/root"
    assert bytes(restored) == original.encode()


def test_cross_section_sensitive_match_is_not_split_or_exported(tmp_path: Path) -> None:
    """The aggregate strict scan must reject a match lost at a component boundary."""
    producer = runpy.run_path(str(HELPER))
    text = "diff --git a/a b/a\n+Authorization: Bearer \ndiff --git a/b b/b\n+b\n"
    with pytest.raises(ValueError, match="review-direct-component-boundary-sensitive"):
        producer["_direct_component_inputs"](tmp_path, {"source_root": tmp_path.as_posix()}, "direct-diff", text)


@pytest.mark.integration
@pytest.mark.parametrize("version", [1, 2])
def test_historical_direct_plan_inputs_keep_aggregate_contract(tmp_path: Path, version: int) -> None:
    """Historical plans retain original input digests and pass the unchanged source admission gate."""
    run = _build_review_inputs(tmp_path)
    prepared = _prepare(run, paged=False)
    assert prepared.returncode == 0, prepared.stderr
    producer = runpy.run_path(str(HELPER))
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan["schema_version"] = version
    if version == 1:
        del plan["model_selection"]
    inputs = producer["_direct_review_inputs"](run, plan)
    assert "direct-plan" in inputs
    assert "direct-diff" in inputs
    assert not any(":file:" in key or ":section:" in key or ":remainder:" in key for key in inputs)
    assert (
        inputs["direct-plan"]
        == producer["_json_bytes"](
            {key: value for key, value in plan.items() if key not in {"inert_fixture_admissions", "routing_sha256"}}
        ).decode()
    )
    plan_path.write_bytes(producer["_json_bytes"](plan))
    manifest = {
        "manifest_kind": "direct-review",
        "parent_thread_id": "parent",
        "review_run_id": plan["review_run_id"],
        "review_input_sha256": plan["review_input_sha256"],
        "direct_execution": {
            "plan_path": "direct-plan.json",
            "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
    }
    with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
        producer["validator"]._validate_direct_review(run, manifest, [], {"challenger"}, tmp_path, "parent")


@pytest.mark.integration
def test_exporting_one_component_does_not_admit_unknown_sibling(tmp_path: Path) -> None:
    """Verified small-component export must not authorize an unselected source record or raw source."""
    fixture = "sk-" + "k" * 24
    unknown = "sk-" + "m" * 24
    run = _build_review_inputs(
        tmp_path,
        fixture_source=f"example = '{fixture}'\n",
        unchanged_caller=True,
        unchanged_caller_content=f"unknown = '{unknown}'\n",
    )
    briefs_path = run / "review-briefs.json"
    briefs = json.loads(briefs_path.read_bytes())
    briefs["challenger"]["source_paths"].append("stable.py")
    briefs_path.write_bytes(json.dumps(briefs).encode())
    producer = runpy.run_path(str(HELPER))
    producer["review_routing"].synchronize_routing(run)
    discovered = _prepare(run, paged=False, discover_fixtures=True)
    assert discovered.returncode == 0, discovered.stderr
    selections = {
        "schema_version": 1,
        "inputs": [
            {
                "input_id": item["input_id"],
                "complete_input_verified": True,
                "verification_evidence": "Parent inspected only the complete known synthetic component.",
                "matches": [
                    {**match, "match_sha256": hashlib.sha256(fixture.encode()).hexdigest()} for match in item["matches"]
                ],
            }
            for item in json.loads(discovered.stdout)["inputs"]
            if item["input_id"] in {"direct-plan:file:1", "direct-diff:section:0", "direct-source:1"}
        ],
    }
    selection_path = run / "verified-only-known.json"
    selection_path.write_bytes(json.dumps(selections).encode())
    exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
    assert exported.returncode == 0, exported.stderr
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing["inert_fixture_admissions"] = [
        {
            **binding,
            "approval_evidence": "Parent approved only the inspected synthetic component.",
            "rationale": "Known inert material without credential authority.",
        }
        for item in json.loads(exported.stdout)["inputs"]
        for binding in item["bindings"]
    ]
    routing_path.write_bytes(json.dumps(routing).encode())
    refused = _prepare(run, paged=False)
    assert refused.returncode != 0
    assert "review-direct-sensitive-material" in refused.stderr
    assert not (run / "dispatch.json").exists()
    assert not (run / "direct-plan.json").exists()
    assert all(value not in discovered.stdout + exported.stdout + refused.stderr for value in (fixture, unknown))


@pytest.mark.integration
def test_component_approved_source_survives_full_direct_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise component approval with the actual producer, ordinary source reads and native evidence assembly."""
    import test_review_direct as direct  # Reuse the native rollout fixture, not production internals.

    fixture = "sk-" + "n" * 24

    def inputs(path: Path, **kwargs: object) -> Path:
        """Build real collected source containing the independently known inert fixture."""
        return _build_review_inputs(path, fixture_source=f"example = '{fixture}'\n", **kwargs)

    def prepare_approved(run: Path) -> subprocess.CompletedProcess[str]:
        """Exercise discovery and binding export before recording fixture authority in routing."""
        producer = runpy.run_path(str(HELPER))
        producer["review_routing"].synchronize_routing(run)
        discovered = _prepare(run, paged=False, discover_fixtures=True)
        assert discovered.returncode == 0, discovered.stderr
        selections = {
            "schema_version": 1,
            "inputs": [
                {
                    "input_id": item["input_id"],
                    "complete_input_verified": True,
                    "verification_evidence": "Parent inspected this complete inert synthetic component.",
                    "matches": [
                        {**match, "match_sha256": hashlib.sha256(fixture.encode()).hexdigest()}
                        for match in item["matches"]
                    ],
                }
                for item in json.loads(discovered.stdout)["inputs"]
                if item["matches"]
            ],
        }
        selection_path = run / "verified-components.json"
        selection_path.write_bytes(json.dumps(selections).encode())
        exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
        assert exported.returncode == 0, exported.stderr
        routing_path = run / "review-routing.json"
        routing = json.loads(routing_path.read_bytes())
        routing["inert_fixture_admissions"] = [
            {
                **binding,
                "approval_evidence": "Explicit approval of the exact synthetic component.",
                "rationale": "Inert material without credential authority.",
            }
            for item in json.loads(exported.stdout)["inputs"]
            for binding in item["bindings"]
        ]
        routing_path.write_bytes(json.dumps(routing).encode())
        return _prepare(run, paged=False)

    monkeypatch.setattr(direct, "_review_inputs", inputs)
    monkeypatch.setattr(direct, "_direct_prepare", prepare_approved)
    run, home = direct._direct_evidence(tmp_path)
    assembled = direct._assemble_direct(run, home)
    assert assembled.returncode == 0, assembled.stderr
    assert fixture not in assembled.stdout + assembled.stderr
    assert json.loads(assembled.stdout)["source_consumption"] == "reviewer-reported"


@pytest.mark.parametrize(
    "field",
    [
        "review_run_id",
        "parent_thread_id",
        "contexts",
        "model_selection",
        "briefs_sha256",
        "brief_evidence",
        "expected_head",
        "expected_diff_base",
        "scope_path",
        "snapshot_revision",
        "artifact_root",
    ],
)
@pytest.mark.parametrize("kind", ["direct-plan", "direct-diff", "direct-source"])
def test_component_approval_rejects_changed_review_context(tmp_path: Path, field: str, kind: str) -> None:
    """Equal-length context changes at the same artifact root must invalidate exact fixture approval."""
    producer = runpy.run_path(str(HELPER))
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
    for role in ("first", "other"):
        card = tmp_path / "role-cards" / role / "ROLE.md"
        card.parent.mkdir(parents=True)
        card.write_bytes(b"Role.")
    before = producer["_direct_review_inputs"](tmp_path, plan)
    identity = {
        "direct-plan": "direct-plan:file:0",
        "direct-diff": "direct-diff:section:0",
        "direct-source": "direct-source:0",
    }[kind]
    selected = before[identity]
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
    assert not producer["validator"].review_input_has_sensitive_material(selected, admissions)
    artifact_root = tmp_path
    if field == "artifact_root":
        artifact_root = tmp_path.with_name(tmp_path.name + "-moved")
        shutil.copytree(tmp_path, artifact_root)
    elif field == "contexts":
        plan[field][0]["role_id"] = "other"
    elif field == "model_selection":
        plan[field]["model"] = "other"
    elif field == "brief_evidence":
        plan[field]["a.md"] = "b" * 64
    elif field == "snapshot_revision":
        plan["source_snapshot"]["revision"] = "b" * 40
    else:
        plan[field] = "b" * len(plan[field])
    after = producer["_direct_review_inputs"](artifact_root, plan)
    assert producer["validator"].review_input_has_sensitive_material(after[identity], admissions)


@pytest.mark.integration
def test_receipt_backed_pr_base_change_requires_fresh_component_approval(tmp_path: Path) -> None:
    """An unchanged fixture section at one tip cannot reuse approvals from another verified PR base."""
    fixture = "sk-" + "r" * 24
    run = _build_review_inputs(tmp_path, source_name="a.py", second_file=True)
    local_receipt = run / "local-source/review-worktree.json"
    root = Path(json.loads(local_receipt.read_bytes())["review_worktree"])

    def git(*arguments: str) -> bytes:
        """Run task-local Git operations on the isolated real source repository."""
        return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True).stdout

    base_a = git("rev-parse", "HEAD").decode().strip()
    (root / "a.py").write_bytes(b"value = 1\n")
    git("add", "other.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "Second base")
    base_b = git("rev-parse", "HEAD").decode().strip()
    (root / "a.py").write_bytes(f"value = 2\nexample = '{fixture}'\n".encode())
    (root / "other.py").write_bytes(b"other = 3\n")
    git("add", "a.py", "other.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "Reviewed tip")
    head = git("rev-parse", "HEAD").decode().strip()
    assert not git("status", "--porcelain")
    local_receipt.unlink()
    briefs_path = run / "review-briefs.json"
    briefs = json.loads(briefs_path.read_bytes())
    for brief in briefs.values():
        brief["source_paths"] = ["a.py", "other.py"]
    briefs_path.write_bytes(json.dumps(briefs).encode())
    producer = runpy.run_path(str(HELPER))
    producer["review_routing"].synchronize_routing(run)
    proposals = []
    for base in (base_a, base_b):
        (run / "local-checkout.json").write_bytes(
            json.dumps(
                {"worktree": root.as_posix(), "expected_head": head, "diff_base_oid": base, "diff_head_oid": head}
            ).encode()
        )
        (run / "diff.patch").write_bytes(git("diff", "--binary", f"{base}...{head}", "--"))
        (run / "files.txt").write_bytes(git("diff", "--name-only", f"{base}...{head}", "--"))
        discovery = _prepare(run, source_root=root, paged=False, discover_fixtures=True)
        assert discovery.returncode == 0, discovery.stderr
        selections = {
            "schema_version": 1,
            "inputs": [
                {
                    "input_id": entry["input_id"],
                    "complete_input_verified": True,
                    "verification_evidence": "Parent inspected the complete known fixture and this verified source comparison.",
                    "matches": [
                        {**match, "match_sha256": hashlib.sha256(fixture.encode()).hexdigest()}
                        for match in entry["matches"]
                    ],
                }
                for entry in json.loads(discovery.stdout)["inputs"]
                if entry["matches"]
            ],
        }
        selection_path = run / "verified-comparison.json"
        selection_path.write_bytes(json.dumps(selections).encode())
        exported = _prepare(run, source_root=root, paged=False, export_fixture_bindings=selection_path)
        assert exported.returncode == 0, exported.stderr
        proposals.append({entry["input_id"]: entry["bindings"] for entry in json.loads(exported.stdout)["inputs"]})
    expected_inputs = {"direct-plan:file:0", "direct-diff:section:0", "direct-source:0"}
    assert set(proposals[0]) == expected_inputs
    assert set(proposals[1]) == expected_inputs
    for identity in expected_inputs:
        assert proposals[0][identity][0]["input_sha256"] != proposals[1][identity][0]["input_sha256"]
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    for index, proposal in enumerate(proposals):
        routing["inert_fixture_admissions"] = [
            {
                **binding,
                "approval_evidence": "Explicit approval for the complete synthetic component in this comparison.",
                "rationale": "Known inert fixture.",
            }
            for bindings in proposal.values()
            for binding in bindings
        ]
        routing_path.write_bytes(json.dumps(routing).encode())
        result = _prepare(run, source_root=root, paged=False)
        if index == 0:
            assert result.returncode != 0
            assert "review-direct-sensitive-material" in result.stderr
        else:
            assert result.returncode == 0, result.stderr
    plan = json.loads((run / "direct-plan.json").read_bytes())
    assert plan["expected_diff_base"] is None
    assert plan["source_comparison_base"] == base_b


@pytest.mark.integration
def test_direct_consumer_rederives_frozen_source_comparison_base(tmp_path: Path) -> None:
    """Rehashing a plan cannot make a claimed comparison base replace the verified actual base."""
    import test_review_direct as direct  # Reuse complete ordinary native provenance for the final consumer.

    run, home = direct._direct_evidence(tmp_path)
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    assert plan["source_comparison_base"] == plan["source_snapshot"]["revision"]
    plan["source_comparison_base"] = "0" * 40
    plan_path.write_bytes((json.dumps(plan, indent=2, sort_keys=True) + "\n").encode())
    result = direct._assemble_direct(run, home)
    assert result.returncode != 0
    assert "review-direct-source-comparison-changed" in result.stderr
