"""Extract the patch targets a test module installs via ``mock.patch``."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .calls import resolve_call_chain
from .imports import _process_ast_import, _process_ast_import_from

# Forms a mock patch can take in a test file.
#: Form label for a mock patch applied as a decorator.
_MOCK_FORM_DECORATOR = "decorator"


#: Form label for a mock patch made through a direct call.
_MOCK_FORM_CALL = "call"


#: Form label for a mock patch made through the pytest-mock mocker fixture.
_MOCK_FORM_MOCKER = "mocker"


def _normalize_patch_target(dotted: str) -> str | None:
    """Normalize a dotted patch string to ``module::symbol`` form.

    Heuristic: if the second-to-last component starts with an uppercase letter,
    treat it as a class (``module::ClassName.attr``); otherwise treat the last
    component as a free function or class at module level (``module::name``).

    Returns ``None`` when the input lacks any dotted structure (cannot be split
    into module + attribute).

    Examples:
        >>> _normalize_patch_target("mypackage.core.my_func")
        'mypackage.core::my_func'
        >>> _normalize_patch_target("mypackage.core.MyClass.method")
        'mypackage.core::MyClass.method'
        >>> _normalize_patch_target("mypackage.core.MyClass")
        'mypackage.core::MyClass'
        >>> _normalize_patch_target("singletoken") is None
        True
    """
    parts = dotted.split(".")
    if len(parts) < 2:
        return None
    # Detect "module.Class.method" — penultimate component starts uppercase ⇒ class.
    if len(parts) >= 3 and parts[-2][:1].isupper():
        module = ".".join(parts[:-2])
        attr = ".".join(parts[-2:])
    else:
        module = ".".join(parts[:-1])
        attr = parts[-1]
    return f"{module}::{attr}"


def _patch_string_arg(node: ast.Call) -> str | None:
    """Return the first argument's string value if it is an ``ast.Constant`` of type str."""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _is_patch_call(call: ast.Call) -> str | None:
    """Identify ``patch(...)``, ``mock.patch(...)``, or ``mocker.patch(...)`` calls.

    Returns the form label (``"mocker"`` when the receiver is ``mocker``, ``"call"`` otherwise) when *call* invokes
    ``patch`` with a string target, or ``None`` otherwise.
    """
    func = call.func
    # mocker.patch('...') or mock.patch('...')
    if isinstance(func, ast.Attribute) and func.attr == "patch":
        if isinstance(func.value, ast.Name):
            if func.value.id == "mocker":
                return _MOCK_FORM_MOCKER
            # mock.patch('...') — fallthrough to generic call form
            return _MOCK_FORM_CALL
        return _MOCK_FORM_CALL
    # bare patch('...')
    if isinstance(func, ast.Name) and func.id == "patch":
        return _MOCK_FORM_CALL
    return None


def _is_patch_object_call(call: ast.Call) -> bool:
    """Check whether a call invokes a supported object-patching function."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "object":
        return False
    inner = func.value
    if isinstance(inner, ast.Name) and inner.id == "patch":
        return True
    if isinstance(inner, ast.Attribute) and inner.attr == "patch":
        return True
    return False


def _resolve_patch_object(call: ast.Call, name_map: dict[str, str]) -> str | None:
    """Resolve a ``patch.object(mod, 'attr')`` call into a ``module::attr`` key.

    Returns ``None`` when the first argument is not a simple ``Name`` resolvable
    via *name_map*, or when the second argument is not a string constant.
    """
    if len(call.args) < 2:
        return None
    target_mod_node = call.args[0]
    attr_node = call.args[1]
    if not (isinstance(attr_node, ast.Constant) and isinstance(attr_node.value, str)):
        return None
    if isinstance(target_mod_node, ast.Name):
        resolved = name_map.get(target_mod_node.id, target_mod_node.id)
        return f"{resolved}::{attr_node.value}"
    if isinstance(target_mod_node, ast.Attribute):
        chain = resolve_call_chain(target_mod_node)
        if not chain:
            return None
        first = chain.split(".")[0]
        if first in name_map:
            resolved_base = name_map[first]
            rest = chain[len(first) + 1 :]
            full_mod = f"{resolved_base}.{rest}" if rest else resolved_base
            return f"{full_mod}::{attr_node.value}"
    return None


def extract_mock_patches(tree: ast.Module, filepath: Path) -> list[dict]:
    """Extract every ``patch(...)`` and ``patch.object(...)`` target in a test module.

    Detects four forms (decorator-string, decorator ``patch.object``, in-body call,
    and ``mocker.patch`` pytest-mock idiom) and normalises each to
    ``{"target": "module::symbol", "file": str(filepath), "line": int, "form": str}``.

    Unresolvable strings (no dots, or ``patch.object`` with non-Name module) are
    logged to stderr and skipped — never raise.

    Args:
        tree: parsed AST of the module.
        filepath: filesystem path of the test module (recorded in each entry).

    Returns:
        List of dicts, one per detected patch site, in source order.

    Examples:
        >>> import ast, pathlib
        >>> src = "from unittest.mock import patch\\n@patch('pkg.x.fn')\\ndef test_a():\\n    pass\\n"
        >>> result = extract_mock_patches(ast.parse(src), pathlib.Path("test_a.py"))
        >>> result[0]["target"]
        'pkg.x::fn'
        >>> result[0]["form"]
        'decorator'
    """
    collector = _PatchCollector(name_map=_patch_object_name_map(tree), file_str=str(filepath))

    # Walk decorators of every function/async-function/class definition.
    decorator_call_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for deco in node.decorator_list:
                # Decorator can be a Name (no args) or a Call.
                if isinstance(deco, ast.Call):
                    decorator_call_ids.add(id(deco))
                    collector.handle_call(deco, in_decorator=True)

    # Walk all remaining ast.Call nodes for in-body forms.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and id(node) not in decorator_call_ids:
            collector.handle_call(node, in_decorator=False)

    return collector.results


def _patch_object_name_map(tree: ast.Module) -> dict[str, str]:
    """Build the import name map used to resolve ``patch.object`` module references."""
    name_map: dict[str, str] = {}
    module_map: dict[str, str] = {}
    star_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            _process_ast_import(node, name_map, module_map)
        elif isinstance(node, ast.ImportFrom):
            _process_ast_import_from(node, "", name_map, star_imports)
    return name_map


@dataclass
class _PatchCollector:
    """Accumulate de-duplicated patch sites for one test module, in source order.

    Attributes:
        name_map: import name map resolving ``patch.object`` module arguments.
        file_str: test module path recorded in every entry and warning.
        results: collected ``{"target", "file", "line", "form"}`` entries.
        seen: ``(target, line, form)`` signatures already emitted.
    """

    name_map: dict[str, str]
    file_str: str
    results: list[dict] = field(default_factory=list)
    seen: set[tuple[str, int, str]] = field(default_factory=set)

    def emit(self, target_key: str, line: int, form: str) -> None:
        """Record one patch site unless the same target, line, and form was already recorded."""
        sig = (target_key, line, form)
        if sig in self.seen:
            return
        self.seen.add(sig)
        self.results.append({"target": target_key, "file": self.file_str, "line": line, "form": form})

    def handle_string_patch(self, call: ast.Call, form: str) -> None:
        """Record a ``patch("pkg.mod.name")`` call, warning when its target cannot be resolved."""
        raw = _patch_string_arg(call)
        if raw is None:
            return
        target_key = _normalize_patch_target(raw)
        if target_key is None:
            print(
                f"[codemap] ⚠ mock patch with unresolvable target '{raw}' at {self.file_str}:{call.lineno} — skipped",
                file=sys.stderr,
            )
            return
        self.emit(target_key, call.lineno, form)

    def handle_patch_object(self, call: ast.Call, form: str) -> None:
        """Record a ``patch.object(module, "name")`` call, warning when its module cannot be resolved."""
        target_key = _resolve_patch_object(call, self.name_map)
        if target_key is None:
            print(
                f"[codemap] ⚠ patch.object with unresolvable module at {self.file_str}:{call.lineno} — skipped",
                file=sys.stderr,
            )
            return
        self.emit(target_key, call.lineno, form)

    def handle_call(self, call: ast.Call, in_decorator: bool) -> None:
        """Classify one call as a patch form and record it under the form label it earns."""
        form_label = _MOCK_FORM_DECORATOR if in_decorator else None
        if _is_patch_object_call(call):
            self.handle_patch_object(call, form_label or _MOCK_FORM_CALL)
            return
        call_form = _is_patch_call(call)
        if call_form is None:
            return
        # Decorator wins over call-form label; mocker.patch keeps mocker form.
        if in_decorator and call_form == _MOCK_FORM_CALL:
            effective = _MOCK_FORM_DECORATOR
        else:
            effective = call_form
        self.handle_string_patch(call, effective)
