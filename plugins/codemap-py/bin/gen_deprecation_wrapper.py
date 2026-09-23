#!/usr/bin/env python3
"""Generate Python deprecation wrapper code for codemap:rename-refs.

Given symbol type (function/method/class), old name, new name, and optional
version strings, outputs the Python source code to stdout. The caller inserts
this block immediately after the new definition in the source file.

Requires pyDeprecate (``pip install pyDeprecate``).

Two modes:

**Auto** — supply ``--type``, ``--old-name``, ``--new-name`` (and optionally
``--since`` / ``--removed-in``); the script builds the full decorator line.

**Explicit** — supply ``--decorator "@deprecated(...)"`` (the full decorator
line, already built by the caller) plus ``--old-name``.  The script adds the
correct import statement and the stub definition.

Usage::

    # auto mode — function/method
    python gen_deprecation_wrapper.py \\
        --type function --old-name foo --new-name bar \\
        --since 1.2.0 --removed-in 2.0.0

    # auto mode — class
    python gen_deprecation_wrapper.py \\
        --type class --old-name OldCls --new-name NewCls

    # explicit mode (skill or user supplies full decorator line)
    python gen_deprecation_wrapper.py \\
        --decorator "@deprecated(target=bar, deprecated_in='1.0', remove_in='2.0')" \\
        --old-name foo
"""

from __future__ import annotations

import argparse
import ast
import json
import keyword
import sys
import textwrap
from enum import Enum


class SymbolType(str, Enum):
    """Kind of symbol the generated wrapper deprecates.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``. Declared locally rather than
    imported from ``codemap_py.schema``: this script is a standalone codegen entry point with no package import path of
    its own.
    """

    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"


# ---------------------------------------------------------------------------
# Import inference
# ---------------------------------------------------------------------------

_IMPORT_MAP = {
    "deprecated_class": "from deprecate import deprecated_class",
    "deprecated": "from deprecate import deprecated",
}


def _import_for_decorator(decorator: str) -> str:
    """Return the ``from deprecate import ...`` line for *decorator*.

    Retained for its own pinned direct-call tests and doctest below. The runtime
    path in :func:`gen_wrapper_from_decorator` does not call this — it calls
    :func:`_decorator_func_name` once and reuses the result for both the import
    line and the ``def``/``class`` stub choice, rather than parsing twice.

    >>> _import_for_decorator("@deprecated_class(target=New, deprecated_in='1.0', remove_in='2.0')")
    'from deprecate import deprecated_class'
    >>> _import_for_decorator("@deprecated(target=bar, deprecated_in='1.0', remove_in='2.0')")
    'from deprecate import deprecated'
    """
    return _import_line_for_name(_decorator_func_name(decorator), decorator)


def _import_line_for_name(func_name: str, decorator: str) -> str:
    """Return the ``from deprecate import ...`` line for *func_name*.

    Args:
        func_name: the decorator call's own function name, as returned by
            :func:`_decorator_func_name` — ``"deprecated"`` or ``"deprecated_class"``.
        decorator: original decorator line, used only to compose the error message.

    Raises:
        ValueError: *func_name* is neither ``"deprecated"`` nor ``"deprecated_class"``.

    Examples:
        >>> _import_line_for_name("deprecated", "@deprecated(...)")
        'from deprecate import deprecated'
        >>> _import_line_for_name("deprecated_class", "@deprecated_class(...)")
        'from deprecate import deprecated_class'
        >>> _import_line_for_name("bad", "@bad()")
        Traceback (most recent call last):
            ...
        ValueError: Cannot infer import — decorator is not deprecated()/deprecated_class(): '@bad()'
    """
    if func_name not in _IMPORT_MAP:
        raise ValueError(f"Cannot infer import — decorator is not deprecated()/deprecated_class(): {decorator!r}")
    return _IMPORT_MAP[func_name]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
#
# Every generator below embeds old_name/new_name/since/removed_in/decorator
# directly into generated Python source with no syntax-level sandbox. The
# guards here are the complete set: _check_identifier for the two name-shaped
# parameters (interpolated bare, no quotes to escape out of), _reject_unsafe
# for the two version-string parameters (interpolated once inside a
# json.dumps()-quoted literal, and — for removed_in only — once more raw inside
# a comment), and _decorator_func_name/_is_safe_arg_node for the explicit-mode
# decorator line, which a caller supplies pre-built and which a substring check
# alone cannot safely validate (a "deprecated"-looking string can still carry
# an arbitrary call as one of its own argument values).


def _check_identifier(value: str, flag: str, *, allow_dotted: bool = False) -> None:
    """Raise ``ValueError`` unless *value* is safe to interpolate as a name.

    ``old_name`` and ``new_name`` are embedded directly into generated Python
    source (``def {old_name}(...)``, ``target={new_name}``) with no surrounding
    quotes. An unvalidated value lets an arbitrary Python expression ride along
    in its place instead of a plain name. ASCII-only, on top of the identifier
    check: Python accepts many non-ASCII "confusable" characters as identifier
    text (e.g. fullwidth ``ｐｒｉｎｔ``, which CPython's tokenizer NFKC-normalizes
    to ``print``) — not executable on their own, but a way to make generated
    code visually misleading under review.

    Args:
        value: candidate identifier, or dotted ``Class.method`` pair when *allow_dotted*.
        flag: CLI flag name for the error message, e.g. ``"--old-name"``.
        allow_dotted: accept exactly one ``.``-separated pair of identifiers — the
            method-rename case, where the replacement target is ``Class.method``.
            Each segment is validated independently; a bare identifier (no dot)
            is still accepted when this is set.

    Raises:
        ValueError: *value* is not a bare ASCII identifier (or, when
            *allow_dotted*, not a bare identifier or a single dotted pair), any
            segment is a Python keyword, or any segment contains a non-ASCII
            character.

    Examples:
        >>> _check_identifier("old_fn", "--old-name")
        >>> _check_identifier("bad name", "--old-name")
        Traceback (most recent call last):
            ...
        ValueError: --old-name must be a bare ASCII identifier: 'bad name'
        >>> _check_identifier("class", "--old-name")
        Traceback (most recent call last):
            ...
        ValueError: --old-name must be a bare ASCII identifier: 'class'
        >>> _check_identifier("ｐｒｉｎｔ", "--old-name")
        Traceback (most recent call last):
            ...
        ValueError: --old-name must be a bare ASCII identifier: 'ｐｒｉｎｔ'
        >>> _check_identifier("MyClass.new_method", "--new-name", allow_dotted=True)
        >>> _check_identifier("a.b.c", "--new-name", allow_dotted=True)
        Traceback (most recent call last):
            ...
        ValueError: --new-name must be a bare ASCII identifier or a single 'Class.method' pair: 'a.b.c'
        >>> _check_identifier('os.system("x")', "--new-name", allow_dotted=True)
        Traceback (most recent call last):
            ...
        ValueError: --new-name must be a bare ASCII identifier or a single 'Class.method' pair: 'os.system("x")'
    """
    segments = value.split(".") if allow_dotted else [value]
    if len(segments) > 2 or any(
        not segment.isascii() or not segment.isidentifier() or keyword.iskeyword(segment) for segment in segments
    ):
        if allow_dotted:
            raise ValueError(f"{flag} must be a bare ASCII identifier or a single 'Class.method' pair: {value!r}")
        raise ValueError(f"{flag} must be a bare ASCII identifier: {value!r}")


def _reject_unsafe(value: str, field_name: str) -> None:
    """Raise ``ValueError`` if *value* could break out of its interpolation site.

    ``since``/``removed_in`` reach generated source two ways: inside a
    double-quoted literal built with :func:`json.dumps` (which escapes quotes and
    backslashes on its own), and — for ``removed_in`` only — raw and unquoted
    inside the comment header (``# ... after {removed_in} release``). The comment
    site has no quoting to escape out of, so a bare newline there is a
    code-injection vector by itself; this guard rejects the underlying characters
    up front, before either site is built, rather than relying on the quoting alone.

    Args:
        value: candidate ``since``/``removed_in`` version string.
        field_name: CLI flag name for the error message, e.g. ``"--since"``.

    Raises:
        ValueError: *value* contains a control character (``ord(c) < 32``), a
            double quote, or a backslash.

    Examples:
        >>> _reject_unsafe("1.2.0", "--since")
        >>> _reject_unsafe('1.0"', "--since")
        Traceback (most recent call last):
            ...
        ValueError: --since must not contain quotes, backslashes, or control characters: '1.0"'
        >>> _reject_unsafe("1.0\\ninject", "--removed-in")
        Traceback (most recent call last):
            ...
        ValueError: --removed-in must not contain quotes, backslashes, or control characters: '1.0\\ninject'
    """
    if any(ord(c) < 32 for c in value) or '"' in value or "\\" in value:
        raise ValueError(f"{field_name} must not contain quotes, backslashes, or control characters: {value!r}")


def _is_safe_arg_node(node: ast.expr) -> bool:
    """Return whether *node* is safe to appear as a decorator-call argument value.

    Only three expression shapes carry no executable behavior of their own: a
    literal constant, a bare name reference, and an attribute-access chain that
    bottoms out in a bare name (``Class.method``, ``pkg.mod.Klass``). Anything
    else — a call, a comprehension, a binary operation, unpacking (``*args``),
    ... — can run arbitrary code the moment :func:`gen_wrapper_from_decorator`'s
    output is imported.

    Args:
        node: an argument or keyword-argument value from a parsed ``ast.Call``.

    Returns:
        ``True`` if *node* is an :class:`ast.Constant`, :class:`ast.Name`, or an
        :class:`ast.Attribute` chain rooted in one of those; ``False`` otherwise.

    Examples:
        >>> _is_safe_arg_node(ast.parse("bar", mode="eval").body)
        True
        >>> _is_safe_arg_node(ast.parse("MyClass.new_method", mode="eval").body)
        True
        >>> _is_safe_arg_node(ast.parse("__import__('os').system('id')", mode="eval").body)
        False
    """
    if isinstance(node, (ast.Constant, ast.Name)):
        return True
    if isinstance(node, ast.Attribute):
        return _is_safe_arg_node(node.value)
    return False


def _decorator_func_name(decorator: str) -> str:
    """Parse *decorator* and return the bare name of the single call it makes.

    A prior fix guarded ``old_name``/``new_name``/``since``/``removed_in`` but
    left ``decorator`` itself checked only for control characters plus a
    substring test for ``"deprecated"``/``"deprecated_class"`` — insufficient on
    two counts: a string containing that substring can still carry an arbitrary
    expression as one of its own argument values (e.g.
    ``@deprecated(target=__import__("os").system("id"))``), and the substring
    test itself misclassifies a decorator whose *argument* text happens to
    contain ``"deprecated_class"`` (e.g. ``target=deprecated_class``). This
    parses the decorator structurally instead: exactly one call to a bare name,
    every argument restricted to :func:`_is_safe_arg_node`.

    Args:
        decorator: full decorator line, e.g. ``"@deprecated(target=bar, ...)"``.

    Returns:
        The decorator call's function name (before any import/name validity
        check against :data:`_IMPORT_MAP` — see :func:`_import_line_for_name`).

    Raises:
        ValueError: *decorator* does not start with ``@``; is not a single valid
            Python expression once the ``@`` is stripped; is not exactly one call
            to a bare name; or any positional/keyword argument value fails
            :func:`_is_safe_arg_node`.

    Examples:
        >>> _decorator_func_name("@deprecated(target=bar, deprecated_in='1.0', remove_in='2.0')")
        'deprecated'
        >>> _decorator_func_name("@deprecated_class(target=Bar)")
        'deprecated_class'
        >>> _decorator_func_name("@deprecated")
        Traceback (most recent call last):
            ...
        ValueError: --decorator must be a single call to a bare name, e.g. '@deprecated(...)': '@deprecated'
        >>> _decorator_func_name("@deprecated(target=f())")
        Traceback (most recent call last):
            ...
        ValueError: --decorator arguments must be names, attributes, or constants only: '@deprecated(target=f())'
    """
    if not decorator.startswith("@"):
        raise ValueError(f"--decorator must start with '@': {decorator!r}")
    try:
        parsed = ast.parse(decorator[1:], mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"--decorator is not a valid Python expression: {decorator!r}") from exc
    call = parsed.body
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        raise ValueError(f"--decorator must be a single call to a bare name, e.g. '@deprecated(...)': {decorator!r}")
    values = (*call.args, *(kw.value for kw in call.keywords))
    if any(not _is_safe_arg_node(value) for value in values):
        raise ValueError(f"--decorator arguments must be names, attributes, or constants only: {decorator!r}")
    return call.func.id


# ---------------------------------------------------------------------------
# Code generators
# ---------------------------------------------------------------------------


def gen_function_wrapper(old_name: str, new_name: str, since: str, removed_in: str) -> str:
    """Return ``@deprecated`` block for a function or method (auto mode).

    Args:
        old_name: bare name of the symbol being deprecated.
        new_name: bare name of the replacement symbol, or a ``Class.method`` pair
            for a method rename.
        since: ``deprecated_in`` version string (e.g. ``"1.2.0"``).
        removed_in: ``remove_in`` version string (e.g. ``"2.0.0"``).

    Returns:
        Python source string ready to insert after the new definition.

    Raises:
        ValueError: *new_name* is not a bare identifier or a single dotted
            ``Class.method`` pair; *since* or *removed_in* contains a control
            character, quote, or backslash; or *old_name* is not a bare
            identifier (raised by :func:`gen_wrapper_from_decorator`).

    Examples:
        >>> code = gen_function_wrapper("old_fn", "new_fn", "1.0", "2.0")
        >>> "deprecated" in code and "old_fn" in code and "new_fn" in code
        True
        >>> "remove_in" in code and "warnings" not in code
        True
        >>> gen_function_wrapper("old_fn", 'os.system("x")', "1.0", "2.0")
        Traceback (most recent call last):
            ...
        ValueError: --new-name must be a bare ASCII identifier or a single 'Class.method' pair: 'os.system("x")'
        >>> code = gen_function_wrapper("old", "deprecated_class", "1.0", "2.0")
        >>> "def old(*args, **kwargs): ..." in code and "from deprecate import deprecated_class" not in code
        True
    """
    _check_identifier(new_name, "--new-name", allow_dotted=True)
    _reject_unsafe(since, "--since")
    _reject_unsafe(removed_in, "--removed-in")
    decorator = f"@deprecated(target={new_name}, deprecated_in={json.dumps(since)}, remove_in={json.dumps(removed_in)})"
    return gen_wrapper_from_decorator(decorator, old_name, removed_in)


def gen_class_wrapper(old_name: str, new_name: str, since: str, removed_in: str) -> str:
    """Return ``@deprecated_class`` block for a class (auto mode).

    Args:
        old_name: bare name of the class being deprecated.
        new_name: bare name of the replacement class.
        since: ``deprecated_in`` version string.
        removed_in: ``remove_in`` version string.

    Returns:
        Python source string ready to insert after the new definition.

    Raises:
        ValueError: *new_name* is not a bare identifier or a single dotted
            ``Class.method`` pair; *since* or *removed_in* contains a control
            character, quote, or backslash; or *old_name* is not a bare
            identifier (raised by :func:`gen_wrapper_from_decorator`).

    Examples:
        >>> code = gen_class_wrapper("OldCls", "NewCls", "1.0", "2.0")
        >>> "deprecated_class" in code and "OldCls" in code and "NewCls" in code
        True
        >>> "warnings" not in code
        True
        >>> gen_class_wrapper("OldCls", "bad name", "1.0", "2.0")
        Traceback (most recent call last):
            ...
        ValueError: --new-name must be a bare ASCII identifier or a single 'Class.method' pair: 'bad name'
    """
    _check_identifier(new_name, "--new-name", allow_dotted=True)
    _reject_unsafe(since, "--since")
    _reject_unsafe(removed_in, "--removed-in")
    decorator = (
        f"@deprecated_class(target={new_name}, deprecated_in={json.dumps(since)}, remove_in={json.dumps(removed_in)})"
    )
    return gen_wrapper_from_decorator(decorator, old_name, removed_in)


def gen_wrapper_from_decorator(decorator: str, old_name: str, removed_in: str = "?") -> str:
    """Build wrapper block from an explicit *decorator* line (explicit mode).

    Infers the correct import statement from the decorator name.  Chooses
    ``def`` stub for ``@deprecated`` and ``class`` stub for ``@deprecated_class``.
    Both auto-mode generators (:func:`gen_function_wrapper`, :func:`gen_class_wrapper`)
    delegate here, so this is the single choke point for *old_name* and *removed_in* —
    the two parameters every mode funnels through this function.

    Args:
        decorator: full decorator line, e.g. ``"@deprecated(target=bar, ...)"``
        old_name: bare name of the symbol to deprecate.
        removed_in: version string for the comment header.

    Returns:
        Python source string ready to insert after the new definition.

    Raises:
        ValueError: *decorator* contains any control character (which would
            permit code injection); *decorator* is not exactly one call to a
            bare name of ``deprecated``/``deprecated_class`` with only
            name/attribute/constant arguments (see :func:`_decorator_func_name`);
            *old_name* is not a bare identifier (it is interpolated into a
            ``def``/``class`` header, where a dot or any other non-identifier
            character is never valid); or *removed_in* contains a control
            character, quote, or backslash (it is interpolated raw and unquoted
            into the comment header, so a newline there injects a statement on
            its own).

    Examples:
        >>> code = gen_wrapper_from_decorator(
        ...     "@deprecated(target=bar, deprecated_in='1.0', remove_in='2.0')", "foo"
        ... )
        >>> "from deprecate import deprecated" in code
        True
        >>> "def foo(*args, **kwargs): ..." in code
        True
        >>> code = gen_wrapper_from_decorator(
        ...     "@deprecated_class(target=Bar, deprecated_in='1.0', remove_in='2.0')", "Foo"
        ... )
        >>> "from deprecate import deprecated_class" in code
        True
        >>> "class Foo: ..." in code
        True
        >>> gen_wrapper_from_decorator("@deprecated(target=bar)\\nimport os", "foo")
        Traceback (most recent call last):
            ...
        ValueError: --decorator must not contain newlines or control characters
        >>> gen_wrapper_from_decorator("@deprecated(target=bar)", "foo():__import__('os')")
        Traceback (most recent call last):
            ...
        ValueError: --old-name must be a bare ASCII identifier: "foo():__import__('os')"
        >>> gen_wrapper_from_decorator("@deprecated(target=bar)", "foo", removed_in="1.0\\ninject")
        Traceback (most recent call last):
            ...
        ValueError: --removed-in must not contain quotes, backslashes, or control characters: '1.0\\ninject'
        >>> gen_wrapper_from_decorator("@deprecated(target=f())", "foo")
        Traceback (most recent call last):
            ...
        ValueError: --decorator arguments must be names, attributes, or constants only: '@deprecated(target=f())'
    """
    # The decorator is embedded verbatim in generated Python source; any
    # control character (newline, null byte, escape, etc.) would inject code,
    # and can survive inside an otherwise-valid triple-quoted string constant
    # (structural parsing alone would accept it) — reject it up front regardless.
    if any(ord(c) < 32 for c in decorator):
        raise ValueError("--decorator must not contain newlines or control characters")
    _check_identifier(old_name, "--old-name")
    _reject_unsafe(removed_in, "--removed-in")
    func_name = _decorator_func_name(decorator)
    import_line = _import_line_for_name(func_name, decorator)
    stub = f"class {old_name}: ..." if func_name == "deprecated_class" else f"def {old_name}(*args, **kwargs): ..."
    return textwrap.dedent(f"""\
        # Deprecated alias — remove after {removed_in} release
        {import_line}


        {decorator}
        {stub}
    """)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate(
    symbol_type: SymbolType | str,
    old_name: str,
    new_name: str,
    since: str = "?",
    removed_in: str = "?",
) -> str:
    """Return deprecation wrapper Python source code (auto mode).

    Args:
        symbol_type: A :class:`SymbolType` member, or its plain value from the CLI
        old_name: bare symbol name being deprecated (e.g. ``validate_token``)
        new_name: bare replacement symbol name (e.g. ``validate_access_token``)
        since: ``deprecated_in`` version string; default ``"?"`` when unknown
        removed_in: ``remove_in`` version string; default ``"?"`` when unknown

    Returns:
        Multi-line Python source string, ready to insert after the new definition.

    Raises:
        ValueError: if *symbol_type* is not one of the three accepted values,
            or if *old_name*/*new_name*/*since*/*removed_in* fail the
            interpolation-safety checks in :func:`gen_function_wrapper` /
            :func:`gen_class_wrapper`.

    Examples:
        >>> "deprecated" in generate(SymbolType.FUNCTION, "old", "new")
        True
        >>> "deprecated_class" in generate(SymbolType.CLASS, "Old", "New")
        True
        >>> 'deprecated_in="0.9"' in generate(SymbolType.METHOD, "m", "n", since="0.9", removed_in="1.0")
        True
    """
    try:
        symbol_type = SymbolType(symbol_type)
    except ValueError:
        legal = ", ".join(t.value for t in SymbolType)
        raise ValueError(f"Unknown symbol_type {symbol_type!r}. Expected: {legal}") from None
    if symbol_type in (SymbolType.FUNCTION, SymbolType.METHOD):
        return gen_function_wrapper(old_name, new_name, since, removed_in)
    return gen_class_wrapper(old_name, new_name, since, removed_in)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Output Python deprecation wrapper code to stdout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # --- shared ---
    parser.add_argument("--old-name", required=True, help="Bare old symbol name")
    parser.add_argument("--removed-in", default="?", help="remove_in version for comment header (default: ?)")
    # --- auto mode ---
    parser.add_argument(
        "--type",
        dest="symbol_type",
        choices=[t.value for t in SymbolType],
        help="Auto mode: symbol type",
    )
    parser.add_argument("--new-name", help="Auto mode: bare replacement symbol name")
    parser.add_argument("--since", default="?", help="Auto mode: deprecated_in version (default: ?)")
    # --- explicit mode ---
    parser.add_argument(
        "--decorator",
        help='Explicit mode: full decorator line, e.g. "@deprecated(target=bar, ...)"',
    )
    args = parser.parse_args()

    try:
        if args.decorator:
            code = gen_wrapper_from_decorator(args.decorator, args.old_name, args.removed_in)
        elif args.symbol_type and args.new_name:
            code = generate(SymbolType(args.symbol_type), args.old_name, args.new_name, args.since, args.removed_in)
        else:
            parser.error("Provide either --decorator OR both --type and --new-name.")
            return
    except ValueError as exc:
        print(f"! {exc}", file=sys.stderr)
        sys.exit(1)

    print(code, end="")


if __name__ == "__main__":
    main()
