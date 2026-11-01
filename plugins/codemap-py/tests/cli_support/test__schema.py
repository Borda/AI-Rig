"""Tests for ``bin/_schema.py`` — index data contract: version, enum values, call-resolution set."""

from __future__ import annotations

import doctest
import json
from enum import Enum

import _schema
import pytest
from _schema import (
    DEAD_SYMBOL_MIN_VER,
    DOCSTRING_MIN_VER,
    IMPORT_GROUPS_MIN_VER,
    MOCK_PATCHES_MIN_VER,
    SCAN_VERSION,
    SPHINX_XREFS_MIN_VER,
    UNCOVERED_MIN_VER,
    VALID_CALL_RESOLUTIONS,
    EntityType,
    Resolution,
    SymbolType,
)

#: Names of the per-feature min-version constants exported by ``_schema``.
_PER_FEATURE_MIN_VER_NAMES = (
    "MOCK_PATCHES_MIN_VER",
    "UNCOVERED_MIN_VER",
    "IMPORT_GROUPS_MIN_VER",
    "DOCSTRING_MIN_VER",
    "SPHINX_XREFS_MIN_VER",
    "DEAD_SYMBOL_MIN_VER",
    "MODULE_ALIASES_MIN_VER",
    "SUBPROCESS_CALLS_MIN_VER",
    "FIXTURE_GRAPH_MIN_VER",
    "COVERAGE_MIN_VER",
    "SYMBOL_ALIASES_MIN_VER",
)


class TestSchemaStringEnums:
    """Persisted-schema enum types retain their plain-string JSON contract."""

    @pytest.mark.parametrize(
        ("enum_type", "values"),
        [
            pytest.param(EntityType, ["pkg", "test", "docs", "example"], id="entity-type"),
            pytest.param(SymbolType, ["class", "function", "method"], id="symbol-type"),
            pytest.param(Resolution, ["import", "local", "self", "builtin", "star", "unresolved"], id="resolution"),
        ],
    )
    def test_direct_str_enum_base_serializes_as_values(self, enum_type: type[Enum], values: list[str]) -> None:
        """Schema enums use Python 3.10-compatible base classes and original wire values.

        Each enum must derive directly from ``(str, Enum)`` and dump to its original JSON strings, in declaration order,
        so previously written indexes keep loading.
        """
        assert enum_type.__bases__ == (str, Enum)
        assert json.loads(json.dumps(list(enum_type))) == values


class TestResolutionEnum:
    """Resolution enum: string inheritance, expected members, value spelling."""

    @pytest.mark.parametrize(
        ("member", "value"),
        [
            pytest.param(Resolution.IMPORT, "import", id="import"),
            pytest.param(Resolution.LOCAL, "local", id="local"),
            pytest.param(Resolution.SELF, "self", id="self"),
            pytest.param(Resolution.BUILTIN, "builtin", id="builtin"),
            pytest.param(Resolution.STAR, "star", id="star"),
            pytest.param(Resolution.UNRESOLVED, "unresolved", id="unresolved"),
        ],
    )
    def test_member_is_plain_string_with_wire_value(self, member: Resolution, value: str) -> None:
        """Each member is a plain string equal to its wire spelling (json.dumps relies on this).

        Index JSON stores the raw string, so a member must compare equal to it and pass ``isinstance(..., str)``.
        """
        assert isinstance(member, str)
        assert member == value

    def test_complete_member_set(self) -> None:
        """Exactly six resolution kinds — adding/removing one must update this test."""
        expected = {"import", "local", "self", "builtin", "star", "unresolved"}
        assert {m.value for m in Resolution} == expected


class TestValidCallResolutions:
    """Validate the call-resolution schema and its exclusions."""

    def test_is_frozenset(self) -> None:
        """Must be a frozenset — immutable, safe to share across modules."""
        assert isinstance(VALID_CALL_RESOLUTIONS, frozenset)

    @pytest.mark.parametrize(
        ("member", "valid"),
        [
            pytest.param(Resolution.IMPORT, True, id="import-cross-module-included"),
            pytest.param(Resolution.LOCAL, True, id="local-same-file-included"),
            pytest.param(Resolution.SELF, True, id="self-method-to-method-included"),
            pytest.param(Resolution.BUILTIN, False, id="builtin-outside-project-excluded"),
            pytest.param(Resolution.STAR, False, id="star-unresolvable-excluded"),
            pytest.param(Resolution.UNRESOLVED, False, id="unresolved-no-graph-info-excluded"),
        ],
    )
    def test_membership(self, member: Resolution, valid: bool) -> None:
        """Only resolutions that carry call-graph information are valid call edges.

        IMPORT, LOCAL and SELF link two project functions; BUILTIN, STAR and UNRESOLVED do not, so graph queries must
        never see them.
        """
        assert (member in VALID_CALL_RESOLUTIONS) is valid

    def test_size(self) -> None:
        """Exactly three valid resolutions."""
        assert len(VALID_CALL_RESOLUTIONS) == 3


class TestScanVersion:
    """Validate the scan-version type and range."""

    def test_is_int(self) -> None:
        """Version must be a plain Python int."""
        assert isinstance(SCAN_VERSION, int)

    def test_is_positive(self) -> None:
        """Version must be a positive number (≥1)."""
        assert SCAN_VERSION >= 1

    def test_value_is_thirteen(self) -> None:
        """v5.7 bumps SCAN_VERSION for resolved static submodule imports.

        A deliberate pin: changing the schema version must update this test together with the index-staleness logic.
        """
        assert SCAN_VERSION == 13


class TestPerFeatureVersionConstants:
    """Per-feature min-version constants: type, positivity, ordering."""

    @pytest.mark.parametrize("name", _PER_FEATURE_MIN_VER_NAMES)
    def test_is_int(self, name: str) -> None:
        """Each per-feature min-version constant must be a plain int."""
        assert isinstance(getattr(_schema, name), int), f"{name} must be int"

    @pytest.mark.parametrize("name", _PER_FEATURE_MIN_VER_NAMES)
    def test_is_positive(self, name: str) -> None:
        """Each per-feature min-version constant must be ≥1."""
        assert getattr(_schema, name) > 0, f"{name} must be positive"

    def test_sphinx_before_dead_symbol(self) -> None:
        """Dead-symbol depends on sphinx_xref_count — its min version must be higher."""
        assert SPHINX_XREFS_MIN_VER < DEAD_SYMBOL_MIN_VER

    def test_v4_constants_equal(self) -> None:
        """v4.1–v4.4 all require the same index version."""
        assert MOCK_PATCHES_MIN_VER == UNCOVERED_MIN_VER == IMPORT_GROUPS_MIN_VER == DOCSTRING_MIN_VER


def test_doctests_pass() -> None:
    """Doctest examples in _schema must not regress."""
    results = doctest.testmod(_schema, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"
