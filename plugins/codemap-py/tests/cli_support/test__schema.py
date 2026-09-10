"""Tests for ``bin/_schema.py`` — index data contract: version, enum values, call-resolution set."""

from __future__ import annotations

import json
from enum import Enum

import pytest

import _schema
from _schema import EntityType, SCAN_VERSION, Resolution, SymbolType, VALID_CALL_RESOLUTIONS


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
    def test_members_are_direct_str_enum_subclasses_and_serialize_as_values(
        self, enum_type: type[Enum], values: list[str]
    ) -> None:
        """Schema enums use Python 3.10-compatible base classes and original wire values."""
        assert enum_type.__bases__ == (str, Enum)
        assert json.loads(json.dumps(list(enum_type))) == values


class TestResolutionEnum:
    """Resolution enum: string inheritance, expected members, value spelling."""

    def test_all_members_inherit_str(self) -> None:
        """Every Resolution member must be usable as a plain string (json.dumps relies on this)."""
        for member in Resolution:
            assert isinstance(member, str)

    def test_import_value(self) -> None:
        """IMPORT serialises to 'import'."""
        assert Resolution.IMPORT == "import"

    def test_local_value(self) -> None:
        """LOCAL serialises to 'local'."""
        assert Resolution.LOCAL == "local"

    def test_self_value(self) -> None:
        """SELF serialises to 'self'."""
        assert Resolution.SELF == "self"

    def test_builtin_value(self) -> None:
        """BUILTIN serialises to 'builtin'."""
        assert Resolution.BUILTIN == "builtin"

    def test_star_value(self) -> None:
        """STAR serialises to 'star'."""
        assert Resolution.STAR == "star"

    def test_unresolved_value(self) -> None:
        """UNRESOLVED serialises to 'unresolved'."""
        assert Resolution.UNRESOLVED == "unresolved"

    def test_complete_member_set(self) -> None:
        """Exactly six resolution kinds — adding/removing one must update this test."""
        expected = {"import", "local", "self", "builtin", "star", "unresolved"}
        assert {m.value for m in Resolution} == expected


class TestValidCallResolutions:
    """Validate the call-resolution schema and its exclusions."""

    def test_is_frozenset(self) -> None:
        """Must be a frozenset — immutable, safe to share across modules."""
        assert isinstance(VALID_CALL_RESOLUTIONS, frozenset)

    def test_includes_import(self) -> None:
        """IMPORT represents cross-module calls — must be in the valid set."""
        assert Resolution.IMPORT in VALID_CALL_RESOLUTIONS

    def test_includes_local(self) -> None:
        """LOCAL represents same-file calls — must be in the valid set."""
        assert Resolution.LOCAL in VALID_CALL_RESOLUTIONS

    def test_includes_self(self) -> None:
        """SELF represents method-to-method calls — must be in the valid set."""
        assert Resolution.SELF in VALID_CALL_RESOLUTIONS

    def test_excludes_builtin(self) -> None:
        """BUILTIN calls are outside the project graph — must be excluded."""
        assert Resolution.BUILTIN not in VALID_CALL_RESOLUTIONS

    def test_excludes_star(self) -> None:
        """STAR imports are unresolvable — must be excluded."""
        assert Resolution.STAR not in VALID_CALL_RESOLUTIONS

    def test_excludes_unresolved(self) -> None:
        """UNRESOLVED calls carry no graph information — must be excluded."""
        assert Resolution.UNRESOLVED not in VALID_CALL_RESOLUTIONS

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
        """v5.7 bumps SCAN_VERSION for resolved static submodule imports."""
        assert SCAN_VERSION == 13


class TestPerFeatureVersionConstants:
    """Per-feature min-version constants: type, positivity, ordering."""

    def test_all_are_int(self) -> None:
        """Each per-feature min-version constant must be a plain int."""
        for name in (
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
        ):
            assert isinstance(getattr(_schema, name), int), f"{name} must be int"

    def test_all_positive(self) -> None:
        """Each per-feature min-version constant must be ≥1."""
        for name in (
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
        ):
            assert getattr(_schema, name) > 0, f"{name} must be positive"

    def test_sphinx_before_dead_symbol(self) -> None:
        """Dead-symbol depends on sphinx_xref_count — its min version must be higher."""
        from _schema import DEAD_SYMBOL_MIN_VER, SPHINX_XREFS_MIN_VER

        assert SPHINX_XREFS_MIN_VER < DEAD_SYMBOL_MIN_VER

    def test_v4_constants_equal(self) -> None:
        """v4.1–v4.4 all require the same index version."""
        from _schema import (
            DOCSTRING_MIN_VER,
            IMPORT_GROUPS_MIN_VER,
            MOCK_PATCHES_MIN_VER,
            UNCOVERED_MIN_VER,
        )

        assert MOCK_PATCHES_MIN_VER == UNCOVERED_MIN_VER == IMPORT_GROUPS_MIN_VER == DOCSTRING_MIN_VER


def test_doctests_pass() -> None:
    """Doctest examples in _schema must not regress."""
    import doctest

    results = doctest.testmod(_schema, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"
