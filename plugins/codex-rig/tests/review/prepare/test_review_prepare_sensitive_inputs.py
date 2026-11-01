"""Exercise sensitive fixture admission and direct review retry input boundaries."""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest
import test_review_direct as direct
from test_review_prepare import (
    HELPER,
    _build_review_inputs,
    _direct_aborted_child_failure,
    _direct_no_child_failure,
    _prepare,
    _write_jsonl,
)

_HEX64 = re.compile(r"[0-9a-f]{64}")
_BEARER = re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+")
_RAW_SOURCE = 'fixture = """Authorization:\r\nBearer inert-source-fixture\r\n"""\r\n'
_RETRY_FIXTURE = "inert-retry-fixture"
_NO_CHILD_FAILURE = "collab spawn failed: transport unavailable"


@pytest.fixture
def producer() -> dict[str, object]:
    """Load the review preparation helper fresh for one test, exposing its validator and internals."""
    return runpy.run_path(str(HELPER))


def _sha(value: str | bytes) -> str:
    """Return the hex SHA-256 digest of text (UTF-8) or bytes."""
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _load(path: Path) -> dict[str, object]:
    """Read a JSON file as bytes and decode it."""
    return json.loads(path.read_bytes())


def _store(path: Path, payload: object) -> None:
    """Write a payload as compact JSON bytes without platform newline translation."""
    path.write_bytes(json.dumps(payload).encode())


def _rows(path: Path) -> list[dict[str, object]]:
    """Read a JSON-lines rollout file into rows."""
    return [json.loads(line) for line in path.read_bytes().decode().splitlines()]


def _assert_no_leak(result: subprocess.CompletedProcess[str], *secrets: str) -> None:
    """Assert that no secret value appears in the command's stdout or stderr."""
    assert all(secret not in result.stdout + result.stderr for secret in secrets)


def _selection_input(item: dict[str, object], evidence: str, digest: str) -> dict[str, object]:
    """Build one verified-selection input that approves every match of a discovered input with one digest."""
    return {
        "input_id": item["input_id"],
        "complete_input_verified": True,
        "verification_evidence": evidence,
        "matches": [{**match, "match_sha256": digest} for match in item["matches"]],
    }


def _admissions(bindings: list[dict[str, object]], approval: str, rationale: str) -> list[dict[str, object]]:
    """Turn exported fixture bindings into inert-fixture admissions carrying the parent's approval."""
    return [{**binding, "approval_evidence": approval, "rationale": rationale} for binding in bindings]


def _proposal_bindings(exported: subprocess.CompletedProcess[str]) -> list[dict[str, object]]:
    """Flatten the bindings of every input in an exported fixture-binding proposal."""
    return [binding for item in json.loads(exported.stdout)["inputs"] for binding in item["bindings"]]


def _matched_inputs(discovered: subprocess.CompletedProcess[str]) -> list[dict[str, object]]:
    """Return the discovered inputs that contain at least one sensitive match."""
    return [item for item in json.loads(discovered.stdout)["inputs"] if item["matches"]]


def _admit(run: Path, admissions: list[dict[str, object]]) -> dict[str, object]:
    """Record inert-fixture admissions in the run's routing file and return the updated routing."""
    routing = _load(run / "review-routing.json")
    routing["inert_fixture_admissions"] = admissions
    _store(run / "review-routing.json", routing)
    return routing


def _direct_manifest(plan_path: Path, plan: dict[str, object]) -> dict[str, object]:
    """Build the direct-review manifest that binds a direct plan file by path and digest."""
    return {
        "manifest_kind": "direct-review",
        "parent_thread_id": "parent",
        "review_run_id": plan["review_run_id"],
        "review_input_sha256": plan["review_input_sha256"],
        "direct_execution": {"plan_path": "direct-plan.json", "plan_sha256": _sha(plan_path.read_bytes())},
    }


def _rebind_plan(
    producer: dict[str, object], plan_path: Path, plan: dict[str, object], manifest: dict[str, object]
) -> None:
    """Rewrite the direct plan and re-pin its digest in the manifest, as a rehashing attacker would."""
    plan_path.write_bytes(producer["_json_bytes"](plan))
    manifest["direct_execution"]["plan_sha256"] = _sha(plan_path.read_bytes())


def _validate_direct(
    producer: dict[str, object], run: Path, manifest: dict[str, object], roles: set[str], home: Path, passes: list = ()
) -> None:
    """Run the direct-review consumer over a manifest, raising the first admission failure."""
    producer["validator"]._validate_direct_review(run, manifest, list(passes), roles, home, "parent")


def _taint_axis(run: Path, secret: str) -> None:
    """Put a secret into the challenger's review axis."""
    briefs = _load(run / "review-briefs.json")
    briefs["challenger"]["axis"] = secret
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")


def _taint_evidence(run: Path, secret: str) -> None:
    """Make the challenger's evidence file consist of a secret."""
    (run / "challenger-evidence.md").write_text(secret, encoding="utf-8", newline="\n")


def _spy_on_scanner(monkeypatch: pytest.MonkeyPatch, validator: object) -> mock.Mock:
    """Wrap the validator's sensitive-material scan so each scanned input is recorded without changing its result."""
    spy = mock.Mock(wraps=validator.review_input_has_sensitive_material)
    monkeypatch.setattr(validator, "review_input_has_sensitive_material", spy)
    return spy


def _full_context_admissions(context: str, fixture: str, approval: str) -> list[dict[str, object]]:
    """Approve every occurrence of a fixture inside the exact scanned context text."""
    return [
        {
            "input_sha256": _sha(context),
            "match_start": match.start(),
            "match_end": match.end(),
            "match_sha256": _sha(match.group()),
            "approval_evidence": approval,
            "rationale": "Synthetic fixture without credential authority.",
        }
        for match in re.finditer(re.escape(fixture), context)
    ]


def _source_root(run: Path) -> Path:
    """Return the review worktree that holds the frozen source of a prepared run."""
    return Path(_load(run / "local-source/review-worktree.json")["review_worktree"])


@pytest.mark.integration
class TestSensitiveBriefInputs:
    """Sensitive reviewer input is refused until the parent approves its exact occurrence."""

    @pytest.mark.parametrize(
        "taint", [pytest.param(_taint_axis, id="axis"), pytest.param(_taint_evidence, id="evidence")]
    )
    def test_prepare_refuses_unadmitted_sensitive_brief(
        self, tmp_path: Path, taint: Callable[[Path, str], None]
    ) -> None:
        """Preparation refuses sensitive reviewer input before creating a dispatch and never echoes the value.

        A credential-shaped string placed in the review axis or in the evidence file is not covered by any admission, so
        the direct preparation must exit nonzero, leak nothing and leave no dispatch behind.
        """
        secret = "sk-" + "z" * 24
        run = _build_review_inputs(tmp_path)
        taint(run, secret)

        result = _prepare(run, paged=False)

        _assert_no_leak(result, secret)
        assert result.returncode != 0
        assert not (run / "dispatch.json").exists()

    def test_identical_briefs_require_separate_input_approvals(
        self, tmp_path: Path, producer: dict[str, object]
    ) -> None:
        """Approval of one complete brief cannot authorize identical bytes at another brief path.

        Two roles carry byte-identical briefs holding a fixture. Approving only the challenger brief must still refuse
        preparation; approving both separate inputs succeeds, and the consumer then refuses a rebinding of the
        challenger to a different brief path or a plan that keeps only the challenger admission.
        """
        fixture = "sk-" + "q" * 24
        run = _build_review_inputs(tmp_path)
        raw = ("Synthetic brief:\r\n" + fixture + "\r\n").encode()
        (run / "challenger-evidence.md").write_bytes(raw)
        (run / "qa-specialist-evidence.md").write_bytes(raw)
        producer["review_routing"].synchronize_routing(run)
        discovered = _prepare(run, paged=False, discover_fixtures=True)
        assert discovered.returncode == 0, discovered.stderr
        _assert_no_leak(discovered, fixture)
        entries = _matched_inputs(discovered)
        assert {item["input_id"] for item in entries} == {"direct-brief:challenger", "direct-brief:qa-specialist"}
        selection_path = run / "verified-one-brief.json"
        challenger = next(item for item in entries if item["input_id"] == "direct-brief:challenger")
        verified = "Parent inspected only the challenger brief as a complete inert input."
        _store(selection_path, {"schema_version": 1, "inputs": [_selection_input(challenger, verified, _sha(fixture))]})
        exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
        assert exported.returncode == 0, exported.stderr
        _admit(
            run,
            _admissions(
                _proposal_bindings(exported),
                "Parent approved only this exact challenger brief and occurrence.",
                "This verified input is synthetic and has no credential authority.",
            ),
        )

        partial = _prepare(run, paged=False)

        _assert_no_leak(partial, fixture)
        assert partial.returncode != 0
        assert "review-direct-sensitive-material" in partial.stderr
        assert not (run / "dispatch.json").exists()
        verified = "Parent inspected each complete current synthetic brief and its separate identity."
        both = [_selection_input(item, verified, _sha(fixture)) for item in entries]
        _store(selection_path, {"schema_version": 1, "inputs": both})
        exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
        assert exported.returncode == 0, exported.stderr
        proposals = json.loads(exported.stdout)["inputs"]
        assert len({binding["input_sha256"] for item in proposals for binding in item["bindings"]}) == 2
        routing = _admit(
            run,
            _admissions(
                _proposal_bindings(exported),
                "Parent approved this separate complete brief input and occurrence.",
                "This verified input is synthetic and has no credential authority.",
            ),
        )
        prepared = _prepare(run, paged=False)
        assert prepared.returncode == 0, prepared.stderr
        plan_path = run / "direct-plan.json"
        original_plan = plan_path.read_bytes()
        plan = json.loads(original_plan)
        manifest = _direct_manifest(plan_path, plan)
        roles = set(routing["triggered_roles"])
        with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
            _validate_direct(producer, run, manifest, roles, tmp_path)
        briefs_path = run / "review-briefs.json"
        original_briefs = briefs_path.read_bytes()
        briefs = json.loads(original_briefs)
        alias_path = "same-role-different-brief.md"
        (run / alias_path).write_bytes(raw)
        briefs["challenger"]["evidence_path"] = alias_path
        briefs_path.write_bytes(json.dumps(briefs).encode())
        plan["briefs_sha256"] = _sha(briefs_path.read_bytes())
        plan["brief_evidence"][alias_path] = _sha(raw)
        _rebind_plan(producer, plan_path, plan, manifest)
        with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
            _validate_direct(producer, run, manifest, roles, tmp_path)
        briefs_path.write_bytes(original_briefs)
        plan = json.loads(original_plan)
        challenger_digest = next(
            item["bindings"][0]["input_sha256"] for item in proposals if item["input_id"] == "direct-brief:challenger"
        )
        kept = [item for item in routing["inert_fixture_admissions"] if item["input_sha256"] == challenger_digest]
        _admit(run, kept)
        plan["inert_fixture_admissions"] = kept
        plan["routing_sha256"] = _sha((run / "review-routing.json").read_bytes())
        _rebind_plan(producer, plan_path, plan, manifest)
        with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
            _validate_direct(producer, run, manifest, roles, tmp_path)


def _consume_direct(run: Path, producer: dict[str, object], routing: dict[str, object], tmp_path: Path) -> None:
    """Show the direct consumer accepts the plan by reaching its later native-provenance guard."""
    plan_path = run / "direct-plan.json"
    manifest = _direct_manifest(plan_path, _load(plan_path))
    with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
        _validate_direct(producer, run, manifest, set(routing["triggered_roles"]), tmp_path)


def _consume_paged(run: Path, producer: dict[str, object], routing: dict[str, object], tmp_path: Path) -> None:
    """Validate the paged inspection contexts against their plan."""
    plan_path = run / "inspection-plan.json"
    producer["validator"].validate_inspection_contexts(_load(plan_path), plan_path, context_limit=None)


def _consume_batches(run: Path, producer: dict[str, object], routing: dict[str, object], tmp_path: Path) -> None:
    """Validate the batch inventory and every source batch's inspection contexts."""
    assert producer["review_batches"].validate_inventory(run)["wave_count"] > 1
    for plan_path in (run / "batches").glob("source-*/inspection-plan.json"):
        producer["validator"].validate_inspection_contexts(_load(plan_path), plan_path, context_limit=None)


@pytest.mark.integration
class TestFixtureDiscovery:
    """Fixture discovery, verified selection, binding export and admission through the real CLI."""

    @pytest.mark.parametrize(
        ("options", "large_source", "id_prefixes", "consume"),
        [
            pytest.param({"paged": False}, False, (), _consume_direct, id="direct"),
            pytest.param({"paged": True}, False, (), _consume_paged, id="paged"),
            pytest.param({"batches": True}, True, ("source-context:", "batch:source-"), _consume_batches, id="batches"),
        ],
    )
    def test_cli_exports_only_verified_bindings(
        self,
        tmp_path: Path,
        producer: dict[str, object],
        options: dict[str, bool],
        large_source: bool,
        id_prefixes: tuple[str, ...],
        consume: Callable[..., None],
    ) -> None:
        """Exact generated inputs are discovered without hashes, then approved and consumed as verified bindings.

        A source holding a fixture secret is refused, discovered, selected, exported and admitted through the CLI for
        each route. A wrong digest must be rejected as unverified, an export must leave the run untouched, and only the
        admitted bindings let preparation and the route's consumer proceed.
        """
        fixture = "sk-" + "d" * 24
        run = _build_review_inputs(
            tmp_path, fixture_source=f"label = 'café'\nexample = '{fixture}'\n", large_source=large_source
        )
        refused = _prepare(run, **options)
        assert refused.returncode != 0
        assert "sensitive-material" in refused.stderr
        _assert_no_leak(refused, fixture)
        routing_path = run / "review-routing.json"
        routing_before = routing_path.read_bytes()

        discovered = _prepare(run, **options, discover_fixtures=True)

        assert discovered.returncode == 0, discovered.stderr
        discovery = json.loads(discovered.stdout)
        assert discovery["schema_version"] == 1
        assert discovery["mode"] == "fixture-discovery"
        _assert_no_leak(discovered, fixture)
        assert _HEX64.search(discovered.stdout) is None
        assert all(set(item) == {"input_id", "matches"} for item in discovery["inputs"])
        matched = _matched_inputs(discovered)
        assert all(
            set(match) == {"pattern_index", "match_start", "match_end"} for item in matched for match in item["matches"]
        )
        assert matched
        assert all(any(item["input_id"].startswith(prefix) for item in matched) for prefix in id_prefixes)
        verified = "Parent verified the complete current synthetic input and every occurrence."
        selections = {
            "schema_version": 1,
            "inputs": [_selection_input(item, verified, _sha(fixture)) for item in matched],
        }
        wrong = json.loads(json.dumps(selections))
        wrong["inputs"][0]["matches"][0]["match_sha256"] = "0" * 64
        selection_path = run / "verified-fixture-selections.json"
        selection_path.write_text(json.dumps(wrong), encoding="utf-8", newline="\n")
        unverified = _prepare(run, **options, export_fixture_bindings=selection_path)
        assert unverified.returncode != 0
        assert "review-fixture-selection-unverified" in unverified.stderr
        assert unverified.stdout == ""
        assert fixture not in unverified.stderr
        assert _HEX64.search(unverified.stderr) is None
        selection_path.write_text(json.dumps(selections), encoding="utf-8", newline="\n")

        exported = _prepare(run, **options, export_fixture_bindings=selection_path)

        assert exported.returncode == 0, exported.stderr
        assert json.loads(exported.stdout)["mode"] == "fixture-bindings"
        _assert_no_leak(exported, fixture)
        bindings = _proposal_bindings(exported)
        assert bindings
        assert all(set(binding) == {"input_sha256", "match_start", "match_end", "match_sha256"} for binding in bindings)
        assert all(binding["match_sha256"] == _sha(fixture) for binding in bindings)
        assert routing_path.read_bytes() == routing_before
        written = ("direct-plan.json", "inspection-plan.json", "dispatch.json", "batch-inventory.json", "batches")
        assert [name for name in written if (run / name).exists()] == []
        unapproved = _prepare(run, **options)
        assert unapproved.returncode != 0
        assert "sensitive-material" in unapproved.stderr
        routing = _admit(
            run,
            _admissions(
                bindings,
                "Parent obtained explicit approval of this exact inert fixture input.",
                "Synthetic fixture without credential authority.",
            ),
        )
        prepared = _prepare(run, **options)
        assert prepared.returncode == 0, prepared.stderr
        consume(run, producer, routing, tmp_path)


@dataclass
class _BearerExport:
    """Outcome of exporting bindings for two selected bearer fixtures in a direct review."""

    run: Path
    component: dict[str, object]
    known_values: list[str]
    secrets: tuple[str, ...]
    routing_before: bytes
    exported: subprocess.CompletedProcess[str]


def _export_bearer_selections(tmp_path: Path, unknown_present: bool) -> _BearerExport:
    """Select two newline-bearing bearer fixtures from inspected component JSON, optionally beside an unselected one."""
    first, second, unknown = "fixture-alpha", "fixture-beta", "unselected-fixture-value"
    fixture_source = f'first = """Authorization: Bearer {first}\nnext text"""\n'
    fixture_source += f"# Authorization: Bearer {unknown}\n" if unknown_present else ""
    fixture_source += f"# Authorization: Bearer {second}\n"
    run = _build_review_inputs(tmp_path, fixture_source=fixture_source)
    retained = _load(run / "local-source/source-snapshot.json")
    component = next(record for record in retained["files"] if record["path"] == "widget.py")
    assert component["content"] == (_source_root(run) / "widget.py").read_bytes().decode("utf-8")
    # Encode the independently inspected complete record, including the comma after its content value.
    encoded = json.dumps(component, indent=2, sort_keys=True)
    matched_values = _BEARER.findall(encoded)
    known_values = [f"Authorization: Bearer {first}\\nnext", f'Authorization: Bearer {second}\\n",']
    assert [value for value in matched_values if first in value or second in value] == known_values
    assert len(matched_values) == 2 + int(unknown_present)
    refused = _prepare(run, paged=False)
    assert refused.returncode != 0
    assert "review-direct-sensitive-material" in refused.stderr
    routing_before = (run / "review-routing.json").read_bytes()
    discovered = _prepare(run, paged=False, discover_fixtures=True)
    assert discovered.returncode == 0, discovered.stderr
    discovery = json.loads(discovered.stdout)
    secrets = (first, second, unknown)
    assert _HEX64.search(discovered.stdout + discovered.stderr) is None
    _assert_no_leak(discovered, *secrets)
    by_id = {item["input_id"]: item for item in discovery["inputs"]}
    entry = by_id["direct-plan:file:0"]
    assert len(entry["matches"]) == len(matched_values)
    diff_entry = by_id["direct-diff:section:0"]
    diff_values = _BEARER.findall((run / "diff.patch").read_bytes().decode("utf-8"))
    source_entry = next(item for item in discovery["inputs"] if item["input_id"].startswith("direct-source:"))
    source_values = _BEARER.findall(component["content"])

    def pick(
        item: dict[str, object], values: list[str], evidence: str, keep: Callable[[str], bool]
    ) -> dict[str, object]:
        """Select the entry's matches whose text passes the filter, each bound to its own digest."""
        pairs = zip(item["matches"], values, strict=True)
        matches = [{**match, "match_sha256": _sha(value)} for match, value in pairs if keep(value)]
        return {
            "input_id": item["input_id"],
            "complete_input_verified": True,
            "verification_evidence": evidence,
            "matches": matches,
        }

    plan_evidence = "Parent inspected the complete current synthetic source component and plan inputs."
    selections = {
        "schema_version": 1,
        "inputs": [
            pick(entry, matched_values, plan_evidence, lambda value: value in known_values),
            pick(
                diff_entry,
                diff_values,
                "Parent inspected the complete synthetic diff and every fixture occurrence.",
                lambda value: unknown not in value,
            ),
            pick(
                source_entry,
                source_values,
                "Parent inspected the complete synthetic source and its verified source identity.",
                lambda value: unknown not in value,
            ),
        ],
    }
    assert len(selections["inputs"][0]["matches"]) == 2
    selection_path = run / "verified-bearer-selections.json"
    selection_path.write_text(json.dumps(selections), encoding="utf-8", newline="\n")
    exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
    return _BearerExport(run, component, known_values, secrets, routing_before, exported)


def _assert_export_left_run_untouched(export: _BearerExport) -> None:
    """Assert an export neither changed routing, wrote a plan or dispatch, nor leaked any bearer value."""
    assert (export.run / "review-routing.json").read_bytes() == export.routing_before
    assert not (export.run / "direct-plan.json").exists()
    assert not (export.run / "dispatch.json").exists()
    _assert_no_leak(export.exported, *export.secrets)


@pytest.mark.integration
class TestBearerFixtureBindings:
    """Bearer fixtures are bound through the complete serialized source component."""

    def test_selected_occurrences_bind_and_reach_consumer(self, tmp_path: Path, producer: dict[str, object]) -> None:
        """Two newline-bearing fixtures bind from inspected component JSON and are admitted end to end.

        Both bearer values sit in one source file, one inside a multi-line string. After the parent selects exactly
        those occurrences, the exported bindings must reproduce their digests, preparation must accept them and the
        consumer must reach its native-provenance guard with the plan's source snapshot equal to the component.
        """
        export = _export_bearer_selections(tmp_path, unknown_present=False)
        run, exported = export.run, export.exported

        _assert_export_left_run_untouched(export)

        assert exported.returncode == 0, exported.stderr
        proposal = json.loads(exported.stdout)
        bindings = next(item["bindings"] for item in proposal["inputs"] if item["input_id"] == "direct-plan:file:0")
        assert [binding["match_sha256"] for binding in bindings] == [_sha(value) for value in export.known_values]
        routing = _admit(
            run,
            _admissions(
                _proposal_bindings(exported),
                "Parent obtained explicit approval for both occurrences in this exact complete input.",
                "Both values and their surrounding input are synthetic and have no credential authority.",
            ),
        )
        prepared = _prepare(run, paged=False)
        assert prepared.returncode == 0, prepared.stderr
        plan_path = run / "direct-plan.json"
        plan = _load(plan_path)
        assert plan["source_snapshot"]["files"] == [export.component]
        # The ordinary consumer authenticates the exact admitted input before requiring native child provenance.
        with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
            _validate_direct(
                producer, run, _direct_manifest(plan_path, plan), set(routing["triggered_roles"]), tmp_path
            )

    def test_unselected_sibling_refuses_export(self, tmp_path: Path) -> None:
        """An unselected bearer value beside the selected ones makes the export fail as unverified.

        The same two fixtures are selected, but a third bearer value in the file is not. Verification of the complete
        input must therefore fail, printing no binding proposal and no digest.
        """
        export = _export_bearer_selections(tmp_path, unknown_present=True)

        _assert_export_left_run_untouched(export)

        assert export.exported.returncode != 0
        assert export.exported.stdout == ""
        assert "review-fixture-selection-unverified" in export.exported.stderr
        assert _HEX64.search(export.exported.stderr) is None


def _admit_each_refused_context(
    producer: dict[str, object], spy: mock.Mock, run: Path, fixture: str
) -> list[dict[str, object]]:
    """Refuse a paged preparation per triggered role, approving each fixture occurrence in the scanned context."""
    admissions: list[dict[str, object]] = []
    for _ in _load(run / "review-routing.json")["triggered_roles"]:
        with pytest.raises(ValueError, match="review-context-sensitive-material"):
            producer["prepare"](run, "bounded-review", "parent", _source_root(run), paged=True)
        context = spy.call_args.args[0]
        admissions += _full_context_admissions(context, fixture, "Parent approved this inert test fixture.")
        _admit(run, admissions)
    return admissions


@pytest.mark.integration
class TestFixtureAdmissionBindings:
    """Admissions bind the exact scanned input and are rechecked by the consumer."""

    def test_paged_admissions_flow_from_collection_to_consumer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, producer: dict[str, object]
    ) -> None:
        """Only approved occurrences in real frozen source contexts are allowed, then rechecked at consumption.

        A paged run refuses once per triggered role while each scanned context is captured. Approving every captured
        occurrence lets preparation succeed and the plan validate; dropping one admission makes validation fail.
        """
        fixture = "sk-" + "a" * 24
        run = _build_review_inputs(tmp_path, fixture_source=f"example = '{fixture}'\n")
        validator = producer["validator"]
        spy = _spy_on_scanner(monkeypatch, validator)
        admissions = _admit_each_refused_context(producer, spy, run, fixture)

        prepared = _prepare(run)

        assert admissions
        assert prepared.returncode == 0, prepared.stderr
        plan_path = run / "inspection-plan.json"
        plan = _load(plan_path)
        assert plan["inert_fixture_admissions"] == admissions
        validator.validate_inspection_contexts(plan, plan_path, context_limit=None)
        plan["inert_fixture_admissions"] = admissions[1:]
        with pytest.raises((SystemExit, ValueError), match="sensitive"):
            validator.validate_inspection_contexts(plan, plan_path, context_limit=None)

    def test_direct_admission_binds_frozen_plan_and_routing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, producer: dict[str, object]
    ) -> None:
        """Direct preparation and consumer enforce exact admissions without a circular routing digest.

        Admissions captured from the refused scan must equal those exported after verified discovery. The accepted plan
        pins them; the consumer must reject a plan with the admissions removed and, once routing is also emptied and re-
        pinned, report the sensitive material again.
        """
        fixture = "sk-" + "b" * 24
        run = _build_review_inputs(tmp_path, fixture_source=f"example = '{fixture}'\n")
        spy = _spy_on_scanner(monkeypatch, producer["validator"])
        with pytest.raises(ValueError, match="review-direct-sensitive-material"):
            producer["prepare"](run, "bounded-review", "parent", _source_root(run))
        captured = _full_context_admissions(spy.call_args.args[0], fixture, "Parent approved this inert test fixture.")
        assert captured
        discovered = _prepare(run, paged=False, discover_fixtures=True)
        assert discovered.returncode == 0, discovered.stderr
        verified = "Parent inspected this complete synthetic input and its identity."
        selections = {
            "schema_version": 1,
            "inputs": [_selection_input(item, verified, _sha(fixture)) for item in _matched_inputs(discovered)],
        }
        selection_path = run / "verified-plan-and-sibling-inputs.json"
        _store(selection_path, selections)
        exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
        assert exported.returncode == 0, exported.stderr
        plan_bindings = next(
            item["bindings"]
            for item in json.loads(exported.stdout)["inputs"]
            if item["input_id"] == "direct-plan:file:0"
        )
        assert plan_bindings == [
            {key: value for key, value in admission.items() if key not in {"approval_evidence", "rationale"}}
            for admission in captured
        ]
        admissions = _admissions(
            _proposal_bindings(exported),
            "Parent approved this exact complete synthetic input identity and occurrence.",
            "Synthetic fixture without credential authority.",
        )
        routing = _admit(run, admissions)
        prepared = _prepare(run, paged=False)
        assert prepared.returncode == 0, prepared.stderr
        plan_path = run / "direct-plan.json"
        plan = _load(plan_path)
        manifest = _direct_manifest(plan_path, plan)
        roles = set(routing["triggered_roles"])
        assert plan["schema_version"] == 3
        assert plan["inert_fixture_admissions"] == admissions
        # The next guard requires real child provenance; reaching it proves the consumer accepted this exact plan.
        with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
            _validate_direct(producer, run, manifest, roles, tmp_path)
        plan["inert_fixture_admissions"] = []
        _rebind_plan(producer, plan_path, plan, manifest)
        with pytest.raises(SystemExit, match="review-direct-fixture-admissions-mismatch"):
            _validate_direct(producer, run, manifest, roles, tmp_path)
        _admit(run, [])
        plan["routing_sha256"] = _sha((run / "review-routing.json").read_bytes())
        _rebind_plan(producer, plan_path, plan, manifest)
        with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
            _validate_direct(producer, run, manifest, roles, tmp_path)


@dataclass(frozen=True)
class _BriefKind:
    """How a sensitive fixture is placed in, approved for, and later changed within one kind of reviewer brief."""

    input_id: str
    path: str
    place: Callable[[Path, str], None]
    change: Callable[[bytes, str], bytes]


def _place_axis(run: Path, fixture: str) -> None:
    """Make the fixture the challenger's review axis."""
    briefs = _load(run / "review-briefs.json")
    briefs["challenger"]["axis"] = fixture
    (run / "review-briefs.json").write_bytes((json.dumps(briefs) + "\n").encode())


def _change_axis(approved: bytes, fixture: str) -> bytes:
    """Return the briefs file with extra context around the approved axis."""
    briefs = json.loads(approved)
    briefs["challenger"]["axis"] = "Changed context " + fixture
    return json.dumps(briefs).encode()


def _place_evidence(run: Path, fixture: str) -> None:
    """Make the fixture part of the challenger's evidence file, with CRLF line endings."""
    (run / "challenger-evidence.md").write_bytes(("Synthetic fixture: " + fixture + "\r\n").encode())


def _change_evidence(approved: bytes, fixture: str) -> bytes:
    """Return the evidence file with an extra line after the approved bytes."""
    return approved + b"Changed context.\n"


_AXIS = _BriefKind("direct-task:challenger", "review-briefs.json", _place_axis, _change_axis)
_EVIDENCE = _BriefKind("direct-brief:challenger", "challenger-evidence.md", _place_evidence, _change_evidence)


@pytest.mark.integration
@pytest.mark.parametrize("kind", [pytest.param(_AXIS, id="axis"), pytest.param(_EVIDENCE, id="evidence")])
def test_brief_fixture_requires_exact_approval_through_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, producer: dict[str, object], kind: _BriefKind
) -> None:
    """An approved inert brief is consumed with native provenance, then revoked occurrence authority is rejected.

    The fixture is approved for the exact challenger brief. A changed brief or the same secret in a sibling brief must
    be refused during preparation, the original brief must assemble with real direct runtime evidence, and removing the
    admissions from routing and plan must make the consumer report the sensitive material.
    """
    fixture = "sk-" + "e" * 24

    def prepare_approved(run: Path) -> subprocess.CompletedProcess[str]:
        """Prepare the exact inspected fixture before the runtime evidence recorder observes its task."""
        kind.place(run, fixture)
        producer["review_routing"].synchronize_routing(run)
        discovery = _prepare(run, paged=False, discover_fixtures=True)
        assert discovery.returncode == 0, discovery.stderr
        _assert_no_leak(discovery, fixture)
        assert _HEX64.search(discovery.stdout) is None
        entries = _matched_inputs(discovery)
        assert [item["input_id"] for item in entries] == [kind.input_id]
        verified = "Parent inspected this complete synthetic reviewer input."
        selection_path = run / "verified-direct-selections.json"
        _store(
            selection_path,
            {"schema_version": 1, "inputs": [_selection_input(item, verified, _sha(fixture)) for item in entries]},
        )
        exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
        assert exported.returncode == 0, exported.stderr
        _admit(
            run,
            _admissions(
                _proposal_bindings(exported),
                "Parent approved this exact complete inert input and occurrence.",
                "Synthetic text with no credential authority.",
            ),
        )
        approved_path = run / kind.path
        approved_bytes = approved_path.read_bytes()
        approved_path.write_bytes(kind.change(approved_bytes, fixture))
        changed = _prepare(run, paged=False)
        assert changed.returncode != 0
        assert "review-direct-sensitive-material" in changed.stderr
        _assert_no_leak(changed, fixture)
        approved_path.write_bytes(approved_bytes)
        sibling = run / "qa-specialist-evidence.md"
        sibling_bytes = sibling.read_bytes()
        sibling.write_bytes(sibling_bytes + fixture.encode())
        transferred = _prepare(run, paged=False)
        assert transferred.returncode != 0
        assert "review-direct-sensitive-material" in transferred.stderr
        _assert_no_leak(transferred, fixture)
        sibling.write_bytes(sibling_bytes)
        assert not (run / "dispatch.json").exists()
        return _prepare(run, paged=False)

    monkeypatch.setattr(direct, "_direct_prepare", prepare_approved)
    run, home = direct._direct_evidence(tmp_path)

    assembled = direct._assemble_direct(run, home)

    assert assembled.returncode == 0, assembled.stderr
    _assert_no_leak(assembled, fixture)
    manifest = _load(run / "specialist-manifest.json")
    plan_path = run / "direct-plan.json"
    plan = _load(plan_path)
    routing = _admit(run, [])
    plan["inert_fixture_admissions"] = []
    plan["routing_sha256"] = _sha((run / "review-routing.json").read_bytes())
    _rebind_plan(producer, plan_path, plan, manifest)
    with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
        _validate_direct(producer, run, manifest, set(routing["triggered_roles"]), home, manifest["passes"])


def _rebind_task(plan: dict[str, object], context: dict[str, object], digest: str) -> None:
    """Pin the tampered task file's digest in its inspection context."""
    context["context_sha256"] = digest


def _rebind_evidence(plan: dict[str, object], context: dict[str, object], digest: str) -> None:
    """Pin the tampered evidence file's digest in the plan's brief evidence."""
    plan["brief_evidence"]["challenger-evidence.md"] = digest


def _rebind_role_card(plan: dict[str, object], context: dict[str, object], digest: str) -> None:
    """Pin the tampered role card's digest in its inspection context."""
    context["role_card_sha256"] = digest


def _rebind_diff(plan: dict[str, object], context: dict[str, object], digest: str) -> None:
    """Pin the tampered diff's digest as the plan's review input."""
    plan["review_input_sha256"] = digest


@pytest.mark.integration
class TestUnadmittedInputs:
    """The consumer scans the reviewer-readable inputs themselves, not only their recorded digests."""

    @pytest.mark.parametrize(
        ("locate", "rebind"),
        [
            pytest.param(lambda run, context: run / context["context_path"], _rebind_task, id="task"),
            pytest.param(lambda run, context: run / "challenger-evidence.md", _rebind_evidence, id="evidence"),
            pytest.param(lambda run, context: run / "role-cards/challenger/ROLE.md", _rebind_role_card, id="role-card"),
            pytest.param(lambda run, context: run / "diff.patch", _rebind_diff, id="diff"),
        ],
    )
    def test_consumer_refuses_unadmitted_delivered_inputs(
        self, tmp_path: Path, producer: dict[str, object], locate: Callable[..., Path], rebind: Callable[..., None]
    ) -> None:
        """Rehashed hand-built evidence cannot bypass scans of actual reviewer-readable inputs.

        After a clean preparation, a secret is appended to the task, evidence, role card or diff and the plan is re-
        pinned to the new digest. The consumer must still report sensitive material because it scans real content.
        """
        run = _build_review_inputs(tmp_path)
        prepared = _prepare(run, paged=False)
        assert prepared.returncode == 0, prepared.stderr
        plan_path = run / "direct-plan.json"
        plan = _load(plan_path)
        context = next(item for item in plan["contexts"] if item["role_id"] == "challenger")
        path = locate(run, context)
        path.write_bytes(path.read_bytes() + ("\n" + "sk-" + "f" * 24).encode())
        rebind(plan, context, _sha(path.read_bytes()))
        manifest = _direct_manifest(plan_path, plan)
        _rebind_plan(producer, plan_path, plan, manifest)

        with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
            _validate_direct(producer, run, manifest, {"challenger", "qa-specialist"}, tmp_path)


def _raw_source_run(tmp_path: Path, producer: dict[str, object]) -> Path:
    """Prepare inputs whose selected unchanged `stable.py` holds a CRLF multi-line bearer fixture."""
    run = _build_review_inputs(tmp_path, unchanged_caller=True, unchanged_caller_content=_RAW_SOURCE)
    briefs = _load(run / "review-briefs.json")
    briefs["challenger"]["source_paths"].append("stable.py")
    _store(run / "review-briefs.json", briefs)
    producer["review_routing"].synchronize_routing(run)
    return run


@pytest.mark.integration
class TestRawSourceInputs:
    """Raw whitespace in an unchanged selected source is scanned and bound as raw bytes."""

    def test_prepare_refuses_raw_sensitive_unchanged_source(self, tmp_path: Path, producer: dict[str, object]) -> None:
        """Raw whitespace in a selected unchanged caller must not disappear behind JSON escaping.

        The scanner flags the raw CRLF text but not its JSON-escaped form, so preparation has to scan the raw source and
        refuse it without leaking the value or writing a dispatch.
        """
        run = _raw_source_run(tmp_path, producer)
        scanner = producer["validator"].review_input_has_sensitive_material

        result = _prepare(run, paged=False)

        assert scanner(_RAW_SOURCE, [])
        assert not scanner(json.dumps({"content": _RAW_SOURCE}), [])
        assert "inert-source-fixture" not in (run / "diff.patch").read_bytes().decode()
        _assert_no_leak(result, "inert-source-fixture")
        assert result.returncode != 0
        assert "review-direct-sensitive-material" in result.stderr
        assert not (run / "dispatch.json").exists()

    def test_discovery_and_consumer_bind_exact_crlf_input(self, tmp_path: Path, producer: dict[str, object]) -> None:
        """Raw unchanged UTF-8 evidence is approved independently of its escaped plan representation.

        Discovery must expose only an offset, the approval must bind the framed raw source with its CRLF bytes, and the
        consumer must reject a renamed source file and an emptied admission list.
        """
        run = _raw_source_run(tmp_path, producer)
        discovered = _prepare(run, paged=False, discover_fixtures=True)
        assert discovered.returncode == 0, discovered.stderr
        _assert_no_leak(discovered, "inert-source-fixture", "stable.py")
        assert _HEX64.search(discovered.stdout) is None
        matched = _matched_inputs(discovered)
        assert len(matched) == 1
        assert re.fullmatch(r"direct-source:[0-9]+", matched[0]["input_id"])
        raw_match = "Authorization:\r\nBearer inert-source-fixture"
        verified = "Parent inspected the complete unchanged synthetic source and its raw whitespace."
        selection_path = run / "verified-raw-source.json"
        _store(
            selection_path, {"schema_version": 1, "inputs": [_selection_input(matched[0], verified, _sha(raw_match))]}
        )
        exported = _prepare(run, paged=False, export_fixture_bindings=selection_path)
        assert exported.returncode == 0, exported.stderr
        bindings = json.loads(exported.stdout)["inputs"][0]["bindings"]
        assert len(bindings) == 1
        routing = _admit(
            run,
            _admissions(
                bindings,
                "Parent approved this exact complete inert raw source occurrence.",
                "Synthetic source without credential authority.",
            ),
        )

        prepared = _prepare(run, paged=False)

        assert prepared.returncode == 0, prepared.stderr
        plan_path = run / "direct-plan.json"
        plan = _load(plan_path)
        record = next(item for item in plan["source_snapshot"]["files"] if item["path"] == "stable.py")
        assert record["content"] == _RAW_SOURCE
        framed = producer["_direct_review_inputs"](run, plan)[matched[0]["input_id"]]
        source_header = producer["_json_bytes"](
            {"source_root": _load(run / "local-source/review-worktree.json")["review_worktree"], "path": "stable.py"}
        ).decode()
        assert (
            framed.split("\n--- Review input ---\n", 1)[1] == source_header + "\n--- UTF-8 source ---\n" + _RAW_SOURCE
        )
        assert bindings[0]["input_sha256"] == _sha(framed)
        assert framed[bindings[0]["match_start"] : bindings[0]["match_end"]] == raw_match
        manifest = _direct_manifest(plan_path, plan)
        roles = set(routing["triggered_roles"])
        # Acceptance of this raw source reaches the separate native-role provenance gate.
        with pytest.raises(SystemExit, match="review-direct-role-set-mismatch"):
            _validate_direct(producer, run, manifest, roles, tmp_path)
        retained_plan = plan_path.read_bytes()
        record["path"] = "different-source.py"
        _rebind_plan(producer, plan_path, plan, manifest)
        with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
            _validate_direct(producer, run, manifest, roles, tmp_path)
        plan = json.loads(retained_plan)
        plan_path.write_bytes(retained_plan)
        _admit(run, [])
        plan["inert_fixture_admissions"] = []
        plan["routing_sha256"] = _sha((run / "review-routing.json").read_bytes())
        _rebind_plan(producer, plan_path, plan, manifest)
        with pytest.raises(SystemExit, match="review-direct-sensitive-material"):
            _validate_direct(producer, run, manifest, roles, tmp_path)


def _taint_spawn_arguments(home: Path, raw: str) -> None:
    """Hide the sensitive text inside the parent's recorded spawn-call arguments."""
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = _rows(parent_path)
    call = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == "spawn-0" and row["payload"].get("type") == "function_call"
    )
    arguments = json.loads(call["payload"]["arguments"])
    arguments["diagnostic"] = {"events": [raw]}
    call["payload"]["arguments"] = json.dumps(arguments)
    _write_jsonl(parent_path, parent)


def _leave_parent(home: Path, raw: str) -> None:
    """Leave the parent rollout untouched because the sensitive text is already in the failure output."""
    return None


_LF = "Authorization:\nBearer inert-retry-fixture"
_CRLF = "Authorization:\r\nBearer inert-retry-fixture"
_PLAIN_OUTPUT = "collab spawn failed: transport unavailable"


@pytest.mark.integration
class TestDirectRetryEvidence:
    """Retry failure evidence is scanned for sensitive material before it is frozen or consumed."""

    @pytest.mark.parametrize(
        ("output", "raw", "taint"),
        [
            pytest.param(f"{_PLAIN_OUTPUT}\n{_LF}", _LF, _leave_parent, id="lf"),
            pytest.param(f"{_PLAIN_OUTPUT}\n{_CRLF}", _CRLF, _leave_parent, id="crlf"),
            pytest.param(
                json.dumps({"isError": True, "details": [{"diagnostic": _LF}]}), _LF, _leave_parent, id="json-value"
            ),
            pytest.param(
                json.dumps({"isError": True, "details": [{_LF: "ignored"}]}), _LF, _leave_parent, id="json-key"
            ),
            pytest.param(_PLAIN_OUTPUT, _LF, _taint_spawn_arguments, id="arguments"),
        ],
    )
    def test_retry_refuses_sensitive_failure_strings(
        self, tmp_path: Path, output: str, raw: str, taint: Callable[[Path, str], None]
    ) -> None:
        """Strict retry failure checks inspect raw and nested decoded strings before freezing any evidence.

        The sensitive text appears as a raw LF or CRLF line, inside a JSON value or key, or in the parent's spawn
        arguments. Each must be refused with a sensitive-failure diagnostic, no leak and no retained retry file.
        """
        run, home, _, _, role = _direct_no_child_failure(tmp_path, output)
        taint(home, raw)

        refused = direct._retry_direct(run, home, role)

        _assert_no_leak(refused, _RETRY_FIXTURE)
        assert refused.returncode != 0
        assert "review-direct-failure-sensitive-material" in refused.stderr
        assert not (run / f"retry/{role}.failure.json").exists()
        assert not (run / f"retry/{role}.json").exists()

    def test_safe_no_child_failure_requires_fresh_review(self, tmp_path: Path) -> None:
        """An unauthenticated failed launch cannot support a current-schema retry.

        A spawn failure that never created a child leaves nothing to authenticate, so the retry is refused with the
        instruction to run a fresh review and no retry receipt is written.
        """
        run, home, _, _, role = _direct_no_child_failure(tmp_path, _NO_CHILD_FAILURE)

        refused = direct._retry_direct(run, home, role)

        assert refused.returncode != 0
        assert "review-direct-failure-no-child-rerun-fresh" in refused.stderr
        assert not (run / f"retry/{role}.json").exists()

    def test_consumer_rejects_rehashed_historical_receipt(self, tmp_path: Path, producer: dict[str, object]) -> None:
        """Historical no-child retry receipts remain subject to consumer-side sensitive scanning.

        A schema-1 run is built from a real no-child failure and retried successfully. Appending a sensitive line to the
        spawn receipt and re-pinning every digest must still be refused when the retry is assembled.
        """
        run, home, child, final, role = _direct_no_child_failure(tmp_path, _NO_CHILD_FAILURE)
        _downgrade_to_historical_plan(run, home)
        prepared = direct._retry_direct(run, home, role)
        assert prepared.returncode == 0, prepared.stderr
        retry = json.loads(prepared.stdout)
        direct._complete_direct_retry(home, child, retry, final)
        clean = direct._assemble_direct(run, home)
        assert clean.returncode == 0, clean.stderr
        (run / "specialist-manifest.json").unlink()
        parent_path = home / "sessions/rollout-parent.jsonl"
        parent = _rows(parent_path)
        receipt = next(
            row
            for row in parent
            if row.get("payload", {}).get("call_id") == "spawn-0"
            and row["payload"].get("type") == "function_call_output"
        )
        receipt["payload"]["output"] += "\n" + _CRLF
        _write_jsonl(parent_path, parent)
        failure_path = run / retry["failure_path"]
        failure = _load(failure_path)
        failure["receipt"] = receipt
        _rehash_retry(producer, run, role, retry, failure_path, failure)

        refused = direct._assemble_direct(run, home)

        _assert_no_leak(refused, _RETRY_FIXTURE)
        assert refused.returncode != 0
        assert "review-direct-failure-sensitive-material" in refused.stderr
        assert not (run / "specialist-manifest.json").exists()

    def test_consumer_rejects_rehashed_multiline_child_failure(
        self, tmp_path: Path, producer: dict[str, object]
    ) -> None:
        """Retry assembly independently scans authenticated, rehashed child failure evidence.

        A real aborted child is retried; its abort reason is then replaced by a multi-line sensitive string and the
        retry failure file and receipt are re-pinned. Assembly must refuse it and produce no manifest.
        """
        run, home, child, final, role = _direct_aborted_child_failure(tmp_path)
        prepared = direct._retry_direct(run, home, role)
        assert prepared.returncode == 0, prepared.stderr
        retry = json.loads(prepared.stdout)
        direct._complete_direct_retry(home, child, retry, final)
        child_path = home / "sessions/rollout-child-0.jsonl"
        child = _rows(child_path)
        child[-1]["payload"]["reason"] = _CRLF
        _write_jsonl(child_path, child)
        failure_path = run / retry["failure_path"]
        failure = _load(failure_path)
        failure["child"] = child
        _rehash_retry(producer, run, role, retry, failure_path, failure)

        refused = direct._assemble_direct(run, home)

        _assert_no_leak(refused, _RETRY_FIXTURE)
        assert refused.returncode != 0
        assert "review-direct-failure-sensitive-material" in refused.stderr
        assert not (run / "specialist-manifest.json").exists()

    @pytest.mark.parametrize(
        ("suffix", "accepted"),
        [pytest.param("", True, id="safe-output"), pytest.param("\n" + _CRLF, False, id="sensitive-output")],
    )
    def test_safe_failure_retry_keeps_reviewer_output_strict(self, tmp_path: Path, suffix: str, accepted: bool) -> None:
        """Safe decoded failure evidence permits recovery while sibling reviewer output stays strictly scanned.

        The aborted child's failure evidence is safe in both cases. Recovery succeeds with a clean reviewer reply and is
        refused when that reply carries a sensitive line, so failure tolerance never loosens output scanning.
        """
        run, home, child, final, role = _direct_aborted_child_failure(tmp_path)
        prepared = direct._retry_direct(run, home, role)
        assert prepared.returncode == 0, prepared.stderr
        retry = json.loads(prepared.stdout)
        direct._complete_direct_retry(home, child, retry, final + suffix)

        assembled = direct._assemble_direct(run, home)

        _assert_no_leak(assembled, _RETRY_FIXTURE)
        assert (assembled.returncode == 0) is accepted, assembled.stderr
        assert ("review-output-sensitive-material" in assembled.stderr) is not accepted
        assert (run / "specialist-manifest.json").is_file() is accepted


def _downgrade_to_historical_plan(run: Path, home: Path) -> None:
    """Turn the direct plan into a schema-1 plan whose spawn used the parent model, as older runs recorded."""
    plan_path = run / "direct-plan.json"
    plan = _load(plan_path)
    plan["schema_version"] = 1
    plan.pop("model_selection")
    for context in plan["contexts"]:
        card_path = run / "role-cards" / context["role_id"] / "ROLE.md"
        card = card_path.read_text(encoding="utf-8").replace("---\n", "---\nmodel: test-parent-model\n", 1)
        card_path.write_text(card, encoding="utf-8")
        context["role_card_sha256"] = _sha(card_path.read_bytes())
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = _rows(parent_path)
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("type") == "function_call" and payload.get("name", "").endswith("spawn_agent"):
            arguments = json.loads(payload["arguments"])
            arguments["model"] = "test-parent-model"
            payload["arguments"] = json.dumps(arguments)
    _write_jsonl(parent_path, parent)


def _rehash_retry(
    producer: dict[str, object],
    run: Path,
    role: str,
    retry: dict[str, object],
    failure_path: Path,
    failure: dict[str, object],
) -> None:
    """Rewrite a retry failure file and its retry receipt with fresh digests, as a rehashing attacker would."""
    failure_path.write_bytes(producer["_json_bytes"](failure))
    retry["failure_sha256"] = _sha(failure_path.read_bytes())
    (run / f"retry/{role}.json").write_bytes(producer["_json_bytes"](retry))


def _abort_with_raw_reason(tmp_path: Path, child: list[dict[str, object]]) -> None:
    """Abort the child with a structured reason that nests the sensitive text in an events list."""
    child[-1]["payload"]["reason"] = {"events": [{"diagnostic": _LF}]}


def _abort_after_custom_output(tmp_path: Path, child: list[dict[str, object]]) -> None:
    """Abort the child after a real native exec call whose stdout, wrapped in content blocks, is sensitive."""
    output_path = tmp_path / "native-tool-response.txt"
    output_path.write_bytes(_CRLF.encode())
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


def _drop_child_receipts(home: Path, original_path: str) -> None:
    """Remove the parent rollout rows authored by the original child so the failure must be retried."""
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [row for row in _rows(parent_path) if row.get("payload", {}).get("author") != original_path]
    _write_jsonl(parent_path, parent)


@pytest.mark.integration
class TestNativeChildFailure:
    """Native aborted-child evidence is scanned for nested sensitive output before retry."""

    @pytest.mark.parametrize(
        "abort",
        [
            pytest.param(_abort_with_raw_reason, id="raw-reason"),
            pytest.param(_abort_after_custom_output, id="custom-output"),
        ],
    )
    def test_aborted_child_failure_refuses_nested_sensitive_output(
        self, tmp_path: Path, abort: Callable[[Path, list[dict[str, object]]], None]
    ) -> None:
        """Raw and native content-block output must not become sensitive retained retry evidence.

        A completed direct review is rewritten so its child aborted, with the sensitive text either in a nested abort
        reason or in the stdout of a real native exec call. Retry must refuse it and keep no failure file.
        """
        run, home = direct._direct_evidence(tmp_path)
        child_path = home / "sessions/rollout-child-0.jsonl"
        child = _rows(child_path)
        original_path = child[0]["payload"]["agent_path"]
        child[-1]["payload"] = {"type": "turn_aborted", "turn_id": "turn-0"}
        abort(tmp_path, child)
        _write_jsonl(child_path, child)
        _drop_child_receipts(home, original_path)
        role = _load(run / "dispatch.json")["calls"][0]["role"]

        refused = direct._retry_direct(run, home, role)

        _assert_no_leak(refused, _RETRY_FIXTURE)
        assert refused.returncode != 0
        assert "review-direct-failure-sensitive-material" in refused.stderr
        assert not (run / f"retry/{role}.failure.json").exists()

    @pytest.mark.parametrize(
        ("output", "accepted"),
        [
            pytest.param("Transport closed.\r\n", True, id="safe-blocks"),
            pytest.param(_CRLF, False, id="sensitive-blocks"),
        ],
    )
    def test_retry_consumer_scans_custom_output_blocks(
        self, tmp_path: Path, producer: dict[str, object], output: str, accepted: bool
    ) -> None:
        """Safe native content blocks permit recovery; rehashed sensitive blocks fail ordinary consumer admission.

        The retried child's failure carries a custom tool output. With harmless output recovery assembles; after the
        output is replaced by sensitive text and its digests re-pinned, the consumer must refuse and write no manifest.
        """
        run, home = direct._direct_evidence(tmp_path)
        child_path = home / "sessions/rollout-child-0.jsonl"
        child = _rows(child_path)
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
        call = {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "original-custom-tool",
                "input": "text({output: 'Transport closed.'});",
            },
        }
        child[-1:-1] = [call, custom_output]
        _write_jsonl(child_path, child)
        _drop_child_receipts(home, original_path)
        role = _load(run / "dispatch.json")["calls"][0]["role"]
        prepared = direct._retry_direct(run, home, role)
        assert prepared.returncode == 0, prepared.stderr
        retry = json.loads(prepared.stdout)
        direct._complete_direct_retry(home, child, retry, final)
        custom_output["payload"]["output"][1]["text"] = json.dumps({"output": output, "exit_code": 0})
        _write_jsonl(child_path, child)
        failure_path = run / retry["failure_path"]
        failure = _load(failure_path)
        failure["child"] = child
        _rehash_retry(producer, run, role, retry, failure_path, failure)

        result = direct._assemble_direct(run, home)

        _assert_no_leak(result, _RETRY_FIXTURE)
        assert (result.returncode == 0) is accepted, result.stderr
        assert ("review-direct-failure-sensitive-material" in result.stderr) is not accepted
        assert (run / "specialist-manifest.json").is_file() is accepted
