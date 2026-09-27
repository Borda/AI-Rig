"""Extract pytest and subprocess structure: fixtures, conftest paths, spawned scripts."""

from __future__ import annotations
import ast
import sys
from pathlib import Path


def _extract_path_file_parent_dir(arg: ast.expr, conftest_dir: Path) -> Path | None:
    """Resolve ``str(Path(__file__).parent / "name")`` form to an absolute directory.

    Returns ``None`` for any unsupported shape — multi-level ``.parent.parent``,
    ``.resolve()`` chains, ``os.path.join`` forms, or any non-literal RHS of the
    division operator. Only the exact 1-level ``Path(__file__).parent / "name"``
    pattern (with name as a string constant) is supported.

    Args:
        arg: AST node taken from the second positional argument of
            ``sys.path.insert(N, ...)``; expected to be a ``Call`` to ``str``.
        conftest_dir: directory containing the conftest.py being parsed —
            used as the anchor for ``Path(__file__).parent``.
    """
    if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "str"):
        return None
    if not arg.args:
        return None
    inner = arg.args[0]
    if not (isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Div)):
        return None
    if not (isinstance(inner.right, ast.Constant) and isinstance(inner.right.value, str)):
        return None
    left = inner.left
    # Require Attribute chain: Path(__file__).parent — left.attr == "parent", left.value is Call to Path(__file__).
    if not (isinstance(left, ast.Attribute) and left.attr == "parent"):
        return None
    base = left.value
    if not (isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "Path"):
        return None
    if not (base.args and isinstance(base.args[0], ast.Name) and base.args[0].id == "__file__"):
        return None
    return conftest_dir / inner.right.value


def _is_syspath_insert_call(call: ast.Call) -> bool:
    """Check whether a call inserts an entry into the Python import path.

    Two positional args required; receiver must be the literal ``sys.path`` attribute chain. ``sys.path.append`` and
    slice assignment are not detected here (treated as unsupported and skipped silently at the walk level).
    """
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "insert"):
        return False
    receiver = func.value
    if not (isinstance(receiver, ast.Attribute) and receiver.attr == "path"):
        return False
    inner = receiver.value
    if not (isinstance(inner, ast.Name) and inner.id == "sys"):
        return False
    return len(call.args) >= 2


def extract_conftest_syspath(conftest_path: Path, root: Path) -> list[Path]:
    """Parse a conftest.py AST and return resolved directory paths added to ``sys.path``.

    Supported patterns (static, constant-foldable):

      * ``sys.path.insert(N, "str_literal")`` — resolved relative to
        ``conftest_path.parent``.
      * ``sys.path.insert(N, str(Path(__file__).parent / "name"))`` — resolved
        to ``conftest_path.parent / "name"``.

    Unsupported patterns are skipped with a single ``⚠ conftest:`` warning to
    stderr — ``Path(__file__).parent.parent / ...``, ``os.path.join(...)``,
    variable-then-use, ``sys.path.append``, slice assignment, ``.resolve()``
    chains.

    Args:
        conftest_path: filesystem path to the conftest.py file.
        root: project root (unused today, reserved for future relative-to-root
            anchoring; kept for API stability).

    Returns:
        List of resolved absolute directory paths added to ``sys.path``.
        Empty when the file has no ``sys.path.insert`` calls or all calls use
        unsupported shapes.
    """
    _ = root  # reserved
    try:
        source = conftest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(conftest_path))
    except SyntaxError:
        return []

    conftest_dir = conftest_path.parent
    results: list[Path] = []
    for stmt in tree.body:
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            continue
        call = stmt.value
        if not _is_syspath_insert_call(call):
            continue
        arg = call.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            results.append(conftest_dir / arg.value)
            continue
        resolved = _extract_path_file_parent_dir(arg, conftest_dir)
        if resolved is not None:
            results.append(resolved)
            continue
        print(
            f"⚠ conftest: unsupported sys.path pattern at {conftest_path}:{call.lineno} — skipped",
            file=sys.stderr,
        )
    return results


def _collect_module_aliases(
    conftest_paths: list[Path],
    indexed_names: set[str],
    root: Path,
) -> dict[str, str]:
    """Map bare module names made importable by conftest ``sys.path`` shims to dotted names.

    For each directory resolved via :func:`extract_conftest_syspath`, list the
    Python files directly inside it (non-recursive). For every file, the bare
    name is its stem; the function searches *indexed_names* for any module
    whose final dotted component equals the bare name.

      * Unique match → record ``bare_name -> full_dotted_name``.
      * Multiple matches → ambiguous; skipped with a stderr warning.
      * No match → likely a non-indexed script; skipped silently.

    Args:
        conftest_paths: every conftest.py discovered during the scan.
        indexed_names: dotted names of all modules with ``status == "ok"``.
        root: project root (unused today; kept for API stability with future
            features that may need to resolve relative paths).

    Returns:
        Mapping of bare module name → fully-qualified dotted module name.
    """
    _ = root
    # Index modules by their final dotted component for O(1) lookup.
    by_last: dict[str, list[str]] = {}
    for name in indexed_names:
        last = name.rsplit(".", 1)[-1]
        by_last.setdefault(last, []).append(name)

    aliases: dict[str, str] = {}
    seen_dirs: set[Path] = set()
    for conftest_path in conftest_paths:
        for syspath_dir in extract_conftest_syspath(conftest_path, root):
            try:
                resolved_dir = syspath_dir.resolve()
            except OSError:
                continue
            if resolved_dir in seen_dirs:
                continue
            seen_dirs.add(resolved_dir)
            if not resolved_dir.is_dir():
                continue
            for entry in sorted(resolved_dir.iterdir()):
                if entry.is_symlink() or not entry.is_file() or entry.suffix != ".py":
                    continue
                bare = entry.stem
                if bare == "__init__":
                    continue
                if bare in aliases:
                    continue
                candidates = by_last.get(bare, [])
                if not candidates:
                    continue
                if len(candidates) > 1:
                    print(
                        f"⚠ conftest: ambiguous alias '{bare}' (candidates: {', '.join(candidates)}) — skipped",
                        file=sys.stderr,
                    )
                    continue
                aliases[bare] = candidates[0]
    return aliases


# Bare interpreter tokens recognised at index 0 of the args list / os.system string.
_SUBPROCESS_PY_TOKENS: frozenset[str] = frozenset({"python", "python3"})


def _resolve_path_file_parent_script(arg: ast.expr, caller_dir: Path) -> Path | None:
    """Resolve ``str(Path(__file__).parent / "name.py")`` to an absolute file path.

    Mirrors :func:`_extract_path_file_parent_dir` but resolves against the
    calling module's directory (``caller_dir``) instead of a conftest dir.
    Returns ``None`` for any unsupported shape.

    Args:
        arg: AST node from the args list element (expected: ``Call`` to ``str``).
        caller_dir: directory containing the file whose AST is being walked —
            anchor for ``Path(__file__).parent``.
    """
    if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "str"):
        return None
    if not arg.args:
        return None
    inner = arg.args[0]
    if not (isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Div)):
        return None
    if not (isinstance(inner.right, ast.Constant) and isinstance(inner.right.value, str)):
        return None
    left = inner.left
    if not (isinstance(left, ast.Attribute) and left.attr == "parent"):
        return None
    base = left.value
    if not (isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "Path"):
        return None
    if not (base.args and isinstance(base.args[0], ast.Name) and base.args[0].id == "__file__"):
        return None
    return caller_dir / inner.right.value


def _is_python_token(node: ast.expr) -> bool:
    """Check whether an expression identifies a supported Python executable."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in _SUBPROCESS_PY_TOKENS
    if isinstance(node, ast.Attribute) and node.attr == "executable":
        return isinstance(node.value, ast.Name) and node.value.id == "sys"
    return False


def _subprocess_script_arg(args_list: ast.List, caller_dir: Path) -> str | None:
    """Extract the script-path string from a ``subprocess.run/Popen`` args list.

    Recognised shapes for ``args_list.elts[1]`` (after the interpreter token):

      * ``Constant(str)`` — bare script name (e.g. ``"other.py"``).
      * ``Call`` to ``str(Path(__file__).parent / "x.py")`` — 1-level Path form.

    Returns the resolved or raw script path string, or ``None`` when the shape
    is unsupported.
    """
    if len(args_list.elts) < 2:
        return None
    if not _is_python_token(args_list.elts[0]):
        return None
    script_node = args_list.elts[1]
    if isinstance(script_node, ast.Constant) and isinstance(script_node.value, str):
        return script_node.value
    resolved = _resolve_path_file_parent_script(script_node, caller_dir)
    if resolved is not None:
        return str(resolved)
    return None


def _is_subprocess_run_or_popen(call: ast.Call) -> bool:
    """Check whether a call starts a subprocess through a supported API."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in ("run", "Popen")):
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "subprocess"


def _is_os_system(call: ast.Call) -> bool:
    """Check whether a call invokes a command through the operating system."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "system"):
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "os"


def _os_system_script(call: ast.Call) -> str | None:
    """Extract the script name from ``os.system("python <script>")``.

    Returns the script token directly following ``python``/``python3`` after a whitespace split, or ``None`` when the
    form is unsupported (non-constant arg, no Python invocation token, no token after the interpreter).
    """
    if not call.args:
        return None
    first = call.args[0]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return None
    tokens = first.value.split()
    if len(tokens) < 2 or tokens[0] not in _SUBPROCESS_PY_TOKENS:
        return None
    return tokens[1]


def _resolve_script_to_module(script: str, caller_dir: Path, root: Path, indexed_files: dict[str, str]) -> str | None:
    """Match a resolved script path against indexed module file paths.

    Resolution rules:
      * Absolute path → resolved as-is.
      * Relative path → resolved against ``caller_dir``.
      * Result then matched against the indexed files map (rel-path → module
        name). The map is built by :func:`extract_subprocess_calls`'s caller.

    Args:
        script: script path string captured from the subprocess call.
        caller_dir: directory of the file whose AST is being walked.
        root: project root used to compute relative paths.
        indexed_files: map of POSIX rel-path → dotted module name for every
            ``status == "ok"`` module in the index.

    Returns:
        Dotted module name if matched, ``None`` otherwise.
    """
    script_path = Path(script)
    if not script_path.is_absolute():
        script_path = caller_dir / script_path
    try:
        resolved = script_path.resolve()
    except OSError:
        return None
    try:
        rel = resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    return indexed_files.get(rel)


def extract_subprocess_calls(
    tree: ast.Module,
    filepath: Path,
    root: Path,
    indexed_files: dict[str, str] | None = None,
) -> list[dict]:
    """Extract subprocess invocations of other Python scripts in this module.

    Scans the AST for three subprocess forms and produces one entry per call
    that resolves to an indexed module:

      * ``subprocess.run([<py>, <script>, ...])``
      * ``subprocess.Popen([<py>, <script>, ...])``
      * ``os.system("python <script> ...")`` (string form, whitespace-split)

    Recognised ``<py>`` tokens are ``"python"`` / ``"python3"`` string constants
    or ``sys.executable``. Recognised ``<script>`` shapes are bare string
    constants or ``str(Path(__file__).parent / "name.py")`` (1-level Path).

    Out of scope (documented for future expansion): ``runpy.run_path``, the
    ``sh`` library, shell strings without a ``python`` interpreter token,
    multi-level ``Path(__file__).parent.parent`` chains.

    Unresolvable scripts (no matching indexed module) emit a single stderr
    warning and are skipped — never raised.

    Args:
        tree: parsed AST of the module being scanned.
        filepath: filesystem path of the source file (recorded in each entry).
        root: project root used to resolve relative script paths.
        indexed_files: map of POSIX rel-path → dotted module name. When
            ``None``, the function returns ``[]`` because no resolution is
            possible without the module list (callers build this once per
            scan from the parsed module entries).

    Returns:
        List of ``{"target_module": str, "file": str, "line": int}`` dicts, one
        per resolved subprocess call, in source order.

    Examples:
        >>> import ast, pathlib
        >>> src = "import subprocess\\nsubprocess.run(['python', 'x.py'])\\n"
        >>> extract_subprocess_calls(ast.parse(src), pathlib.Path("/tmp/a.py"), pathlib.Path("/tmp"), {}) == []
        True
    """
    if indexed_files is None:
        return []
    caller_dir = filepath.parent
    file_str = str(filepath)
    results: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        script: str | None = None
        if _is_subprocess_run_or_popen(node):
            if not node.args:
                continue
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.List):
                continue
            script = _subprocess_script_arg(first_arg, caller_dir)
        elif _is_os_system(node):
            script = _os_system_script(node)
        if script is None:
            continue
        target = _resolve_script_to_module(script, caller_dir, root, indexed_files)
        if target is None:
            print(
                f"⚠ subprocess: unresolvable script {script!r} in {filepath}",
                file=sys.stderr,
            )
            continue
        results.append({"target_module": target, "file": file_str, "line": node.lineno})
    return results


# Pytest fixtures injected at runtime — no static definition in any conftest.
# Listed so test functions taking these as parameters are not flagged unknown.
_PYTEST_BUILTIN_FIXTURES: frozenset[str] = frozenset(
    {
        "tmp_path",
        "tmp_path_factory",
        "tmpdir",
        "tmpdir_factory",
        "monkeypatch",
        "mocker",
        "caplog",
        "capsys",
        "capfd",
        "capsysbinary",
        "capfdbinary",
        "request",
        "pytestconfig",
        "fixture_union",
        "recwarn",
        "cache",
        "doctest_namespace",
        "record_property",
        "record_xml_attribute",
        "record_testsuite_property",
    }
)


def _is_pytest_fixture_decorator(decorator: ast.expr) -> tuple[bool, str | None]:
    """Identify whether *decorator* marks the function as a ``@pytest.fixture``.

    Returns ``(is_fixture, scope)`` where *scope* is the explicit ``scope=`` keyword
    value (string literal only) or ``None`` when omitted, dynamic, or the decorator
    is not a fixture decorator. ``scope`` resolution defaults to ``"function"``
    upstream in :func:`extract_fixtures` when this function returns ``None``.

    Recognised forms:
      * ``@pytest.fixture`` — ``Attribute(value=Name('pytest'), attr='fixture')``
      * ``@pytest.fixture(scope='session')`` — ``Call`` wrapping the attribute form
      * ``@fixture`` — bare ``Name('fixture')`` (assumes ``from pytest import fixture``)

    Args:
        decorator: AST decorator node from a function definition's ``decorator_list``.

    Examples:
        >>> import ast
        >>> tree = ast.parse("import pytest\\n@pytest.fixture\\ndef f(): pass")
        >>> _is_pytest_fixture_decorator(tree.body[1].decorator_list[0])
        (True, None)
        >>> tree = ast.parse("import pytest\\n@pytest.fixture(scope='session')\\ndef f(): pass")
        >>> _is_pytest_fixture_decorator(tree.body[1].decorator_list[0])
        (True, 'session')
    """
    # Bare @pytest.fixture
    if isinstance(decorator, ast.Attribute) and decorator.attr == "fixture":
        if isinstance(decorator.value, ast.Name) and decorator.value.id == "pytest":
            return True, None
        return False, None
    # Bare @fixture (assumes `from pytest import fixture`)
    if isinstance(decorator, ast.Name) and decorator.id == "fixture":
        return True, None
    # @pytest.fixture(...) or @fixture(...)
    if isinstance(decorator, ast.Call):
        func = decorator.func
        is_fixture = False
        if isinstance(func, ast.Attribute) and func.attr == "fixture":
            if isinstance(func.value, ast.Name) and func.value.id == "pytest":
                is_fixture = True
        elif isinstance(func, ast.Name) and func.id == "fixture":
            is_fixture = True
        if not is_fixture:
            return False, None
        for kw in decorator.keywords:
            if kw.arg == "scope" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return True, kw.value.value
        return True, None
    return False, None


def _body_yields(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True when *node*'s body contains any ``Yield`` or ``YieldFrom`` expression.

    Nested function definitions are not descended into — only yields that belong
    directly to *node* count toward classifying the fixture as a generator.

    Examples:
        >>> import ast
        >>> _body_yields(ast.parse("def f():\\n    yield 1").body[0])
        True
        >>> _body_yields(ast.parse("def f():\\n    return 1").body[0])
        False
    """
    for child in ast.walk(node):
        if child is node:
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not node:
            # Skip nested function definitions; their yields are not this fixture's.
            continue
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
    return False


def extract_fixtures(tree: ast.Module, filepath: Path) -> list[dict]:
    """Extract every ``@pytest.fixture`` decorated function defined in *tree*.

    Walks the top-level module body for ``FunctionDef`` / ``AsyncFunctionDef``
    nodes carrying a fixture decorator (see :func:`_is_pytest_fixture_decorator`).
    Each match is recorded with ``name``, ``scope`` (string literal from a
    ``scope=`` kwarg, or ``"function"`` when omitted), ``loc`` (the function's
    line number), ``yields`` (whether the body contains ``yield`` / ``yield from``),
    and ``params`` (the fixture's positional / keyword argument names, ``self`` /
    ``cls`` excluded — used downstream by ``fixture-graph`` to walk per-fixture
    dependency trees).

    Non-decorator forms (functions registered via ``pytest.fixture(scope=...)(fn)``
    call syntax instead of decoration) are not recognised — they fall outside
    pytest's discoverable fixture surface in practice.

    Args:
        tree: parsed AST of the module being scanned.
        filepath: filesystem path of the source file (recorded for diagnostics
            but not embedded in the output today).

    Returns:
        List of ``{"name", "scope", "loc", "yields", "params"}`` dicts in source order.

    Examples:
        >>> import ast, pathlib
        >>> src = "import pytest\\n@pytest.fixture\\ndef f(db):\\n    yield 1\\n"
        >>> extract_fixtures(ast.parse(src), pathlib.Path("conftest.py"))
        [{'name': 'f', 'scope': 'function', 'loc': 3, 'yields': True, 'params': ['db']}]
    """
    _ = filepath  # reserved for future diagnostics
    results: list[dict] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope: str | None = None
        is_fixture = False
        for decorator in node.decorator_list:
            matched, explicit_scope = _is_pytest_fixture_decorator(decorator)
            if matched:
                is_fixture = True
                if explicit_scope is not None:
                    scope = explicit_scope
                break
        if not is_fixture:
            continue
        results.append(
            {
                "name": node.name,
                "scope": scope or "function",
                "loc": node.lineno,
                "yields": _body_yields(node),
                "params": _function_param_names(node),
            }
        )
    return results


def _function_param_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return the positional/keyword parameter names of *node*, excluding ``self`` / ``cls``.

    Skips ``*args``, ``**kwargs``, and positional-only ``/`` marker semantics — pytest
    fixtures are injected through standard positional/keyword args only.

    Examples:
        >>> import ast
        >>> _function_param_names(ast.parse("def f(self, a, b=1, *args): pass").body[0])
        ['a', 'b']
    """
    args = node.args
    names: list[str] = []
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if arg.arg in ("self", "cls"):
            continue
        names.append(arg.arg)
    return names


def extract_fixture_uses(
    tree: ast.Module,
    defined_fixtures: dict[str, dict],
    all_conftest_exports: dict[str, dict],
) -> list[dict]:
    """Extract fixture-name parameters consumed by ``test_*`` and fixture functions in *tree*.

    Walks top-level functions (and methods of top-level classes) and treats each
    non-``self`` / ``cls`` parameter as a candidate fixture name when the function
    is either:

      * named ``test_*`` (a pytest test); or
      * decorated with ``@pytest.fixture`` (a fixture itself, whose parameters
        are fixture dependencies — used by ``fixture-graph`` to walk
        dependency trees in conftest modules).

    Each candidate is resolved against three sources, in this order:

      1. ``defined_fixtures`` — fixtures defined in the same module (local
         conftest/test-file fixtures shadow everything else).
      2. ``all_conftest_exports`` — fixtures exported by any conftest reachable
         in the project, with the closest conftest already resolved by the caller.
      3. :data:`_PYTEST_BUILTIN_FIXTURES` — pytest's runtime-injected fixtures
         (``tmp_path``, ``monkeypatch``, ``mocker``…); emitted with
         ``scope=None`` / ``defined_in=None``.

    Unknown names (plugin fixtures, fixtures defined in non-indexed conftests)
    are emitted with ``scope=None`` / ``defined_in=None`` so the caller can still
    surface them without inventing a source.

    Each fixture name is emitted at most once per module, even when consumed by
    multiple test functions — the caller's interest is reverse-dependency, not
    per-test count.

    Args:
        tree: parsed AST of a test module.
        defined_fixtures: ``name -> {scope, ...}`` map of fixtures defined in the
            same module (typically the test file or a local conftest).
        all_conftest_exports: ``name -> {scope, defined_in}`` map of fixtures
            visible through the conftest hierarchy, deeper-conftest-wins.

    Returns:
        List of ``{"name", "scope", "defined_in"}`` dicts, deduplicated by name,
        ordered alphabetically.

    Examples:
        >>> import ast
        >>> src = "def test_a(tmp_path, my_fix):\\n    pass\\n"
        >>> tree = ast.parse(src)
        >>> uses = extract_fixture_uses(
        ...     tree, {}, {"my_fix": {"scope": "session", "defined_in": "conftest"}}
        ... )
        >>> uses[0]
        {'name': 'my_fix', 'scope': 'session', 'defined_in': 'conftest'}
        >>> uses[1]
        {'name': 'tmp_path', 'scope': None, 'defined_in': None}
    """
    seen: dict[str, dict] = {}

    def _record(name: str) -> None:
        if name in seen:
            return
        if name in defined_fixtures:
            scope = defined_fixtures[name].get("scope", "function")
            seen[name] = {"name": name, "scope": scope, "defined_in": None}
            return
        if name in all_conftest_exports:
            entry = all_conftest_exports[name]
            seen[name] = {
                "name": name,
                "scope": entry.get("scope", "function"),
                "defined_in": entry.get("defined_in"),
            }
            return
        # Builtin or unknown — emit with sentinel nulls so callers can still see usage.
        seen[name] = {"name": name, "scope": None, "defined_in": None}

    def _is_relevant(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        if fn.name.startswith("test_"):
            return True
        for decorator in fn.decorator_list:
            is_fix, _ = _is_pytest_fixture_decorator(decorator)
            if is_fix:
                return True
        return False

    def _walk_function(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if not _is_relevant(fn):
            return
        for param in _function_param_names(fn):
            _record(param)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _walk_function(node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _walk_function(child)

    return sorted(seen.values(), key=lambda d: d["name"])
