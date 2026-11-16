"""Table-scoped parsing of the entry points declared in ``pyproject.toml``.

The parser reads ``[project.scripts]``, ``[project.gui-scripts]`` and ``[project.entry-points.<group>]`` (including
``pytest11``) without a TOML library. Each case pins one TOML spelling or one failure mode: valid spellings yield
exactly the declared entry points, out-of-scope tables yield nothing, and malformed in-scope metadata yields an empty
result rather than a partial one.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from codemap_py.scanner.entry_points import (
    EntryPoint,
    parse_entry_point_target,
    parse_entry_points,
    scan_entry_point_refs,
)


def _triples(text: str) -> list[tuple[str, str, str, str | None]]:
    """Return ``(group, name, module, symbol)`` for every entry point parsed from *text*."""
    return [(ep.group, ep.name, ep.module, ep.symbol) for ep in parse_entry_points(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            '[project.scripts]\ntool = "pkg.cli:main"\n',
            [("console_scripts", "tool", "pkg.cli", "main")],
            id="console-script",
        ),
        pytest.param(
            "[project.gui-scripts]\nviewer = 'pkg.gui:launch'\n",
            [("gui_scripts", "viewer", "pkg.gui", "launch")],
            id="gui-script-literal-string",
        ),
        pytest.param(
            '[project.scripts]\n"my-tool" = "pkg.cli:main"\n\'other.tool\' = "pkg.cli:other"\n',
            [("console_scripts", "my-tool", "pkg.cli", "main"), ("console_scripts", "other.tool", "pkg.cli", "other")],
            id="quoted-keys",
        ),
        pytest.param(
            '[project.entry-points."flake8.extension"]\nX1 = "pkg.checks:Checker"\n',
            [("flake8.extension", "X1", "pkg.checks", "Checker")],
            id="quoted-dotted-group-header",
        ),
        pytest.param(
            '[project.entry-points.pytest11]\ndemo = "pkg.plugin"\n',
            [("pytest11", "demo", "pkg.plugin", None)],
            id="pytest11-module-only",
        ),
        pytest.param(
            '[project]\nname = "demo"\nscripts.tool = "pkg.cli:main"\nentry-points.pytest11.demo = "pkg.plugin"\n',
            [("console_scripts", "tool", "pkg.cli", "main"), ("pytest11", "demo", "pkg.plugin", None)],
            id="dotted-keys-in-project-table",
        ),
        pytest.param(
            '[project]\nscripts = { tool = "pkg.cli:main", "b" = \'pkg.cli:other\' }\n',
            [("console_scripts", "tool", "pkg.cli", "main"), ("console_scripts", "b", "pkg.cli", "other")],
            id="inline-table-in-project-table",
        ),
        pytest.param(
            '[project]\nentry-points = { pytest11 = { demo = "pkg.plugin" } }\n',
            [("pytest11", "demo", "pkg.plugin", None)],
            id="nested-inline-tables",
        ),
        pytest.param(
            '[project.entry-points]\npytest11 = { demo = "pkg.plugin" }\nconsole_scripts.x = "pkg.cli:main"\n',
            [("pytest11", "demo", "pkg.plugin", None), ("console_scripts", "x", "pkg.cli", "main")],
            id="group-inline-table-and-dotted-key",
        ),
        pytest.param(
            '[project.scripts]\n# fake = "pkg.cli:fake"\ntool = "pkg.cli:main"  # not = "pkg.cli:other"\n',
            [("console_scripts", "tool", "pkg.cli", "main")],
            id="comments-and-hash-inside-comment",
        ),
        pytest.param(
            '[project.scripts]\ntool = "pkg.cli:main [color, fast-io]"\n',
            [("console_scripts", "tool", "pkg.cli", "main")],
            id="target-extras",
        ),
        pytest.param(
            '[project.entry-points."demo.hooks"]\nrun = "pkg.tools:Runner.run"\n',
            [("demo.hooks", "run", "pkg.tools", "Runner.run")],
            id="nested-callable",
        ),
        pytest.param(
            '[project.scripts]\ntool = "pkg:main"\n',
            [("console_scripts", "tool", "pkg", "main")],
            id="single-segment-module",
        ),
        pytest.param(
            '[project.scripts]\ntool = "pkg.cli:m\\u0061in"\n',
            [("console_scripts", "tool", "pkg.cli", "main")],
            id="basic-string-escape",
        ),
        pytest.param(
            '[project]\ndependencies = [\n  "a",\n  ["x"]\n]\nscripts.tool = "pkg.cli:main"\n',
            [("console_scripts", "tool", "pkg.cli", "main")],
            id="header-like-line-inside-multiline-array",
        ),
        pytest.param(
            '[project]\nclassifiers = [\n  "]",  # ]\n  "uvicorn[standard]",\n  ["x"]\n]\nscripts.tool = "pkg.cli:main"\n',
            [("console_scripts", "tool", "pkg.cli", "main")],
            id="brackets-in-strings-and-comments-of-multiline-array",
        ),
        pytest.param(
            '[tool.demo]\nitems = ["""a\n]""", ["x"]]\n[project.scripts]\ntool = "pkg.cli:main"\n',
            [("console_scripts", "tool", "pkg.cli", "main")],
            id="array-closing-after-multiline-string",
        ),
    ],
)
def test_valid_spellings_yield_declared_entry_points(text: str, expected: list[tuple]) -> None:
    """Every supported TOML spelling of an entry point parses to the same group, name, module and symbol.

    Each fixture writes one spelling: a plain table, quoted or dotted keys, inline tables, comments, extras, nested
    callables or a basic-string escape. The result lists exactly the declared entry points in file order.
    """
    assert _triples(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('[tool.poetry.scripts]\ntool = "pkg.cli:main"\n', id="poetry-scripts"),
        pytest.param('[project.scripts]\n\n[tool.other]\ntool = "pkg.cli:main"\n', id="later-table-ends-scope"),
        pytest.param('[project.scripts]\n[[tool.items]]\ntool = "pkg.cli:main"\n', id="array-of-tables-ends-scope"),
        pytest.param(
            '[tool.demo]\nnote = """\n[project.scripts]\nfake = "pkg.fake:main"\n"""\n',
            id="header-inside-multiline-string",
        ),
        pytest.param('[project]\nname = "demo"\ntool = "pkg.cli:main"\n', id="non-entry-key-in-project-table"),
        pytest.param('[project.scripts.nested]\ntool = "pkg.cli:main"\n', id="too-deep-script-path"),
        pytest.param(
            '[tool.demo]\nrows = [\n["project"]\n]\nscripts.tool = "pkg.cli:main"\n',
            id="header-like-line-inside-array-of-other-table",
        ),
    ],
)
def test_out_of_scope_text_yields_nothing(text: str) -> None:
    """Text outside the entry-point tables never produces an entry point.

    Each fixture carries a well-formed target in a place packaging tools do not read as an entry point: a Poetry
    table, a table after the scripts table, a header-looking line inside a multi-line string, or an over-nested path.
    """
    assert parse_entry_points(text) == []


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('[project.scripts]\nok = "pkg.cli:main"\nbad = "pkg.cli:other\n', id="unterminated-string"),
        pytest.param('[project.scripts]\nok = "pkg.cli:main"\nbad "pkg.cli:other"\n', id="missing-equals"),
        pytest.param('[project.scripts]\nok = "pkg.cli:main" trailing\n', id="text-after-value"),
        pytest.param('[project]\nscripts = { ok = "pkg.cli:main"\n', id="unclosed-inline-table"),
        pytest.param('[project]\nscripts = { ok = "pkg.cli:main", }\n', id="inline-table-trailing-comma"),
        pytest.param('[project.scripts]\nok = "pkg.cli:main"\n[project.entry-points.x\n', id="unclosed-header"),
        pytest.param('[project.scripts]\nok = "pkg.cli:main"\nbad = 1\n', id="non-string-value"),
        pytest.param('[project.scripts]\nok = "pkg.cli:m\\qain"\n', id="invalid-escape"),
        pytest.param(
            '[project.scripts]\nok = "pkg.cli:main"\n[tool.x]\nnote = """\nopen\n', id="unterminated-multiline"
        ),
        pytest.param(
            '[project.scripts]\nok = "pkg.cli:main"\n[tool.x]\nitems = [\n"a",\n', id="unterminated-multiline-array"
        ),
        pytest.param("[project]\nscripts = " + "{ a = " * 50 + '"pkg.cli:main"' + " }" * 50 + "\n", id="deep-nesting"),
    ],
)
def test_malformed_in_scope_metadata_yields_nothing(text: str) -> None:
    """Malformed TOML in an entry-point table never crashes and never returns the entries parsed before the error.

    Each fixture starts with at least one valid entry point and then breaks the syntax in scope (or leaves a multi-line
    string open at the end of the file); the whole file must contribute nothing rather than a partial list.
    """
    assert parse_entry_points(text) == []


def test_invalid_target_is_skipped_alone() -> None:
    """A well-formed value that is not an object reference is dropped without discarding its neighbours.

    The table holds three values that are valid TOML but not valid references (empty attribute, leading digit, a keyword
    segment) between two valid entries; only the valid entries remain.
    """
    text = (
        '[project.scripts]\nfirst = "pkg.cli:main"\nempty = "pkg.cli:"\ndigit = "1pkg:main"\n'
        'kw = "pkg.class:main"\nlast = "pkg.cli:other"\n'
    )
    assert _triples(text) == [
        ("console_scripts", "first", "pkg.cli", "main"),
        ("console_scripts", "last", "pkg.cli", "other"),
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("pkg.cli:main", ("pkg.cli", "main"), id="function"),
        pytest.param(" pkg.cli : main [a,b] ", ("pkg.cli", "main"), id="spaces-and-extras"),
        pytest.param("pkg.plugin", ("pkg.plugin", None), id="module-only"),
        pytest.param("pkg.mod:Cls.method", ("pkg.mod", "Cls.method"), id="nested-callable"),
        pytest.param("pkg.cli:main [", None, id="unclosed-extras"),
        pytest.param("pkg.cli:main:extra", None, id="two-colons"),
        pytest.param("pkg..cli:main", None, id="empty-segment"),
    ],
)
def test_target_grammar(value: str, expected: tuple[str, str | None] | None) -> None:
    """Object references split into module and attribute path; anything else is rejected.

    The cases cover extras, whitespace around the colon, module-only references and the malformed forms that must not
    produce a half-parsed module or attribute name.
    """
    assert parse_entry_point_target(value) == expected


def test_line_numbers_point_at_declaring_key() -> None:
    """Each entry point records the 1-based line of the key that declares it.

    A comment and a blank line precede the table, so a line counter that ignored skipped lines would drift.
    """
    text = '# header comment\n\n[project.scripts]\ntool = "pkg.cli:main"\n'
    assert parse_entry_points(text) == [EntryPoint("console_scripts", "tool", "pkg.cli", "main", 4)]


def test_scan_keeps_only_indexed_modules(tmp_path: Path) -> None:
    """Targets whose module is not indexed are dropped; indexed targets keep group, name, symbol and line.

    The root ``pyproject.toml`` declares one script whose module is indexed and one whose module does not exist in the
    project; only the indexed module receives a record.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project.scripts]\ntool = "pkg.cli:main"\nghost = "missing.mod:main"\n', encoding="utf-8"
    )
    refs = scan_entry_point_refs(tmp_path, {"pkg.cli", "pkg"})
    assert refs == {
        "pkg.cli": [{"group": "console_scripts", "name": "tool", "symbol": "main", "file": "pyproject.toml", "line": 2}]
    }


def test_scan_reads_pyproject_with_byte_order_mark(tmp_path: Path) -> None:
    """A UTF-8 byte order mark at the start of ``pyproject.toml`` does not hide the first table's entry points.

    Some Windows editors save the file with a BOM. Decoded as plain UTF-8 the mark stays glued to the first header, so a
    ``[project.scripts]`` table on line 1 would never open and its scripts would be lost.
    """
    (tmp_path / "pyproject.toml").write_bytes(b'\xef\xbb\xbf[project.scripts]\ntool = "pkg.cli:main"\n')
    refs = scan_entry_point_refs(tmp_path, {"pkg.cli"})
    assert refs == {
        "pkg.cli": [{"group": "console_scripts", "name": "tool", "symbol": "main", "file": "pyproject.toml", "line": 2}]
    }


def _symlinks_supported() -> bool:
    """Return whether this host lets an unprivileged process create a file symlink."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.write_text("", encoding="utf-8")
        try:
            os.symlink(target, Path(tmp) / "link")
        except (OSError, NotImplementedError):
            return False
    return True


@pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create file symlinks")
def test_scan_ignores_symlinked_pyproject(tmp_path: Path) -> None:
    """A symlinked root ``pyproject.toml`` contributes no entry points, like the other root-config readers.

    The real file lives outside the project root and declares a script for an indexed module.
    """
    outside = tmp_path / "outside.toml"
    outside.write_text('[project.scripts]\ntool = "pkg.cli:main"\n', encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "pyproject.toml").symlink_to(outside)
    assert scan_entry_point_refs(root, {"pkg.cli"}) == {}
