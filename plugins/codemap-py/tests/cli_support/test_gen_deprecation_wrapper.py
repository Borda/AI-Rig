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
    def test_deprecated_class_wins_over_deprecated(self):
        """deprecated_class must not match just 'deprecated'."""
        line = "@deprecated_class(target=New, deprecated_in='1.0', remove_in='2.0')"
        result = _mod._import_for_decorator(line)
        assert result == "from deprecate import deprecated_class"

    def test_plain_deprecated(self):
        """@deprecated(...) maps to plain import."""
        line = "@deprecated(target=bar, deprecated_in='1.0', remove_in='2.0')"
        result = _mod._import_for_decorator(line)
        assert result == "from deprecate import deprecated"

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

    def test_function_type(self):
        """Function routes to @deprecated wrapper."""
        code = generate(SymbolType.FUNCTION, "old_fn", "new_fn")
        assert "@deprecated" in code
        assert "deprecated_class" not in code

    def test_method_type(self):
        """Method routes to @deprecated wrapper (same as function)."""
        code = generate(SymbolType.METHOD, "old_m", "new_m")
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
# gen_function_wrapper
# ---------------------------------------------------------------------------


class TestFunctionWrapper:
    def test_old_name_present(self):
        """Old name appears in stub definition."""
        code = gen_function_wrapper("old_fn", "new_fn", "1.0", "2.0")
        assert "old_fn" in code

    def test_new_name_as_target(self):
        """New name used as target= reference (not string)."""
        code = gen_function_wrapper("old_fn", "new_fn", "1.0", "2.0")
        assert "target=new_fn" in code
        assert 'target="new_fn"' not in code

    def test_deprecated_in_version(self):
        code = gen_function_wrapper("f", "g", "1.5", "2.0")
        assert 'deprecated_in="1.5"' in code

    def test_remove_in_version(self):
        code = gen_function_wrapper("f", "g", "1.5", "2.0")
        assert 'remove_in="2.0"' in code

    def test_stub_body_is_ellipsis(self):
        """Body is '...' — pydeprecate handles forwarding."""
        code = gen_function_wrapper("old_fn", "new_fn", "?", "?")
        assert "def old_fn(*args, **kwargs): ..." in code

    def test_correct_import(self):
        code = gen_function_wrapper("f", "g", "?", "?")
        assert "from deprecate import deprecated" in code
        assert "deprecated_class" not in code

    def test_no_fallback(self):
        code = gen_function_wrapper("f", "g", "?", "?")
        assert "warnings" not in code
        assert "except" not in code


# ---------------------------------------------------------------------------
# gen_class_wrapper
# ---------------------------------------------------------------------------


class TestClassWrapper:
    def test_old_name_present(self):
        code = gen_class_wrapper("OldCls", "NewCls", "1.0", "2.0")
        assert "OldCls" in code

    def test_new_name_as_target(self):
        """New name used as target= reference."""
        code = gen_class_wrapper("OldCls", "NewCls", "?", "?")
        assert "target=NewCls" in code

    def test_decorator_class_import(self):
        code = gen_class_wrapper("OldCls", "NewCls", "?", "?")
        assert "from deprecate import deprecated_class" in code

    def test_decorator_form(self):
        """Uses @deprecated_class decorator, not assignment form."""
        code = gen_class_wrapper("OldCls", "NewCls", "?", "?")
        assert "@deprecated_class(" in code

    def test_class_stub(self):
        """Stub is 'class OldCls: ...' not a def."""
        code = gen_class_wrapper("OldCls", "NewCls", "?", "?")
        assert "class OldCls: ..." in code

    def test_deprecated_in_version(self):
        code = gen_class_wrapper("A", "B", "0.5", "1.0")
        assert 'deprecated_in="0.5"' in code

    def test_remove_in_version(self):
        code = gen_class_wrapper("A", "B", "0.5", "1.0")
        assert 'remove_in="1.0"' in code

    def test_no_fallback(self):
        code = gen_class_wrapper("OldCls", "NewCls", "?", "?")
        assert "warnings" not in code
        assert "except" not in code


# ---------------------------------------------------------------------------
# gen_wrapper_from_decorator — explicit mode
# ---------------------------------------------------------------------------


class TestWrapperFromDecorator:
    def test_function_decorator_explicit(self):
        """Explicit @deprecated(...) decorator produces def stub."""
        code = gen_wrapper_from_decorator("@deprecated(target=bar, deprecated_in='1.0', remove_in='2.0')", "foo")
        assert "from deprecate import deprecated" in code
        assert "def foo(*args, **kwargs): ..." in code

    def test_class_decorator_explicit(self):
        """Explicit @deprecated_class(...) decorator produces class stub."""
        code = gen_wrapper_from_decorator("@deprecated_class(target=Bar, deprecated_in='1.0', remove_in='2.0')", "Foo")
        assert "from deprecate import deprecated_class" in code
        assert "class Foo: ..." in code

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
    def test_auto_mode_function(self, capsys, monkeypatch):
        """Auto mode with ``--type function`` produces deprecated wrapper."""
        monkeypatch.setattr(sys, "argv", ["g", "--type", "function", "--old-name", "f", "--new-name", "g"])
        _mod.main()
        out = capsys.readouterr().out
        assert "@deprecated" in out and "f" in out

    def test_auto_mode_class(self, capsys, monkeypatch):
        """Auto mode with ``--type class`` produces deprecated_class wrapper."""
        monkeypatch.setattr(sys, "argv", ["g", "--type", "class", "--old-name", "Old", "--new-name", "New"])
        _mod.main()
        out = capsys.readouterr().out
        assert "@deprecated_class" in out

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
        assert "1.2.3" in out and "2.0.0" in out

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

    def test_missing_new_name_in_auto_mode_exits(self, monkeypatch):
        """Auto mode without ``--new-name`` should fail."""
        monkeypatch.setattr(sys, "argv", ["g", "--type", "function", "--old-name", "f"])
        with pytest.raises(SystemExit) as exc_info:
            _mod.main()
        assert exc_info.value.code != 0

    def test_bad_type_exits(self, monkeypatch):
        """Argparse rejects invalid ``--type`` choice (exit 2)."""
        monkeypatch.setattr(sys, "argv", ["g", "--type", "variable", "--old-name", "x", "--new-name", "y"])
        with pytest.raises(SystemExit) as exc_info:
            _mod.main()
        assert exc_info.value.code != 0
