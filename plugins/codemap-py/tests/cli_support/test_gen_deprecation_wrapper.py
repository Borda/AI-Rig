"""Tests for ``bin/gen_deprecation_wrapper.py``, the pydeprecate stub generator.

The generator is a standalone script (loaded here by path) that renders a deprecation wrapper for a renamed function,
method or class, either from a symbol type (auto mode) or from an explicit decorator line. These tests pin the rendered
text fragments, the dunder/attribute hardening that keeps hostile arguments out of the generated module, and the
command-line entry point.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from enum import Enum
from pathlib import Path

import pytest

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


class TestImportForDecorator:
    """The import line inferred from a decorator expression."""

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
        """The decorator name picks its import line; ``deprecated_class`` must not match just ``deprecated``.

        ``deprecated_class`` contains ``deprecated``, so the check order decides whether a class decorator gets the
        right import.
        """
        assert _mod._import_for_decorator(line) == expected_import

    def test_unrecognised_decorator_raises(self):
        """A decorator naming neither pydeprecate API raises ``ValueError`` instead of guessing an import.

        A custom decorator has no known import, and silently emitting the wrong one would generate a broken module.
        """
        with pytest.raises(ValueError, match="Cannot infer import"):
            _mod._import_for_decorator("@some_other_decorator()")


class TestGenerate:
    """Auto-mode dispatch from a symbol type to the matching wrapper."""

    def test_symbol_type_is_python_310_compatible_string_enum(self):
        """The wrapper accepts enum members without requiring ``enum.StrEnum``.

        ``StrEnum`` is 3.11+, and the plugin supports 3.10, so ``SymbolType`` must be a ``(str, Enum)`` mixin.
        """
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
        """Function and method symbols route to the plain ``@deprecated`` wrapper, never ``deprecated_class``.

        ``@deprecated`` on a class would emit a ``UserWarning`` and silently delegate, so only classes may use
        ``deprecated_class``.
        """
        code = generate(symbol_type, old, new)

        assert "@deprecated" in code
        assert "deprecated_class" not in code

    def test_class_type_routes_to_deprecated_class_wrapper(self):
        """A class symbol routes to the ``@deprecated_class`` wrapper.

        ``deprecated_class`` is the transparent proxy that forwards attribute access, calls and ``isinstance``.
        """
        assert "@deprecated_class" in generate(SymbolType.CLASS, "OldCls", "NewCls")

    def test_unknown_type_raises(self):
        """An unknown ``symbol_type`` raises ``ValueError`` rather than emitting a wrapper of some default kind.

        A typo such as ``variable`` has no wrapper, and guessing would hide the mistake.
        """
        with pytest.raises(ValueError, match="Unknown symbol_type"):
            generate("variable", "x", "y")

    def test_default_versions_are_question_mark(self):
        """Omitting ``since`` and ``removed_in`` renders the placeholder version ``"?"``.

        The caller may not know the versions yet; the placeholder marks the stub as needing them filled in.
        """
        assert '"?"' in generate(SymbolType.FUNCTION, "old", "new")


class TestWrapperHasNoFallback:
    """Every wrapper relies on pydeprecate alone."""

    @pytest.mark.parametrize(
        ("generator", "args"),
        [
            pytest.param(gen_function_wrapper, ("f", "g", "?", "?"), id="function-wrapper"),
            pytest.param(gen_class_wrapper, ("OldCls", "NewCls", "?", "?"), id="class-wrapper"),
            pytest.param(generate, (SymbolType.FUNCTION, "old", "new"), id="auto-mode"),
            pytest.param(
                gen_wrapper_from_decorator,
                ("@deprecated(target=b, deprecated_in='?', remove_in='?')", "a"),
                id="explicit",
            ),
        ],
    )
    def test_no_fallback(self, generator, args):
        """Generated code uses pydeprecate alone: no ``warnings`` shim and no ``except`` fallback.

        Deprecation policy forbids ``warnings.warn``; a missing pydeprecate must fail loudly, not degrade silently.
        """
        code = generator(*args)

        assert "warnings" not in code
        assert "except" not in code


class TestWrapperContainsFragment:
    """Function and class wrappers share one rendering contract."""

    @pytest.mark.parametrize(
        ("generator", "old", "new", "since", "removed_in", "fragment"),
        [
            pytest.param(gen_function_wrapper, "old_fn", "new_fn", "1.0", "2.0", "old_fn", id="function-old-name"),
            pytest.param(gen_function_wrapper, "f", "g", "1.5", "2.0", 'deprecated_in="1.5"', id="function-since"),
            pytest.param(gen_function_wrapper, "f", "g", "1.5", "2.0", 'remove_in="2.0"', id="function-removed-in"),
            pytest.param(
                gen_function_wrapper,
                "old_fn",
                "new_fn",
                "?",
                "?",
                "def old_fn(*args, **kwargs): ...",
                id="function-stub-body-is-ellipsis",
            ),
            pytest.param(gen_class_wrapper, "OldCls", "NewCls", "1.0", "2.0", "OldCls", id="class-old-name"),
            pytest.param(gen_class_wrapper, "OldCls", "NewCls", "?", "?", "target=NewCls", id="class-new-name-target"),
            pytest.param(
                gen_class_wrapper,
                "OldCls",
                "NewCls",
                "?",
                "?",
                "from deprecate import deprecated_class",
                id="class-import",
            ),
            pytest.param(
                gen_class_wrapper, "OldCls", "NewCls", "?", "?", "@deprecated_class(", id="class-decorator-form"
            ),
            pytest.param(gen_class_wrapper, "OldCls", "NewCls", "?", "?", "class OldCls: ...", id="class-stub-not-def"),
            pytest.param(gen_class_wrapper, "A", "B", "0.5", "1.0", 'deprecated_in="0.5"', id="class-since"),
            pytest.param(gen_class_wrapper, "A", "B", "0.5", "1.0", 'remove_in="1.0"', id="class-removed-in"),
        ],
    )
    def test_wrapper_contains_fragment(self, generator, old, new, since, removed_in, fragment):
        """Function and class wrappers render their names, versions, decorator form and stub.

        The old name appears in the stub definition, both versions are rendered as quoted keyword arguments, and the
        function wrapper's body is ``...`` because pydeprecate handles the forwarding. The class wrapper references the
        new name as ``target=``, uses the ``@deprecated_class`` decorator form (not assignment) and stubs ``class
        OldCls: ...`` rather than a ``def``.
        """
        assert fragment in generator(old, new, since, removed_in)


class TestFunctionWrapper:
    """Details specific to the function wrapper."""

    @pytest.mark.parametrize(
        ("old", "new", "present", "absent"),
        [
            pytest.param("old_fn", "new_fn", "target=new_fn", 'target="new_fn"', id="new-name-as-reference"),
            pytest.param("f", "g", "from deprecate import deprecated", "deprecated_class", id="plain-import"),
        ],
    )
    def test_wrapper_contains_fragment_and_omits_alternative(self, old, new, present, absent):
        """The wrapper uses the right reference form and import, never the alternative spelling.

        The new name is a ``target=`` reference rather than a string, and the function wrapper imports plain
        ``deprecated`` rather than ``deprecated_class``.
        """
        code = gen_function_wrapper(old, new, "?", "?")

        assert present in code
        assert absent not in code


class TestWrapperFromDecorator:
    """Explicit mode: the caller supplies the decorator line."""

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
        """The ``removed_in`` version is shown in the comment header.

        A reader scanning the generated stub sees when the shim is scheduled to disappear.
        """
        decorator = "@deprecated(target=bar, deprecated_in='1.0', remove_in='3.0')"

        assert "3.0" in gen_wrapper_from_decorator(decorator, "foo", removed_in="3.0")

    def test_unknown_decorator_raises(self):
        """A decorator that is neither pydeprecate API raises ``ValueError`` before any code is generated.

        The import cannot be inferred, so refusing is safer than emitting a stub that will not import.
        """
        with pytest.raises(ValueError, match="Cannot infer import"):
            gen_wrapper_from_decorator("@my_custom_deco()", "foo")


class TestDunderAndAttributeHardening:
    """Names and decorator arguments that reach interpreter internals are refused before any code is generated."""

    @pytest.mark.parametrize(
        "decorator",
        [
            pytest.param("@deprecated(target='x'.__class__)", id="constant-rooted-dunder"),
            pytest.param("@deprecated(target=Bar.__subclasses__)", id="name-rooted-dunder"),
            pytest.param("@deprecated(target=a.b.c.d.e.f.g.h)", id="chain-too-deep"),
        ],
    )
    def test_unsafe_attribute_argument_rejected(self, decorator):
        """An attribute chain that runs descriptors at import (dunder segment, constant root, deep chain) is refused.

        Attribute access is evaluated when the generated module is imported, so ``'x'.__class__`` or a deep chain is
        behaviour smuggled into an argument slot that is meant to hold only a reference.
        """
        with pytest.raises(ValueError, match="arguments must be names, attributes, or constants only"):
            gen_wrapper_from_decorator(decorator, "foo")

    def test_dotted_class_method_target_still_accepted(self):
        """A plain ``Class.method`` target, the documented method-rename shape, keeps working.

        The hardening must not reject the one dotted form the tool exists to support.
        """
        code = gen_wrapper_from_decorator("@deprecated(target=Klass.method, deprecated_in='1.0')", "foo")

        assert "target=Klass.method" in code

    @pytest.mark.parametrize("old_name", ["__import__", "__class__"])
    def test_dunder_old_name_rejected(self, old_name):
        """A dunder ``--old-name`` would generate ``def __import__(...)`` and is refused.

        Defining a dunder in the generated module would shadow interpreter hooks.
        """
        with pytest.raises(ValueError, match="dunder"):
            _mod._check_identifier(old_name, "--old-name")

    def test_dunder_method_segment_rejected_in_dotted_target(self):
        """The ``Method`` half of a dotted ``--new-name`` is checked as well.

        ``Klass.__class__`` passes a naive ``Name.Name`` check but still names an interpreter internal.
        """
        with pytest.raises(ValueError, match="dunder"):
            _mod._check_identifier("Klass.__class__", "--new-name", allow_dotted=True)


class TestCLI:
    """The ``main()`` entry point driven through ``sys.argv``."""

    @pytest.mark.parametrize(
        ("symbol_type", "old", "new", "decorator"),
        [
            pytest.param("function", "f", "g", "@deprecated", id="function"),
            pytest.param("class", "Old", "New", "@deprecated_class", id="class"),
        ],
    )
    def test_auto_mode_emits_matching_decorator(self, capsys, monkeypatch, symbol_type, old, new, decorator):
        """Auto mode with ``--type`` produces the wrapper decorator matching that symbol type.

        The generated text goes to stdout and names the old symbol.
        """
        monkeypatch.setattr(sys, "argv", ["g", "--type", symbol_type, "--old-name", old, "--new-name", new])

        _mod.main()

        out = capsys.readouterr().out
        assert decorator in out
        assert old in out

    def test_auto_mode_version_flags(self, capsys, monkeypatch):
        """``--since`` and ``--removed-in`` flow into the generated decorator.

        The versions given on the command line must appear in the printed wrapper.
        """
        argv = ["g", "--type", "function", "--old-name", "f", "--new-name", "g", "--since", "1.2.3"]
        monkeypatch.setattr(sys, "argv", [*argv, "--removed-in", "2.0.0"])

        _mod.main()

        out = capsys.readouterr().out
        assert "1.2.3" in out
        assert "2.0.0" in out

    def test_explicit_mode_decorator(self, capsys, monkeypatch):
        """``--decorator`` mode uses the provided decorator line and derives the stub from it.

        The decorator is echoed with its inferred import and a ``def`` stub for ``--old-name``.
        """
        decorator = "@deprecated(target=bar, deprecated_in='1.0', remove_in='2.0')"
        monkeypatch.setattr(sys, "argv", ["g", "--decorator", decorator, "--old-name", "foo"])

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
        """Auto mode without ``--new-name`` fails, and argparse rejects an invalid ``--type`` choice (exit 2).

        Bad invocations must stop with a non-zero status instead of printing a half-built wrapper.
        """
        monkeypatch.setattr(sys, "argv", argv)

        with pytest.raises(SystemExit) as exc_info:
            _mod.main()

        assert exc_info.value.code != 0
