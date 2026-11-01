"""Acceptance checks for the inert shim lifecycle parsing kernel."""

from __future__ import annotations

import sys
from itertools import starmap
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_registered
from _shim_support import absent_roster, canonical_json, role_ids, root_identity

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PATH = PLUGIN_ROOT / "scripts" / "_agent_shim_lifecycle.py"
GENERATOR_PATH = PLUGIN_ROOT / "scripts" / "generate_roles.py"
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174000"
DIGEST = "a" * 64
CHALLENGER_TARGET = "codex-rig-challenger.toml"


def _load_module(path: Path, name: str) -> ModuleType:
    """Load one installed script module without package assumptions."""
    if path == LIFECYCLE_PATH and "generate_roles" not in sys.modules:
        _load_module(GENERATOR_PATH, "generate_roles")
    return load_module_registered(name, path)


@pytest.fixture
def lifecycle() -> ModuleType:
    """Load a fresh copy of the lifecycle kernel for one test."""
    return _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_under_test")


def _marker_line(role_id: str, *, file_digest: str = DIGEST) -> bytes:
    """Build one exact marker fixture.

    Example:
        >>> _marker_line("challenger").startswith(b"# codex-rig")
        True
    """
    return (
        "# codex-rig-shim schema=1 plugin=codex-rig "
        f"install_id={INSTALL_ID} role_id={role_id} package_hash=sha256:{DIGEST} "
        f"role_hash=sha256:{file_digest} bootstrap=1 generator=1"
    ).encode()


def _state_payload(*, status: str = "current") -> dict[str, object]:
    """Build one complete valid lifecycle state fixture."""
    return {
        "schema": 1,
        "plugin": "codex-rig",
        "scope": "user",
        "install_id": INSTALL_ID,
        "plugin_version": "0.2.0",
        "package_hash": DIGEST,
        "codex_home_identity": root_identity("/codex"),
        "plugin_root_identity": root_identity("/plugin"),
        "state_root_identity": root_identity("/codex/codex-rig/shims"),
        "target_root_identity": root_identity("/codex/agents"),
        "roster_hash": DIGEST,
        "bootstrap": {"protocol": 1, "helper_path": "scripts/verify_role_link.py", "helper_hash": DIGEST},
        "generator_version": 1,
        "roles": [
            {
                "role_id": role_id,
                "target_name": f"codex-rig-{role_id}.toml",
                "card_path": f"roles/{role_id}/ROLE.md",
                "role_hash": DIGEST,
                "file_hash": DIGEST,
            }
            for role_id in role_ids()
        ],
        "transaction_status": status,
    }


def _parsed_state(lifecycle: ModuleType, *, status: str = "current") -> dict[str, object]:
    """Parse the valid state fixture through the kernel under test."""
    return lifecycle.parse_state(canonical_json(_state_payload(status=status)))


def _unsafe_roster(lifecycle: ModuleType) -> dict[str, object]:
    """Observe every roster target as an unsafe node."""
    return {f"codex-rig-{role_id}.toml": lifecycle.TargetObservation("unsafe") for role_id in role_ids()}


def _regular_roster(lifecycle: ModuleType) -> dict[str, object]:
    """Observe every roster target as a regular file carrying its own marker."""
    return {
        f"codex-rig-{role_id}.toml": lifecycle.TargetObservation(
            "regular", DIGEST, lifecycle.parse_marker(_marker_line(role_id))
        )
        for role_id in role_ids()
    }


def _modified_challenger_roster(lifecycle: ModuleType) -> dict[str, object]:
    """Observe a regular roster whose challenger file hash differs from the state."""
    return {**_regular_roster(lifecycle), CHALLENGER_TARGET: lifecycle.TargetObservation("regular", "b" * 64, None)}


def _missing_challenger_roster(lifecycle: ModuleType) -> dict[str, object]:
    """Observe a regular roster whose challenger file is absent."""
    return {**_regular_roster(lifecycle), CHALLENGER_TARGET: lifecycle.TargetObservation("absent")}


def _swap_first_two_roles(value: dict[str, object]) -> None:
    """Reorder the persisted roster so it no longer matches canonical order."""
    value["roles"][0], value["roles"][1] = value["roles"][1], value["roles"][0]


class TestParseJsonObject:
    """Strict JSON parsing at the lifecycle authority boundary."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b'{"a":1,"a":2}', id="duplicate"),
            pytest.param(b'{"value":NaN}', id="nonfinite"),
            pytest.param(b"\xef\xbb\xbf{}", id="bom"),
            pytest.param(b"\xff", id="utf8"),
            pytest.param(b"[]", id="nonobject"),
            pytest.param(b"{} trailing", id="trailing"),
        ],
    )
    def test_rejects_hostile_inputs(self, lifecycle: ModuleType, payload: bytes) -> None:
        """Ambiguous or non-canonical JSON never reaches lifecycle authority.

        Each payload is close to a valid object but breaks the strict grammar (duplicate keys, NaN, BOM, invalid UTF-8,
        non-object root, trailing bytes); accepting any would let a forged state file pick its own reading.
        """
        with pytest.raises(lifecycle.LifecycleDataError):
            lifecycle.parse_json_object(payload, maximum=32)

    def test_converts_bounded_huge_integer_failure(self, lifecycle: ModuleType) -> None:
        """Interpreter integer-size limits surface as the lifecycle error type.

        A 5000-digit integer within the byte bound makes ``json`` raise ``ValueError``; the kernel must wrap it so
        callers only ever handle ``LifecycleDataError``.
        """
        payload = b'{"value":' + b"9" * 5000 + b"}"

        with pytest.raises(lifecycle.LifecycleDataError):
            lifecycle.parse_json_object(payload, maximum=len(payload))

    def test_converts_decoder_recursion_failure(self, lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """Decoder recursion failures surface as the lifecycle error type with the cause kept.

        The decoder is forced to raise ``RecursionError`` so the check does not depend on an interpreter-specific depth
        limit; the original exception must stay reachable as ``__cause__``.
        """

        def _raise_recursion(*args: object, **kwargs: object) -> object:
            """Model decoder recursion failure without interpreter-specific depth assumptions."""
            raise RecursionError("fixture recursion limit")

        monkeypatch.setattr(lifecycle.json, "loads", _raise_recursion)

        with pytest.raises(lifecycle.LifecycleDataError) as caught:
            lifecycle.parse_json_object(b"{}", maximum=2)

        assert isinstance(caught.value.__cause__, RecursionError)


class TestParseMarker:
    """Exact first-line ownership marker parsing."""

    @pytest.mark.parametrize("role_id", ["challenger", "retired-specialist"])
    def test_accepts_current_and_historical_role_ids(self, lifecycle: ModuleType, role_id: str) -> None:
        """Strict role grammar is accepted without tying old ownership to the active roster.

        ``retired-specialist`` is not in the generated roster; a marker for a retired role must still parse so a
        migration can recognise files an earlier release wrote.
        """
        marker = lifecycle.parse_marker(_marker_line(role_id))

        assert marker == lifecycle.Marker(INSTALL_ID, role_id, DIGEST, DIGEST)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(_marker_line("challenger") + b"\n", id="trailing-newline"),
            pytest.param(_marker_line("1unknown"), id="invalid-role-id"),
            pytest.param(b" " + _marker_line("challenger"), id="leading-whitespace"),
        ],
    )
    def test_rejects_noncanonical_payload(self, lifecycle: ModuleType, payload: bytes) -> None:
        """Trailing, malformed-role, and whitespace-padded markers are refused.

        The marker is compared byte for byte; any slack would let a hand-edited line claim ownership.
        """
        with pytest.raises(lifecycle.LifecycleDataError):
            lifecycle.parse_marker(payload)


class TestParseState:
    """Persisted lifecycle state parsing."""

    @pytest.mark.parametrize("status", ["current", "removed"])
    def test_accepts_complete_state(self, lifecycle: ModuleType, status: str) -> None:
        """A state with exact schema and canonical roster ordering parses for both statuses.

        ``removed`` is the tombstone left after uninstall; it carries the same schema as ``current``.
        """
        parsed = lifecycle.parse_state(canonical_json(_state_payload(status=status)))

        assert parsed["transaction_status"] == status

    def test_accepts_state_with_a_retired_role_missing_from_the_roster(self, lifecycle: ModuleType) -> None:
        """A persisted roster that is a subset of today's roster still parses.

        Dropping the last role models a state written by an older release that shipped fewer roles.
        """
        historical = _state_payload()
        historical["roles"] = historical["roles"][:-1]

        parsed = lifecycle.parse_state(canonical_json(historical))

        assert len(parsed["roles"]) == len(role_ids()) - 1

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda value: value.update(schema=2), id="future"),
            pytest.param(lambda value: value.update(schema=True), id="boolean-schema"),
            pytest.param(lambda value: value.update(plugin_version="02.0"), id="invalid-version"),
            pytest.param(lambda value: value.update(unexpected=True), id="extra"),
            pytest.param(_swap_first_two_roles, id="reordered"),
            pytest.param(lambda value: value["target_root_identity"].update(mode="0999"), id="bad-root"),
            pytest.param(
                lambda value: value["target_root_identity"].update(canonical_path="/codex/../tmp"), id="aliased-root"
            ),
            pytest.param(
                lambda value: value["target_root_identity"].update(canonical_path="//remote/path"), id="double-root"
            ),
            pytest.param(
                lambda value: value["target_root_identity"].update(canonical_path="/codex/agents\nforeign"),
                id="control-root",
            ),
            pytest.param(lambda value: value["target_root_identity"].update(device=True), id="boolean-root"),
        ],
    )
    def test_rejects_incompatible_or_partial_state(self, lifecycle: ModuleType, mutate: object) -> None:
        """Malformed ownership state cannot authorize target changes.

        Starting from a valid state, one field at a time is made future-dated, mistyped, extra, reordered, or an unsafe
        root identity; each variant must be refused rather than partially trusted.
        """
        value = _state_payload()
        mutate(value)

        with pytest.raises(lifecycle.LifecycleDataError):
            lifecycle.parse_state(canonical_json(value))


class TestClassifyTargets:
    """Ownership classification of the observed target roster."""

    @pytest.mark.parametrize(
        ("roster", "expected"),
        [
            pytest.param(absent_roster, "absent", id="all-absent"),
            pytest.param(_regular_roster, "foreign", id="regular-without-state"),
        ],
    )
    def test_without_state(self, lifecycle: ModuleType, roster: object, expected: str) -> None:
        """With no persisted state, absent targets are absent and any regular file is foreign.

        A marker alone is never ownership evidence, so regular files carrying valid markers are still foreign.
        """
        assert lifecycle.classify_targets(None, roster(lifecycle)) == expected

    @pytest.mark.parametrize(
        ("status", "roster", "expected"),
        [
            pytest.param("current", _regular_roster, "current", id="current-matching"),
            pytest.param("current", _unsafe_roster, "unsafe", id="current-unsafe"),
            pytest.param("current", _modified_challenger_roster, "modified", id="current-one-modified"),
            pytest.param("current", _missing_challenger_roster, "repairable-missing", id="current-one-missing"),
            pytest.param("removed", absent_roster, "removed", id="removed-all-absent"),
            pytest.param("removed", _missing_challenger_roster, "removed-conflict", id="removed-one-present"),
        ],
    )
    def test_with_state(self, lifecycle: ModuleType, status: str, roster: object, expected: str) -> None:
        """Targets are classified against persisted state: current, modified, unsafe, missing, or removed.

        Separates repairable absence under a current state from files reappearing under a removed tombstone, and keeps a
        hash mismatch or an unsafe node from being mistaken for either.
        """
        parsed = _parsed_state(lifecycle, status=status)

        assert lifecycle.classify_targets(parsed, roster(lifecycle)) == expected

    @pytest.mark.parametrize(
        ("status", "unpersisted_kind", "expected"),
        [
            pytest.param("current", "regular", "foreign", id="current-foreign-outranks-modified"),
            pytest.param("removed", "regular", "removed-conflict", id="removed-conflict-outranks-modified"),
            pytest.param("current", "unsafe", "unsafe", id="current-unsafe-outranks-modified"),
            pytest.param("removed", "unsafe", "unsafe", id="removed-unsafe-outranks-modified"),
        ],
    )
    def test_mixed_roster_precedence(
        self, lifecycle: ModuleType, status: str, unpersisted_kind: str, expected: str
    ) -> None:
        """Later unknown and unsafe targets are scanned before an earlier modification is reported.

        The first persisted role is modified while the last role is missing from state and observed as ``regular`` or
        ``unsafe``; the stronger verdict must win regardless of roster position.
        """
        state_payload = _state_payload(status=status)
        unpersisted_role = role_ids()[-1]
        state_payload["roles"] = [role for role in state_payload["roles"] if role["role_id"] != unpersisted_role]
        parsed = lifecycle.parse_state(canonical_json(state_payload))
        targets = _regular_roster(lifecycle)
        targets[parsed["roles"][0]["target_name"]] = lifecycle.TargetObservation("regular", "b" * 64, None)
        targets[f"codex-rig-{unpersisted_role}.toml"] = lifecycle.TargetObservation(unpersisted_kind)

        assert lifecycle.classify_targets(parsed, targets) == expected

    def test_rejects_an_incomplete_observation_roster(self, lifecycle: ModuleType) -> None:
        """A partial roster is refused before any ownership observation is interpreted.

        Dropping one observed target models an observer that skipped a role; classifying the rest would hide it.
        """
        targets = _regular_roster(lifecycle)
        targets.pop(f"codex-rig-{role_ids()[-1]}.toml")

        with pytest.raises(lifecycle.LifecycleDataError, match="target observation roster mismatch"):
            lifecycle.classify_targets(_parsed_state(lifecycle), targets)


class TestClassifyRecovery:
    """Recovery-authority classification."""

    @pytest.mark.parametrize(
        ("rows", "expected"),
        [
            pytest.param((), "none", id="none"),
            pytest.param((("journal", True),), "journal", id="journal"),
            pytest.param((("probe-receipt", True),), "probe-receipt", id="probe-receipt"),
            pytest.param((("empty-probe", True, True),), "empty-probe", id="empty-probe"),
            pytest.param((("unknown", False),), "blocked-unknown", id="unknown-kind"),
            pytest.param((("journal", "yes"),), "blocked-unknown", id="non-boolean-exact"),
            pytest.param((("journal", True), ("probe-receipt", True)), "blocked-multiple", id="competing-authority"),
        ],
    )
    def test_is_single_exact_and_fail_closed(self, lifecycle: ModuleType, rows: tuple, expected: str) -> None:
        """Exactly one recognised recovery object is accepted; unknown or competing authority blocks.

        Each row is ``(kind, exact[, empty])`` for one residue object; a non-boolean ``exact`` flag counts as unknown
        rather than truthy.
        """
        observations = tuple(starmap(lifecycle.RecoveryObservation, rows))

        assert lifecycle.classify_recovery(observations) == expected
