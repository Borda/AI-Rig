"""Tests for gen_deprecation_wrapper.py."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from enum import Enum
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load module (extensionless file)
# ---------------------------------------------------------------------------

_BIN = Path(__file__).parent.parent.parent / "bin" / "gen_deprecation_wrapper.py"


def _load() -> object:
    """Load the extensionless generator module under test."""
    loader = importlib.machinery.SourceFileLoader("gen_deprecation_wrapper", str(_BIN))
    spec = importlib.util.spec_from_loader("gen_deprecation_wrapper", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["gen_deprecation_wrapper"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load()
generate = _mod.generate
gen_function_wrapper = _mod.gen_function_wrapper
gen_class_wrapper = _mod.gen_class_wrapper
gen_wrapper_from_decorator = _mod.gen_wrapper_from_decorator
SymbolType = _mod.SymbolType


# ---------------------------------------------------------------------------
# _import_for_decorator
# ---------------------------------------------------------------------------


class TestImportForDecorator:
    @pytest.mark.parametrize(
        ("line", "expected_import"),
        [
            pytest.param(
                "@deprecated_class(target=New, deprecated_in='1.0', remove_in='2.0')",
                "from deprecate import deprecated_class",
                id="deprecated-class-wins-over-deprecated",
            ),
            pytest.param(
                "@deprecated(target=bar, deprecated_in='1.0', remove_in='2.0')",
                "from deprecate import deprecated",
                id="plain-deprecated",
            ),
        ],
    )
    def test_decorator_maps_to_its_import(self, line, expected_import):
        """The decorator name picks its import line; ``deprecated_class`` must not match just ``deprecated``."""
        result = _mod._import_for_decorator(line)
        assert result == expected_import

    def test_unrecognised_raises(self):
        """Decorator not containing expected names should raise ValueError."""
        with pytest.raises(ValueError, match="Cannot infer import"):
            _mod._import_for_decorator("@some_other_decorator()")


# ---------------------------------------------------------------------------
# generate() — auto mode dispatch
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_symbol_type_uses_python_310_compatible_string_enum(self):
        """The wrapper accepts enum members without requiring ``enum.StrEnum``."""
        assert SymbolType.__bases__ == (str, Enum)
        assert [member.value for member in SymbolType] == ["function", "method", "class"]

    @pytest.mark.parametrize(
        ("symbol_type", "old", "new"),
        [
            pytest.param(SymbolType.FUNCTION, "old_fn", "new_fn", id="function"),
            pytest.param(SymbolType.METHOD, "old_m", "new_m", id="method"),
        ],
    )
    def test_function_like_type_routes_to_deprecated_wrapper(self, symbol_type, old, new):
        """Function and method symbols route to the plain ``@deprecated`` wrapper, never ``deprecated_class``."""
        code = generate(symbol_type, old, new)
        assert "@deprecated" in code
        assert "deprecated_class" not in code

    def test_class_type(self):
        """Class routes to @deprecated_class wrapper."""
        code = generate(SymbolType.CLASS, "OldCls", "NewCls")
        assert "@deprecated_class" in code

    def test_unknown_type_raises(self):
        """Unknown symbol_type raises ValueError."""
        with pytest.raises(ValueError, match="Unknown symbol_type"):
            generate("variable", "x", "y")

    def test_default_versions_are_question_mark(self):
        """Omitting since/removed_in defaults to '?'."""
        code = generate(SymbolType.FUNCTION, "old", "new")
        assert '"?"' in code

    def test_no_warnings_warn(self):
        """No fallback — pydeprecate only."""
        code = generate(SymbolType.FUNCTION, "old", "new")
        assert "warnings" not in code


# ---------------------------------------------------------------------------
# gen_function_wrapper / gen_class_wrapper — shared fragment contract
# ---------------------------------------------------------------------------


class TestWrapperContainsFragment:
    @pytest.mark.parametrize(
        ("generator", "old", "new", "since", "removed_in", "fragment"),
        [
            pytest.param(
                gen_function_wrapper, "old_fn", "new_fn", "1.0", "2.0", "old_fn", id="function-old-name-present"
            ),
            pytest.param(
                gen_function_wrapper, "f", "g", "1.5", "2.0", 'deprecated_in="1.5"', id="function-deprecated-in-version"
            ),
            pytest.param(
                gen_function_wrapper, "f", "g", "1.5", "2.0", 'remove_in="2.0"', id="function-remove-in-version"
            ),
            pytest.param(
                gen_function_wrapper,
                "old_fn",
                "new_fn",
                "?",
                "?",
                "def old_fn(*args, **kwargs): ...",
                id="function-stub-body-is-ellipsis",
            ),
            pytest.param(gen_class_wrapper, "OldCls", "NewCls", "1.0", "2.0", "OldCls", id="class-old-name-present"),
            pytest.param(
                gen_class_wrapper, "OldCls", "NewCls", "?", "?", "target=NewCls", id="class-new-name-as-target"
            ),
            pytest.param(
                gen_class_wrapper,
                "OldCls",
                "NewCls",
                "?",
                "?",
                "from deprecate import deprecated_class",
                id="class-decorator-class-import",
            ),
            pytest.param(
                gen_class_wrapper,
                "OldCls",
                "NewCls",
                "?",
                "?",
                "@deprecated_class(",
                id="class-decorator-form-not-assignment",
            ),
            pytest.param(gen_class_wrapper, "OldCls", "NewCls", "?", "?", "class OldCls: ...", id="class-stub-not-def"),
            pytest.param(
                gen_class_wrapper, "A", "B", "0.5", "1.0", 'deprecated_in="0.5"', id="class-deprecated-in-version"
            ),
            pytest.param(gen_class_wrapper, "A", "B", "0.5", "1.0", 'remove_in="1.0"', id="class-remove-in-version"),
        ],
    )
    def test_wrapper_contains_fragment(self, generator, old, new, since, removed_in, fragment):
        """Function and class wrappers render their names, versions, decorator form and stub.

        The old name appears in the stub definition, both versions are rendered as quoted keyword arguments, and the
        function wrapper's body is ``...`` because pydeprecate handles the forwarding. The class wrapper references the
        new name as ``target=``, uses the ``@deprecated_class`` decorator form (not assignment) and stubs ``class
        OldCls: ...`` rather than a ``def``.
        """
        code = generator(old, new, since, removed_in)
        assert fragment in code


# ---------------------------------------------------------------------------
# gen_function_wrapper / gen_class_wrapper — shared no-fallback contract
# ---------------------------------------------------------------------------


class TestWrapperHasNoFallback:
    @pytest.mark.parametrize(
        ("generator", "args"),
        [
            pytest.param(gen_function_wrapper, ("f", "g", "?", "?"), id="function-wrapper"),
            pytest.param(gen_class_wrapper, ("OldCls", "NewCls", "?", "?"), id="class-wrapper"),
        ],
    )
    def test_no_fallback(self, generator, args):
        """Function and class wrappers rely on pydeprecate alone: no ``warnings`` shim, no ``except`` fallback."""
        code = generator(*args)
        assert "warnings" not in code
        assert "except" not in code


# ---------------------------------------------------------------------------
# gen_function_wrapper
# ---------------------------------------------------------------------------


class TestFunctionWrapper:
    @pytest.mark.parametrize(
        ("old", "new", "since", "removed_in", "present", "absent"),
        [
            pytest.param("old_fn", "new_fn", "1.0", "2.0", "target=new_fn", 'target="new_fn"', id="new-name-as-target"),
            pytest.param(
                "f", "g", "?", "?", "from deprecate import deprecated", "deprecated_class", id="correct-import"
            ),
        ],
    )
    def test_wrapper_contains_fragment_and_omits_alternative(self, old, new, since, removed_in, present, absent):
        """The wrapper uses the right reference form and import, never the alternative spelling.

        The new name is a ``target=`` reference rather than a string, and the function wrapper imports plain
        ``deprecated`` rather than ``deprecated_class``.
        """
        code = gen_function_wrapper(old, new, since, removed_in)
        assert present in code
        assert absent not in code


# ---------------------------------------------------------------------------
# gen_wrapper_from_decorator — explicit mode
# ---------------------------------------------------------------------------


class TestWrapperFromDecorator:
    @pytest.mark.parametrize(
        ("decorator", "name", "expected_import", "expected_stub"),
        [
            pytest.param(
                "@deprecated(target=bar, deprecated_in='1.0', remove_in='2.0')",
                "foo",
                "from deprecate import deprecated",
                "def foo(*args, **kwargs): ...",
                id="function-decorator",
            ),
            pytest.param(
                "@deprecated_class(target=Bar, deprecated_in='1.0', remove_in='2.0')",
                "Foo",
                "from deprecate import deprecated_class",
                "class Foo: ...",
                id="class-decorator",
            ),
        ],
    )
    def test_explicit_decorator_produces_matching_stub(self, decorator, name, expected_import, expected_stub):
        """An explicit decorator line yields its import and the stub kind it implies.

        ``@deprecated(...)`` produces a ``def`` stub; ``@deprecated_class(...)`` produces a ``class`` stub.
        """
        code = gen_wrapper_from_decorator(decorator, name)
        assert expected_import in code
        assert expected_stub in code

    def test_removed_in_appears_in_comment(self):
        """removed_in version shows in comment header."""
        code = gen_wrapper_from_decorator(
            "@deprecated(target=bar, deprecated_in='1.0', remove_in='3.0')", "foo", removed_in="3.0"
        )
        assert "3.0" in code

    def test_no_fallback(self):
        code = gen_wrapper_from_decorator("@deprecated(target=b, deprecated_in='?', remove_in='?')", "a")
        assert "warnings" not in code

    def test_unknown_decorator_raises(self):
        with pytest.raises(ValueError, match="Cannot infer import"):
            gen_wrapper_from_decorator("@my_custom_deco()", "foo")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    @pytest.mark.parametrize(
        ("symbol_type", "old", "new", "decorator"),
        [
            pytest.param("function", "f", "g", "@deprecated", id="function"),
            pytest.param("class", "Old", "New", "@deprecated_class", id="class"),
        ],
    )
    def test_auto_mode_emits_matching_decorator(self, capsys, monkeypatch, symbol_type, old, new, decorator):
        """Auto mode with ``--type`` produces the wrapper decorator matching that symbol type."""
        monkeypatch.setattr(sys, "argv", ["g", "--type", symbol_type, "--old-name", old, "--new-name", new])
        _mod.main()
        out = capsys.readouterr().out
        assert decorator in out
        assert old in out

    def test_auto_mode_version_flags(self, capsys, monkeypatch):
        """Verify command-line option behavior.

        ``--since`` and ``--removed-in`` flow into decorator.
        """
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "g",
                "--type",
                "function",
                "--old-name",
                "f",
                "--new-name",
                "g",
                "--since",
                "1.2.3",
                "--removed-in",
                "2.0.0",
            ],
        )
        _mod.main()
        out = capsys.readouterr().out
        assert "1.2.3" in out
        assert "2.0.0" in out

    def test_explicit_mode_decorator(self, capsys, monkeypatch):
        """Verify command-line option behavior.

        ``--decorator`` mode uses provided decorator line.
        """
        monkeypatch.setattr(
            sys,
            "argv",
            ["g", "--decorator", "@deprecated(target=bar, deprecated_in='1.0', remove_in='2.0')", "--old-name", "foo"],
        )
        _mod.main()
        out = capsys.readouterr().out
        assert "from deprecate import deprecated" in out
        assert "def foo(*args, **kwargs): ..." in out

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["g", "--type", "function", "--old-name", "f"], id="missing-new-name-in-auto-mode"),
            pytest.param(["g", "--type", "variable", "--old-name", "x", "--new-name", "y"], id="bad-type-choice"),
        ],
    )
    def test_invalid_arguments_exit_nonzero(self, monkeypatch, argv):
        """Auto mode without ``--new-name`` fails, and argparse rejects an invalid ``--type`` choice (exit 2)."""
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc_info:
            _mod.main()
        assert exc_info.value.code != 0
