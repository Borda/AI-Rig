"""Exercise sensitive fixture admission and direct review retry input boundaries."""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from test_review_prepare import (
    HELPER,
    _build_review_inputs,
    _direct_aborted_child_failure,
    _direct_no_child_failure,
    _prepare,
    _write_jsonl,
)


@pytest.mark.integration
@pytest.mark.parametrize("input_kind", ["axis", "evidence"])
def test_direct_prepare_refuses_unadmitted_sensitive_brief_input(tmp_path: Path, input_kind: str) -> None:
    """Refuse sensitive reviewer input before creating a dispatch and never echo the value."""
    secret = "sk-" + "z" * 24
    run = _build_review_inputs(tmp_path)
    if input_kind == "axis":
        briefs_path = run / "review-briefs.json"
        briefs = json.loads(briefs_path.read_text(encoding="utf-8"))
        briefs["challenger"]["axis"] = secret
        briefs_path.write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    else:
        (run / "challenger-evidence.md").write_text(secret, encoding="utf-8", newline="\n")

    result = _prepare(run, paged=False)

    assert secret not in result.stdout + result.stderr
    assert result.returncode != 0
    assert not (run / "dispatch.json").exists()


@pytest.mark.integration
@pytest.mark.parametrize("route", ["direct", "paged", "batches"])
def test_fixture_discovery_cli_exports_only_verified_bindings(tmp_path: Path, route: str) -> None:
    """Discover exact generated inputs without hashes, then approve and consume verified binding proposals."""
    fixture = "sk-" + "d" * 24
    run = _build_review_inputs(
        tmp_path, fixture_source=f"label = 'café'\nexample = '{fixture}'\n", large_source=route == "batches"
    )
    options = {"paged": route == "paged", "batches": route == "batches"}
    refused = _prepare(run, **options)
    assert refused.returncode != 0
    assert "sensitive-material" in refused.stderr
    assert fixture not in refused.stdout + refused.stderr
    routing_path = run / "review-routing.json"
    routing_before = routing_path.read_bytes()
    discovered = _prepare(run, **options, discover_fixtures=True)
    assert discovered.returncode == 0, discovered.stderr
    discovery = json.loads(discovered.stdout)
    assert discovery["schema_version"] == 1
    assert discovery["mode"] == "fixture-discovery"
    assert fixture not in discovered.stdout + discovered.stderr
    assert re.search(r"[0-9a-f]{64}", discovered.stdout) is None
    selections = {"schema_version": 1, "inputs": []}
    for item in discovery["inputs"]:
        assert set(item) == {"input_id", "matches"}
        if not item["matches"]:
            continue
        assert all(set(match) == {"pattern_index", "match_start", "match_end"} for match in item["matches"])
        selections["inputs"].append(
            {
                "input_id": item["input_id"],
                "complete_input_verified": True,
                "verification_evidence": "Parent verified the complete current synthetic input and every occurrence.",
                "matches": [
                    {**match, "match_sha256": hashlib.sha256(fixture.encode()).hexdigest()} for match in item["matches"]
                ],
            }
        )
    assert selections["inputs"]
    if route == "batches":
        assert any(item["input_id"].startswith("source-context:") for item in selections["inputs"])
        assert any(item["input_id"].startswith("batch:source-") for item in selections["inputs"])
    selection_path = run / "verified-fixture-selections.json"
    wrong_selection = json.loads(json.dumps(selections))
    wrong_selection["inputs"][0]["matches"][0]["match_sha256"] = "0" * 64
    selection_path.write_text(json.dumps(wrong_selection), encoding="utf-8", newline="\n")
    unverified = _prepare(run, **options, export_fixture_bindings=selection_path)
    assert unverified.returncode != 0
    assert "review-fixture-selection-unverified" in unverified.stderr
    assert unverified.stdout == ""
    assert fixture not in unverified.stderr
    assert re.search(r"[0-9a-f]{64}", unverified.stderr) is None
    selection_path.write_text(json.dumps(selections), encoding="utf-8", newline="\n")
    exported = _prepare(run, **options, export_fixture_bindings=selection_path)
    assert exported.returncode == 0, exported.stderr
    proposal = json.loads(exported.stdout)
    assert proposal["mode"] == "fixture-bindings"
    assert fixture not in exported.stdout + exported.stderr
    bindings = [binding for item in proposal["inputs"] for binding in item["bindings"]]
    assert bindings
    assert all(set(binding) == {"input_sha256", "match_start", "match_end", "match_sha256"} for binding in bindings)
    assert all(binding["match_sha256"] == hashlib.sha256(fixture.encode()).hexdigest() for binding in bindings)
    assert routing_path.read_bytes() == routing_before
    for name in ("direct-plan.json", "inspection-plan.json", "dispatch.json", "batch-inventory.json", "batches"):
        assert not (run / name).exists(), name
    unapproved = _prepare(run, **options)
    assert unapproved.returncode != 0
    assert "sensitive-material" in unapproved.stderr
    routing = json.loads(routing_before)
    routing["inert_fixture_admissions"] = [
        {
            **binding,
            "approval_evidence": "Parent obtained explicit approval of this exact inert fixture input.",
            "rationale": "Synthetic fixture without credential authority.",
        }
        for binding in bindings
    ]
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    prepared = _prepare(run, **options)
    assert prepared.returncode == 0, prepared.stderr
    producer = runpy.run_path(str(HELPER))
    validator = producer["validator"]
    if route == "direct":
        plan_path = run / "direct-plan.json"
        plan = json.loads(plan_path.read_bytes())
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
            validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")
    elif route == "paged":
        plan_path = run / "inspection-plan.json"
        validator.validate_inspection_contexts(json.loads(plan_path.read_bytes()), plan_path, context_limit=None)
    else:
        inventory = producer["review_batches"].validate_inventory(run)
        assert inventory["wave_count"] > 1
        for plan_path in (run / "batches").glob("source-*/inspection-plan.json"):
            validator.validate_inspection_contexts(json.loads(plan_path.read_bytes()), plan_path, context_limit=None)


@pytest.mark.integration
@pytest.mark.parametrize("unknown_present", [False, True])
def test_direct_bearer_fixture_bindings_use_complete_serialized_source_component(
    tmp_path: Path, unknown_present: bool
) -> None:
    """Bind two newline-bearing fixtures from inspected component JSON and refuse an unselected sibling."""
    first = "fixture-alpha"
    second = "fixture-beta"
    unknown = "unselected-fixture-value"
    fixture_source = f'first = """Authorization: Bearer {first}\nnext text"""\n'
    if unknown_present:
        fixture_source += f"# Authorization: Bearer {unknown}\n"
    fixture_source += f"# Authorization: Bearer {second}\n"
    run = _build_review_inputs(tmp_path, fixture_source=fixture_source)
    receipt = json.loads((run / "local-source/review-worktree.json").read_bytes())
    source_root = Path(receipt["review_worktree"])
    retained = json.loads((run / "local-source/source-snapshot.json").read_bytes())
    component = next(record for record in retained["files"] if record["path"] == "widget.py")
    assert component["content"] == (source_root / "widget.py").read_bytes().decode("utf-8")
    # Encode the independently inspected complete record, including the comma after its content value.
    encoded = json.dumps(component, indent=2, sort_keys=True)
    matched_values = re.findall(r"(?i)authorization\s*:\s*bearer\s+\S+", encoded)
    known_values = [f"Authorization: Bearer {first}\\nnext", f'Authorization: Bearer {second}\\n",']
    assert [value for value in matched_values if first in value or second in value] == known_values
    assert len(matched_values) == 2 + int(unknown_present)
    refused = _prepare(run, paged=False)
    assert refused.returncode != 0
    assert "review-direct-sensitive-material" in refused.stderr
    routing_path = run / "review-routing.json"
    routing_before = routing_path.read_bytes()
    discovered = _prepare(run, paged=False, discover_fixtures=True)
    assert discovered.returncode == 0, discovered.stderr
    discovery = json.loads(discovered.stdout)
    entry = next(item for item in discovery["inputs"] if item["input_id"] == "direct-plan:file:0")
    assert entry["input_id"] == "direct-plan:file:0"
    assert len(entry["matches"]) == len(matched_values)
    assert re.search(r"[0-9a-f]{64}", discovered.stdout + discovered.stderr) is None
    assert all(value not in discovered.stdout + discovered.stderr for value in (first, second, unknown))
    selections = {
        "schema_version": 1,
        "inputs": [
            {
                "input_id": "direct-plan:file:0",
                "complete_input_verified": True,
                "verification_evidence": "Parent inspected the complete current synthetic source component and plan inputs.",
                "matches": [
                    {**match, "match_sha256": hashlib.sha256(value.encode()).hexdigest()}
                    for match, value in zip(entry["matches"], matched_values, strict=True)
                    if value in known_values
                ],
            }
        ],
    }
    assert len(selections["inputs"][0]["matches"]) == 2
    diff_entry = next(item for item in discovery["inputs"] if item["input_id"] == "direct-diff:section:0")
    diff_values = re.findall(r"(?i)authorization\s*:\s*bearer\s+\S+", (run / "diff.patch").read_bytes().decode("utf-8"))
    selections["inputs"].append(
        {
            "input_id": "direct-diff:section:0",
            "complete_input_verified": True,
            "verification_evidence": "Parent inspected the complete synthetic diff and every fixture occurrence.",
            "matches": [
                {**match, "match_sha256": hashlib.sha256(value.encode()).hexdigest()}
                for match, value in zip(diff_entry["matches"], diff_values, strict=True)
                if unknown not in value
            ],
        }
    )
    source_entry = next(item for item in discovery["inputs"] if item["input_id"].startswith("direct-source:"))
    source_values = re.findall(r"(?i)authorization\s*:\s*bearer\s+\S+", component["content"])
    selections["inputs"].append(
        {
            "input_id": source_entry["input_id"],
            "complete_input_verified": True,
            "verification_evidence": "Parent inspected the complete synthetic source and its verified source identity.",
            "matches": [
                {**match, "match_sha256": hashlib.sha256(value.encode()).hexdigest()}
                for match, value in zip(source_entry["matches"], source_values, strict=True)
                if unknown not in value
            ],
        }
    )
    selection_path = run / "verified-bearer-selections.json"
    selection_path.write_text(json.dumps(selections), encoding="utf-8", newline="\n")
    exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
    assert routing_path.read_bytes() == routing_before
    assert not (run / "direct-plan.json").exists()
    assert not (run / "dispatch.json").exists()
    assert all(value not in exported.stdout + exported.stderr for value in (first, second, unknown))
    if unknown_present:
        assert exported.returncode != 0
        assert exported.stdout == ""
        assert "review-fixture-selection-unverified" in exported.stderr
        assert re.search(r"[0-9a-f]{64}", exported.stderr) is None
        return
    assert exported.returncode == 0, exported.stderr
    proposal = json.loads(exported.stdout)
    bindings = next(item["bindings"] for item in proposal["inputs"] if item["input_id"] == "direct-plan:file:0")
    assert [binding["match_sha256"] for binding in bindings] == [
        hashlib.sha256(value.encode()).hexdigest() for value in known_values
    ]
    routing = json.loads(routing_before)
    routing["inert_fixture_admissions"] = [
        {
            **binding,
            "approval_evidence": "Parent obtained explicit approval for both occurrences in this exact complete input.",
            "rationale": "Both values and their surrounding input are synthetic and have no credential authority.",
        }
        for item in proposal["inputs"]
        for binding in item["bindings"]
    ]
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    prepared = _prepare(run, paged=False)
    assert prepared.returncode == 0, prepared.stderr
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    assert plan["source_snapshot"]["files"] == [component]
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
    validator = runpy.run_path(str(HELPER))["validator"]
    # The ordinary consumer authenticates the exact admitted input before requiring native child provenance.
    with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")


@pytest.mark.integration
def test_paged_fixture_admissions_flow_from_collection_to_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allow only approved occurrences in real frozen source contexts, then recheck them at consumption."""
    fixture = "sk-" + "a" * 24
    run = _build_review_inputs(tmp_path, fixture_source=f"example = '{fixture}'\n")
    source_root = Path(json.loads((run / "local-source/review-worktree.json").read_bytes())["review_worktree"])
    producer = runpy.run_path(str(HELPER))
    validator = producer["validator"]
    scan = validator.review_input_has_sensitive_material
    captured: list[str] = []

    def capture(text: str, admissions: object) -> bool:
        """Retain scanner input while preserving its actual decision."""
        captured.append(text)
        return scan(text, admissions)

    monkeypatch.setattr(validator, "review_input_has_sensitive_material", capture)
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    admissions: list[dict[str, object]] = []
    for _ in routing["triggered_roles"]:
        with pytest.raises(ValueError, match="review-context-sensitive-material"):
            producer["prepare"](run, "bounded-review", "parent", source_root, paged=True)
        context = captured[-1]
        matches = list(re.finditer(re.escape(fixture), context))
        assert matches
        admissions.extend(
            {
                "input_sha256": hashlib.sha256(context.encode()).hexdigest(),
                "match_start": match.start(),
                "match_end": match.end(),
                "match_sha256": hashlib.sha256(match.group().encode()).hexdigest(),
                "approval_evidence": "Parent approved this inert test fixture.",
                "rationale": "Synthetic fixture without credential authority.",
            }
            for match in matches
        )
        routing["inert_fixture_admissions"] = admissions
        routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")

    prepared = _prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    plan_path = run / "inspection-plan.json"
    plan = json.loads(plan_path.read_bytes())
    assert plan["inert_fixture_admissions"] == admissions
    validator.validate_inspection_contexts(plan, plan_path, context_limit=None)
    plan["inert_fixture_admissions"] = admissions[1:]
    with pytest.raises((SystemExit, ValueError), match="sensitive"):
        validator.validate_inspection_contexts(plan, plan_path, context_limit=None)


@pytest.mark.integration
def test_direct_fixture_admission_binds_frozen_plan_and_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct preparation and consumer enforce exact admissions without a circular routing digest."""
    fixture = "sk-" + "b" * 24
    run = _build_review_inputs(tmp_path, fixture_source=f"example = '{fixture}'\n")
    source_root = Path(json.loads((run / "local-source/review-worktree.json").read_bytes())["review_worktree"])
    producer = runpy.run_path(str(HELPER))
    validator = producer["validator"]
    scan = validator.review_input_has_sensitive_material
    captured: list[str] = []

    def capture(text: str, admissions: object) -> bool:
        """Retain the exact producer input without changing the scanner's result."""
        captured.append(text)
        return scan(text, admissions)

    monkeypatch.setattr(validator, "review_input_has_sensitive_material", capture)
    with pytest.raises(ValueError, match="review-direct-sensitive-material"):
        producer["prepare"](run, "bounded-review", "parent", source_root)
    context = captured[-1]
    matches = list(re.finditer(re.escape(fixture), context))
    assert matches
    admissions = [
        {
            "input_sha256": hashlib.sha256(context.encode()).hexdigest(),
            "match_start": match.start(),
            "match_end": match.end(),
            "match_sha256": hashlib.sha256(match.group().encode()).hexdigest(),
            "approval_evidence": "Parent approved this inert test fixture.",
            "rationale": "Synthetic fixture without credential authority.",
        }
        for match in matches
    ]
    discovered = _prepare(run, paged=False, discover_fixtures=True)
    assert discovered.returncode == 0, discovered.stderr
    selections = {
        "schema_version": 1,
        "inputs": [
            {
                "input_id": item["input_id"],
                "complete_input_verified": True,
                "verification_evidence": "Parent inspected this complete synthetic input and its identity.",
                "matches": [
                    {**match, "match_sha256": hashlib.sha256(fixture.encode()).hexdigest()} for match in item["matches"]
                ],
            }
            for item in json.loads(discovered.stdout)["inputs"]
            if item["matches"]
        ],
    }
    selection_path = run / "verified-plan-and-sibling-inputs.json"
    selection_path.write_bytes(json.dumps(selections).encode())
    exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
    assert exported.returncode == 0, exported.stderr
    proposal = json.loads(exported.stdout)
    plan_bindings = next(item["bindings"] for item in proposal["inputs"] if item["input_id"] == "direct-plan:file:0")
    assert plan_bindings == [
        {key: value for key, value in admission.items() if key not in {"approval_evidence", "rationale"}}
        for admission in admissions
    ]
    admissions = [
        {
            **binding,
            "approval_evidence": "Parent approved this exact complete synthetic input identity and occurrence.",
            "rationale": "Synthetic fixture without credential authority.",
        }
        for item in proposal["inputs"]
        for binding in item["bindings"]
    ]
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing["inert_fixture_admissions"] = admissions
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    prepared = _prepare(run, paged=False)
    assert prepared.returncode == 0, prepared.stderr
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    assert plan["schema_version"] == 3
    assert plan["inert_fixture_admissions"] == admissions
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
    # The next guard requires real child provenance; reaching it proves the consumer accepted this exact plan.
    with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")
    plan["inert_fixture_admissions"] = []
    plan_path.write_bytes(producer["_json_bytes"](plan))
    manifest["direct_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="review-direct-fixture-admissions-mismatch"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")
    routing["inert_fixture_admissions"] = []
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    plan["routing_sha256"] = hashlib.sha256(routing_path.read_bytes()).hexdigest()
    plan_path.write_bytes(producer["_json_bytes"](plan))
    manifest["direct_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")


@pytest.mark.integration
@pytest.mark.parametrize("input_kind", ["axis", "evidence"])
def test_direct_brief_fixture_requires_exact_approval_through_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, input_kind: str
) -> None:
    """Consume an approved inert brief with native provenance, then reject revoked occurrence authority."""
    import test_review_direct as direct  # Reuse the ordinary direct runtime evidence boundary.

    fixture = "sk-" + "e" * 24

    def prepare_approved(run: Path) -> subprocess.CompletedProcess[str]:
        """Prepare the exact inspected fixture before the runtime evidence recorder observes its task."""
        if input_kind == "axis":
            path = run / "review-briefs.json"
            briefs = json.loads(path.read_bytes())
            briefs["challenger"]["axis"] = fixture
            path.write_bytes((json.dumps(briefs) + "\n").encode())
        else:
            (run / "challenger-evidence.md").write_bytes(("Synthetic fixture: " + fixture + "\r\n").encode())
        runpy.run_path(str(HELPER))["review_routing"].synchronize_routing(run)
        discovery = _prepare(run, paged=False, discover_fixtures=True)
        assert discovery.returncode == 0, discovery.stderr
        assert fixture not in discovery.stdout + discovery.stderr
        assert re.search(r"[0-9a-f]{64}", discovery.stdout) is None
        entries = [item for item in json.loads(discovery.stdout)["inputs"] if item["matches"]]
        assert [item["input_id"] for item in entries] == [
            "direct-task:challenger" if input_kind == "axis" else "direct-brief:challenger"
        ]
        selections = {
            "schema_version": 1,
            "inputs": [
                {
                    "input_id": item["input_id"],
                    "complete_input_verified": True,
                    "verification_evidence": "Parent inspected this complete synthetic reviewer input.",
                    "matches": [
                        {**match, "match_sha256": hashlib.sha256(fixture.encode()).hexdigest()}
                        for match in item["matches"]
                    ],
                }
                for item in entries
            ],
        }
        selection_path = run / "verified-direct-selections.json"
        selection_path.write_bytes(json.dumps(selections).encode())
        exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
        assert exported.returncode == 0, exported.stderr
        routing_path = run / "review-routing.json"
        routing = json.loads(routing_path.read_bytes())
        routing["inert_fixture_admissions"] = [
            {
                **binding,
                "approval_evidence": "Parent approved this exact complete inert input and occurrence.",
                "rationale": "Synthetic text with no credential authority.",
            }
            for item in json.loads(exported.stdout)["inputs"]
            for binding in item["bindings"]
        ]
        routing_path.write_bytes(json.dumps(routing).encode())
        approved_path = run / ("review-briefs.json" if input_kind == "axis" else "challenger-evidence.md")
        approved_bytes = approved_path.read_bytes()
        if input_kind == "axis":
            changed_briefs = json.loads(approved_bytes)
            changed_briefs["challenger"]["axis"] = "Changed context " + fixture
            approved_path.write_bytes(json.dumps(changed_briefs).encode())
        else:
            approved_path.write_bytes(approved_bytes + b"Changed context.\n")
        changed = _prepare(run, paged=False)
        assert changed.returncode != 0
        assert "review-direct-sensitive-material" in changed.stderr
        assert fixture not in changed.stdout + changed.stderr
        approved_path.write_bytes(approved_bytes)
        sibling = run / "qa-specialist-evidence.md"
        sibling_bytes = sibling.read_bytes()
        sibling.write_bytes(sibling_bytes + fixture.encode())
        transferred = _prepare(run, paged=False)
        assert transferred.returncode != 0
        assert "review-direct-sensitive-material" in transferred.stderr
        assert fixture not in transferred.stdout + transferred.stderr
        sibling.write_bytes(sibling_bytes)
        assert not (run / "dispatch.json").exists()
        return _prepare(run, paged=False)

    monkeypatch.setattr(direct, "_direct_prepare", prepare_approved)
    run, home = direct._direct_evidence(tmp_path)
    assembled = direct._assemble_direct(run, home)
    assert assembled.returncode == 0, assembled.stderr
    assert fixture not in assembled.stdout + assembled.stderr
    producer = runpy.run_path(str(HELPER))
    validator = producer["validator"]
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing["inert_fixture_admissions"] = []
    routing_path.write_bytes(json.dumps(routing).encode())
    plan["inert_fixture_admissions"] = []
    plan["routing_sha256"] = hashlib.sha256(routing_path.read_bytes()).hexdigest()
    plan_path.write_bytes(producer["_json_bytes"](plan))
    manifest["direct_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
        validator._validate_direct_review(
            run, manifest, manifest["passes"], set(routing["triggered_roles"]), home, "parent"
        )


@pytest.mark.integration
@pytest.mark.parametrize("input_kind", ["task", "evidence", "role-card", "diff"])
def test_direct_consumer_refuses_unadmitted_delivered_inputs(tmp_path: Path, input_kind: str) -> None:
    """Rehashed hand-built evidence cannot bypass scans of actual reviewer-readable inputs."""
    fixture = "sk-" + "f" * 24
    run = _build_review_inputs(tmp_path)
    prepared = _prepare(run, paged=False)
    assert prepared.returncode == 0, prepared.stderr
    producer = runpy.run_path(str(HELPER))
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    context = next(item for item in plan["contexts"] if item["role_id"] == "challenger")
    paths = {
        "task": run / context["context_path"],
        "evidence": run / "challenger-evidence.md",
        "role-card": run / "role-cards/challenger/ROLE.md",
        "diff": run / "diff.patch",
    }
    path = paths[input_kind]
    path.write_bytes(path.read_bytes() + ("\n" + fixture).encode())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if input_kind == "task":
        context["context_sha256"] = digest
    elif input_kind == "evidence":
        plan["brief_evidence"]["challenger-evidence.md"] = digest
    elif input_kind == "role-card":
        context["role_card_sha256"] = digest
    else:
        plan["review_input_sha256"] = digest
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
    with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
        producer["validator"]._validate_direct_review(
            run, manifest, [], {"challenger", "qa-specialist"}, tmp_path, "parent"
        )


@pytest.mark.integration
def test_direct_prepare_refuses_raw_sensitive_unchanged_selected_source(tmp_path: Path) -> None:
    """Raw whitespace in a selected unchanged caller must not disappear behind JSON escaping."""
    source = 'fixture = """Authorization:\r\nBearer inert-source-fixture\r\n"""\r\n'
    run = _build_review_inputs(tmp_path, unchanged_caller=True, unchanged_caller_content=source)
    briefs_path = run / "review-briefs.json"
    briefs = json.loads(briefs_path.read_bytes())
    briefs["challenger"]["source_paths"].append("stable.py")
    briefs_path.write_bytes(json.dumps(briefs).encode())
    producer = runpy.run_path(str(HELPER))
    scanner = producer["validator"].review_input_has_sensitive_material
    assert scanner(source, [])
    assert not scanner(json.dumps({"content": source}), [])
    assert "inert-source-fixture" not in (run / "diff.patch").read_bytes().decode()
    result = _prepare(run, paged=False)
    assert "inert-source-fixture" not in result.stdout + result.stderr
    assert result.returncode != 0
    assert "review-direct-sensitive-material" in result.stderr
    assert not (run / "dispatch.json").exists()


@pytest.mark.integration
def test_direct_raw_source_fixture_discovery_and_consumer_bind_exact_crlf_input(tmp_path: Path) -> None:
    """Approve raw unchanged UTF-8 evidence independently of its escaped plan representation."""
    source = 'fixture = """Authorization:\r\nBearer inert-source-fixture\r\n"""\r\n'
    run = _build_review_inputs(tmp_path, unchanged_caller=True, unchanged_caller_content=source)
    briefs_path = run / "review-briefs.json"
    briefs = json.loads(briefs_path.read_bytes())
    briefs["challenger"]["source_paths"].append("stable.py")
    briefs_path.write_bytes(json.dumps(briefs).encode())
    runpy.run_path(str(HELPER))["review_routing"].synchronize_routing(run)
    discovered = _prepare(run, paged=False, discover_fixtures=True)
    assert discovered.returncode == 0, discovered.stderr
    assert "inert-source-fixture" not in discovered.stdout + discovered.stderr
    assert "stable.py" not in discovered.stdout + discovered.stderr
    assert re.search(r"[0-9a-f]{64}", discovered.stdout) is None
    matched = [item for item in json.loads(discovered.stdout)["inputs"] if item["matches"]]
    assert len(matched) == 1
    assert re.fullmatch(r"direct-source:[0-9]+", matched[0]["input_id"])
    raw_match = "Authorization:\r\nBearer inert-source-fixture"
    selections = {
        "schema_version": 1,
        "inputs": [
            {
                "input_id": matched[0]["input_id"],
                "complete_input_verified": True,
                "verification_evidence": "Parent inspected the complete unchanged synthetic source and its raw whitespace.",
                "matches": [
                    {**match, "match_sha256": hashlib.sha256(raw_match.encode()).hexdigest()}
                    for match in matched[0]["matches"]
                ],
            }
        ],
    }
    selection_path = run / "verified-raw-source.json"
    selection_path.write_bytes(json.dumps(selections).encode())
    exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
    assert exported.returncode == 0, exported.stderr
    bindings = json.loads(exported.stdout)["inputs"][0]["bindings"]
    assert len(bindings) == 1
    producer = runpy.run_path(str(HELPER))
    receipt = json.loads((run / "local-source/review-worktree.json").read_bytes())
    source_input = (
        producer["_json_bytes"]({"source_root": receipt["review_worktree"], "path": "stable.py"}).decode()
        + "\n--- UTF-8 source ---\n"
        + source
    )
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing["inert_fixture_admissions"] = [
        {
            **binding,
            "approval_evidence": "Parent approved this exact complete inert raw source occurrence.",
            "rationale": "Synthetic source without credential authority.",
        }
        for binding in bindings
    ]
    routing_path.write_bytes(json.dumps(routing).encode())
    prepared = _prepare(run, paged=False)
    assert prepared.returncode == 0, prepared.stderr
    producer = runpy.run_path(str(HELPER))
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    record = next(item for item in plan["source_snapshot"]["files"] if item["path"] == "stable.py")
    assert record["content"] == source
    framed_source = producer["_direct_review_inputs"](run, plan)[matched[0]["input_id"]]
    assert framed_source.split("\n--- Review input ---\n", 1)[1] == source_input
    assert bindings[0]["input_sha256"] == hashlib.sha256(framed_source.encode()).hexdigest()
    assert framed_source[bindings[0]["match_start"] : bindings[0]["match_end"]] == raw_match
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
    # Acceptance of this raw source reaches the separate native-role provenance gate.
    with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")
    retained_plan = plan_path.read_bytes()
    record["path"] = "different-source.py"
    plan_path.write_bytes(producer["_json_bytes"](plan))
    manifest["direct_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")
    plan_path.write_bytes(retained_plan)
    plan = json.loads(retained_plan)
    routing["inert_fixture_admissions"] = []
    routing_path.write_bytes(json.dumps(routing).encode())
    plan["inert_fixture_admissions"] = []
    plan["routing_sha256"] = hashlib.sha256(routing_path.read_bytes()).hexdigest()
    plan_path.write_bytes(producer["_json_bytes"](plan))
    manifest["direct_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")


@pytest.mark.integration
def test_direct_identical_briefs_require_separate_input_approvals(tmp_path: Path) -> None:
    """Approval of one complete brief cannot authorize identical bytes at another brief path."""
    fixture = "sk-" + "q" * 24
    run = _build_review_inputs(tmp_path)
    raw = ("Synthetic brief:\r\n" + fixture + "\r\n").encode()
    for role in ("challenger", "qa-specialist"):
        (run / f"{role}-evidence.md").write_bytes(raw)
    producer = runpy.run_path(str(HELPER))
    producer["review_routing"].synchronize_routing(run)
    discovered = _prepare(run, paged=False, discover_fixtures=True)
    assert discovered.returncode == 0, discovered.stderr
    assert fixture not in discovered.stdout + discovered.stderr
    entries = [item for item in json.loads(discovered.stdout)["inputs"] if item["matches"]]
    assert {item["input_id"] for item in entries} == {"direct-brief:challenger", "direct-brief:qa-specialist"}
    selected = next(item for item in entries if item["input_id"] == "direct-brief:challenger")
    selections = {
        "schema_version": 1,
        "inputs": [
            {
                "input_id": selected["input_id"],
                "complete_input_verified": True,
                "verification_evidence": "Parent inspected only the challenger brief as a complete inert input.",
                "matches": [
                    {**match, "match_sha256": hashlib.sha256(fixture.encode()).hexdigest()}
                    for match in selected["matches"]
                ],
            }
        ],
    }
    selection_path = run / "verified-one-brief.json"
    selection_path.write_bytes(json.dumps(selections).encode())
    exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
    assert exported.returncode == 0, exported.stderr
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing["inert_fixture_admissions"] = [
        {
            **binding,
            "approval_evidence": "Parent approved only this exact challenger brief and occurrence.",
            "rationale": "This verified input is synthetic and has no credential authority.",
        }
        for item in json.loads(exported.stdout)["inputs"]
        for binding in item["bindings"]
    ]
    routing_path.write_bytes(json.dumps(routing).encode())
    prepared = _prepare(run, paged=False)
    assert fixture not in prepared.stdout + prepared.stderr
    assert prepared.returncode != 0
    assert "review-direct-sensitive-material" in prepared.stderr
    assert not (run / "dispatch.json").exists()
    selections["inputs"] = [
        {
            "input_id": item["input_id"],
            "complete_input_verified": True,
            "verification_evidence": "Parent inspected each complete current synthetic brief and its separate identity.",
            "matches": [
                {**match, "match_sha256": hashlib.sha256(fixture.encode()).hexdigest()} for match in item["matches"]
            ],
        }
        for item in entries
    ]
    selection_path.write_bytes(json.dumps(selections).encode())
    exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
    assert exported.returncode == 0, exported.stderr
    proposals = json.loads(exported.stdout)["inputs"]
    assert len({binding["input_sha256"] for item in proposals for binding in item["bindings"]}) == 2
    routing["inert_fixture_admissions"] = [
        {
            **binding,
            "approval_evidence": "Parent approved this separate complete brief input and occurrence.",
            "rationale": "This verified input is synthetic and has no credential authority.",
        }
        for item in proposals
        for binding in item["bindings"]
    ]
    routing_path.write_bytes(json.dumps(routing).encode())
    prepared = _prepare(run, paged=False)
    assert prepared.returncode == 0, prepared.stderr
    plan_path = run / "direct-plan.json"
    original_plan = plan_path.read_bytes()
    plan = json.loads(original_plan)
    manifest = {
        "manifest_kind": "direct-review",
        "parent_thread_id": "parent",
        "review_run_id": plan["review_run_id"],
        "review_input_sha256": plan["review_input_sha256"],
        "direct_execution": {"plan_path": "direct-plan.json", "plan_sha256": hashlib.sha256(original_plan).hexdigest()},
    }
    validator = producer["validator"]
    with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")
    briefs_path = run / "review-briefs.json"
    original_briefs = briefs_path.read_bytes()
    briefs = json.loads(original_briefs)
    alias_path = "same-role-different-brief.md"
    (run / alias_path).write_bytes(raw)
    briefs["challenger"]["evidence_path"] = alias_path
    briefs_path.write_bytes(json.dumps(briefs).encode())
    plan["briefs_sha256"] = hashlib.sha256(briefs_path.read_bytes()).hexdigest()
    plan["brief_evidence"][alias_path] = hashlib.sha256(raw).hexdigest()
    plan_path.write_bytes(producer["_json_bytes"](plan))
    manifest["direct_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")
    briefs_path.write_bytes(original_briefs)
    plan = json.loads(original_plan)
    challenger_digest = next(
        item["bindings"][0]["input_sha256"] for item in proposals if item["input_id"] == "direct-brief:challenger"
    )
    routing["inert_fixture_admissions"] = [
        item for item in routing["inert_fixture_admissions"] if item["input_sha256"] == challenger_digest
    ]
    routing_path.write_bytes(json.dumps(routing).encode())
    plan["inert_fixture_admissions"] = routing["inert_fixture_admissions"]
    plan["routing_sha256"] = hashlib.sha256(routing_path.read_bytes()).hexdigest()
    plan_path.write_bytes(producer["_json_bytes"](plan))
    manifest["direct_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
        validator._validate_direct_review(run, manifest, [], set(routing["triggered_roles"]), tmp_path, "parent")


@pytest.mark.integration
@pytest.mark.parametrize("representation", ["lf", "crlf", "json-value", "json-key", "arguments"])
def test_direct_retry_refuses_sensitive_failure_strings(tmp_path: Path, representation: str) -> None:
    """Strict retry failure checks inspect raw and nested decoded strings before freezing any evidence."""
    import test_review_direct as direct  # Reuse the real retry CLI boundary.

    raw = "Authorization:" + ("\r\n" if representation == "crlf" else "\n") + "Bearer inert-retry-fixture"
    if representation in {"lf", "crlf"}:
        output = "collab spawn failed: transport unavailable\n" + raw
    elif representation == "json-value":
        output = json.dumps({"isError": True, "details": [{"diagnostic": raw}]})
    elif representation == "json-key":
        output = json.dumps({"isError": True, "details": [{raw: "ignored"}]})
    else:
        output = "collab spawn failed: transport unavailable"
    run, home, _, _, role = _direct_no_child_failure(tmp_path, output)
    if representation == "arguments":
        parent_path = home / "sessions/rollout-parent.jsonl"
        parent = [json.loads(line) for line in parent_path.read_bytes().decode().splitlines()]
        call = next(
            row
            for row in parent
            if row.get("payload", {}).get("call_id") == "spawn-0" and row["payload"].get("type") == "function_call"
        )
        arguments = json.loads(call["payload"]["arguments"])
        arguments["diagnostic"] = {"events": [raw]}
        call["payload"]["arguments"] = json.dumps(arguments)
        _write_jsonl(parent_path, parent)
    refused = direct._retry_direct(run, home, role)
    assert "inert-retry-fixture" not in refused.stdout + refused.stderr
    assert refused.returncode != 0
    assert "review-direct-failure-sensitive-material" in refused.stderr
    assert not (run / f"retry/{role}.failure.json").exists()
    assert not (run / f"retry/{role}.json").exists()


@pytest.mark.integration
def test_direct_safe_no_child_failure_requires_fresh_review(tmp_path: Path) -> None:
    """An unauthenticated failed launch cannot support a current-schema retry."""
    import test_review_direct as direct

    run, home, _, _, role = _direct_no_child_failure(tmp_path, "collab spawn failed: transport unavailable")
    refused = direct._retry_direct(run, home, role)
    assert refused.returncode != 0
    assert "review-direct-failure-no-child-rerun-fresh" in refused.stderr
    assert not (run / f"retry/{role}.json").exists()


@pytest.mark.integration
def test_direct_historical_retry_consumer_rejects_rehashed_sensitive_receipt(tmp_path: Path) -> None:
    """Historical no-child retry receipts remain subject to consumer-side sensitive scanning."""
    import test_review_direct as direct

    run, home, child, final, role = _direct_no_child_failure(tmp_path, "collab spawn failed: transport unavailable")
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan["schema_version"] = 1
    plan.pop("model_selection")
    for context in plan["contexts"]:
        card_path = run / "role-cards" / context["role_id"] / "ROLE.md"
        card = card_path.read_text(encoding="utf-8").replace("---\n", "---\nmodel: test-parent-model\n", 1)
        card_path.write_text(card, encoding="utf-8")
        context["role_card_sha256"] = hashlib.sha256(card_path.read_bytes()).hexdigest()
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_bytes().decode().splitlines()]
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("type") == "function_call" and payload.get("name", "").endswith("spawn_agent"):
            arguments = json.loads(payload["arguments"])
            arguments["model"] = "test-parent-model"
            payload["arguments"] = json.dumps(arguments)
    _write_jsonl(parent_path, parent)
    prepared = direct._retry_direct(run, home, role)
    assert prepared.returncode == 0, prepared.stderr
    retry = json.loads(prepared.stdout)
    direct._complete_direct_retry(home, child, retry, final)
    clean = direct._assemble_direct(run, home)
    assert clean.returncode == 0, clean.stderr
    (run / "specialist-manifest.json").unlink()
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_bytes().decode().splitlines()]
    receipt = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == "spawn-0" and row["payload"].get("type") == "function_call_output"
    )
    receipt["payload"]["output"] += "\nAuthorization:\r\nBearer inert-retry-fixture"
    _write_jsonl(parent_path, parent)
    failure_path = run / retry["failure_path"]
    failure = json.loads(failure_path.read_bytes())
    failure["receipt"] = receipt
    producer = runpy.run_path(str(HELPER))
    failure_path.write_bytes(producer["_json_bytes"](failure))
    retry["failure_sha256"] = hashlib.sha256(failure_path.read_bytes()).hexdigest()
    (run / f"retry/{role}.json").write_bytes(producer["_json_bytes"](retry))
    refused = direct._assemble_direct(run, home)
    assert "inert-retry-fixture" not in refused.stdout + refused.stderr
    assert refused.returncode != 0
    assert "review-direct-failure-sensitive-material" in refused.stderr
    assert not (run / "specialist-manifest.json").exists()


@pytest.mark.integration
def test_direct_retry_consumer_rejects_rehashed_multiline_child_failure(tmp_path: Path) -> None:
    """Retry assembly must independently scan authenticated, rehashed child failure evidence."""
    import test_review_direct as direct  # Reuse the real producer and native retry lifecycle.

    run, home, child, final, role = _direct_aborted_child_failure(tmp_path)
    prepared = direct._retry_direct(run, home, role)
    assert prepared.returncode == 0, prepared.stderr
    retry = json.loads(prepared.stdout)
    direct._complete_direct_retry(home, child, retry, final)
    child_path = home / "sessions/rollout-child-0.jsonl"
    child = [json.loads(line) for line in child_path.read_bytes().decode().splitlines()]
    child[-1]["payload"]["reason"] = "Authorization:\r\nBearer inert-retry-fixture"
    _write_jsonl(child_path, child)
    failure_path = run / retry["failure_path"]
    failure = json.loads(failure_path.read_bytes())
    failure["child"] = child
    producer = runpy.run_path(str(HELPER))
    failure_path.write_bytes(producer["_json_bytes"](failure))
    retry["failure_sha256"] = hashlib.sha256(failure_path.read_bytes()).hexdigest()
    (run / f"retry/{role}.json").write_bytes(producer["_json_bytes"](retry))
    refused = direct._assemble_direct(run, home)
    assert "inert-retry-fixture" not in refused.stdout + refused.stderr
    assert refused.returncode != 0
    assert "review-direct-failure-sensitive-material" in refused.stderr
    assert not (run / "specialist-manifest.json").exists()


@pytest.mark.integration
@pytest.mark.parametrize("sensitive_output", [False, True])
def test_direct_safe_failure_retry_preserves_strict_reviewer_output(tmp_path: Path, sensitive_output: bool) -> None:
    """Safe decoded failure evidence permits recovery while sibling reviewer output remains strictly scanned."""
    import test_review_direct as direct  # Reuse the real retry lifecycle and assembly CLI.

    run, home, child, final, role = _direct_aborted_child_failure(tmp_path)
    prepared = direct._retry_direct(run, home, role)
    assert prepared.returncode == 0, prepared.stderr
    retry = json.loads(prepared.stdout)
    if sensitive_output:
        final += "\nAuthorization:\r\nBearer inert-retry-fixture"
    direct._complete_direct_retry(home, child, retry, final)
    assembled = direct._assemble_direct(run, home)
    assert "inert-retry-fixture" not in assembled.stdout + assembled.stderr
    if sensitive_output:
        assert assembled.returncode != 0
        assert "review-output-sensitive-material" in assembled.stderr
        assert not (run / "specialist-manifest.json").exists()
    else:
        assert assembled.returncode == 0, assembled.stderr
        assert (run / "specialist-manifest.json").is_file()


@pytest.mark.integration
@pytest.mark.parametrize("representation", ["raw-reason", "custom-output"])
def test_direct_aborted_child_failure_refuses_raw_nested_sensitive_output(tmp_path: Path, representation: str) -> None:
    """Raw and native content-block output must not become sensitive retained retry evidence."""
    import test_review_direct as direct  # Reuse actual native aborted-child evidence.

    run, home = direct._direct_evidence(tmp_path)
    child_path = home / "sessions/rollout-child-0.jsonl"
    child = [json.loads(line) for line in child_path.read_bytes().decode().splitlines()]
    original_path = child[0]["payload"]["agent_path"]
    child[-1]["payload"] = {"type": "turn_aborted", "turn_id": "turn-0"}
    if representation == "raw-reason":
        child[-1]["payload"]["reason"] = {"events": [{"diagnostic": "Authorization:\nBearer inert-retry-fixture"}]}
    else:
        output_path = tmp_path / "native-tool-response.txt"
        output_path.write_bytes(b"Authorization:\r\nBearer inert-retry-fixture")
        command = [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())",
            str(output_path),
        ]
        completed = subprocess.run(command, capture_output=True, check=True)
        assert completed.stdout == output_path.read_bytes()
        # Match the supported native exec content-block envelope, retaining real command stdout inside JSON.
        child[-1:-1] = [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "nested-sensitive-output",
                    "input": "const r = await tools.exec_command("
                    + json.dumps({"cmd": shlex.join(command)})
                    + "); text(r);",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "nested-sensitive-output",
                    "output": [
                        {"type": "input_text", "text": "Script completed\nOutput:\n"},
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {"output": completed.stdout.decode("utf-8"), "exit_code": completed.returncode}
                            ),
                        },
                    ],
                },
            },
        ]
    _write_jsonl(child_path, child)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_bytes().decode().splitlines()]
    parent = [row for row in parent if row.get("payload", {}).get("author") != original_path]
    _write_jsonl(parent_path, parent)
    role = json.loads((run / "dispatch.json").read_bytes())["calls"][0]["role"]
    refused = direct._retry_direct(run, home, role)
    assert "inert-retry-fixture" not in refused.stdout + refused.stderr
    assert refused.returncode != 0
    assert "review-direct-failure-sensitive-material" in refused.stderr
    assert not (run / f"retry/{role}.failure.json").exists()


@pytest.mark.integration
@pytest.mark.parametrize("sensitive_receipt", [False, True])
def test_direct_retry_consumer_scans_custom_output_blocks(tmp_path: Path, sensitive_receipt: bool) -> None:
    """Safe native content blocks permit recovery; rehashed sensitive blocks fail ordinary consumer admission."""
    import test_review_direct as direct  # Reuse the native failed-child and retry lifecycle boundary.

    run, home = direct._direct_evidence(tmp_path)
    child_path = home / "sessions/rollout-child-0.jsonl"
    child = [json.loads(line) for line in child_path.read_bytes().decode().splitlines()]
    final = child[-1]["payload"]["last_agent_message"]
    original_path = child[0]["payload"]["agent_path"]
    child[-1]["payload"] = {"type": "turn_aborted", "turn_id": "turn-0"}
    custom_output = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": "original-custom-tool",
            "output": [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": json.dumps({"output": "Transport closed.\r\n", "exit_code": 0})},
            ],
        },
    }
    child[-1:-1] = [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "original-custom-tool",
                "input": "text({output: 'Transport closed.'});",
            },
        },
        custom_output,
    ]
    _write_jsonl(child_path, child)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_bytes().decode().splitlines()]
    parent = [row for row in parent if row.get("payload", {}).get("author") != original_path]
    _write_jsonl(parent_path, parent)
    role = json.loads((run / "dispatch.json").read_bytes())["calls"][0]["role"]
    prepared = direct._retry_direct(run, home, role)
    assert prepared.returncode == 0, prepared.stderr
    retry = json.loads(prepared.stdout)
    direct._complete_direct_retry(home, child, retry, final)
    if sensitive_receipt:
        custom_output["payload"]["output"][1]["text"] = json.dumps(
            {"output": "Authorization:\r\nBearer inert-retry-fixture", "exit_code": 0}
        )
        _write_jsonl(child_path, child)
        producer = runpy.run_path(str(HELPER))
        failure_path = run / retry["failure_path"]
        failure = json.loads(failure_path.read_bytes())
        failure["child"] = child
        failure_path.write_bytes(producer["_json_bytes"](failure))
        retry["failure_sha256"] = hashlib.sha256(failure_path.read_bytes()).hexdigest()
        (run / f"retry/{role}.json").write_bytes(producer["_json_bytes"](retry))
    result = direct._assemble_direct(run, home)
    assert "inert-retry-fixture" not in result.stdout + result.stderr
    if sensitive_receipt:
        assert result.returncode != 0
        assert "review-direct-failure-sensitive-material" in result.stderr
        assert not (run / "specialist-manifest.json").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (run / "specialist-manifest.json").is_file()
